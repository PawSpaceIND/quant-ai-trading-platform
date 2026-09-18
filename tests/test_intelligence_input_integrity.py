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
                     "IRLTLT01INM156N": [{"date": "2026-08-01", "value": "6"}]})
    result = item.fetch(("US10Y", "INDIA10Y"), NOW)
    assert result.indicators == {"US10Y": Decimal(4), "INDIA10Y": Decimal(6)}
    assert result.observed_at == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert FreshnessValidator().validate(DataCategory.MACRO, result.observed_at, NOW).state == FreshnessState.STALE


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_fred_nonfinite_observation_refuses(value):
    item = provider({"DGS10": [{"date": "2026-09-18", "value": value}]})
    with pytest.raises(ValueError, match="fred_nonfinite_observation"):
        item.fetch(("US10Y",), NOW)


def test_fred_future_observation_cannot_hide_behind_oldest_time():
    item = provider({"DGS10": [{"date": "2026-09-19", "value": "4"}],
                     "IRLTLT01INM156N": [{"date": "2026-08-01", "value": "6"}]})
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
    assert macro.fetch(("US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"), NOW).indicators == {}


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
