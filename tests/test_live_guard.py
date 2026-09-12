import pytest

from quant_ai.execution.live_guard import LiveTradingDisabled, assert_live_trading_disabled


def test_live_trading_is_fail_closed() -> None:
    with pytest.raises(LiveTradingDisabled):
        assert_live_trading_disabled()
