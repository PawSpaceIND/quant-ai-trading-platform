"""Missed-opportunity report over the decision journal.

On 21 September 2026, the first twelve-name session, every decision was a hold, and the
decision-quality report had nothing to score. These tests pin what the missed-opportunity
report says about such a day: which holds were followed by an up-move a long-only book
could have bought, which were right to skip, which cannot be judged yet, and that the
daemon writes the file and says so once after the close without ever costing a tick.
Numbers are checked by hand, not by re-running the implementation's arithmetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import missed_opportunities as missed
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import TradingNotificationDispatcher
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.governance.directives import country_for
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed
from quant_ai.notifications.trading import AlertPriority, TradingAlertCode
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

ROOT = Path(__file__).resolve().parents[1]
TENANT = "pilot"
SESSION_DATE = "2026-09-21"  # Monday; NSE regular hours
NOW = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)  # 16:00 IST, after the close
NEUTRAL_VOTES = {
    "technical-quant-mas": {"stance": "NEUTRAL", "confidence": "0.42"},
    "indian-equities": {"stance": "NEUTRAL", "confidence": "0.40"},
}
DISSENTING_VOTES = {
    "technical-quant-mas": {"stance": "BUY", "confidence": "0.71"},
    "indian-equities": {"stance": "NEUTRAL", "confidence": "0.40"},
}


# ------------------------------------------------------------------ helpers


def utc(hour: int, minute: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def row(decision_id, symbol, decided_at, *, forward=None, side=None, regime="RANGE_BOUND",
        reference="100", agents=None, tenant=TENANT, **overrides):
    """A journal row with only what the report reads; ``side`` None is a hold."""
    base = {
        "decision_id": decision_id,
        "tenant_id": tenant,
        "symbol": symbol,
        "market": "INDIA",
        "asset_class": "EQUITY",
        "decided_at": decided_at.isoformat(),
        "stance": "NEUTRAL" if side is None else side,
        "side": side,
        "confidence": "0.4" if side is None else "0.8",
        "reference_price": reference,
        "regime": regime,
        "mode": "llm",
        "governance": journal.GOVERNANCE_ABSTAINED if side is None else journal.GOVERNANCE_FILLED,
        "reason": None,
        "order_id": None if side is None else f"PAPER-{decision_id}",
        "agents": json.dumps(agents if agents is not None else NEUTRAL_VOTES, sort_keys=True),
        "forward_return_60m": None if forward is None else str(forward),
    }
    base.update(overrides)
    return base


def session_rows(tenant=TENANT):
    """The 21 September fixture: four names, eleven holds, one fill, one rejection."""
    return [
        # RELIANCE: two missed (the 11:20 IST one is the day's best), one quiet, one unresolved.
        row("rel-1", "RELIANCE", utc(4, 30), forward="0.012", regime="BULL_TRENDING", reference="2950", tenant=tenant),
        row("rel-2", "RELIANCE", utc(5, 50), forward="0.018", regime="BULL_TRENDING", reference="2950", tenant=tenant),
        row("rel-3", "RELIANCE", utc(6, 30), forward="-0.004", reference="2950", tenant=tenant),
        row("rel-4", "RELIANCE", utc(7, 0), forward=None, reference="2950", tenant=tenant),
        # INFY: both thresholds hit exactly, one hold in between, one filled BUY that is not a hold.
        row("infy-1", "INFY", utc(4, 40), forward="0.01", agents=DISSENTING_VOTES, reference="1500", tenant=tenant),
        row("infy-2", "INFY", utc(5, 30), forward="-0.01", reference="1500", tenant=tenant),
        row("infy-3", "INFY", utc(6, 0), forward="0.003", reference="1500", tenant=tenant),
        row("infy-buy", "INFY", utc(6, 20), forward="0.05", side="BUY", reference="1500", tenant=tenant),
        # TCS: right to skip twice, flat once; and a governed rejection that carries a side.
        row("tcs-1", "TCS", utc(4, 50), forward="-0.02", tenant=tenant),
        row("tcs-2", "TCS", utc(5, 40), forward="-0.015", tenant=tenant),
        row("tcs-3", "TCS", utc(7, 10), forward="0", tenant=tenant),
        row("tcs-rej", "TCS", utc(7, 20), forward="0.03", side="BUY", governance=journal.GOVERNANCE_REJECTED,
            reason="max_positions", order_id=None, tenant=tenant),
        # 03:00 UTC is 08:30 IST on the 21st; 19:00 UTC is already 00:30 IST on the 22nd.
        row("hdfc-early", "HDFCBANK", utc(3, 0), forward="0.02", tenant=tenant),
        row("hdfc-late", "HDFCBANK", utc(19, 0), forward="0.05", tenant=tenant),
    ]


def seeded_broker(tmp_path, name="ledger.sqlite", tenant=TENANT):
    broker = PaperBrokerService(tmp_path / name, starting_capital=D(100000))
    for item in session_rows(tenant):
        assert journal.insert_decision(broker, item)
    return broker


def by_symbol(report):
    return {item["symbol"]: item for item in report["symbols"]}


# ------------------------------------------------------------------ report


def test_report_counts_missed_avoided_and_unresolved_holds_per_symbol(tmp_path):
    broker = seeded_broker(tmp_path)

    report = missed.build_report(broker, tenant_id=TENANT, now=NOW)

    assert report["schema"] == "pramana.missed_opportunities.v1"
    assert report["session_date"] == SESSION_DATE
    assert report["generated_at"] == NOW.isoformat()
    assert report["tenant_id"] == TENANT
    assert report["threshold"] == 0.01
    assert report["horizon"] == "forward_return_60m"
    # The fill and the rejection carry a side and are not holds; the 19:00 UTC row is tomorrow.
    assert report["decisions"] == 13
    assert report["holds"] == 11
    assert report["evaluated"] == 10
    assert report["missed"] == 4
    assert report["avoided"] == 3
    assert report["unresolved"] == 1

    symbols = by_symbol(report)
    assert {k: (v["missed"], v["avoided"], v["evaluated"]) for k, v in symbols.items()} == {
        "RELIANCE": (2, 0, 3),
        "INFY": (1, 1, 3),
        "TCS": (0, 2, 3),
        "HDFCBANK": (1, 0, 1),
    }
    # Best missed return descending, then the names with nothing missed.
    assert [item["symbol"] for item in report["symbols"]] == ["HDFCBANK", "RELIANCE", "INFY", "TCS"]
    assert symbols["TCS"]["best"] is None


def test_best_block_carries_the_decision_and_the_specialists_votes(tmp_path):
    broker = seeded_broker(tmp_path)

    report = missed.build_report(broker, tenant_id=TENANT, now=NOW)

    best = by_symbol(report)["RELIANCE"]["best"]
    assert best == {
        "decision_id": "rel-2",
        "decided_at": "2026-09-21T11:20:00+05:30",
        "reference_price": 2950.0,
        "forward_return": 0.018,
        "regime": "BULL_TRENDING",
        "mode": "llm",
        "reason": None,
        "agents": NEUTRAL_VOTES,
    }
    assert by_symbol(report)["INFY"]["best"]["agents"] == DISSENTING_VOTES
    assert any("last traded price" in note and "costs" in note for note in report["limitations"])
    assert any("not a claim" in note for note in report["limitations"])


def test_threshold_and_horizon_are_parameters(tmp_path):
    broker = seeded_broker(tmp_path)
    journal.update_decision(broker, "tcs-3", forward_return_10m="0.02")

    wider = missed.build_report(broker, tenant_id=TENANT, now=NOW, threshold=D("0.015"))
    assert wider["threshold"] == 0.015
    assert (wider["missed"], wider["avoided"]) == (2, 2)
    assert by_symbol(wider)["INFY"]["best"] is None  # +1.00% no longer clears 1.50%
    assert by_symbol(wider)["TCS"]["avoided"] == 2  # -1.50% still counts as avoided

    ten = missed.build_report(broker, tenant_id=TENANT, now=NOW, horizon="forward_return_10m")
    assert ten["horizon"] == "forward_return_10m"
    assert (ten["evaluated"], ten["missed"], ten["unresolved"]) == (1, 1, 10)
    assert by_symbol(ten)["TCS"]["best"]["decision_id"] == "tcs-3"

    with pytest.raises(ValueError):
        missed.build_report(broker, tenant_id=TENANT, now=NOW, horizon="realized_net_pnl")
    with pytest.raises(ValueError):
        missed.build_report(broker, tenant_id=TENANT, now=NOW, threshold=D(0))


def test_session_is_the_ist_calendar_date(tmp_path):
    broker = seeded_broker(tmp_path)

    today = missed.build_report(broker, tenant_id=TENANT, now=NOW, session_date=SESSION_DATE)
    tomorrow = missed.build_report(broker, tenant_id=TENANT, now=NOW, session_date="2026-09-22")
    # ``now`` at 19:30 UTC is already the 22nd in IST, so the default date follows it.
    default = missed.build_report(broker, tenant_id=TENANT, now=utc(19, 30))

    assert by_symbol(today)["HDFCBANK"]["best"]["decision_id"] == "hdfc-early"
    assert tomorrow["session_date"] == "2026-09-22"
    assert tomorrow["holds"] == tomorrow["missed"] == 1
    assert by_symbol(tomorrow)["HDFCBANK"]["best"]["decided_at"] == "2026-09-22T00:30:00+05:30"
    assert default["session_date"] == "2026-09-22"
    assert missed.build_report(broker, tenant_id="nobody", now=NOW)["holds"] == 0


def test_render_is_a_table_with_a_one_line_verdict(tmp_path):
    broker = seeded_broker(tmp_path)
    report = missed.build_report(broker, tenant_id=TENANT, now=NOW)

    text = missed.render(report)
    lines = text.splitlines()

    assert lines[0] == (
        "Missed opportunities 2026-09-21 tenant=pilot threshold=1.00% horizon=forward_return_60m"
    )
    assert lines[1] == "decisions=13 holds=11 evaluated=10 missed=4 avoided=3 unresolved=1"
    assert lines[2].split() == ["SYMBOL", "MISSED", "AVOIDED", "EVALUATED", "BEST", "MISSED", "MOVE"]
    assert [line.split()[0] for line in lines[3:7]] == ["HDFCBANK", "RELIANCE", "INFY", "TCS"]
    assert lines[4].endswith("2        0          3  +1.80% at 11:20 IST, BULL_TRENDING, specialists neutral")
    assert "+1.00% at 10:10 IST, RANGE_BOUND, technical-quant-mas BUY, 1 neutral" in lines[5]
    assert lines[6].endswith("0        2          3  -")
    assert lines[-1] == "10 holds evaluated, 4 missed moves above 1.00%"

    empty = missed.summarize([], tenant_id=TENANT, now=NOW, session_date=SESSION_DATE)
    assert "no holds journaled for this session" in missed.render(empty)
    assert missed.render(empty).endswith("0 holds evaluated, 0 missed moves above 1.00%")


def test_notification_message_is_compact_and_bounded(tmp_path):
    broker = seeded_broker(tmp_path)
    report = missed.build_report(broker, tenant_id=TENANT, now=NOW)

    message = missed.notification_message(report)

    assert message.splitlines() == [
        "Missed moves 2026-09-21: 10 holds evaluated, 4 above 1.00% on the 60-minute horizon (1 unresolved).",
        "HDFCBANK +2.00% at 08:30 IST, RANGE_BOUND, specialists neutral",
        "RELIANCE +1.80% at 11:20 IST, BULL_TRENDING, specialists neutral",
        "INFY +1.00% at 10:10 IST, RANGE_BOUND, technical-quant-mas BUY, 1 neutral",
    ]
    assert len(missed.notification_message(report, limit=2).splitlines()) == 3
    quiet = missed.build_report(broker, tenant_id=TENANT, now=NOW, threshold=D("0.5"))
    assert missed.notification_message(quiet).startswith(
        "Missed moves 2026-09-21: 10 holds evaluated, none above 50.00%"
    )


def test_write_report_replaces_the_file_atomically_in_a_new_directory(tmp_path):
    broker = seeded_broker(tmp_path)
    report = missed.build_report(broker, tenant_id=TENANT, now=NOW)
    target = tmp_path / "missed" / "deep" / f"{SESSION_DATE}.json"

    written = missed.write_report(target, report)
    missed.write_report(target, {**report, "missed": 99})

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8"))["missed"] == 99
    assert [item.name for item in target.parent.iterdir()] == [target.name]  # no temp file left


# ------------------------------------------------------------------ operator shell


def load_ops():
    spec = importlib.util.spec_from_file_location("pilot_ops_missed", ROOT / "scripts/pilot_ops.py")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    return ops


def test_pilot_ops_missed_reads_the_ledger_read_only(tmp_path):
    broker = seeded_broker(tmp_path)
    broker.close()
    database = tmp_path / "ledger.sqlite"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    text = load_ops().missed(database, TENANT, session_date=SESSION_DATE, now=NOW)

    assert text.splitlines()[-1] == "10 holds evaluated, 4 missed moves above 1.00%"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    # A ledger that never journaled has no table to create, and nothing was missed.
    bare = PaperBrokerService(tmp_path / "bare.sqlite")
    bare.close()
    assert "no holds journaled" in load_ops().missed(tmp_path / "bare.sqlite", TENANT, now=NOW)


def test_pilot_ops_missed_action_prints_the_report_and_exits_zero(tmp_path):
    seeded_broker(tmp_path).close()
    command = [
        sys.executable, str(ROOT / "scripts/pilot_ops.py"), "missed",
        "--database", str(tmp_path / "ledger.sqlite"), "--tenant", TENANT,
        "--date", SESSION_DATE, "--threshold", "0.015",
    ]

    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=60,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Missed opportunities 2026-09-21 tenant=pilot threshold=1.50%")
    assert result.stdout.rstrip().endswith("10 holds evaluated, 2 missed moves above 1.50%")

    bad = subprocess.run(
        [*command[:-1], "lots"], capture_output=True, text=True, check=False, timeout=60,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert bad.returncode == 2 and "not a decimal fraction" in bad.stderr


# ------------------------------------------------------------------ daemon


class CaptureChannel:
    def __init__(self) -> None:
        self.items = []

    def send(self, notification) -> None:
        self.items.append(notification)


INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
DAEMON_TENANT = "daemon"


def build_daemon(tmp_path, clock, *, missed_dir):
    """A sandbox daemon on an NSE name, so the IST session date and the close are the pilot's."""
    broker = PaperBrokerService(tmp_path / "daemon.db", starting_capital=D(100000), slippage_bps=D(0))
    feed = IndiaSandboxMarketDataFeed()
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=SwarmPaperTradingService(broker=broker),
    )
    scheduler = AutonomousCadenceScheduler(pipeline, calendar=MarketCalendar(holidays=default_holidays()))
    tracker = PortfolioTracker(broker, feed, tenant_id=DAEMON_TENANT)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D("0.82"), D("0.20"), expected_edge=D("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    channel = CaptureChannel()
    daemon = AutonomousTradingDaemon(
        scheduler, tracker, INFY, plan, quantity=10, country=country_for(INFY), tenant_id=DAEMON_TENANT,
        notifications=TradingNotificationDispatcher((channel,)), idle_sleep_seconds=0.01,
        clock=clock, missed_opportunity_dir=missed_dir,
    )
    return daemon, broker, channel


def missed_notes(channel):
    return [item for item in channel.items if item.metadata.get("kind") == "missed_opportunities"]


def test_daemon_writes_the_session_file_every_cycle_and_reports_once_after_the_close(tmp_path):
    missed_dir = tmp_path / "missed"
    open_hours = utc(5, 0)  # 10:30 IST
    daemon, broker, channel = build_daemon(tmp_path, lambda: open_hours, missed_dir=missed_dir)
    for item in session_rows(DAEMON_TENANT):
        journal.insert_decision(broker, item)
    calendar = daemon.scheduler.calendar
    assert calendar.state(Market.INDIA, open_hours, exchange="NSE") == MarketState.REGULAR_HOURS
    assert calendar.state(Market.INDIA, NOW, exchange="NSE") != MarketState.REGULAR_HOURS

    brief = asyncio.run(daemon.run_once(open_hours))

    assert brief.subject == "INFY"
    written = json.loads((missed_dir / f"{SESSION_DATE}.json").read_text(encoding="utf-8"))
    assert written["schema"] == "pramana.missed_opportunities.v1"
    assert by_symbol(written)["RELIANCE"]["missed"] == 2
    assert missed_notes(channel) == []  # the market is open: nothing to sum up yet

    asyncio.run(daemon.run_once(NOW))
    asyncio.run(daemon.run_once(utc(10, 40)))

    notes = missed_notes(channel)
    assert len(notes) == 1
    assert notes[0].code == TradingAlertCode.CADENCE_BRIEF
    assert notes[0].priority == AlertPriority.INFO
    assert notes[0].tenant_id == DAEMON_TENANT
    assert "Missed moves 2026-09-21:" in notes[0].message
    assert "RELIANCE +1.80% at 11:20 IST, BULL_TRENDING, specialists neutral" in notes[0].message
    assert notes[0].metadata["session_date"] == SESSION_DATE
    assert notes[0].metadata["missed"] == "4"
    marker = missed_dir / f".notified-{SESSION_DATE}"
    assert marker.read_text(encoding="utf-8") == NOW.isoformat()
    # The file keeps being rewritten after the close; the note is not.
    assert json.loads((missed_dir / f"{SESSION_DATE}.json").read_text())["generated_at"] == utc(10, 40).isoformat()

    # A restart has an empty memory; the marker file is what stops the second copy.
    broker.close()
    restarted, reopened, fresh_channel = build_daemon(tmp_path, lambda: utc(10, 50), missed_dir=missed_dir)
    asyncio.run(restarted.run_once(utc(10, 50)))
    assert missed_notes(fresh_channel) == []
    marker.unlink()
    asyncio.run(restarted.run_once(utc(11, 0)))
    assert missed_notes(fresh_channel) == []  # already remembered in-process for this date
    reopened.close()


def test_daemon_without_a_directory_writes_nothing_and_a_broken_report_costs_no_tick(tmp_path, monkeypatch, caplog):
    daemon, broker, _channel = build_daemon(tmp_path, lambda: utc(5, 0), missed_dir=None)
    asyncio.run(daemon.run_once(utc(5, 0)))
    assert not list(tmp_path.glob("**/2026-09-21.json"))

    daemon.missed_opportunity_dir = tmp_path / "missed"
    monkeypatch.setattr(missed, "build_report", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    brief = asyncio.run(daemon.run_once(utc(5, 10)))

    assert brief.subject == "INFY"
    assert "missed_opportunity_report_failed" in caplog.text
    assert not (tmp_path / "missed").exists()
    broker.close()
