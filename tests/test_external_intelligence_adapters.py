from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
from quant_ai.intelligence.external.yahoo import YahooFinanceMarketDataAdapter
from quant_ai.intelligence.resilience import HttpResponse, ResilientHttpClient


class StaticTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        return HttpResponse(200, self.body, {})


def client(body: bytes) -> ResilientHttpClient:
    return ResilientHttpClient(StaticTransport(body), max_attempts=1)


def test_yahoo_normalizes_candles() -> None:
    payload = {
        "chart": {"error": None, "result": [{
            "timestamp": [1700000000],
            "indicators": {"quote": [{
                "open": [100], "high": [102], "low": [99], "close": [101], "volume": [5000]
            }]},
        }]}
    }
    adapter = YahooFinanceMarketDataAdapter(client(json.dumps(payload).encode()))
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    candles = adapter.fetch_ohlcv(
        instrument,
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        datetime(2023, 11, 15, tzinfo=timezone.utc),
    )
    assert len(candles) == 1
    assert candles[0].close == Decimal(101)


def test_fred_normalizes_latest_observation() -> None:
    body = json.dumps({"observations": [{"date": "2026-09-11", "value": "4.25"}]}).encode()
    provider = FredMacroProvider(client(body), "a" * 32)
    snapshot = provider.fetch(("US10Y",), datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert snapshot.indicators["US10Y"] == Decimal("4.25")


def test_rss_maps_news_sentiment() -> None:
    xml = b"""<rss><channel><item><title>AAPL growth beat rally</title>
    <description>strong gain</description><pubDate>Sat, 12 Sep 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>"""
    provider = RssNewsSentimentAdapter(client(xml), ("https://news.invalid/rss",))
    items = provider.fetch("AAPL", datetime(2026, 9, 12, 10, 5, tzinfo=timezone.utc))
    assert len(items) == 1
    assert items[0].sentiment > 0


class RoutingTransport:
    """Answers per ``series_id`` so one retired FRED series can be isolated."""

    def __init__(self, bodies: dict[str, bytes], rejected: frozenset[str] = frozenset()) -> None:
        self.bodies = bodies
        self.rejected = rejected
        self.requested: list[str] = []

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        series = params["series_id"]
        self.requested.append(series)
        if series in self.rejected:
            return HttpResponse(400, b"", {})
        return HttpResponse(200, self.bodies[series], {})


def observations(day: str, value: str) -> bytes:
    return json.dumps({"observations": [{"date": day, "value": value}]}).encode()


def test_fred_keeps_the_working_series_when_another_is_rejected() -> None:
    transport = RoutingTransport(
        {"DGS10": observations("2026-09-17", "4.94")},
        rejected=frozenset({"GOLDAMGBD228NLBM"}),
    )
    provider = FredMacroProvider(ResilientHttpClient(transport, max_attempts=1), "a" * 32)
    snapshot = provider.fetch(("GOLD", "US10Y"), datetime(2026, 9, 19, tzinfo=timezone.utc))
    # The rejected series is asked for first on purpose: before this fix its error left
    # the loop and the caller received an empty snapshot for every indicator.
    assert transport.requested == ["GOLDAMGBD228NLBM", "DGS10"]
    assert snapshot.indicators == {"US10Y": Decimal("4.94")}
    assert snapshot.observed_at == datetime(2026, 9, 17, tzinfo=timezone.utc)


def test_fred_abstention_names_the_indicator_and_series(caplog) -> None:
    transport = RoutingTransport({}, rejected=frozenset({"GOLDAMGBD228NLBM"}))
    provider = FredMacroProvider(ResilientHttpClient(transport, max_attempts=1), "a" * 32)
    with caplog.at_level("WARNING", logger="quant_ai.intelligence.external.fred"):
        snapshot = provider.fetch(("GOLD",), datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert snapshot.indicators == {}
    assert "fred macro abstain: indicator=GOLD series=GOLDAMGBD228NLBM" in caplog.text
    assert "provider_request_rejected" in caplog.text


def test_fred_abstention_never_renders_the_api_key(caplog) -> None:
    key = "b" * 32

    class LeakingTransport:
        def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
            raise OSError(f"connect failed: {url}?api_key={params['api_key']}")

    provider = FredMacroProvider(ResilientHttpClient(LeakingTransport(), max_attempts=1), key)
    with caplog.at_level("WARNING", logger="quant_ai.intelligence.external.fred"):
        provider.fetch(("US10Y",), datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert "fred macro abstain" in caplog.text
    assert key not in caplog.text
    assert "***" in caplog.text
