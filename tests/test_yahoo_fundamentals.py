from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.daemon import _env_intelligence_providers
from quant_ai.domain.models import Market
from quant_ai.governance.directives import DIRECTIVES_FILE_ENV, DIRECTIVES_JSON_ENV
from quant_ai.intelligence.external.yahoo_fundamentals import YahooFundamentalsProvider
from quant_ai.intelligence.failover import ProviderCategory
from quant_ai.intelligence.resilience import (
    HttpResponse,
    ResilientHttpClient,
    TokenBucketRateLimiter,
)

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
MARKET_TIME = datetime(2026, 9, 15, 8, 55, tzinfo=timezone.utc)
COOKIE = "A3=d=abc&S=xyz"
COOKIE_HEADER = f"{COOKIE}; Expires=Wed, 15 Sep 2027 08:00:00 GMT; Domain=.yahoo.com; Secure"
CRUMB = "crumb-token"
LOGGER = "quant_ai.yahoo_fundamentals"


def summary_result() -> dict:
    return {
        "summaryDetail": {
            "trailingPE": {"raw": 24.5, "fmt": "24.50"},
            "marketCap": {"raw": 6500000000000, "fmt": "6.5T"},
        },
        "financialData": {
            "debtToEquity": {"raw": 8.9, "fmt": "8.90%"},
            "operatingMargins": {"raw": 0.21, "fmt": "21.00%"},
            "freeCashflow": {"raw": 230000000000, "fmt": "230B"},
        },
        "defaultKeyStatistics": {"mostRecentQuarter": {"raw": 1751241600, "fmt": "2025-06-30"}},
        "price": {"regularMarketTime": {"raw": int(MARKET_TIME.timestamp()), "fmt": "8:55AM"}},
    }


def envelope(result: dict | None, error: dict | None = None) -> dict:
    return {"quoteSummary": {"result": None if result is None else [result], "error": error}}


def ok(payload: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode(), {})


class YahooTransport:
    """Serves Yahoo's cookie bootstrap, crumb and quoteSummary from canned responses."""

    def __init__(self, *summaries: HttpResponse) -> None:
        self.summaries = deque(summaries)
        self.requests: list[tuple[str, dict | None, dict | None]] = []

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.requests.append((url, params, headers))
        if url.startswith("https://fc.yahoo.com"):
            return HttpResponse(404, b"not found", {"set-cookie": COOKIE_HEADER})
        if url.endswith("/getcrumb"):
            assert headers["Cookie"] == COOKIE
            return HttpResponse(200, CRUMB.encode(), {})
        assert headers["Cookie"] == COOKIE
        assert params["crumb"] == CRUMB
        assert "financialData" in params["modules"]
        return self.summaries.popleft()

    def summary_urls(self) -> list[str]:
        return [url for url, _, _ in self.requests if "/quoteSummary/" in url]

    def crumb_requests(self) -> int:
        return sum(url.endswith("/getcrumb") for url, _, _ in self.requests)


def provider(transport: YahooTransport, markets=None, **kwargs) -> YahooFundamentalsProvider:
    client = ResilientHttpClient(
        transport,
        rate_limiter=TokenBucketRateLimiter(10.0, 1.0, sleeper=lambda _: None),
        max_attempts=1,
    )
    return YahooFundamentalsProvider(
        client, markets or {"INFY": Market.INDIA, "AAPL": Market.USA}, **kwargs
    )


def test_full_payload_maps_four_decimal_metrics_with_unit_conversions() -> None:
    transport = YahooTransport(ok(envelope(summary_result())))
    snapshot = provider(transport).fetch("INFY", NOW)
    assert snapshot.subject == "INFY"
    assert snapshot.metrics == {
        "pe": Decimal("24.5"),
        "debt_equity": Decimal("0.089"),
        "operating_margin": Decimal("0.21"),
        "fcf_yield": Decimal(230000000000) / Decimal(6500000000000),
    }
    assert all(isinstance(value, Decimal) for value in snapshot.metrics.values())
    assert snapshot.observed_at == MARKET_TIME
    assert transport.summary_urls() == [f"{YahooFundamentalsProvider.quote_summary_url}/INFY.NS"]


def test_plain_number_payload_is_accepted() -> None:
    result = summary_result()
    result["summaryDetail"] = {"trailingPE": 30, "marketCap": 1000}
    result["financialData"] = {"debtToEquity": 145.0, "operatingMargins": 0.3, "freeCashflow": 32}
    snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("AAPL", NOW)
    assert snapshot.metrics == {
        "pe": Decimal(30),
        "debt_equity": Decimal("1.45"),
        "operating_margin": Decimal("0.3"),
        "fcf_yield": Decimal("0.032"),
    }


def test_us_symbol_stays_bare() -> None:
    transport = YahooTransport(ok(envelope(summary_result())))
    provider(transport).fetch("AAPL", NOW)
    assert transport.summary_urls() == [f"{YahooFundamentalsProvider.quote_summary_url}/AAPL"]


def test_missing_field_is_left_out_and_named(caplog: pytest.LogCaptureFixture) -> None:
    result = summary_result()
    del result["financialData"]["debtToEquity"]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("INFY", NOW)
    assert snapshot.metrics == {
        "pe": Decimal("24.5"),
        "operating_margin": Decimal("0.21"),
        "fcf_yield": Decimal(230000000000) / Decimal(6500000000000),
    }
    assert snapshot.observed_at == MARKET_TIME
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]
    partial = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(partial) == 1
    message = partial[0].getMessage()
    assert "yahoo fundamentals partial: symbol=INFY.NS" in message
    assert "ratios=pe,operating_margin,fcf_yield" in message
    assert "missing=financialData.debtToEquity" in message


def test_non_numeric_field_is_left_out(caplog: pytest.LogCaptureFixture) -> None:
    result = summary_result()
    result["summaryDetail"]["trailingPE"] = {"raw": "n/a"}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("INFY", NOW)
    assert set(snapshot.metrics) == {"debt_equity", "operating_margin", "fcf_yield"}
    assert "missing=summaryDetail.trailingPE" in caplog.text


def test_missing_module_leaves_its_ratios_out(caplog: pytest.LogCaptureFixture) -> None:
    result = summary_result()
    del result["financialData"]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("INFY", NOW)
    assert snapshot.metrics == {"pe": Decimal("24.5")}
    assert (
        "missing=financialData.debtToEquity,financialData.operatingMargins,"
        "financialData.freeCashflow" in caplog.text
    )


def test_nse_listing_without_free_cashflow_keeps_the_other_three() -> None:
    # The shape Yahoo returned for eight of the pilot's nine NSE equities on 21 September
    # 2026: every ratio but free cash flow.
    result = summary_result()
    result["financialData"]["freeCashflow"] = {}
    snapshot = provider(YahooTransport(ok(envelope(result))), {"TRENT": Market.INDIA}).fetch("TRENT", NOW)
    assert set(snapshot.metrics) == {"pe", "debt_equity", "operating_margin"}
    assert snapshot.observed_at == MARKET_TIME


def test_fcf_yield_needs_a_positive_market_cap(caplog: pytest.LogCaptureFixture) -> None:
    result = summary_result()
    result["summaryDetail"]["marketCap"] = {"raw": 0}
    with caplog.at_level(logging.INFO, logger=LOGGER):
        snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("INFY", NOW)
    assert set(snapshot.metrics) == {"pe", "debt_equity", "operating_margin"}
    assert "missing=summaryDetail.marketCap:non_positive" in caplog.text
    absent = summary_result()
    del absent["summaryDetail"]["marketCap"]
    snapshot = provider(YahooTransport(ok(envelope(absent)))).fetch("INFY", NOW)
    assert set(snapshot.metrics) == {"pe", "debt_equity", "operating_margin"}


def test_no_ratio_at_all_abstains_and_names_every_field(caplog: pytest.LogCaptureFixture) -> None:
    # A metal ETF: a market cap and a price, no earnings, no balance sheet, no cash flow.
    result = summary_result()
    result["summaryDetail"] = {"marketCap": {"raw": 120000000000}}
    del result["financialData"]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        snapshot = provider(YahooTransport(ok(envelope(result))), {"GOLDBEES": Market.INDIA}).fetch("GOLDBEES", NOW)
    assert snapshot.metrics == {}
    assert snapshot.observed_at == NOW
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message.startswith("yahoo fundamentals abstain: symbol=GOLDBEES.NS reason=no_ratio_available: ")
    for name in (
        "summaryDetail.trailingPE",
        "financialData.debtToEquity",
        "financialData.operatingMargins",
        "financialData.freeCashflow",
    ):
        assert name in message
    assert not [record for record in caplog.records if record.levelno == logging.INFO]


def test_http_error_abstains(caplog: pytest.LogCaptureFixture) -> None:
    transport = YahooTransport(HttpResponse(500, b"upstream", {}))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        snapshot = provider(transport).fetch("INFY", NOW)
    assert snapshot.metrics == {}
    assert "ProviderHttpError" in caplog.text


def test_yahoo_error_envelope_abstains(caplog: pytest.LogCaptureFixture) -> None:
    error = {"code": "Not Found", "description": "Quote not found for ticker symbol: INFY.NS"}
    transport = YahooTransport(ok(envelope(None, error)))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        snapshot = provider(transport).fetch("INFY", NOW)
    assert snapshot.metrics == {}
    assert "Quote not found" in caplog.text


def test_unmapped_subject_abstains_without_request(caplog: pytest.LogCaptureFixture) -> None:
    transport = YahooTransport(ok(envelope(summary_result())))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        snapshot = provider(transport).fetch("TCS", NOW)
    assert snapshot.metrics == {}
    assert transport.requests == []
    assert "no_market_mapping" in caplog.text


def test_cache_hit_avoids_second_request() -> None:
    transport = YahooTransport(ok(envelope(summary_result())), ok(envelope(summary_result())))
    yahoo = provider(transport)
    first = yahoo.fetch("INFY", NOW)
    again = yahoo.fetch("INFY", NOW + timedelta(hours=5, minutes=59))
    assert again is first
    assert len(transport.summary_urls()) == 1
    assert transport.crumb_requests() == 1
    yahoo.fetch("INFY", NOW + timedelta(hours=6))
    assert len(transport.summary_urls()) == 2
    assert transport.crumb_requests() == 1


def test_abstention_is_retried_after_short_ttl() -> None:
    transport = YahooTransport(HttpResponse(503, b"", {}), ok(envelope(summary_result())))
    yahoo = provider(transport)
    assert yahoo.fetch("INFY", NOW).metrics == {}
    assert yahoo.fetch("INFY", NOW + timedelta(minutes=29)).metrics == {}
    assert len(transport.summary_urls()) == 1
    assert yahoo.fetch("INFY", NOW + timedelta(minutes=30)).metrics["pe"] == Decimal("24.5")
    assert len(transport.summary_urls()) == 2


def test_rejected_crumb_is_refreshed_once() -> None:
    rejected = HttpResponse(401, b'{"finance":{"error":{"description":"Invalid Crumb"}}}', {})
    transport = YahooTransport(rejected, ok(envelope(summary_result())))
    snapshot = provider(transport).fetch("INFY", NOW)
    assert snapshot.metrics["operating_margin"] == Decimal("0.21")
    assert len(transport.summary_urls()) == 2
    assert transport.crumb_requests() == 2


def test_observed_at_falls_back_to_now_without_market_time() -> None:
    result = summary_result()
    del result["price"]
    snapshot = provider(YahooTransport(ok(envelope(result)))).fetch("INFY", NOW)
    assert snapshot.metrics["pe"] == Decimal("24.5")
    assert snapshot.observed_at == NOW


def test_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError):
        provider(YahooTransport(), cache_ttl=timedelta(0))


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PRAMANA_NEWS_RSS_URLS", "FRED_API_KEY", DIRECTIVES_JSON_ENV, DIRECTIVES_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRAMANA_TARGET_SYMBOL", "INFY")
    monkeypatch.setenv("PRAMANA_TARGET_MARKET", "INDIA")


def _registered(fundamentals) -> tuple[object, ...]:
    return fundamentals.registry._providers.get(ProviderCategory.FUNDAMENTALS, ())


def test_env_wiring_registers_yahoo_by_default_without_boot_io(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.delenv("PRAMANA_FUNDAMENTALS_PROVIDER", raising=False)
    transport = YahooTransport(ok(envelope(summary_result())))
    monkeypatch.setattr("quant_ai.daemon.UrllibTransport", lambda: transport)
    _, fundamentals, _ = _env_intelligence_providers()
    assert [item.provider_id for item in _registered(fundamentals)] == ["yahoo-fundamentals"]
    assert transport.requests == []
    snapshot = fundamentals.fetch("INFY", NOW)
    assert snapshot.metrics["pe"] == Decimal("24.5")
    assert transport.summary_urls() == [f"{YahooFundamentalsProvider.quote_summary_url}/INFY.NS"]


def test_env_wiring_maps_founder_watchlist_symbols(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "yahoo")
    watchlist = [{"symbol": "AAPL", "market": "USA", "asset_class": "EQUITY", "currency": "USD"}]
    monkeypatch.setenv(DIRECTIVES_JSON_ENV, json.dumps({"watchlist": watchlist}))
    transport = YahooTransport(ok(envelope(summary_result())))
    monkeypatch.setattr("quant_ai.daemon.UrllibTransport", lambda: transport)
    _, fundamentals, _ = _env_intelligence_providers()
    assert fundamentals.fetch("AAPL", NOW).metrics["pe"] == Decimal("24.5")
    assert transport.summary_urls() == [f"{YahooFundamentalsProvider.quote_summary_url}/AAPL"]
    # The watchlist replaces the target instrument, exactly as build_ghost_runner evaluates it.
    assert fundamentals.fetch("INFY", NOW).metrics == {}
    assert len(transport.summary_urls()) == 1


def test_env_wiring_none_disables(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    transport = YahooTransport(ok(envelope(summary_result())))
    monkeypatch.setattr("quant_ai.daemon.UrllibTransport", lambda: transport)
    _, fundamentals, _ = _env_intelligence_providers()
    assert _registered(fundamentals) == ()
    assert fundamentals.fetch("INFY", NOW).metrics == {}
    assert transport.requests == []


def test_env_wiring_rejects_unknown_provider(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "bloomberg")
    with pytest.raises(RuntimeError, match="PRAMANA_FUNDAMENTALS_PROVIDER"):
        _env_intelligence_providers()
