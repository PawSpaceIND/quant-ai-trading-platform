"""Shared explicit selection for daemon and read-only preflight; no auto fallback."""
from __future__ import annotations

import os

from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
from quant_ai.marketdata.kite_history import KiteDailyHistoryProvider, KiteHistoryError
from quant_ai.marketdata.timeframes import DailyHistoryProvider


def daily_history_from_env(*, source=None, environ=None, yahoo_factory=DailyHistoryProvider):
    environment = os.environ if environ is None else environ
    selected = (source if source is not None else environment.get("PRAMANA_DAILY_HISTORY_PROVIDER", "yahoo")).strip().lower()
    if selected in {"", "yahoo"}:
        return yahoo_factory(ResilientHttpClient(UrllibTransport()))
    if selected == "none":
        return None
    if selected == "kite":
        if environment.get("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() != "false":
            raise KiteHistoryError("kite_history_paper_only")
        return KiteDailyHistoryProvider(environment.get("ZERODHA_API_KEY", ""),
                                        environment.get("ZERODHA_ACCESS_TOKEN", ""))
    raise KiteHistoryError("unsupported_daily_history_provider")
