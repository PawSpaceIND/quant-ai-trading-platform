"""The opportunity scan: what would trade today outside the book, and the gates to add it.

Candidates come from the operator's NSE list, are read from closed daily bars, classified and
routed through the same playbooks as the watched names, and reported in the session plan
with the promotion gates they pass or miss. Read-only: nothing is promoted or ordered.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_premarket_check import env, manifest, payload
from test_session_plan import INFY, TCS, build_daemon, plans, utc

from quant_ai.agents import scanner, strategist
from quant_ai.agents.atlas import AtlasPolicy
from quant_ai.agents.scanner import SCAN_UNIVERSE_ENV, scan, scan_universe_from_env
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.notifications.trading import TradingAlertCode
from quant_ai.operations.premarket import premarket_checks

D = Decimal
NOW = datetime(2026, 9, 22, 3, 5, tzinfo=timezone.utc)


def candidate(symbol: str) -> Instrument:
    return Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", "NSE", tradable=False)


class History:
    """Daily bars per symbol: rising, flat, falling, or too few to read."""

    def __init__(self, shapes: dict[str, str]) -> None:
        self.shapes = shapes
        self.fetched: list[str] = []

    def fetch(self, instrument, now):
        self.fetched.append(instrument.symbol)
        shape = self.shapes.get(instrument.symbol, "none")
        if shape == "none":
            return ()
        if shape == "error":
            raise OSError("yahoo down")
        count = 45 if shape != "short" else 8
        start = now - timedelta(days=80)
        bars = []
        for i in range(count):
            price = {"up": D(100 + i), "flat": D(100 + (i % 2)), "down": D(200 - i), "short": D(100 + i)}[shape]
            bars.append(Candle(instrument, start + timedelta(days=i), price, price + 1, price - 1, price, D(1000)))
        return tuple(bars)


# ----------------------------------------------------------------- the environment


def test_the_universe_reads_symbols_or_rows_and_refuses_anything_it_cannot_scan() -> None:
    assert scan_universe_from_env({}) == ()
    names = scan_universe_from_env({SCAN_UNIVERSE_ENV: '["bel", {"symbol": "LT", "exchange": "nse"}]'})
    assert [(i.symbol, i.exchange, i.tradable, i.asset_class) for i in names] == [
        ("BEL", "NSE", False, AssetClass.EQUITY), ("LT", "NSE", False, AssetClass.EQUITY),
    ]
    for bad, message in (
        ("not json", "not JSON"), ('{"symbol": "BEL"}', "must be a JSON list"), ("[1]", "symbol or a"),
        ('["bel bhel"]', "bad symbol"), ('[{"symbol": "GOLD", "exchange": "MCX"}]', "NSE cash only"),
        ('["BEL", "BEL"]', "duplicate BEL"), (json.dumps([f"S{i}" for i in range(101)]), "at most 100"),
    ):
        with pytest.raises(RuntimeError, match=message):
            scan_universe_from_env({SCAN_UNIVERSE_ENV: bad})


# ----------------------------------------------------------------- the scan


def test_the_scan_routes_candidates_through_the_playbooks_and_reports_the_gates() -> None:
    history = History({"BEL": "up", "LT": "flat", "NTPC": "down", "SUNPHARMA": "short", "INFY": "up"})
    report = scan(
        (candidate("BEL"), candidate("LT"), candidate("NTPC"), candidate("SUNPHARMA"), candidate("INFY"), candidate("ZOMATO")),
        history=history, now=NOW, watched=("INFY", "TCS"),
        gates={"token": {"BEL", "LT"}, "sector": {"BEL", "NTPC"}},
    )
    assert "INFY" not in history.fetched  # already watched: not read, not reported
    assert report["scanned"] == 5 and report["skipped_watched"] == 1 and report["history"] == "daily"
    assert report["gates"] == ["sector", "token"]
    assert report["opportunities"] == ["BEL", "LT"]
    by_symbol = {item["symbol"]: item for item in report["names"]}
    assert by_symbol["BEL"]["regime"] == "trending_up" and by_symbol["BEL"]["playbook"] == "trend_following"
    assert by_symbol["BEL"]["role"] == "opportunity" and by_symbol["BEL"]["promotable"] is True
    assert by_symbol["BEL"]["gates_missing"] == []
    assert by_symbol["LT"]["regime"] == "ranging" and by_symbol["LT"]["role"] == "opportunity"
    assert by_symbol["LT"]["gates_missing"] == ["sector"] and by_symbol["LT"]["promotable"] is False
    assert by_symbol["NTPC"]["regime"] == "trending_down" and by_symbol["NTPC"]["role"] == "quiet"
    assert by_symbol["SUNPHARMA"]["role"] == "unread" and by_symbol["SUNPHARMA"]["daily_bars"] == 8
    assert by_symbol["ZOMATO"]["role"] == "unread" and by_symbol["ZOMATO"]["daily_bars"] == 0
    assert by_symbol["ZOMATO"]["gates_missing"] == ["sector", "token"]
    # Opportunities first, strongest trend first, then the quiet and the unread.
    assert [item["symbol"] for item in report["names"]] == ["BEL", "LT", "NTPC", "SUNPHARMA", "ZOMATO"]


def test_a_failing_or_absent_history_reads_nothing_and_no_gates_means_the_plan_cannot_say() -> None:
    report = scan((candidate("BEL"),), history=History({"BEL": "error"}), now=NOW)
    assert report["names"][0]["role"] == "unread" and report["names"][0]["promotable"] is False
    assert report["names"][0]["gates_missing"] == [] and report["gates"] == []
    none = scan((candidate("BEL"),), history=None, now=NOW)
    assert none["history"] == "none" and none["names"][0]["daily_bars"] == 0
    assert scanner.brief_lines(none) == ["Outside the book (1 scanned): 0 would trade today, no daily history to read them."]
    assert scanner.brief_lines({}) == [] and scanner.brief_lines({"scanned": 0}) == []


def test_routing_off_still_reports_every_read_name_as_an_opportunity() -> None:
    report = scan((candidate("NTPC"),), history=History({"NTPC": "down"}), now=NOW, regime_playbooks=False)
    assert report["names"][0]["playbook"] == "unrouted" and report["names"][0]["role"] == "opportunity"


def test_the_brief_lines_name_the_gates_and_bound_the_list() -> None:
    history = History({f"S{i}": "up" for i in range(7)})
    report = scan(tuple(candidate(f"S{i}") for i in range(7)), history=history, now=NOW, gates={"token": {"S0"}})
    lines = scanner.brief_lines(report)
    assert lines[0] == "Outside the book (7 scanned): 7 would trade today."
    assert lines[1].startswith("S0 trending_up trend_following trend ") and lines[1].endswith("(needs token)") is False
    assert lines[1].endswith("(gates passed)")
    assert lines[2].endswith("(needs token)")
    assert lines[-1] == "+2 more in the plan file"
    plan = strategist.build_session_plan(tenant_id="plan", now=NOW, session_date="2026-09-22", policy=AtlasPolicy(),
                                         names=(), outside_book=report)
    brief = strategist.morning_brief(plan)
    assert "Outside the book (7 scanned): 7 would trade today." in brief
    assert plan["outside_book"]["opportunities"] == [f"S{i}" for i in range(7)]
    text = strategist.render(plan)
    assert "OUTSIDE THE BOOK (7 scanned, gates: token)" in text and "promotable" in text and "needs token" in text


# ----------------------------------------------------------------- the daemon and the pre-market check


def test_the_daemon_scans_the_universe_into_the_plan_with_the_gates_it_could_map(tmp_path) -> None:
    plan_dir = tmp_path / "session-plans"
    daemon, _broker, channel = build_daemon(tmp_path, lambda: utc(3, 5), plan_dir=plan_dir)
    daemon.scan_universe = (candidate("BEL"), candidate("LT"), candidate("INFY"))
    daemon.scan_gates = {"token": frozenset({"BEL"}), "sector": frozenset({"BEL", "LT"})}
    daemon.scheduler.pipeline.history = History({"BEL": "up", "LT": "up", "INFY": "up"})
    asyncio.run(daemon.run_once(utc(3, 5)))
    written = json.loads((plan_dir / "2026-09-22.json").read_text())
    outside = written["outside_book"]
    assert outside["scanned"] == 2 and outside["skipped_watched"] == 1  # INFY is on the book
    assert outside["opportunities"] == ["BEL", "LT"]
    by_symbol = {item["symbol"]: item for item in outside["names"]}
    assert by_symbol["BEL"]["promotable"] is True and by_symbol["LT"]["gates_missing"] == ["token"]
    (note,) = plans(channel)
    assert "Outside the book (2 scanned): 2 would trade today." in note.message
    assert "BEL trending_up trend_following trend" in note.message and "(gates passed)" in note.message
    assert "LT trending_up trend_following trend" in note.message and "(needs token)" in note.message
    assert note.metadata["opportunities"] == "2"
    assert INFY.symbol == "INFY" and TCS.symbol == "TCS"


def test_without_a_universe_the_plan_carries_no_scan(tmp_path) -> None:
    plan_dir = tmp_path / "session-plans"
    daemon, _broker, channel = build_daemon(tmp_path, lambda: utc(3, 5), plan_dir=plan_dir)
    asyncio.run(daemon.run_once(utc(3, 5)))
    written = json.loads((plan_dir / "2026-09-22.json").read_text())
    assert written["outside_book"] is None
    (note,) = plans(channel)
    assert "Outside the book" not in note.message and note.metadata["opportunities"] == "0"
    assert note.code is TradingAlertCode.SESSION_PLAN_READY


def test_the_premarket_check_reports_the_scan_universe() -> None:
    off = next(c for c in premarket_checks(payload(), manifest(), env(), NOW) if c.id == "scan_universe")
    assert off.state == "INFO" and off.detail.startswith("off (set PRAMANA_SCAN_UNIVERSE_JSON")
    watched = payload()["watchlist"][0]["symbol"]
    on = next(c for c in premarket_checks(payload(), manifest(), {**env(), SCAN_UNIVERSE_ENV: json.dumps(["ZOMATO", "IRCTC", watched])}, NOW)
              if c.id == "scan_universe")
    assert on.state == "INFO" and on.detail == "2 NSE names scanned outside the book each pre-open, 1 already watched"
    bad = next(c for c in premarket_checks(payload(), manifest(), {**env(), SCAN_UNIVERSE_ENV: '["GOLD MCX"]'}, NOW)
               if c.id == "scan_universe")
    assert bad.state == "FAIL" and "bad symbol" in bad.detail
