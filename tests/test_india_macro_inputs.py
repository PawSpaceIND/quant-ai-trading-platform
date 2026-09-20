"""India VIX and NSE FII/DII flows ride beside FRED's core series, fail closed, and reach
the Indian equities specialist as optional inputs it can tell apart from zero.

The payloads below are the shapes the host observed on 20 September 2026: Yahoo's
``^INDIAVIX`` daily chart and NSE's ``fiidiiTradeReact`` rows for 18 September.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.swarm import AgentAnalysisRequest, IndianEquitiesAgent
from quant_ai.daemon import _env_intelligence_providers
from quant_ai.domain.models import AssetClass, Market
from quant_ai.governance.runtime_manifest import describe
from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.external.india_macro import (
    CompositeMacroProvider,
    MacroPartsUnavailable,
    NseInstitutionalFlowsProvider,
    YahooIndiaVixProvider,
)
from quant_ai.intelligence.failover import (
    FailoverMacroProvider,
    ProviderCategory,
    ProviderFailoverRegistry,
)
from quant_ai.intelligence.providers import (
    INDIA_MACRO_INDICATORS,
    MACRO_CORE_INDICATORS,
    MACRO_INDICATORS,
    MacroSnapshot,
)
from quant_ai.intelligence.resilience import HttpResponse, ResilientHttpClient
from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration

# Sunday 20 September 2026, 12:00 UTC: the weekend before the first measured session.
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
FRIDAY_LAST_TRADE = 1789726403  # 18 Sep 2026 10:13:23 UTC, Yahoo's regularMarketTime
FRIDAY_BAR = 1789703100  # 18 Sep 2026 03:45 UTC, the 09:15 IST bar stamp

VIX_PAYLOAD = {
    "chart": {
        "error": None,
        "result": [{
            "meta": {"symbol": "^INDIAVIX", "exchangeTimezoneName": "Asia/Kolkata",
                     "regularMarketTime": FRIDAY_LAST_TRADE},
            "timestamp": [1789443900, 1789530300, 1789616700, FRIDAY_BAR],
            "indicators": {"quote": [{
                "close": [13.430000305175781, 13.170000076293945, 12.289999961853027, 11.390000343322754],
            }]},
        }],
    }
}
NSE_ROWS = [
    {"buyValue": "17310.04", "category": "DII", "date": "18-Sep-2026", "netValue": "1019.69", "sellValue": "16290.35"},
    {"buyValue": "38461.63", "category": "FII/FPI", "date": "18-Sep-2026", "netValue": "599.54", "sellValue": "37862.09"},
]
NSE_COOKIES = (
    "nsit=abc123; Path=/; Secure",
    "nseappid=eyJ.token; Path=/; HttpOnly",
    "ak_bmsc=0AF1; Domain=.nseindia.com; Path=/",
)
FRIDAY_CLOSE_UTC = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)  # 15:30 IST


class ScriptedTransport:
    """Answers each exact URL from a script of responses; the last entry repeats."""

    def __init__(self, script: dict[str, list[HttpResponse]]) -> None:
        self.script = {prefix: list(answers) for prefix, answers in script.items()}
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.calls.append((url, dict(headers or {}), dict(params or {})))
        answers = self.script[url]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def count(self, url: str) -> int:
        return sum(1 for called, _, _ in self.calls if called == url)


def ok(payload) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode(), {})


def client(transport, **kwargs) -> ResilientHttpClient:
    return ResilientHttpClient(transport, max_attempts=1, **kwargs)


def vix_provider(payload=VIX_PAYLOAD):
    transport = ScriptedTransport({YahooIndiaVixProvider.endpoint: [ok(payload)]})
    return YahooIndiaVixProvider(client(transport)), transport


def nse_provider(rows=NSE_ROWS, api_answers=None):
    transport = ScriptedTransport({
        NseInstitutionalFlowsProvider.endpoint: api_answers or [ok(rows)],
        NseInstitutionalFlowsProvider.cookie_url: [HttpResponse(200, b"<html>nse</html>", {}, NSE_COOKIES)],
    })
    return NseInstitutionalFlowsProvider(client(transport)), transport


# --- India VIX --------------------------------------------------------------------------


def test_india_vix_reads_the_last_two_bars_and_stamps_the_last_trade():
    provider, transport = vix_provider()
    snapshot = provider.fetch(MACRO_INDICATORS, NOW)
    assert snapshot.indicators == {"INDIA_VIX": Decimal("11.39"), "INDIA_VIX_PREV_CLOSE": Decimal("12.29")}
    assert snapshot.observed_at == datetime.fromtimestamp(FRIDAY_LAST_TRADE, tz=timezone.utc)
    assert snapshot.freshness_observed_at == snapshot.observed_at
    url, headers, params = transport.calls[0]
    assert url.endswith("/chart/%5EINDIAVIX")
    assert params == {"range": "5d", "interval": "1d"}
    assert headers["User-Agent"] == "quant-ai-readonly/1.0"


def test_india_vix_is_cached_across_instruments_and_ignores_requests_it_does_not_serve():
    provider, transport = vix_provider()
    for _ in range(5):
        provider.fetch(MACRO_INDICATORS, NOW)
    assert transport.count(YahooIndiaVixProvider.endpoint) == 1
    provider.fetch(MACRO_INDICATORS, NOW + timedelta(minutes=6))
    assert transport.count(YahooIndiaVixProvider.endpoint) == 2
    assert provider.fetch(MACRO_CORE_INDICATORS, NOW).indicators == {}
    assert transport.count(YahooIndiaVixProvider.endpoint) == 2


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda r: r["indicators"]["quote"][0].__setitem__("close", [None, None, None, 11.39]), "india_vix_insufficient_bars"),
        (lambda r: r["meta"].__setitem__("regularMarketTime", int(NOW.timestamp()) + 3600), "india_vix_future_observation"),
        (lambda r: r["indicators"]["quote"][0].__setitem__("close", [13.4, 13.1, 12.2, 0.0]), "india_vix_nonpositive"),
    ],
)
def test_india_vix_refuses_thin_future_or_nonpositive_readings(mutate, reason):
    payload = json.loads(json.dumps(VIX_PAYLOAD))
    mutate(payload["chart"]["result"][0])
    provider, _ = vix_provider(payload)
    with pytest.raises(ValueError, match=reason):
        provider.fetch(MACRO_INDICATORS, NOW)


# --- NSE flows ----------------------------------------------------------------------------


def test_nse_flows_bootstrap_every_cookie_then_read_the_latest_day():
    provider, transport = nse_provider()
    snapshot = provider.fetch(MACRO_INDICATORS, NOW)
    assert snapshot.indicators == {"FII_NET_CRORE": Decimal("599.54"), "DII_NET_CRORE": Decimal("1019.69")}
    assert snapshot.observed_at == FRIDAY_CLOSE_UTC
    assert snapshot.oldest_observed_at is None
    api_calls = [headers for url, headers, _ in transport.calls if url == NseInstitutionalFlowsProvider.endpoint]
    assert api_calls[0]["Cookie"] == "nsit=abc123; nseappid=eyJ.token; ak_bmsc=0AF1"
    assert api_calls[0]["User-Agent"] == "quant-ai-readonly/1.0"
    assert api_calls[0]["Referer"] == NseInstitutionalFlowsProvider.referer
    assert transport.count(NseInstitutionalFlowsProvider.cookie_url) == 1


def test_nse_flows_refresh_the_session_once_when_the_api_refuses_it():
    provider, transport = nse_provider(api_answers=[HttpResponse(401, b"", {}), ok(NSE_ROWS)])
    snapshot = provider.fetch(MACRO_INDICATORS, NOW)
    assert snapshot.indicators["FII_NET_CRORE"] == Decimal("599.54")
    assert transport.count(NseInstitutionalFlowsProvider.cookie_url) == 2
    assert transport.count(NseInstitutionalFlowsProvider.endpoint) == 2


def test_nse_flows_tolerate_commas_and_carry_a_partial_day():
    rows = [{"category": "FII/FPI", "date": "18-Sep-2026", "netValue": "-3,412.50"}]
    provider, _ = nse_provider(rows)
    snapshot = provider.fetch(MACRO_INDICATORS, NOW)
    assert snapshot.indicators == {"FII_NET_CRORE": Decimal("-3412.50")}


@pytest.mark.parametrize(
    "rows, reason",
    [
        ([], "nse_flows_no_rows"),
        ([{"category": "FII/FPI", "date": "22-Sep-2026", "netValue": "1"}], "nse_flows_future_observation"),
        ([{"category": "FII/FPI", "date": "2026-09-18", "netValue": "1"}], "nse_flows_date_invalid"),
        ([{"category": "FII/FPI", "date": "18-Sep-2026", "netValue": "n/a"}], "not_a_number"),
    ],
)
def test_nse_flows_refuse_malformed_rows(rows, reason):
    provider, _ = nse_provider(rows)
    with pytest.raises(ValueError, match=reason):
        provider.fetch(MACRO_INDICATORS, NOW)


def test_nse_flows_hold_a_failure_as_an_abstention_instead_of_hammering():
    provider, transport = nse_provider(api_answers=[HttpResponse(500, b"", {})])
    with pytest.raises(RuntimeError):
        provider.fetch(MACRO_INDICATORS, NOW)
    assert provider.fetch(MACRO_INDICATORS, NOW + timedelta(minutes=1)).indicators == {}
    assert transport.count(NseInstitutionalFlowsProvider.endpoint) == 1
    with pytest.raises(RuntimeError):
        provider.fetch(MACRO_INDICATORS, NOW + timedelta(minutes=11))
    assert transport.count(NseInstitutionalFlowsProvider.endpoint) == 2


# --- Composite ------------------------------------------------------------------------------


class StubPart:
    def __init__(self, provider_id, serves, snapshot=None, error=None):
        self.provider_id = provider_id
        self.serves = serves
        self.snapshot = snapshot
        self.error = error
        self.requests: list[tuple[str, ...]] = []

    def fetch(self, indicators, now):
        self.requests.append(tuple(indicators))
        if self.error is not None:
            raise self.error
        return self.snapshot


class CoreLikeFred:
    """No ``serves``: given every name, answers the ones it maps, like FRED does."""

    provider_id = "fred"

    def __init__(self, error=None):
        self.error = error
        self.requests = []

    def fetch(self, indicators, now):
        self.requests.append(tuple(indicators))
        if self.error is not None:
            raise self.error
        core = {name: Decimal(1) for name in indicators if name in MACRO_CORE_INDICATORS}
        return MacroSnapshot(
            core,
            datetime(2026, 9, 18, tzinfo=timezone.utc),
            datetime(2026, 9, 17, tzinfo=timezone.utc),
        )


def test_composite_routes_by_served_names_and_merges_clocks():
    fred = CoreLikeFred()
    vix = StubPart("yahoo-india-vix", ("INDIA_VIX", "INDIA_VIX_PREV_CLOSE"),
                   MacroSnapshot({"INDIA_VIX": Decimal("11.39"), "INDIA_VIX_PREV_CLOSE": Decimal("12.29")},
                                 datetime(2026, 9, 18, 10, 13, tzinfo=timezone.utc)))
    flows = StubPart("nse-fii-dii", ("FII_NET_CRORE", "DII_NET_CRORE"),
                     MacroSnapshot({"FII_NET_CRORE": Decimal("599.54")}, FRIDAY_CLOSE_UTC))
    snapshot = CompositeMacroProvider((fred, vix, flows)).fetch(MACRO_INDICATORS, NOW)
    assert set(snapshot.indicators) == set(MACRO_CORE_INDICATORS) | {"INDIA_VIX", "INDIA_VIX_PREV_CLOSE", "FII_NET_CRORE"}
    # The clock is the core's latest observation, not the fresher VIX trade time; the oldest
    # clock still bounds freshness across every part.
    assert snapshot.observed_at == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert snapshot.freshness_observed_at == datetime(2026, 9, 17, tzinfo=timezone.utc)
    assert fred.requests == [MACRO_INDICATORS]
    assert vix.requests == [("INDIA_VIX", "INDIA_VIX_PREV_CLOSE")]
    assert flows.requests == [("FII_NET_CRORE", "DII_NET_CRORE")]


def test_composite_leaves_a_failed_part_out_and_logs_it(caplog):
    fred = CoreLikeFred()
    flows = StubPart("nse-fii-dii", ("FII_NET_CRORE", "DII_NET_CRORE"), error=TimeoutError("nse down"))
    with caplog.at_level(logging.WARNING, logger="quant_ai.india_macro"):
        snapshot = CompositeMacroProvider((fred, flows)).fetch(MACRO_INDICATORS, NOW)
    assert set(snapshot.indicators) == set(MACRO_CORE_INDICATORS)
    assert "macro_part_unavailable provider=nse-fii-dii error=TimeoutError" in caplog.text


def test_composite_raises_only_when_every_asked_part_fails_and_the_registry_blanks_it():
    fred = CoreLikeFred(error=ValueError("fred_key_rejected"))
    vix = StubPart("yahoo-india-vix", ("INDIA_VIX", "INDIA_VIX_PREV_CLOSE"), error=OSError("yahoo down"))
    composite = CompositeMacroProvider((fred, vix))
    with pytest.raises(MacroPartsUnavailable, match="macro_parts_unavailable:fred;yahoo-india-vix"):
        composite.fetch(MACRO_INDICATORS, NOW)
    registry = ProviderFailoverRegistry()
    registry.register(ProviderCategory.MACRO, composite)
    assert FailoverMacroProvider(registry).fetch(MACRO_INDICATORS, NOW).indicators == {}
    assert [(a.provider_id, a.error) for a in registry.last_attempts] == [("composite-macro", "MacroPartsUnavailable")]


def test_composite_asks_nobody_for_names_nobody_serves():
    vix = StubPart("yahoo-india-vix", ("INDIA_VIX", "INDIA_VIX_PREV_CLOSE"), MacroSnapshot({}, NOW))
    snapshot = CompositeMacroProvider((vix,)).fetch(("US_10Y",), NOW)
    assert snapshot.indicators == {} and vix.requests == []


# --- Pipeline metrics ------------------------------------------------------------------------


def pipeline_metrics():
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline

    holder = SwarmMarketAnalysisPipeline.__new__(SwarmMarketAnalysisPipeline)
    holder._macro_current_at = None
    holder._macro_current = {}
    holder._macro_previous = {}
    return holder


def test_macro_metrics_carry_india_context_only_when_observed():
    holder = pipeline_metrics()
    core = {name: Decimal(2) for name in MACRO_CORE_INDICATORS}
    with_india = MacroSnapshot({**core, "INDIA_VIX": Decimal("11.39"), "INDIA_VIX_PREV_CLOSE": Decimal("12.29"),
                                "FII_NET_CRORE": Decimal("599.54"), "DII_NET_CRORE": Decimal("1019.69")}, NOW)
    metrics = holder._macro_metrics(with_india)
    assert metrics["india_vix"] == Decimal("11.39")
    assert metrics["india_vix_change"] == (Decimal("11.39") - Decimal("12.29")) / Decimal("12.29")
    assert metrics["fii_net_crore"] == Decimal("599.54") and metrics["dii_net_crore"] == Decimal("1019.69")
    without = holder._macro_metrics(MacroSnapshot(core, NOW))
    assert not {"india_vix", "india_vix_change", "fii_net_crore", "dii_net_crore"} & without.keys()
    assert without["us10y"] == Decimal(2)


def test_composite_keeps_the_core_clock_so_daily_changes_survive_a_moving_vix():
    friday = {"US10Y": Decimal("4.0"), "BRENT": Decimal(80), "GOLD": Decimal(100), "USD_BROAD": Decimal(120)}
    monday = {"US10Y": Decimal("4.2"), "BRENT": Decimal(84), "GOLD": Decimal(100), "USD_BROAD": Decimal(120)}
    fred_day = datetime(2026, 9, 18, tzinfo=timezone.utc)
    session = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)

    class Core:
        def __init__(self):
            self.values, self.day = friday, fred_day

        def fetch(self, indicators, now):
            return MacroSnapshot(dict(self.values), self.day)

    class Vix(StubPart):
        def fetch(self, indicators, now):
            # A new bar every cycle: the VIX stamp is always the freshest thing in the book.
            return MacroSnapshot({"INDIA_VIX": Decimal(11)}, now - timedelta(minutes=1))

    core = Core()
    composite = CompositeMacroProvider((core, Vix("yahoo-india-vix", ("INDIA_VIX", "INDIA_VIX_PREV_CLOSE"))))
    holder = pipeline_metrics()
    first = composite.fetch(MACRO_INDICATORS, session)
    assert first.observed_at == fred_day and first.indicators["INDIA_VIX"] == Decimal(11)
    holder._macro_metrics(first)
    # Ten minutes later: same core observation, newer VIX. The clock has not moved, so there
    # is no spurious "previous" and the daily changes still read zero.
    same = holder._macro_metrics(composite.fetch(MACRO_INDICATORS, session + timedelta(minutes=10)))
    assert same["brent_change"] == 0 and same["yield_change"] == 0
    # FRED publishes Monday's observation: the clock moves once and the change is against
    # Friday's values, not against ten minutes ago.
    core.values, core.day = monday, datetime(2026, 9, 21, tzinfo=timezone.utc)
    moved = holder._macro_metrics(composite.fetch(MACRO_INDICATORS, session + timedelta(hours=1)))
    assert moved["brent_change"] == Decimal(4) / Decimal(80)
    assert moved["yield_change"] == Decimal("0.2") / Decimal("4.0")
    later = holder._macro_metrics(composite.fetch(MACRO_INDICATORS, session + timedelta(hours=2)))
    assert later["brent_change"] == Decimal(4) / Decimal(80)
    # Without a core part the clock is whatever answered.
    vix_only = CompositeMacroProvider((Vix("yahoo-india-vix", ("INDIA_VIX",)),)).fetch(MACRO_INDICATORS, session)
    assert vix_only.observed_at == session - timedelta(minutes=1)


# --- Indian equities specialist ------------------------------------------------------------


BASE = {"pe": Decimal(22), "debt_equity": Decimal("0.3"), "operating_margin": Decimal("0.2"),
        "fcf_yield": Decimal("0.03"), "equity_news_sentiment": Decimal(0)}


def evidence(metrics, asset_class=AssetClass.EQUITY):
    request = AgentAnalysisRequest("TRENT", Market.INDIA, asset_class, NOW, {**BASE, **metrics}, 0)
    return IndianEquitiesAgent().analyze(request)


def score_of(item) -> Decimal:
    return item.expected_return / Decimal("0.04")


def test_indian_equities_baseline_is_unchanged_when_india_context_is_absent():
    item = evidence({})
    assert score_of(item) == Decimal("0.90")
    assert item.rationale[0] == "india_valuation_balance_sheet_margin_and_news"


@pytest.mark.parametrize(
    "metrics, delta, note",
    [
        ({"india_vix": Decimal("11.39"), "india_vix_change": Decimal("-0.07")}, Decimal(0), ";india_vix=11.39:calm"),
        ({"india_vix": Decimal("21.5")}, Decimal("-0.20"), ";india_vix=21.5:elevated"),
        ({"india_vix": Decimal(27)}, Decimal("-0.35"), ";india_vix=27:stressed"),
        ({"india_vix": Decimal(14), "india_vix_change": Decimal("0.18")}, Decimal("-0.15"), ";india_vix=14:calm;india_vix_spike"),
        ({"fii_net_crore": Decimal("599.54")}, Decimal(0), ";fii_net_crore=599.54:quiet"),
        ({"fii_net_crore": Decimal(2500)}, Decimal("0.10"), ";fii_net_crore=2500:inflow"),
        ({"fii_net_crore": Decimal(-3412)}, Decimal("-0.15"), ";fii_net_crore=-3412:outflow"),
    ],
)
def test_indian_equities_reads_the_tape_and_says_what_it_read(metrics, delta, note):
    baseline = score_of(evidence({}))
    item = evidence(metrics)
    assert score_of(item) == baseline + delta
    assert item.rationale[0] == "india_valuation_balance_sheet_margin_and_news" + note


def test_stressed_tape_with_foreign_selling_turns_a_buy_into_neutral():
    item = evidence({"india_vix": Decimal(26), "india_vix_change": Decimal("0.2"), "fii_net_crore": Decimal(-4000)})
    assert score_of(item) == Decimal("0.90") - Decimal("0.35") - Decimal("0.15") - Decimal("0.15")
    assert item.stance.value == "BUY"  # 0.25: still a buy, no longer a strong one
    calm = evidence({})
    assert calm.stance.value == "STRONG_BUY"


def test_metal_etfs_ignore_the_equity_tape():
    stressed = evidence({"india_vix": Decimal(30), "fii_net_crore": Decimal(-5000)}, AssetClass.ETF)
    calm = evidence({}, AssetClass.ETF)
    assert score_of(stressed) == score_of(calm)
    assert stressed.rationale[0] == "india_valuation_balance_sheet_margin_and_news"


# --- Wiring: daemon, inspector, manifest ---------------------------------------------------


def configure(monkeypatch, vix="yahoo", flows="nse", fred=True):
    monkeypatch.setenv("PRAMANA_NEWS_RSS_URLS", "")
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.delenv("PRAMANA_NEWS_SYMBOL_ALIASES_JSON", raising=False)
    if fred:
        monkeypatch.setenv("FRED_API_KEY", "fake-fred-key")
    else:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setenv("PRAMANA_INDIA_VIX_PROVIDER", vix)
    monkeypatch.setenv("PRAMANA_INDIA_FLOWS_PROVIDER", flows)


def test_daemon_builds_one_composite_with_a_client_per_part(monkeypatch):
    configure(monkeypatch)
    _, _, macro = _env_intelligence_providers()
    (composite,) = macro.registry._providers[ProviderCategory.MACRO]
    assert isinstance(composite, CompositeMacroProvider)
    assert [type(part) for part in composite.parts] == [FredMacroProvider, YahooIndiaVixProvider, NseInstitutionalFlowsProvider]
    clients = [part.client for part in composite.parts]
    assert len({id(c) for c in clients}) == 3
    assert composite.parts[2].client.max_payload_bytes == 4_000_000


def test_daemon_defaults_leave_india_parts_out_and_refuse_unknown_sources(monkeypatch):
    configure(monkeypatch, vix="none", flows="none")
    _, _, macro = _env_intelligence_providers()
    (composite,) = macro.registry._providers[ProviderCategory.MACRO]
    assert [type(part) for part in composite.parts] == [FredMacroProvider]
    configure(monkeypatch, vix="none", flows="none", fred=False)
    _, _, macro = _env_intelligence_providers()
    assert ProviderCategory.MACRO not in macro.registry._providers
    configure(monkeypatch, vix="bloomberg")
    with pytest.raises(RuntimeError, match="PRAMANA_INDIA_VIX_PROVIDER"):
        _env_intelligence_providers()
    configure(monkeypatch, flows="sebi")
    with pytest.raises(RuntimeError, match="PRAMANA_INDIA_FLOWS_PROVIDER"):
        _env_intelligence_providers()


def test_inspector_reports_the_parts_and_refuses_a_foreign_one(monkeypatch):
    configure(monkeypatch)
    report = inspect_intelligence_configuration(clock=lambda: NOW)
    assert report["inputs"]["macro"]["adapter"] == "composite"
    assert report["inputs"]["macro"]["parts"] == ["fred", "yahoo_india_vix", "nse_fii_dii"]
    assert report["provider_fetch_performed"] is False
    from quant_ai import daemon

    news, fundamentals, macro = _env_intelligence_providers()
    (composite,) = macro.registry._providers[ProviderCategory.MACRO]
    composite.parts = composite.parts + (StubPart("rogue", ("INDIA_VIX",)),)
    monkeypatch.setattr(daemon, "_env_intelligence_providers", lambda: (news, fundamentals, macro))
    with pytest.raises(ValueError, match="intelligence_inputs_unexpected_macro_part"):
        inspect_intelligence_configuration(clock=lambda: NOW)


def test_manifest_describes_the_composite_and_its_parts_without_issues(monkeypatch):
    configure(monkeypatch)
    _, _, macro = _env_intelligence_providers()
    issues: list[str] = []
    described = describe(macro, issues)
    assert issues == []
    (composite,) = described["providers"][ProviderCategory.MACRO.value]
    assert composite["type"].endswith("CompositeMacroProvider")
    parts = composite["parts"]
    assert [part["type"].rsplit(".", 1)[1] for part in parts] == ["FredMacroProvider", "YahooIndiaVixProvider", "NseInstitutionalFlowsProvider"]
    assert parts[1]["parameters"]["serves"] == ["INDIA_VIX", "INDIA_VIX_PREV_CLOSE"]
    assert parts[2]["parameters"]["cache_ttl"] == 1800.0
    assert "endpoint_sha256" in parts[2] and "nseindia" not in json.dumps(parts[2]["endpoint_sha256"])


def test_indicator_sets_are_disjoint_and_complete():
    assert set(MACRO_CORE_INDICATORS).isdisjoint(INDIA_MACRO_INDICATORS)
    assert MACRO_INDICATORS == MACRO_CORE_INDICATORS + INDIA_MACRO_INDICATORS
    assert set(YahooIndiaVixProvider.serves) | set(NseInstitutionalFlowsProvider.serves) == set(INDIA_MACRO_INDICATORS)
