"""Five-name pilot acceptance with synthetic bars; never calls a broker or paid model."""
from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_portfolio_risk_controls import ALTERNATING, book, buy, closes, history, plan

from quant_ai import daemon
from quant_ai.agents.traded_runtime import runtime_configuration
from quant_ai.domain.models import OrderIntent, Side
from quant_ai.governance.directives import FounderDirectives
from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import DailyHistoryProvider
from quant_ai.risk.book_history import DailyCloseHistory, normalize_sector_map, sector_map_from_env
from quant_ai.risk.policy import BookRiskFirewall

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)
DIRECTIVES = FounderDirectives.from_json(json.loads((ROOT / "deploy/founder-directives.example.json").read_text()))
SECTORS = json.loads((ROOT / "deploy/pilot-sector-map.example.json").read_text())
SYMBOLS = tuple(item.symbol for item in DIRECTIVES.watchlist)
GATES = {"sector_concentration", "correlation_adjusted_gross", "book_expected_shortfall"}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Item 1 now validates profile before Item 2's boot checks. Keep that real
    # validation active, but supply an offline SDK boundary for these synthetic tests.
    from quant_ai.operations import zerodha_renewal
    class Profile:
        def set_access_token(self, token):
            assert token == "test"
        def profile(self):
            return {"user_id": "FIXTURE"}
    monkeypatch.setattr(zerodha_renewal, "_kite_client", lambda _key: Profile())
    monkeypatch.delenv("PRAMANA_ZERODHA_TOKEN_ISSUED_AT", raising=False)
    monkeypatch.delenv("PRAMANA_ZERODHA_USER_ID", raising=False)
    for name in ("PRAMANA_SECTOR_MAP_JSON", "PRAMANA_SECTOR_MAP_FILE", "PRAMANA_REQUIRE_BOOK_RISK_GATES",
                 "PRAMANA_BOOK_RISK_HISTORY", "PRAMANA_FOUNDER_DIRECTIVES_JSON", "PRAMANA_FOUNDER_DIRECTIVES_FILE"):
        monkeypatch.delenv(name, raising=False)


def bars(instrument, count=120, end=NOW - timedelta(days=1), multipliers=None):
    dates = []
    day = end.replace(hour=3, minute=45)
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day)
        day -= timedelta(days=1)
    dates.reverse()
    prices = closes(multipliers or [Decimal("1.001"), Decimal("0.999")] * 60)
    return tuple(Candle(instrument, timestamp, price, price, price, price, Decimal(1000))
                 for timestamp, price in zip(dates, prices))


class Feed:
    def __init__(self, *, missing=(), count=120):
        self.missing = missing
        self.count = count
        self.calls = []

    def fetch_ohlcv(self, instrument, start, now, interval):
        self.calls.append(instrument.symbol)
        return () if instrument.symbol in self.missing else bars(instrument, self.count)


def daily(feed=None):
    return DailyHistoryProvider(ResilientHttpClient(UrllibTransport()), feed=feed or Feed())


def runner(tmp_path, provider, sectors=SECTORS, required=True):
    return daemon.build_ghost_runner(
        zerodha_api_key="test", zerodha_access_token="test", zerodha_instrument_tokens=tuple(range(1,6)),
        zerodha_symbol_by_token=dict(enumerate(SYMBOLS, 1)), ib_client=SimpleNamespace(), ib_contracts=(),
        include_ibkr=False, database=tmp_path / "paper.db", tenant_id="pilot",
        log_path=tmp_path / "events.jsonl", xai_directory=tmp_path / "proofs", halt_file=tmp_path / "HALT",
        directives=replace(DIRECTIVES, sector_map=sectors), history_provider=provider,
        book_risk_history=provider, require_book_risk_gates=required, pilot_mode=True)


def gates(runner):
    runner.daemon.clock = lambda: NOW
    runner.daemon.protection_tick(NOW)
    payload = json.loads(runner.daemon.tracker.broker._connection.execute(
        "SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])
    return {row["id"]: row for row in payload["riskGates"]["gates"] if row["id"] in GATES}


def test_committed_example_arms_runtime_and_retains_five_name_scope(tmp_path):
    provider = daily()
    actual = runner(tmp_path, provider)
    assert set(SECTORS) == set(SYMBOLS) and len(SYMBOLS) == 5
    assert SECTORS["INFY"] == SECTORS["TCS"] == "IT_SERVICES"
    assert SECTORS["GOLDBEES"] == SECTORS["SILVERBEES"] == "PRECIOUS_METALS"
    for instrument in DIRECTIVES.watchlist:
        provider.fetch(instrument, NOW)  # same cache the regime pipeline populates
    published = gates(actual)
    assert all(row["armed"] is True and row["required"] is True for row in published.values())
    assert published["sector_concentration"]["records"] == 5
    assert published["sector_concentration"]["groups"] == 3
    for key in GATES - {"sector_concentration"}:
        assert published[key]["dataReady"] is True
        assert published[key]["records"] == 600
        assert published[key]["alignedIntervals"] == 119
    assert len(provider.feed.calls) == 5, "Protection telemetry made an extra data request"
    assert published["sector_concentration"]["limit"] == 0.25
    assert published["correlation_adjusted_gross"]["limit"] == 0.45
    assert published["book_expected_shortfall"]["limit"] == 0.03


@pytest.mark.parametrize("missing", ["history", "map", "one_symbol"])
def test_missing_required_configuration_refuses_startup(tmp_path, missing):
    provider = None if missing == "history" else daily()
    sectors = {} if missing == "map" else {k:v for k,v in SECTORS.items() if missing != "one_symbol" or k != "TCS"}
    with pytest.raises(ValueError, match="pilot_risk_gates_unarmed"):
        runner(tmp_path, provider, sectors)


@pytest.mark.parametrize("missing", ["history", "map", "one_symbol"])
def test_post_boot_input_loss_is_unarmed_and_refuses_entry_not_an_exit(tmp_path, missing):
    actual = runner(tmp_path, daily())
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    if missing == "history": risk.history_provider = None
    elif missing == "map": risk.sector_map = {}
    else: risk.sector_map.pop("TCS")
    observed = gates(actual)
    assert any(row["armed"] is False for row in observed.values())
    order = OrderIntent("INFY", DIRECTIVES.watchlist[0].market, Side.BUY, 1, Decimal(100), "synthetic")
    assert not risk.evaluate(order, book({})).approved
    # Existing Warden exit exemption remains independent of missing data and mappings.
    guard = actual.daemon.scheduler.pipeline.runtime.warden
    proposal = replace(buy("INFY", 1), side=Side.SELL)
    assert guard.evaluate(proposal, plan(), book({"INFY": Decimal(100)}, symbol_quantity={"INFY":1})).approved


@pytest.mark.parametrize("count,missing", [(120, ("TCS",)), (61, ()), (100, ())])
def test_provider_object_is_not_proof_of_sufficient_records(tmp_path, count, missing):
    provider = daily(Feed(missing=missing, count=count))
    actual = runner(tmp_path, provider)
    for instrument in DIRECTIVES.watchlist: provider.fetch(instrument, NOW)
    published = gates(actual)
    assert published["book_expected_shortfall"]["armed"] is True
    assert published["book_expected_shortfall"]["dataReady"] is False
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    symbol = "TCS" if missing else "INFY"
    order = OrderIntent(symbol, DIRECTIVES.watchlist[0].market, Side.BUY, 1, Decimal(100), "synthetic")
    assert not risk.evaluate(order, book({})).approved


def test_cold_or_expired_cache_is_not_green_and_telemetry_never_fetches(tmp_path):
    provider = daily()
    actual = runner(tmp_path, provider)
    assert not gates(actual)["book_expected_shortfall"]["dataReady"]
    assert provider.feed.calls == []
    for instrument in DIRECTIVES.watchlist: provider.fetch(instrument, NOW - timedelta(days=1))
    assert not gates(actual)["book_expected_shortfall"]["dataReady"]
    assert len(provider.feed.calls) == 5


@pytest.mark.parametrize("bad", ["stale", "future", "naive", "wrong_instrument", "duplicate", "unclosed"])
def test_invalid_daily_evidence_refuses_instead_of_manufacturing_records(bad):
    instrument = DIRECTIVES.watchlist[0]
    values = list(bars(instrument))
    if bad == "stale": values = list(bars(instrument, end=NOW-timedelta(days=30)))
    elif bad == "future": values[-1] = replace(values[-1], timestamp=NOW+timedelta(days=1))
    elif bad == "naive": values[-1] = replace(values[-1], timestamp=NOW.replace(tzinfo=None))
    elif bad == "wrong_instrument": values[-1] = replace(values[-1], instrument=DIRECTIVES.watchlist[1])
    elif bad == "duplicate": values.append(values[-1])
    else: values[-1] = replace(values[-1], timestamp=NOW.replace(hour=3, minute=45))
    provider = SimpleNamespace(fetch=lambda *_args: tuple(values))
    adapted = DailyCloseHistory(provider, (instrument,), clock=lambda: NOW, max_age=timedelta(days=7))
    with pytest.raises(ValueError, match="book_history"):
        adapted((instrument.symbol,))
    assert not adapted.readiness((instrument.symbol,), NOW)["dataReady"]


def test_concentrated_correlated_and_tail_books_are_really_refused():
    values = closes(ALTERNATING)
    source = history(**{symbol:values for symbol in SYMBOLS})
    risk = BookRiskFirewall(history_provider=source, sector_map=SECTORS, required_symbols=SYMBOLS)
    market = DIRECTIVES.watchlist[0].market
    def order(symbol, quantity): return OrderIntent(symbol, market, Side.BUY, quantity, Decimal(100), "synthetic")
    sector = risk.evaluate(order("TCS", 110), book({"INFY":Decimal(15000)}))
    assert sector.reason == "sector_concentration_limit:IT_SERVICES"
    corr = risk.evaluate(order("GOLDBEES", 10), book({"INFY":Decimal(10000),"TCS":Decimal(10000),
                   "RELIANCE":Decimal(15000),"GOLDBEES":Decimal(14000)}))
    assert corr.reason == "correlation_adjusted_gross_limit"
    tail = risk.evaluate(order("RELIANCE", 200), book({}))
    assert tail.reason == "book_expected_shortfall_limit"
    assert not sector.approved and not corr.approved and not tail.approved


def test_unmapped_held_position_is_not_dropped_from_required_group_checks():
    risk = BookRiskFirewall(history_provider=lambda _: {}, sector_map=SECTORS, required_symbols=SYMBOLS)
    decision = risk.evaluate(OrderIntent("INFY", DIRECTIVES.watchlist[0].market, Side.BUY, 1,
                            Decimal(100), "synthetic"), book({"UNKNOWN":Decimal(1000)}))
    assert not decision.approved and "required_sector_mapping_missing:UNKNOWN" in decision.reason


@pytest.mark.parametrize("value", [[], {"INFY":None}, {"INFY":12}, {"INFY":[]}, {"INFY":"IT", " infy ":"OTHER"}])
def test_bad_sector_mappings_never_become_armed_strings(value):
    with pytest.raises((ValueError, TypeError)):
        normalize_sector_map(value)


@pytest.mark.parametrize("value", ['{"INFY":"IT","INFY":"OTHER"}', '{"INFY":null}', '[]'])
def test_env_mapping_rejects_ambiguous_or_nonstring_records(monkeypatch, value):
    monkeypatch.setenv("PRAMANA_SECTOR_MAP_JSON", value)
    with pytest.raises((ValueError, TypeError)):
        sector_map_from_env()


def test_compose_passes_actual_settings_and_mounts_the_committed_map():
    config = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())
    ghost = config["services"]["pramana-ghost"]
    env = ghost["environment"]
    assert env["PRAMANA_REQUIRE_BOOK_RISK_GATES"] == "true"
    assert env["PRAMANA_BOOK_RISK_HISTORY"] == "${PRAMANA_BOOK_RISK_HISTORY:-daily}"
    assert env["PRAMANA_SECTOR_MAP_JSON"] == "${PRAMANA_SECTOR_MAP_JSON:-}"
    mount = next(row for row in ghost["volumes"] if isinstance(row,dict) and row.get("target")==env["PRAMANA_SECTOR_MAP_FILE"])
    assert mount["read_only"] is True and mount["bind"]["create_host_path"] is False
    assert mount["source"] == "${PRAMANA_SECTOR_MAP_HOST_FILE:-./pilot-sector-map.example.json}"
    assert env["TRADING_LIVE_MONEY_ACTIVE"] == "false"


@pytest.mark.parametrize("value,expected", [("daily",True), ("none",False), ("",False)])
def test_real_env_provider_wiring_arms_only_on_daily(monkeypatch,value,expected):
    provider = daily()
    monkeypatch.setenv("PRAMANA_BOOK_RISK_HISTORY",value)
    result = daemon._env_book_risk_history_provider(provider)
    assert (result is provider) is expected
    if not expected: assert result is None


@pytest.mark.parametrize("value", ["yes", "", "falsee"])
def test_malformed_required_flag_cannot_silently_disable_pilot(monkeypatch,value):
    monkeypatch.setenv("PRAMANA_REQUIRE_BOOK_RISK_GATES",value)
    with pytest.raises(ValueError,match="invalid_required"):
        daemon._env_required_book_risk()


def test_manifest_distinguishes_scope_group_and_freshness_changes(tmp_path):
    actual = runner(tmp_path, daily())
    runtime = actual.daemon.scheduler.pipeline.runtime
    first = runtime_configuration(runtime)
    runtime.warden.book_risk.sector_map["TCS"] = "DIFFERENT"
    second = runtime_configuration(runtime)
    assert first != second
    runtime.warden.book_risk.required_symbols = ()
    third = runtime_configuration(runtime)
    assert second != third
    runtime.warden.book_risk.history_provider.max_age = timedelta(days=20)
    assert third != runtime_configuration(runtime)


def test_preflight_never_needs_credentials_or_an_order(tmp_path):
    spec = importlib.util.spec_from_file_location("risk_preflight",ROOT / "scripts/check_pilot_risk_gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.check(DIRECTIVES,SECTORS,daily(),NOW)
    assert report["allArmed"] and report["dataReady"]
    assert report["history"]["records"] == 600 and report["hostAcceptance"] is False
    assert module.main([]) == 2


@pytest.mark.parametrize("absent", [None, "history", "sectors"])
def test_environment_builder_enforces_actual_pilot_inputs(tmp_path, monkeypatch, absent):
    provider = daily()
    settings = {
        "TRADING_LIVE_MONEY_ACTIVE": "false", "ANTHROPIC_API_KEY": "test",
        "ZERODHA_API_KEY": "test", "ZERODHA_ACCESS_TOKEN": "test",
        "PRAMANA_ZERODHA_TOKENS_JSON": "[1,2,3,4,5]",
        "PRAMANA_ZERODHA_SYMBOLS_JSON": json.dumps(dict(enumerate(SYMBOLS,1))),
        "PRAMANA_TARGET_SYMBOL": "INFY", "PRAMANA_TARGET_MARKET": "INDIA",
        "PRAMANA_TARGET_ASSET_CLASS": "EQUITY", "PRAMANA_TARGET_CURRENCY": "INR",
        "PRAMANA_TARGET_EXCHANGE": "NSE", "PRAMANA_IBKR_ENABLED": "false",
        "PRAMANA_PILOT_MODE": "true", "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_PAPER_DB": str(tmp_path / "paper.db"),
        "PRAMANA_LEDGER_PATH": str(tmp_path / "paper.db"),
        "PRAMANA_GHOST_LOG": str(tmp_path / "events.jsonl"),
        "PRAMANA_PROOF_DIR": str(tmp_path / "proofs"),
        "PRAMANA_HALT_FILE": str(tmp_path / "HALT"),
        "PRAMANA_FOUNDER_DIRECTIVES_FILE": str(ROOT / "deploy/founder-directives.example.json"),
    }
    for key, value in settings.items(): monkeypatch.setenv(key,value)
    if absent != "history": monkeypatch.setenv("PRAMANA_BOOK_RISK_HISTORY", "daily")
    if absent != "sectors":
        monkeypatch.setenv("PRAMANA_SECTOR_MAP_FILE",str(ROOT / "deploy/pilot-sector-map.example.json"))
    monkeypatch.setattr(daemon, "AnthropicSwarmClient", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(daemon, "import_module", lambda _name: SimpleNamespace(IB=SimpleNamespace))
    monkeypatch.setattr(daemon, "_env_daily_history_provider", lambda: provider)
    monkeypatch.setattr(daemon, "_env_intelligence_providers", lambda: (None,None,None))
    if absent:
        with pytest.raises(ValueError, match="pilot_risk_gates_unarmed"):
            daemon.build_ghost_runner_from_env()
    else:
        actual = daemon.build_ghost_runner_from_env()
        risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
        assert risk.required_symbols == tuple(sorted(SYMBOLS))
        assert risk.sector_map == SECTORS
        assert risk.history_provider.provider is provider
        assert risk.history_provider.max_age == timedelta(days=7)
        assert risk.configuration_problem() is None
