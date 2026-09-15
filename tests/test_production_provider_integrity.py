from datetime import datetime, timezone

from quant_ai.daemon import _env_intelligence_providers
from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays


def test_unconfigured_production_providers_never_invent_inputs(monkeypatch):
    monkeypatch.delenv("PRAMANA_NEWS_RSS_URLS", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    news, fundamentals, macro = _env_intelligence_providers()
    now = datetime.now(timezone.utc)
    assert news.fetch("GEOPOLITICAL", now) == ()
    assert fundamentals.fetch("RELIANCE", now).metrics == {}
    assert macro.fetch(("US_10Y",), now).indicators == {}


def test_holi_and_election_closures():
    calendar = MarketCalendar(default_holidays())
    for month, day in ((1, 15), (3, 3), (3, 26), (3, 31), (5, 28), (6, 26), (10, 20), (11, 10), (11, 24)):
        assert calendar.state(Market.INDIA, datetime(2026, month, day, 6, tzinfo=timezone.utc)) == MarketState.CLOSED
    assert calendar.state(Market.INDIA, datetime(2026, 3, 4, 6, tzinfo=timezone.utc)) == MarketState.REGULAR_HOURS
