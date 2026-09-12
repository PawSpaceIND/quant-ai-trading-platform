import pytest

from quant_ai.operations.kill_switch import KillSwitch


def test_kill_switch_blocks_trading() -> None:
    switch = KillSwitch()
    switch.engage("drawdown breach")
    with pytest.raises(RuntimeError):
        switch.assert_trading_allowed()
    switch.reset()
    switch.assert_trading_allowed()
