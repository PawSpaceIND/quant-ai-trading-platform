class LiveTradingDisabled(RuntimeError):
    pass


def assert_live_trading_disabled() -> None:
    """Fail closed: V1 has no path to submit real-money orders."""
    raise LiveTradingDisabled(
        "Live trading is intentionally disabled until promotion gates are satisfied."
    )
