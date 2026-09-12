from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.intelligence.providers import FundamentalSnapshot, MacroSnapshot, NewsSignal


def test_provider_payload_contracts_are_structured() -> None:
    now = datetime.now(timezone.utc)
    news = NewsSignal("AAPL", "Example", Decimal("0.5"), "sandbox", now)
    fundamentals = FundamentalSnapshot("AAPL", {"pe": Decimal(30)}, now)
    macro = MacroSnapshot({"us10y": Decimal("4.1")}, now)
    assert news.sentiment == Decimal("0.5")
    assert fundamentals.metrics["pe"] == Decimal(30)
    assert macro.indicators["us10y"] == Decimal("4.1")
