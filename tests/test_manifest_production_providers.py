"""Every provider the environment factory can build is a supported manifest component.

The runtime manifest marks itself incomplete on any component it cannot describe, and the
order manager refuses every new entry while it is incomplete. The Yahoo fundamentals
provider, the default source since it was added, was never registered, so every production
boot was incomplete and no entry could ever have been placed. This pins the whole
production graph, in every configuration the factory offers, to "no issues".
"""

from __future__ import annotations

import json

import pytest

from quant_ai.daemon import _env_intelligence_providers
from quant_ai.governance.runtime_manifest import FIELDS, describe
from quant_ai.intelligence.failover import ProviderCategory


def configure(monkeypatch, *, fundamentals: str, vix: str, flows: str, feeds: bool, fred: bool):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("PRAMANA_TARGET_MARKET", "INDIA")
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", fundamentals)
    monkeypatch.setenv("PRAMANA_INDIA_VIX_PROVIDER", vix)
    monkeypatch.setenv("PRAMANA_INDIA_FLOWS_PROVIDER", flows)
    monkeypatch.setenv("PRAMANA_NEWS_RSS_URLS", "https://news.invalid/a.rss,https://news.invalid/b.rss" if feeds else "")
    monkeypatch.delenv("PRAMANA_NEWS_SYMBOL_ALIASES_JSON", raising=False)
    if fred:
        monkeypatch.setenv("FRED_API_KEY", "fake-fred-key")
    else:
        monkeypatch.delenv("FRED_API_KEY", raising=False)


@pytest.mark.parametrize(
    "fundamentals, vix, flows, feeds, fred",
    [
        ("yahoo", "yahoo", "nse", True, True),  # the pilot host's configuration
        ("yahoo", "none", "none", True, True),  # the configuration the pilot ran all last week
        ("yahoo", "none", "none", False, False),  # fundamentals alone
        ("none", "yahoo", "nse", True, True),
        ("none", "none", "none", False, False),
    ],
)
def test_every_production_provider_graph_is_a_supported_manifest_component(
    monkeypatch, fundamentals, vix, flows, feeds, fred
):
    configure(monkeypatch, fundamentals=fundamentals, vix=vix, flows=flows, feeds=feeds, fred=fred)
    for wrapper in _env_intelligence_providers():
        issues: list[str] = []
        described = describe(wrapper, issues)
        assert issues == [], (type(wrapper).__name__, issues)
        # Nothing in the description is left unsupported or carries a raw endpoint.
        rendered = json.dumps(described)
        assert '"supported": false' not in rendered
        assert "https://" not in rendered


def test_yahoo_fundamentals_description_carries_its_strategy_relevant_settings(monkeypatch):
    configure(monkeypatch, fundamentals="yahoo", vix="none", flows="none", feeds=False, fred=False)
    _, fundamentals, _ = _env_intelligence_providers()
    issues: list[str] = []
    described = describe(fundamentals, issues)
    (yahoo,) = described["providers"][ProviderCategory.FUNDAMENTALS.value]
    assert issues == []
    assert yahoo["type"].endswith("YahooFundamentalsProvider")
    assert yahoo["parameters"]["cache_ttl"] == 6 * 3600.0
    assert yahoo["parameters"]["abstain_ttl"] == 30 * 60.0
    assert yahoo["parameters"]["modules"] == ["summaryDetail", "financialData", "defaultKeyStatistics", "price"]
    assert isinstance(yahoo["parameters"]["markets"], dict) and yahoo["parameters"]["markets"]
    assert "endpoint_sha256" in yahoo and "yahoo" not in yahoo["endpoint_sha256"]
    assert yahoo["http"]["type"].endswith("ResilientHttpClient")


def test_every_provider_class_the_factory_imports_is_registered():
    from quant_ai import daemon

    factory_types = {
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in (
            daemon.RssNewsSentimentAdapter,
            daemon.FredMacroProvider,
            daemon.YahooFundamentalsProvider,
            daemon.YahooIndiaVixProvider,
            daemon.NseInstitutionalFlowsProvider,
            daemon.CompositeMacroProvider,
        )
    }
    assert factory_types <= set(FIELDS), factory_types - set(FIELDS)
