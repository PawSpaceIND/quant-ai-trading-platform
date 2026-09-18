"""Enforced scope of the private pilot, not a claim of multi-asset readiness."""
from __future__ import annotations

from datetime import datetime, timezone

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.derivative_fees import (
    MCX_DERIVATIVE_ASSET_CLASSES,
    DerivativeFeeSchedule,
)
from quant_ai.execution.derivative_margin import DerivativeMarginSource
from quant_ai.instruments.contract import assert_contract_tradable
from quant_ai.instruments.identity import canonical_instrument_identity

_EVIDENCE_UNSET = object()


def _cash_pilot_instrument(item: Instrument) -> bool:
    return (
        item.market == Market.INDIA
        and item.currency == "INR"
        and item.exchange == "NSE"
        and item.asset_class in {AssetClass.EQUITY, AssetClass.ETF}
        and item.tradable
    )


def _mcx_pilot_instrument(item: Instrument) -> bool:
    return (
        item.market == Market.INDIA
        and item.currency == "INR"
        and item.exchange == "MCX"
        and item.asset_class in MCX_DERIVATIVE_ASSET_CLASSES
        and item.tradable
    )
def _mcx_probe_order(item: Instrument) -> OrderIntent:
    # This order is never submitted. Existing fee/margin components use OrderIntent
    # as their lookup contract, so admission exercises their real symbol/class rules.
    return OrderIntent(
        item.symbol,
        item.market,
        Side.BUY,
        item.lot_size or 0,
        item.tick_size,
        "pilot-mcx-admission",
        item.asset_class,
    )


def _validate_mcx(
    item: Instrument,
    *,
    derivative_fee_schedule: DerivativeFeeSchedule | None,
    margin_source: DerivativeMarginSource | None,
    now: datetime,
) -> None:
    if (
        item.expiry is None
        or item.lot_size is None
        or item.tick_size is None
        or not isinstance(item.underlying, str)
        or not item.underlying.strip()
    ):
        raise ValueError(f"pilot_mcx_contract_identity_incomplete:{item.symbol}")
    try:
        canonical_instrument_identity(item)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"pilot_mcx_contract_identity_incomplete:{item.symbol}"
        ) from error

    # Opening risk must be on a genuinely LIVE contract. Rollover and expiry keep
    # their existing specific refusal messages from the contract lifecycle module.
    assert_contract_tradable(item, Side.BUY, now)
    probe = _mcx_probe_order(item)
    if derivative_fee_schedule is None or not derivative_fee_schedule.verified:
        raise ValueError(f"pilot_mcx_fee_schedule_unverified:{item.symbol}")
    # Metals are covered by the reconciled non-agri schedule. Commodity symbols
    # must also pass the schedule's explicit agricultural/non-agricultural map.
    derivative_fee_schedule.assert_supported_order(probe)

    if margin_source is None:
        raise ValueError(f"pilot_mcx_margin_source_required:{item.symbol}")
    requirement = margin_source.requirement_for(probe, now=now)
    if requirement.lot_size != item.lot_size:
        raise ValueError(f"pilot_mcx_margin_contract_mismatch:{item.symbol}")


def validate_pilot_instruments(
    instruments: tuple[Instrument, ...],
    *,
    derivative_fee_schedule: DerivativeFeeSchedule | None | object = _EVIDENCE_UNSET,
    margin_source: DerivativeMarginSource | None | object = _EVIDENCE_UNSET,
    now: datetime | None = None,
) -> None:
    if not instruments:
        raise ValueError("pilot_watchlist_required")
    if len(instruments) > 50:
        raise ValueError("pilot_watchlist_limit")

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("pilot_validation_clock_must_be_timezone_aware")

    if any(_mcx_pilot_instrument(item) for item in instruments):
        if derivative_fee_schedule is _EVIDENCE_UNSET:
            derivative_fee_schedule = DerivativeFeeSchedule.from_env()
        if margin_source is _EVIDENCE_UNSET:
            margin_source = DerivativeMarginSource.from_env()
    else:
        derivative_fee_schedule = None
        margin_source = None

    for item in instruments:
        if _cash_pilot_instrument(item):
            continue
        if _mcx_pilot_instrument(item):
            _validate_mcx(
                item,
                derivative_fee_schedule=derivative_fee_schedule,
                margin_source=margin_source,
                now=instant,
            )
            continue
        raise ValueError(
            f"pilot_instrument_not_supported:{item.symbol}:"
            "NSE_INR_cash_or_verified_MCX_future_only"
        )

    if len({item.symbol for item in instruments}) != len(instruments):
        raise ValueError("pilot_duplicate_symbol")
