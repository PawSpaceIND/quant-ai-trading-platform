"""Offline intelligence configuration and conservative macro-clock regressions."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.daemon import _env_intelligence_providers
from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.freshness import DataCategory, FreshnessState, FreshnessValidator
from quant_ai.intelligence.resilience import HttpResponse, ResilientHttpClient

NOW = datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc)


class RecordedTransport:
    def __init__(self, observations):
        self.observations = observations
        self.calls = []

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.calls.append(params["series_id"])
        return HttpResponse(200, json.dumps({"observations": self.observations[params["series_id"]]}).encode(), {})


def provider(observations):
    transport = RecordedTransport(observations)
    return FredMacroProvider(ResilientHttpClient(transport, max_attempts=1), "unit-test-not-a-key")


def test_fred_mixed_age_cannot_refresh_an_older_indicator():
    item = provider({"DGS10": [{"date": "2026-09-18", "value": "4"}],
                     "INDIRLTLT01STM": [{"date": "2026-08-01", "value": "6"}]})
    result = item.fetch(("US10Y", "INDIA10Y"), NOW)
    assert result.indicators == {"US10Y": Decimal(4), "INDIA10Y": Decimal(6)}
    assert result.observed_at == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert result.freshness_observed_at == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert FreshnessValidator().validate(DataCategory.MACRO, result.freshness_observed_at, NOW).state == FreshnessState.STALE


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_fred_nonfinite_observation_refuses(value):
    item = provider({"DGS10": [{"date": "2026-09-18", "value": value}]})
    with pytest.raises(ValueError, match="fred_nonfinite_observation"):
        item.fetch(("US10Y",), NOW)


def test_fred_future_observation_cannot_hide_behind_oldest_time():
    item = provider({"DGS10": [{"date": "2026-09-19", "value": "4"}],
                     "INDIRLTLT01STM": [{"date": "2026-08-01", "value": "6"}]})
    with pytest.raises(ValueError, match="fred_future_observation"):
        item.fetch(("US10Y", "INDIA10Y"), NOW)


def test_fred_clock_must_be_aware_before_transport():
    item = provider({"DGS10": [{"date": "2026-09-18", "value": "4"}]})
    with pytest.raises(ValueError, match="fred_clock_must_be_aware"):
        item.fetch(("US10Y",), NOW.replace(tzinfo=None))


@pytest.mark.parametrize("stamp", ["2026-09-18T00:00:00+05:30", "not-a-date", "20260918"])
def test_fred_observation_date_is_not_silently_reinterpreted(stamp):
    item = provider({"DGS10": [{"date": stamp, "value": "4"}]})
    with pytest.raises(ValueError, match="fred_observation_date_invalid"):
        item.fetch(("US10Y",), NOW)


def test_fred_empty_and_unknown_series_stay_empty():
    item = provider({"DGS10": [{"date": "2026-09-18", "value": "."}]})
    result = item.fetch(("US10Y", "UNKNOWN"), NOW)
    assert result.indicators == {}
    assert result.observed_at == NOW


@pytest.fixture
def clean_config(monkeypatch):
    for key in ("PRAMANA_NEWS_RSS_URLS", "FRED_API_KEY", "PRAMANA_NEWS_SYMBOL_ALIASES_JSON",
                "PRAMANA_FOUNDER_DIRECTIVES_FILE", "PRAMANA_FOUNDER_DIRECTIVES_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    return monkeypatch


def test_missing_production_inputs_are_empty_not_sandbox(clean_config):
    news, fundamentals, macro = _env_intelligence_providers()
    assert news.fetch("INFY", NOW) == ()
    assert fundamentals.fetch("INFY", NOW).metrics == {}
    assert macro.fetch(("US10Y", "INDIA10Y", "BRENT", "GOLD", "USD_BROAD"), NOW).indicators == {}


@pytest.mark.parametrize("rss,fred,yahoo", [(a,b,c) for a in (False,True) for b in (False,True) for c in (False,True)])
def test_actual_factory_selection_is_configuration_not_fresh_data(clean_config,rss,fred,yahoo):
    from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration
    clean_config.setenv("PRAMANA_NEWS_RSS_URLS", "https://news.invalid/rss" if rss else "")
    clean_config.setenv("FRED_API_KEY", "unit-test-not-a-key" if fred else "")
    clean_config.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "yahoo" if yahoo else "none")
    clean_config.setenv("PRAMANA_TARGET_MARKET", "INDIA")
    result = inspect_intelligence_configuration(clock=lambda: NOW)
    expected = {"news":rss,"macro":fred,"fundamentals":yahoo}
    for key, configured in expected.items():
        assert result["inputs"][key]["selection"] == ("configured_unverified" if configured else "not_configured")
        assert result["inputs"][key]["freshness"] == "not_probed"
        assert result["inputs"][key]["data_availability"] == "not_probed"
    assert result["checked_at"] == NOW.isoformat()
    assert result["scope"] == "current_process_environment_factory_preview"
    assert result["provider_fetch_performed"] is False
    assert result["running_process_verified"] is False
    assert result["source_authenticity_verified"] is False
    assert result["trading_authorized"] is False
    assert len(result["limitations"]) >= 4


def test_config_inspector_does_not_fetch_or_echo_endpoints_and_keys(clean_config):
    from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
    from quant_ai.intelligence.external.yahoo_fundamentals import YahooFundamentalsProvider
    from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration
    secret = "DO_NOT_ECHO_PRIVATE_VALUE"
    clean_config.setenv("PRAMANA_NEWS_RSS_URLS", "https://news.invalid/"+secret)
    clean_config.setenv("FRED_API_KEY", secret)
    clean_config.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "yahoo")
    clean_config.setenv("PRAMANA_TARGET_MARKET", "INDIA")
    called = []
    def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("inspection must not fetch")
    for cls in (RssNewsSentimentAdapter,FredMacroProvider,YahooFundamentalsProvider):
        clean_config.setattr(cls,"fetch",forbidden)
    result = inspect_intelligence_configuration(clock=lambda:NOW)
    assert not called
    assert secret not in json.dumps(result)
    assert "https://" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["provider_graph","unexpected_wrapper","registry_binding","adapter_count","unexpected_adapter","unexpected_category"])
def test_unrecognized_provider_graph_is_not_certified(clean_config,kind):
    from quant_ai import daemon
    from quant_ai.intelligence.failover import ProviderCategory, ProviderFailoverRegistry
    from quant_ai.intelligence.sandbox import SandboxNewsSentimentProvider
    from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration
    selected = _env_intelligence_providers()
    registry = selected[0].registry
    if kind == "provider_graph":
        selected = list(selected)
    elif kind == "unexpected_wrapper":
        from types import SimpleNamespace
        selected = (SimpleNamespace(registry=registry), *selected[1:])
    elif kind == "registry_binding":
        selected[1].registry = ProviderFailoverRegistry()
    elif kind == "adapter_count":
        from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
        adapter=RssNewsSentimentAdapter(ResilientHttpClient(RecordedTransport({})), ("https://news.invalid/rss",))
        registry._providers[ProviderCategory.NEWS] = (adapter,adapter)
    elif kind == "unexpected_adapter":
        registry.register(ProviderCategory.NEWS, SandboxNewsSentimentProvider())
    else:
        registry.register(ProviderCategory.PRICE, object())
    clean_config.setattr(daemon,"_env_intelligence_providers",lambda:selected)
    with pytest.raises(ValueError,match="intelligence_inputs_"+kind):
        inspect_intelligence_configuration(clock=lambda:NOW)


@pytest.mark.parametrize("moment",[None,1,NOW.replace(tzinfo=None)])
def test_configuration_clock_refuses_before_factory(clean_config,moment):
    from quant_ai import daemon
    from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration
    def unexpected():
        raise AssertionError("factory called before clock check")
    clean_config.setattr(daemon,"_env_intelligence_providers",unexpected)
    with pytest.raises(ValueError,match="intelligence_inputs_aware_clock"):
        inspect_intelligence_configuration(clock=lambda:moment)


def test_configuration_live_mode_refuses_before_factory(clean_config):
    from quant_ai import daemon
    from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration
    clean_config.setenv("TRADING_LIVE_MONEY_ACTIVE","true")
    called=[]
    clean_config.setattr(daemon,"_env_intelligence_providers",lambda:called.append(True))
    with pytest.raises(ValueError,match="intelligence_inputs_paper_only"):
        inspect_intelligence_configuration(clock=lambda:NOW)
    assert not called


def test_actual_failover_on_provider_errors_remains_missing(clean_config):
    from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
    from quant_ai.intelligence.external.yahoo_fundamentals import YahooFundamentalsProvider
    clean_config.setenv("PRAMANA_NEWS_RSS_URLS","https://news.invalid/rss")
    clean_config.setenv("FRED_API_KEY","unit-test-not-a-key")
    clean_config.setenv("PRAMANA_FUNDAMENTALS_PROVIDER","yahoo")
    clean_config.setenv("PRAMANA_TARGET_MARKET","INDIA")
    def unavailable(*args,**kwargs):
        raise ValueError("private-provider-diagnostic")
    for cls in (RssNewsSentimentAdapter,FredMacroProvider,YahooFundamentalsProvider):
        clean_config.setattr(cls,"fetch",unavailable)
    news,fundamentals,macro=_env_intelligence_providers()
    assert news.fetch("INFY",NOW)==()
    assert fundamentals.fetch("INFY",NOW).metrics=={}
    assert macro.fetch(("US10Y",),NOW).indicators=={}


@pytest.mark.parametrize("mode",["valid","live","bad_config","bad_argument"])
def test_real_cli_redacts_and_never_claims_live_acceptance(tmp_path,mode):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    sentinel="do_not_echo_private_value"
    env={"PATH":os.environ.get("PATH", ""),"HOME":str(tmp_path),
         "PYTHONDONTWRITEBYTECODE":"1","PYTHONPATH":str(root/"src"),
         "TRADING_LIVE_MONEY_ACTIVE":"true" if mode=="live" else "false",
         "PRAMANA_FUNDAMENTALS_PROVIDER":sentinel if mode=="bad_config" else "none"}
    args=[sys.executable,"-B",str(root/"scripts/inspect_intelligence_inputs.py")]
    if mode=="bad_argument":
        args.append(sentinel)
    result=subprocess.run(args,cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20,check=False)
    assert sentinel not in result.stdout+result.stderr
    if mode=="valid":
        assert result.returncode==0,result.stderr
        report=json.loads(result.stdout)
        assert report["mode"]=="CONFIGURATION_ONLY"
        assert report["running_process_verified"] is False
        assert all(v["selection"]=="not_configured" for v in report["inputs"].values())
    else:
        assert result.returncode==2
        assert result.stdout==""
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize("value", ["garbled", "", "1,000"])
def test_invalid_numeric_macro_record_refuses_as_provider_failure(value):
    from quant_ai.intelligence.failover import (
        FailoverMacroProvider,
        ProviderCategory,
        ProviderFailoverRegistry,
    )
    item=provider({"DGS10": [{"date": "2026-09-18", "value": value}]})
    error = None
    try:
        item.fetch(("US10Y",),NOW)
    except (ValueError, ArithmeticError) as caught:
        error = caught
    assert type(error) is ValueError
    assert str(error) == "fred_invalid_observation_value"
    registry=ProviderFailoverRegistry()
    registry.register(ProviderCategory.MACRO,item)
    assert FailoverMacroProvider(registry).fetch(("US10Y",),NOW).indicators=={}


def test_macro_changes_keep_latest_clock_when_oldest_series_does_not_move():
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
    pipeline=object.__new__(SwarmMarketAnalysisPipeline)
    pipeline._macro_current_at=None
    pipeline._macro_current={}
    pipeline._macro_previous={}
    first=provider({"DGS10":[{"date":"2026-09-17","value":"4"}],
                    "INDIRLTLT01STM":[{"date":"2026-08-01","value":"6"}]}).fetch(("US10Y","INDIA10Y"),NOW)
    second=provider({"DGS10":[{"date":"2026-09-18","value":"5"}],
                     "INDIRLTLT01STM":[{"date":"2026-08-01","value":"6"}]}).fetch(("US10Y","INDIA10Y"),NOW)
    assert pipeline._macro_metrics(first)["yield_change"]==0
    assert pipeline._macro_metrics(second)["yield_change"]==Decimal("0.25")


@pytest.mark.parametrize('kind', ['naive', 'type', 'order', 'both_naive'])
def test_macro_snapshot_clock_contract_rejects_misleading_metadata(kind):
    from datetime import timedelta

    from quant_ai.intelligence.providers import MacroSnapshot
    latest = NOW.replace(tzinfo=None) if kind == 'both_naive' else NOW
    oldest = NOW.replace(tzinfo=None) if kind in ('naive','both_naive') else 'bad' if kind == 'type' else NOW + timedelta(seconds=1)
    reason = 'macro_snapshot_oldest_after_latest' if kind == 'order' else 'macro_snapshot_clock_invalid'
    with pytest.raises(ValueError, match=reason):
        MacroSnapshot({'US10Y': Decimal(4)}, latest, oldest)


def test_legacy_macro_snapshot_keeps_its_existing_clock():
    from quant_ai.intelligence.providers import MacroSnapshot
    snapshot = MacroSnapshot({'US10Y': Decimal(4)}, NOW)
    assert snapshot.oldest_observed_at is None
    assert snapshot.freshness_observed_at == snapshot.observed_at == NOW


def complete_mixed_macro():
    # BRENT carries the deliberately old observation. It must be an indicator the pipeline
    # actually requests, or the oldest-clock property below is never exercised: INDIA10Y
    # used to play this role and is no longer asked for.
    return provider({series: [{'date': '2026-08-01' if name == 'BRENT' else '2026-09-18',
                              'value': '6' if name == 'BRENT' else '4'}]
                     for name, series in FredMacroProvider.series.items()})


@pytest.mark.parametrize('asynchronous', [False, True])
def test_real_pipeline_uses_oldest_freshness_not_latest_change_clock(tmp_path, asynchronous):
    import asyncio

    from test_intelligence_pipeline import instrument, plan, portfolio

    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
    from quant_ai.intelligence.sandbox import (
        SandboxFundamentalDataProvider,
        SandboxNewsSentimentProvider,
    )
    from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
    broker = PaperBrokerService(tmp_path/'clock.db', starting_capital=Decimal(100000))
    pipeline = SwarmMarketAnalysisPipeline(UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), complete_mixed_macro(),
        runtime=SwarmPaperTradingService(broker=broker))
    args = (instrument(), NOW, plan(), portfolio())
    kwargs = {'quantity': 0, 'country': 'USA', 'tenant_id': 'clock-test'}
    result = asyncio.run(pipeline.run_async(*args, **kwargs)) if asynchronous else pipeline.run(*args, **kwargs)
    assert result.freshness.macro.state == FreshnessState.STALE
    assert result.freshness.macro.age_seconds == int((NOW-datetime(2026,8,1,tzinfo=timezone.utc)).total_seconds())
    cached, freshness_time = pipeline.cache.get('macro:core')
    assert freshness_time == cached.oldest_observed_at
    assert cached.observed_at == datetime(2026,9,18,tzinfo=timezone.utc)
    assert result.execution.fill is None


def test_macro_evidence_preserves_latest_time_and_names_oldest_separately():
    from quant_ai.intelligence.pipeline import PipelineFreshness, SwarmMarketAnalysisPipeline
    from quant_ai.intelligence.providers import FundamentalSnapshot
    macro=complete_mixed_macro().fetch(tuple(FredMacroProvider.series),NOW)
    validator=FreshnessValidator()
    fresh=validator.validate(DataCategory.PRICE,NOW,NOW)
    stale=validator.validate(DataCategory.MACRO,macro.freshness_observed_at,NOW)
    context=SwarmMarketAnalysisPipeline._evidence_context((),{},(),macro,
        FundamentalSnapshot('TEST',{},NOW),PipelineFreshness(fresh,fresh,stale,fresh))
    fields=dict(context.freshness)
    assert context.macro_observed_at==macro.observed_at.isoformat()
    assert 'macro_oldest_observed_at' in fields
    assert fields['macro_oldest_observed_at']==macro.oldest_observed_at.isoformat()
    assert fields['macro'].startswith('STALE(')
