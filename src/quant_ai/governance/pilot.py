"""Enforced scope of the private pilot, not a claim of multi-asset readiness."""
from __future__ import annotations

from quant_ai.domain.models import AssetClass, Instrument, Market


def validate_pilot_instruments(instruments: tuple[Instrument, ...]) -> None:
    if not instruments:
        raise ValueError("pilot_watchlist_required")
    if len(instruments) > 50:
        raise ValueError("pilot_watchlist_limit")
    for item in instruments:
        if (item.market != Market.INDIA or item.currency != "INR" or item.exchange != "NSE"
                or item.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
                or not item.tradable):
            raise ValueError(f"pilot_instrument_not_supported:{item.symbol}:NSE_INR_cash_only")
    if len({item.symbol for item in instruments}) != len(instruments):
        raise ValueError("pilot_duplicate_symbol")
