from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)


def test_sandbox_news_sentiment_is_bounded_and_timestamped() -> None:
    now = datetime.now(timezone.utc)
    items = SandboxNewsSentimentProvider().fetch("NVDA", now)
    assert items and all(Decimal(-1) <= item.sentiment <= Decimal(1) for item in items)
    assert items[0].published_at == now


def test_sandbox_fundamentals_cover_india_and_us() -> None:
    now = datetime.now(timezone.utc)
    provider = SandboxFundamentalDataProvider()
    for symbol in ("RELIANCE", "TCS", "AAPL", "NVDA"):
        snapshot = provider.fetch(symbol, now)
        assert {"pe", "debt_equity", "operating_margin", "fcf_yield"} <= snapshot.metrics.keys()


def test_sandbox_macro_has_required_indicators() -> None:
    now = datetime.now(timezone.utc)
    keys = ("US10Y", "INDIA10Y", "BRENT", "GOLD", "USD_BROAD")
    snapshot = SandboxMacroIndicatorProvider().fetch(keys, now)
    assert set(snapshot.indicators) == set(keys)
