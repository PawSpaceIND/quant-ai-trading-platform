"""The pre-market check reads the live engine record and names every condition that can stop a session."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_ai.operations.premarket import load_engine_records, premarket_checks, render

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
# Monday 21 September 2026, 08:50 IST: when the runbook runs this.
NOW = datetime(2026, 9, 21, 8, 50, tzinfo=IST)
SYMBOLS = ["TRENT", "BEL", "GOLDBEES"]
TOKENS = {"1": "TRENT", "2": "BEL", "3": "GOLDBEES"}
CALENDAR = ROOT / "deploy" / "event-calendar.example.json"


def env(**overrides) -> dict[str, str]:
    base = {
        "ZERODHA_ACCESS_TOKEN": "not-a-real-token",
        "PRAMANA_ZERODHA_TOKEN_ISSUED_AT": datetime(2026, 9, 21, 8, 15, tzinfo=IST).isoformat(),
        "PRAMANA_ZERODHA_SYMBOLS_JSON": json.dumps(TOKENS),
        "PRAMANA_NEWS_RSS_URLS": "https://a.example/rss,https://b.example/rss",
        "FRED_API_KEY": "fake-fred-key",
        "PRAMANA_FUNDAMENTALS_PROVIDER": "yahoo",
        "PRAMANA_INDIA_VIX_PROVIDER": "yahoo",
        "PRAMANA_INDIA_FLOWS_PROVIDER": "nse",
        "PRAMANA_EVENT_CALENDAR": str(CALENDAR),
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def payload(**overrides) -> dict:
    base = {
        "status": "running",
        "halted": False,
        "updatedAt": (NOW - timedelta(seconds=12)).astimezone(timezone.utc).isoformat(),
        "watchlist": [{"symbol": s} for s in SYMBOLS],
        "strategyManifest": {"status": "matched", "issues": []},
        "riskGates": {"gates": [{"id": "sector_concentration", "armed": True}, {"id": "overnight", "armed": True}]},
        "marketDataIntegrity": {"accepted": 0, "rejected": {}},
    }
    base.update(overrides)
    return base


def manifest(**overrides) -> dict:
    def provider(type_name, **extra):
        return {"type": f"quant_ai.x.{type_name}", **extra}

    base = {
        "specialists": [{"agent_id": f"agent-{i}", "supported": True} for i in range(7)],
        "components": {
            "news": {"providers": {"NEWS": [provider("RssNewsSentimentAdapter", feed_identities=["a", "b"])]}},
            "fundamentals": {"providers": {"FUNDAMENTALS": [provider("YahooFundamentalsProvider")]}},
            "macro": {"providers": {"MACRO": [provider(
                "CompositeMacroProvider",
                parts=[provider("FredMacroProvider"), provider("YahooIndiaVixProvider"), provider("NseInstitutionalFlowsProvider")],
            )]}},
        },
    }
    base.update(overrides)
    return base


def states(checks) -> dict[str, str]:
    return {check.id: check.state for check in checks}


def test_a_ready_engine_before_the_open_is_all_green_with_ticks_pending():
    checks = premarket_checks(payload(), manifest(), env(), NOW)
    assert states(checks) == {
        "engine_running": "OK", "manifest_matched": "OK", "watchlist_mapped": "OK",
        "token_covers_session": "OK", "specialists_supported": "OK", "providers_configured": "OK",
        "risk_gates_armed": "OK", "event_calendar": "OK", "market_data": "INFO",
    }
    text = render(checks)
    assert text.endswith("READY TO TRADE")
    assert "valid until 2026-09-22 06:00 IST" in text
    assert "3 names on the watchlist, 3 tokens mapped" in text
    assert "0 events, timezone Asia/Kolkata" in text
    assert "not-a-real-token" not in text


def test_the_incomplete_manifest_that_blocked_21_september_is_a_fail():
    checks = premarket_checks(
        payload(strategyManifest={"status": "incomplete", "issues": ["unsupported_component:x.YahooFundamentalsProvider"]}),
        manifest(), env(), NOW,
    )
    assert states(checks)["manifest_matched"] == "FAIL"
    assert "NOT READY: fix manifest_matched" in render(checks)


def test_a_watchlist_that_does_not_match_the_token_map_is_a_fail():
    checks = premarket_checks(payload(watchlist=[{"symbol": "INFY"}, {"symbol": "TRENT"}]), manifest(), env(), NOW)
    check = next(c for c in checks if c.id == "watchlist_mapped")
    assert check.state == "FAIL"
    assert "no token for INFY" in check.detail and "token but not watched BEL,GOLDBEES" in check.detail


@pytest.mark.parametrize(
    "issued, expected",
    [
        (datetime(2026, 9, 20, 9, 0, tzinfo=IST), "token expired at 2026-09-21 06:00 IST; log in again"),
        (datetime(2026, 9, 21, 5, 30, tzinfo=IST), "token expired at 2026-09-21 06:00 IST; log in again"),
        (datetime(2026, 9, 21, 9, 30, tzinfo=IST), "token issuance is in the future"),
        (None, "PRAMANA_ZERODHA_TOKEN_ISSUED_AT missing or invalid"),
    ],
)
def test_a_token_that_dies_before_the_close_is_a_fail(issued, expected):
    configured = env(PRAMANA_ZERODHA_TOKEN_ISSUED_AT=issued.isoformat() if issued else None)
    check = next(c for c in premarket_checks(payload(), manifest(), configured, NOW) if c.id == "token_covers_session")
    assert check.state == "FAIL" and check.detail == expected


def test_missing_providers_are_named_against_what_the_environment_asked_for():
    thin = manifest()
    thin["components"]["news"]["providers"]["NEWS"][0]["feed_identities"] = ["a"]
    thin["components"]["macro"]["providers"]["MACRO"][0]["parts"] = [{"type": "quant_ai.x.FredMacroProvider"}]
    check = next(c for c in premarket_checks(payload(), thin, env(), NOW) if c.id == "providers_configured")
    assert check.state == "FAIL"
    assert "news feeds 1/2" in check.detail
    assert "macro india_vix" in check.detail and "macro fii_dii" in check.detail
    relaxed = env(PRAMANA_INDIA_VIX_PROVIDER="none", PRAMANA_INDIA_FLOWS_PROVIDER="none", PRAMANA_NEWS_RSS_URLS="https://a.example/rss")
    assert next(c for c in premarket_checks(payload(), thin, relaxed, NOW) if c.id == "providers_configured").state == "OK"


def test_no_manifest_record_yet_fails_the_manifest_backed_checks():
    result = states(premarket_checks(payload(), None, env(), NOW))
    assert result["specialists_supported"] == "FAIL" and result["providers_configured"] == "FAIL"


def test_an_unarmed_gate_and_a_halted_engine_are_fails():
    stopped = payload(halted=True, haltReason="operator", riskGates={"gates": [{"id": "sector_concentration", "armed": False}]})
    checks = premarket_checks(stopped, manifest(), env(), NOW)
    result = states(checks)
    assert result["engine_running"] == "FAIL" and result["risk_gates_armed"] == "FAIL"
    assert "halt_reason=operator" in next(c for c in checks if c.id == "engine_running").detail


def test_a_stale_heartbeat_is_a_fail():
    stale = payload(updatedAt=(NOW - timedelta(minutes=5)).astimezone(timezone.utc).isoformat())
    assert states(premarket_checks(stale, manifest(), env(), NOW))["engine_running"] == "FAIL"


def test_event_calendar_states(tmp_path):
    assert states(premarket_checks(payload(), manifest(), env(PRAMANA_EVENT_CALENDAR=None), NOW))["event_calendar"] == "INFO"
    broken = tmp_path / "calendar.json"
    broken.write_text('{"events": [{"date": "2026-10-16", "category": "earnings"}]}')
    check = next(c for c in premarket_checks(payload(), manifest(), env(PRAMANA_EVENT_CALENDAR=str(broken)), NOW) if c.id == "event_calendar")
    assert check.state == "FAIL" and "earnings event requires the symbol" in check.detail


def test_market_data_is_informational_before_the_open_and_a_fail_after_it():
    quiet = payload()
    assert states(premarket_checks(quiet, manifest(), env(), NOW))["market_data"] == "INFO"
    pre_open = NOW.replace(hour=9, minute=5)
    assert states(premarket_checks(quiet, manifest(), env(), pre_open))["market_data"] == "INFO"
    open_now = NOW.replace(hour=9, minute=20)
    assert states(premarket_checks(quiet, manifest(), env(), open_now))["market_data"] == "FAIL"
    ticking = payload(marketDataIntegrity={"accepted": 361, "rejected": {"duplicate_tick": 107}})
    check = next(c for c in premarket_checks(ticking, manifest(), env(), open_now) if c.id == "market_data")
    assert check.state == "OK" and "361 ticks accepted" in check.detail


def test_naive_clock_is_refused():
    with pytest.raises(ValueError, match="timezone"):
        premarket_checks(payload(), manifest(), env(), NOW.replace(tzinfo=None))


def engine_database(tmp_path, runtime: dict, manifests: list[tuple[str, dict]]) -> Path:
    database = tmp_path / "pramana.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE pilot_runtime (tenant_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("INSERT INTO pilot_runtime VALUES ('ghost', ?, ?)", (runtime["updatedAt"], json.dumps(runtime)))
        db.execute("CREATE TABLE pilot_strategy_manifests (tenant_id TEXT,sha256 TEXT,created_at TEXT,payload TEXT,PRIMARY KEY(tenant_id,sha256))")
        for created_at, document in manifests:
            db.execute("INSERT INTO pilot_strategy_manifests VALUES ('ghost', ?, ?, ?)", ("sha-" + created_at, created_at, json.dumps(document)))
    return database


def test_records_are_read_from_the_ledger_and_the_newest_manifest_wins(tmp_path):
    older = manifest(specialists=[{"agent_id": "old", "supported": False}])
    database = engine_database(tmp_path, payload(), [("2026-09-20T03:00:00+00:00", older), ("2026-09-21T03:19:00+00:00", manifest())])
    runtime, latest = load_engine_records(database, "ghost")
    assert runtime["status"] == "running"
    assert all(s["supported"] for s in latest["specialists"])
    assert load_engine_records(database, "nobody") == ({}, None)


def test_pilot_ops_premarket_action_reports_and_exits_on_failures(tmp_path):
    spec = importlib.util.spec_from_file_location("pilot_ops_premarket", ROOT / "scripts/pilot_ops.py")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    ready = engine_database(tmp_path, payload(), [("2026-09-21T03:19:00+00:00", manifest())])
    report, ok = ops.premarket(ready, "ghost", env=env(), now=NOW)
    assert ok and report.endswith("READY TO TRADE")
    blocked = engine_database(
        tmp_path / "blocked", payload(strategyManifest={"status": "incomplete", "issues": ["unsupported_component:x"]}),
        [("2026-09-21T03:19:00+00:00", manifest())],
    ) if (tmp_path / "blocked").mkdir() is None else None
    report, ok = ops.premarket(blocked, "ghost", env=env(), now=NOW)
    assert not ok and "NOT READY: fix manifest_matched" in report
