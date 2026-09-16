from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation

from quant_ai.domain.models import AssetClass, OrderIntent, Side

LOGGER = logging.getLogger(__name__)
MCX_FEE_ENV_PREFIX = "PRAMANA_MCX_FEE_"
MCX_DERIVATIVE_ASSET_CLASSES = frozenset({AssetClass.METAL, AssetClass.COMMODITY})
CHARGE_CODES = ("BROKERAGE", "CTT", "EXCHANGE", "SEBI", "GST", "STAMP")


def _nonnegative_decimal(raw: object, label: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (ArithmeticError, InvalidOperation, ValueError) as error:
        raise ValueError(f"{label}_must_be_decimal") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label}_must_be_nonnegative_finite")
    return value


def _positive_decimal(raw: object, label: str) -> Decimal:
    value = _nonnegative_decimal(raw, label)
    if value <= 0:
        raise ValueError(f"{label}_must_be_positive")
    return value


@dataclass(frozen=True)
class DerivativeContractNote:
    """Operator-entered totals from one real MCX futures contract note.

    Buy/sell order turnovers are required because CTT and stamp duty are side-specific,
    while per-order turnovers are required because brokerage caps are per executed order.
    The reconciliation tolerance is supplied by the operator too; the engine does not
    invent a rounding convention for a broker's note.
    """

    reference: str
    turnover: Decimal
    buy_order_turnovers: tuple[Decimal, ...]
    sell_order_turnovers: tuple[Decimal, ...]
    brokerage: Decimal
    ctt: Decimal
    exchange: Decimal
    sebi: Decimal
    gst: Decimal
    stamp: Decimal
    grand_total: Decimal
    tolerance: Decimal

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("contract_note_reference_required")
        for name in ("turnover", "brokerage", "ctt", "exchange", "sebi", "gst", "stamp", "grand_total", "tolerance"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"contract_note_{name}_must_be_nonnegative_finite")
        for value in (*self.buy_order_turnovers, *self.sell_order_turnovers):
            if not value.is_finite() or value <= 0:
                raise ValueError("contract_note_order_turnover_must_be_positive_finite")

    @classmethod
    def from_json(cls, raw: str | Mapping[str, object]) -> DerivativeContractNote:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        charges = payload.get("charges")
        if not isinstance(charges, Mapping):
            raise TypeError("contract_note_charges_required")
        buys = payload.get("buyOrderTurnovers")
        sells = payload.get("sellOrderTurnovers")
        if not isinstance(buys, list) or not isinstance(sells, list):
            raise TypeError("contract_note_side_order_turnovers_required")
        return cls(
            reference=str(payload.get("reference", "")).strip(),
            turnover=_positive_decimal(payload.get("turnover"), "contract_note_turnover"),
            buy_order_turnovers=tuple(_positive_decimal(v, "buy_order_turnover") for v in buys),
            sell_order_turnovers=tuple(_positive_decimal(v, "sell_order_turnover") for v in sells),
            brokerage=_nonnegative_decimal(charges.get("BROKERAGE"), "contract_note_brokerage"),
            ctt=_nonnegative_decimal(charges.get("CTT"), "contract_note_ctt"),
            exchange=_nonnegative_decimal(charges.get("EXCHANGE"), "contract_note_exchange"),
            sebi=_nonnegative_decimal(charges.get("SEBI"), "contract_note_sebi"),
            gst=_nonnegative_decimal(charges.get("GST"), "contract_note_gst"),
            stamp=_nonnegative_decimal(charges.get("STAMP"), "contract_note_stamp"),
            grand_total=_nonnegative_decimal(payload.get("grandTotal"), "contract_note_grand_total"),
            tolerance=_nonnegative_decimal(payload.get("tolerance"), "contract_note_tolerance"),
        )


@dataclass(frozen=True)
class DerivativeFeeReconciliation:
    note_reference: str
    expected: tuple[tuple[str, Decimal], ...]
    observed: tuple[tuple[str, Decimal], ...]
    mismatches: tuple[str, ...]

    @property
    def matched(self) -> bool:
        return not self.mismatches

    def provenance(self) -> dict[str, object]:
        return {
            "contractNoteReference": self.note_reference,
            "reconciliationStatus": "matched" if self.matched else "mismatch",
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True)
class DerivativeFeeSchedule:
    """Operator-sourced MCX futures fee schedule with no numeric defaults.

    Every numeric field is required. A schedule is usable for fills only after the same
    rates reproduce a real contract note. Agricultural commodities are intentionally
    refused; commodity symbols must be explicitly classified so crude/gas cannot inherit
    an agricultural rule (or vice versa) by accident.
    """

    name: str
    ctt_sell_rate_non_agri: Decimal
    exchange_transaction_rate: Decimal
    sebi_turnover_rate: Decimal
    gst_rate: Decimal
    stamp_duty_buy_rate: Decimal
    brokerage_turnover_rate: Decimal
    brokerage_cap_per_order: Decimal
    brokerage_minimum_per_order: Decimal
    rate_source: str
    rates_verified_on: date
    agricultural_symbols: frozenset[str] = frozenset()
    non_agricultural_symbols: frozenset[str] = frozenset()
    reconciliation: DerivativeFeeReconciliation | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.rate_source.strip():
            raise ValueError("derivative_fee_schedule_provenance_required")
        for name in (
            "ctt_sell_rate_non_agri",
            "exchange_transaction_rate",
            "sebi_turnover_rate",
            "gst_rate",
            "stamp_duty_buy_rate",
            "brokerage_turnover_rate",
            "brokerage_cap_per_order",
            "brokerage_minimum_per_order",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name}_must_be_nonnegative_finite")
        object.__setattr__(self, "agricultural_symbols", frozenset(s.strip().upper() for s in self.agricultural_symbols if s.strip()))
        object.__setattr__(self, "non_agricultural_symbols", frozenset(s.strip().upper() for s in self.non_agricultural_symbols if s.strip()))
        if self.agricultural_symbols & self.non_agricultural_symbols:
            raise ValueError("mcx_commodity_classification_conflict")

    @property
    def verified(self) -> bool:
        return self.reconciliation is not None and self.reconciliation.matched

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DerivativeFeeSchedule | None:
        source = os.environ if environ is None else environ
        configured = {k: v for k, v in source.items() if k.startswith(MCX_FEE_ENV_PREFIX)}
        if not configured:
            return None
        required_text = ("NAME", "RATE_SOURCE", "RATES_VERIFIED_ON")
        required_numeric = (
            "CTT_SELL_RATE_NON_AGRI",
            "EXCHANGE_TRANSACTION_RATE",
            "SEBI_TURNOVER_RATE",
            "GST_RATE",
            "STAMP_DUTY_BUY_RATE",
            "BROKERAGE_TURNOVER_RATE",
            "BROKERAGE_CAP_PER_ORDER",
            "BROKERAGE_MINIMUM_PER_ORDER",
        )
        missing = [suffix for suffix in (*required_text, *required_numeric) if not str(source.get(f"{MCX_FEE_ENV_PREFIX}{suffix}", "")).strip()]
        if missing:
            LOGGER.warning("incomplete MCX fee schedule; refusing derivatives; missing=%s", ",".join(missing))
            return None
        try:
            verified_on = date.fromisoformat(str(source[f"{MCX_FEE_ENV_PREFIX}RATES_VERIFIED_ON"]).strip())
            values = {suffix: _nonnegative_decimal(source[f"{MCX_FEE_ENV_PREFIX}{suffix}"], suffix.lower()) for suffix in required_numeric}
            schedule = cls(
                name=str(source[f"{MCX_FEE_ENV_PREFIX}NAME"]).strip(),
                ctt_sell_rate_non_agri=values["CTT_SELL_RATE_NON_AGRI"],
                exchange_transaction_rate=values["EXCHANGE_TRANSACTION_RATE"],
                sebi_turnover_rate=values["SEBI_TURNOVER_RATE"],
                gst_rate=values["GST_RATE"],
                stamp_duty_buy_rate=values["STAMP_DUTY_BUY_RATE"],
                brokerage_turnover_rate=values["BROKERAGE_TURNOVER_RATE"],
                brokerage_cap_per_order=values["BROKERAGE_CAP_PER_ORDER"],
                brokerage_minimum_per_order=values["BROKERAGE_MINIMUM_PER_ORDER"],
                rate_source=str(source[f"{MCX_FEE_ENV_PREFIX}RATE_SOURCE"]).strip(),
                rates_verified_on=verified_on,
                agricultural_symbols=_symbols(source.get(f"{MCX_FEE_ENV_PREFIX}AGRICULTURAL_SYMBOLS", "")),
                non_agricultural_symbols=_symbols(source.get(f"{MCX_FEE_ENV_PREFIX}NON_AGRICULTURAL_SYMBOLS", "")),
            )
        except (KeyError, ValueError) as error:
            LOGGER.warning("invalid MCX fee schedule; refusing derivatives: %s", error)
            return None
        raw_note = str(source.get(f"{MCX_FEE_ENV_PREFIX}RECONCILIATION_JSON", "")).strip()
        if not raw_note:
            return schedule
        try:
            return schedule.with_reconciliation(DerivativeContractNote.from_json(raw_note))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            LOGGER.warning("MCX fee reconciliation invalid; schedule remains unverified: %s", error)
            return schedule

    def brokerage(self, turnover: Decimal) -> Decimal:
        if turnover <= 0:
            return Decimal(0)
        amount = turnover * self.brokerage_turnover_rate
        if self.brokerage_cap_per_order > 0:
            amount = min(amount, self.brokerage_cap_per_order)
        return min(max(amount, self.brokerage_minimum_per_order), turnover)

    def assert_supported_order(self, order: OrderIntent) -> None:
        if order.asset_class not in MCX_DERIVATIVE_ASSET_CLASSES:
            raise ValueError(f"mcx_derivative_asset_class_not_supported:{order.asset_class.value}")
        symbol = order.symbol.strip().upper()
        if order.asset_class == AssetClass.METAL:
            return
        if symbol in self.agricultural_symbols:
            raise ValueError(f"mcx_agricultural_fee_schedule_not_configured:{order.symbol}")
        if symbol not in self.non_agricultural_symbols:
            raise ValueError(f"mcx_commodity_classification_required:{order.symbol}")

    def charges_for(self, order: OrderIntent, turnover: Decimal) -> tuple[tuple[str, Decimal], ...]:
        if not self.verified:
            raise ValueError("mcx_derivative_schedule_unverified_internal")
        self.assert_supported_order(order)
        brokerage = self.brokerage(turnover)
        ctt = turnover * self.ctt_sell_rate_non_agri if order.side == Side.SELL else Decimal(0)
        exchange = turnover * self.exchange_transaction_rate
        sebi = turnover * self.sebi_turnover_rate
        gst = (brokerage + exchange + sebi) * self.gst_rate
        stamp = turnover * self.stamp_duty_buy_rate if order.side == Side.BUY else Decimal(0)
        return tuple((code, amount) for code, amount in (
            ("BROKERAGE", brokerage), ("CTT", ctt), ("EXCHANGE", exchange),
            ("SEBI", sebi), ("GST", gst), ("STAMP", stamp),
        ) if amount > 0)

    def reconcile(self, note: DerivativeContractNote) -> DerivativeFeeReconciliation:
        buy_turnover = sum(note.buy_order_turnovers, Decimal(0))
        sell_turnover = sum(note.sell_order_turnovers, Decimal(0))
        turnover = buy_turnover + sell_turnover
        brokerage = sum((self.brokerage(v) for v in (*note.buy_order_turnovers, *note.sell_order_turnovers)), Decimal(0))
        ctt = sell_turnover * self.ctt_sell_rate_non_agri
        exchange = turnover * self.exchange_transaction_rate
        sebi = turnover * self.sebi_turnover_rate
        gst = (brokerage + exchange + sebi) * self.gst_rate
        stamp = buy_turnover * self.stamp_duty_buy_rate
        computed = {
            "TURNOVER": turnover,
            "BROKERAGE": brokerage,
            "CTT": ctt,
            "EXCHANGE": exchange,
            "SEBI": sebi,
            "GST": gst,
            "STAMP": stamp,
        }
        observed = {
            "TURNOVER": note.turnover,
            "BROKERAGE": note.brokerage,
            "CTT": note.ctt,
            "EXCHANGE": note.exchange,
            "SEBI": note.sebi,
            "GST": note.gst,
            "STAMP": note.stamp,
        }
        computed_total = sum((computed[code] for code in CHARGE_CODES), Decimal(0))
        observed_total = note.grand_total
        mismatches = [code for code in ("TURNOVER", *CHARGE_CODES) if abs(computed[code] - observed[code]) > note.tolerance]
        if abs(computed_total - observed_total) > note.tolerance:
            mismatches.append("GRAND_TOTAL")
        return DerivativeFeeReconciliation(
            note_reference=note.reference,
            expected=(*computed.items(), ("GRAND_TOTAL", computed_total)),
            observed=(*observed.items(), ("GRAND_TOTAL", observed_total)),
            mismatches=tuple(mismatches),
        )

    def with_reconciliation(self, note: DerivativeContractNote) -> DerivativeFeeSchedule:
        return replace(self, reconciliation=self.reconcile(note))

    def provenance(self, order: OrderIntent) -> dict[str, object]:
        proof: dict[str, object] = {
            "schedule": self.name,
            "exchange": "MCX",
            "rateSource": self.rate_source,
            "ratesVerifiedOn": self.rates_verified_on.isoformat(),
            "scheduleVerified": self.verified,
            "commodityClassification": "non_agricultural" if order.asset_class in MCX_DERIVATIVE_ASSET_CLASSES else None,
        }
        if self.reconciliation is not None:
            proof.update(self.reconciliation.provenance())
        return proof


def _symbols(raw: object) -> frozenset[str]:
    return frozenset(item.strip().upper() for item in str(raw).split(",") if item.strip())
