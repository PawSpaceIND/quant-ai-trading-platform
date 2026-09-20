"""Each RSS feed stands on its own: one failing feed costs that feed, not the cycle.

Before this guard the adapter fetched every feed inside one try, so a single HTTP error,
an oversized body or a non-XML answer emptied the news category for that subject, and an
HTML error page served with status 200 raised a ``ParseError`` that the failover registry
does not catch and so ended the whole analysis cycle. It also re-downloaded every feed for
every instrument and shared its HTTP client - and therefore its circuit breaker - with
FRED, so a dead feed could blank macro as well.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from quant_ai.daemon import _env_intelligence_providers
from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.external.rss import NewsFeedsUnavailable, RssNewsSentimentAdapter
from quant_ai.intelligence.failover import (
    FailoverNewsProvider,
    ProviderCategory,
    ProviderFailoverRegistry,
)
from quant_ai.intelligence.resilience import HttpResponse, ResilientHttpClient

NOW = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
GOOD = "https://good.example/markets.rss"
BAD = "https://bad.example/feed?api_key=DO_NOT_LOG_THIS_VALUE"
SECRET = "DO_NOT_LOG_THIS_VALUE"


def feed(*titles: str) -> HttpResponse:
    items = "".join(
        f"<item><title>{title}</title><pubDate>Mon, 21 Sep 2026 03:30:00 GMT</pubDate></item>"
        for title in titles
    )
    return HttpResponse(200, f"<rss><channel>{items}</channel></rss>".encode(), {})


class ScriptedTransport:
    """Answers each URL from its script; the last entry repeats once the script runs out."""

    def __init__(self, script: dict[str, list[HttpResponse]]) -> None:
        self.script = {url: list(answers) for url, answers in script.items()}
        self.calls: list[str] = []

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.calls.append(url)
        answers = self.script[url]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def count(self, url: str) -> int:
        return self.calls.count(url)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def adapter(script: dict[str, list[HttpResponse]], clock: Clock | None = None):
    transport = ScriptedTransport(script)
    client = ResilientHttpClient(transport, max_attempts=1)
    feeds = tuple(script)
    return (
        RssNewsSentimentAdapter(client, feeds, clock=clock or Clock()),
        transport,
    )


def failover(rss: RssNewsSentimentAdapter) -> tuple[FailoverNewsProvider, ProviderFailoverRegistry]:
    registry = ProviderFailoverRegistry()
    registry.register(ProviderCategory.NEWS, rss)
    return FailoverNewsProvider(registry), registry


def test_one_failing_feed_costs_only_that_feed(caplog):
    rss, transport = adapter({GOOD: [feed("TRENT rally")], BAD: [HttpResponse(500, b"", {})]})
    with caplog.at_level(logging.WARNING, logger="quant_ai.rss_news"):
        signals = rss.fetch("TRENT", NOW)
    assert [(item.headline, item.source) for item in signals] == [("TRENT rally", GOOD)]
    assert transport.count(GOOD) == 1 and transport.count(BAD) == 1
    record = next(r for r in caplog.records if r.getMessage().startswith("rss_feed_unavailable"))
    assert "host=bad.example" in record.getMessage()
    assert "error=ProviderHttpError" in record.getMessage()
    assert "held_body=False" in record.getMessage()
    # Paid feeds carry their key in the URL; only the host is ever written down.
    assert SECRET not in caplog.text
    assert BAD not in caplog.text


# A real error page, not well-formed XML: unclosed <meta> and <br> make the parser refuse it.
HTML_ERROR_PAGE = (
    b"<!DOCTYPE html><html><head><meta charset=utf-8><title>503</title></head>"
    b"<body><p>Service temporarily unavailable<br></body></html>"
)


def test_html_error_page_is_one_feed_out_not_a_cycle_failure(caplog):
    rss, _ = adapter({GOOD: [feed("BEL wins order")], BAD: [HttpResponse(200, HTML_ERROR_PAGE, {})]})
    provider, registry = failover(rss)
    with caplog.at_level(logging.WARNING, logger="quant_ai.rss_news"):
        signals = provider.fetch("BEL", NOW)
    assert [item.headline for item in signals] == ["BEL wins order"]
    assert "error=ParseError" in caplog.text
    # Nothing failed as far as the registry is concerned: the good feed answered.
    assert registry.last_attempts == ()


def test_every_feed_out_is_no_headlines_with_the_hosts_on_record():
    rss, _ = adapter({GOOD: [HttpResponse(503, b"", {})], BAD: [HttpResponse(500, b"", {})]})
    with pytest.raises(NewsFeedsUnavailable) as failure:
        rss.fetch("TRENT", NOW)
    assert str(failure.value) == "news_feeds_unavailable:good.example;bad.example"
    assert SECRET not in str(failure.value)
    provider, registry = failover(rss)
    assert provider.fetch("TRENT", NOW) == ()
    assert [(a.provider_id, a.error) for a in registry.last_attempts] == [
        ("rss-news", "NewsFeedsUnavailable")
    ]


def test_each_feed_is_downloaded_once_per_window_however_many_subjects_ask():
    clock = Clock()
    rss, transport = adapter({GOOD: [feed("TRENT rally", "world markets")]}, clock)
    for subject in ("TRENT", "GEOPOLITICAL", "BEL", "NTPC", "GOLDBEES"):
        rss.fetch(subject, NOW)
    assert transport.count(GOOD) == 1
    clock.advance(299)
    rss.fetch("TRENT", NOW)
    assert transport.count(GOOD) == 1
    clock.advance(2)
    assert [item.headline for item in rss.fetch("TRENT", NOW)] == ["TRENT rally"]
    assert transport.count(GOOD) == 2


def test_last_good_body_stands_in_while_a_feed_fails_then_expires(caplog):
    clock = Clock()
    rss, transport = adapter({GOOD: [feed("TRENT rally"), HttpResponse(500, b"", {})]}, clock)
    assert len(rss.fetch("TRENT", NOW)) == 1
    clock.advance(400)  # past the cache window: a refresh is attempted and refused
    with caplog.at_level(logging.WARNING, logger="quant_ai.rss_news"):
        assert [item.headline for item in rss.fetch("TRENT", NOW)] == ["TRENT rally"]
    assert transport.count(GOOD) == 2
    assert "held_body=True" in caplog.text
    clock.advance(1401)  # 1801 s after the last good body: the grace is spent
    with pytest.raises(NewsFeedsUnavailable):
        rss.fetch("TRENT", NOW)
    assert transport.count(GOOD) == 3


def test_a_failed_feed_waits_out_its_backoff_before_it_is_asked_again():
    clock = Clock()
    rss, transport = adapter({GOOD: [feed("TRENT rally")], BAD: [HttpResponse(500, b"", {})]}, clock)
    rss.fetch("TRENT", NOW)
    clock.advance(30)
    rss.fetch("GEOPOLITICAL", NOW)
    clock.advance(29)
    rss.fetch("BEL", NOW)
    assert transport.count(BAD) == 1
    clock.advance(2)
    rss.fetch("NTPC", NOW)
    assert transport.count(BAD) == 2


def test_a_recovered_feed_answers_again_and_clears_its_failure():
    clock = Clock()
    rss, transport = adapter({BAD: [HttpResponse(500, b"", {}), feed("TRENT rally")]}, clock)
    with pytest.raises(NewsFeedsUnavailable):
        rss.fetch("TRENT", NOW)
    clock.advance(61)
    assert [item.headline for item in rss.fetch("TRENT", NOW)] == ["TRENT rally"]
    clock.advance(10)
    rss.fetch("TRENT", NOW)
    assert transport.count(BAD) == 2  # cached again after recovery


@pytest.mark.parametrize(
    "windows",
    [
        {"cache_ttl_seconds": 0},
        {"retry_after_seconds": -1},
        {"stale_grace_seconds": 0},
        {"cache_ttl_seconds": 600, "stale_grace_seconds": 300},
    ],
)
def test_feed_windows_are_validated(windows):
    client = ResilientHttpClient(ScriptedTransport({GOOD: [feed()]}), max_attempts=1)
    with pytest.raises(ValueError):
        RssNewsSentimentAdapter(client, (GOOD,), **windows)


def test_daemon_gives_news_and_macro_separate_clients(monkeypatch):
    monkeypatch.setenv("PRAMANA_NEWS_RSS_URLS", f"{GOOD}, https://other.example/rss")
    monkeypatch.setenv("FRED_API_KEY", "fake-fred-key")
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    monkeypatch.delenv("PRAMANA_NEWS_SYMBOL_ALIASES_JSON", raising=False)
    news, _, macro = _env_intelligence_providers()
    (rss,) = news.registry._providers[ProviderCategory.NEWS]
    (composite,) = macro.registry._providers[ProviderCategory.MACRO]
    (fred,) = composite.parts  # macro is one composite adapter; FRED is its core part
    assert isinstance(rss, RssNewsSentimentAdapter) and isinstance(fred, FredMacroProvider)
    assert rss.feed_urls == (GOOD, "https://other.example/rss")
    assert rss.client is not fred.client
    assert rss.client.circuit_breaker is not fred.client.circuit_breaker
    assert rss.client.max_attempts == 1
    assert fred.client.max_attempts == 3
