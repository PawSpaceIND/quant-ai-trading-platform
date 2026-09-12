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
