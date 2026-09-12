from __future__ import annotations

import os

from quant_ai.execution.live_guard import LiveTradingDisabled, assert_live_trading_disabled


class LiveMoneyDisabledError(LiveTradingDisabled):
    pass


class LiveBrokerFactory:
    """Hard stop for live-money construction. V1 cannot return a live broker."""

    @staticmethod
    def create() -> None:
        if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "").strip().lower() != "true":
            raise LiveMoneyDisabledError("TRADING_LIVE_MONEY_ACTIVE is not explicitly true")
        try:
            assert_live_trading_disabled()
        except LiveTradingDisabled as exc:
            raise LiveMoneyDisabledError(str(exc)) from exc
        raise LiveMoneyDisabledError("live broker construction is disabled")
