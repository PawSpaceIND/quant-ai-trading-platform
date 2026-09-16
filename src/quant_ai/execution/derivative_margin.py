from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quant_ai.domain.models import AssetClass, Market, OrderIntent

LOGGER = logging.getLogger(__name__)
DERIVATIVE_MARGIN_JSON_ENV = "PRAMANA_DERIVATIVE_MARGIN_JSON"
DERIVATIVE_MARGIN_FILE_ENV = "PRAMANA_DERIVATIVE_MARGIN_FILE"
MARGINED_FUTURES_ASSET_CLASSES = frozenset(
    {AssetClass.FUTURE, AssetClass.FX, AssetClass.COMMODITY, AssetClass.METAL}
)


def _decimal(raw: object, label: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (ArithmeticError, InvalidOperation, ValueError) as error:
        raise ValueError(f"{label}_must_be_decimal") from error
    if not value.is_finite() or value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{label}_must_be_{qualifier}_finite")
    return value


@dataclass(frozen=True)
class ContractMarginRequirement:
    """Exact broker-sourced margin for one futures contract.

    SPAN and exposure margin are money amounts per exchange lot, never percentages of
    notional. The exact symbol and lot size come from the same broker/instrument identity
    as the quote; a nearby contract's margin is not a substitute.
    """

    symbol: str
    market: Market
    asset_class: AssetClass
    lot_size: int
    span_per_lot: Decimal
    exposure_per_lot: Decimal
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.source.strip():
            raise ValueError("derivative_margin_identity_and_source_required")
        if self.asset_class not in MARGINED_FUTURES_ASSET_CLASSES:
            raise ValueError("derivative_margin_asset_class_not_supported")
        if self.lot_size < 1:
            raise ValueError("derivative_margin_lot_size_must_be_positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("derivative_margin_observed_at_must_be_timezone_aware")
        if (
            not self.span_per_lot.is_finite()
            or not self.exposure_per_lot.is_finite()
            or self.span_per_lot < 0
            or self.exposure_per_lot < 0
            or self.total_per_lot <= 0
        ):
            raise ValueError("derivative_margin_amount_must_be_positive_finite")

    @property
    def total_per_lot(self) -> Decimal:
        return self.span_per_lot + self.exposure_per_lot

    def margin_for_quantity(self, quantity: int) -> Decimal:
        if type(quantity) is not int or quantity <= 0 or quantity % self.lot_size:
            raise ValueError(f"derivative_quantity_not_whole_lots:{self.symbol}:{self.lot_size}")
        return self.total_per_lot * Decimal(quantity // self.lot_size)

    def provenance(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "market": self.market.value,
            "assetClass": self.asset_class.value,
            "lotSize": str(self.lot_size),
            "spanPerLot": str(self.span_per_lot),
            "exposurePerLot": str(self.exposure_per_lot),
            "source": self.source,
            "observedAt": self.observed_at.isoformat(),
        }


class DerivativeMarginSource:
    """Exact contract margin snapshot supplied from broker evidence; no fallback rate."""

    def __init__(self, requirements: tuple[ContractMarginRequirement, ...]) -> None:
        by_key: dict[tuple[str, Market, AssetClass], ContractMarginRequirement] = {}
        for item in requirements:
            key = (item.symbol.strip().upper(), item.market, item.asset_class)
            if key in by_key:
                raise ValueError(f"duplicate_derivative_margin_requirement:{item.symbol}")
            by_key[key] = item
        self._requirements = by_key

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DerivativeMarginSource | None:
        source = os.environ if environ is None else environ
        raw_json = str(source.get(DERIVATIVE_MARGIN_JSON_ENV, "")).strip()
        raw_file = str(source.get(DERIVATIVE_MARGIN_FILE_ENV, "")).strip()
        if not raw_json and not raw_file:
            return None
        if raw_json and raw_file:
            LOGGER.warning("both derivative margin JSON and FILE are configured; refusing ambiguity")
            return None
        try:
            raw = raw_json if raw_json else Path(raw_file).expanduser().read_text(encoding="utf-8")
            return cls.from_json(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            LOGGER.warning("invalid derivative margin source; derivatives remain refused: %s", error)
            return None

    @classmethod
    def from_json(cls, raw: str | Mapping[str, object]) -> DerivativeMarginSource:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        source = str(payload.get("source", "")).strip()
        observed_raw = str(payload.get("observedAt", "")).strip()
        contracts = payload.get("contracts")
        if not source or not observed_raw or not isinstance(contracts, list):
            raise ValueError("derivative_margin_source_observed_at_and_contracts_required")
        observed_at = datetime.fromisoformat(observed_raw)
        rows: list[ContractMarginRequirement] = []
        for row in contracts:
            if not isinstance(row, Mapping):
                raise TypeError("derivative_margin_contract_must_be_object")
            lot_size = int(str(row.get("lotSize", "")).strip())
            rows.append(
                ContractMarginRequirement(
                    symbol=str(row.get("symbol", "")).strip().upper(),
                    market=Market(str(row.get("market", "")).strip().upper()),
                    asset_class=AssetClass(str(row.get("assetClass", "")).strip().upper()),
                    lot_size=lot_size,
                    span_per_lot=_decimal(row.get("spanPerLot"), "span_per_lot"),
                    exposure_per_lot=_decimal(row.get("exposurePerLot"), "exposure_per_lot"),
                    source=source,
                    observed_at=observed_at,
                )
            )
        if not rows:
            raise ValueError("derivative_margin_contracts_required")
        return cls(tuple(rows))

    def requirement_for(self, order: OrderIntent) -> ContractMarginRequirement:
        key = (order.symbol.strip().upper(), order.market, order.asset_class)
        requirement = self._requirements.get(key)
        if requirement is None:
            raise ValueError(f"derivative_margin_requirement_unavailable:{order.symbol}")
        return requirement

    def margin_for_order(self, order: OrderIntent, *, quantity: int | None = None) -> Decimal:
        return self.requirement_for(order).margin_for_quantity(
            order.quantity if quantity is None else quantity
        )
