"""The pre-open strategist: one plan per trading day, from the code the decisions run on.

The plan names what Atlas will focus on and sit out, from the daily regime and its playbook,
the operator's blackouts, the last session's numbers, the probe budget, the lessons in force
and the week's skill weights. It is deterministic, written once per trading day from 08:30
local, read out as the morning brief, and changes no gate, size or floor.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.agents import strategist
from quant_ai.agents.atlas import AtlasPolicy
from quant_ai.agents.playbook import PLAYBOOKS
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics import decision_journal as journal
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import TradingNotificationDispatcher
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketCalendar, default_holidays
from quant_ai.governance.directives import country_for
from quant_ai.governance.event_calendar import EventCalendar, ScheduledEvent
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.notifications.trading import TradingAlertCode
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

D = Decimal
TENANT = "plan"
TODAY = date(2026, 9, 22)  # a Tuesday
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
TCS = Instrument("TCS", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
NIFTY = Instrument("NIFTY 50", Market.INDIA, AssetClass.INDEX, "INR", "NSE", tradable=False)


def utc(hour: int, minute: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def name(symbol: str, regime: str | None, *, blackout: str | None = None, tradable: bool = True, bars: int = 45):
    return strategist.NameInputs(symbol, tradable, regime, bars, blackout)


# ----------------------------------------------------------------- the plan


def test_roles_and_posture_follow_the_playbooks_and_the_blackouts() -> None:
    policy = AtlasPolicy(exploration_max_per_day=3)
    plan = strategist.build_session_plan(
        tenant_id=TENANT, now=utc(3), session_date=TODAY, policy=policy,
        names=(
            name("TRENT", "trending_up"), name("INFY", "ranging"), name("TCS", "trending_down"),
            name("COALINDIA", "high_volatility"), name("HDFCBANK", "insufficient_history", bars=12),
            name("RELIANCE", "trending_up", blackout="event_blackout:earnings"),
            name("NIFTY 50", "trending_up", tradable=False), name("UNREAD", None, bars=0),
        ),
    )
    assert plan["schema"] == "pramana.session_plan.v1" and plan["session_date"] == "2026-09-22"
    assert plan["focus"] == ["INFY", "TRENT"]
    assert plan["standdown"] == ["COALINDIA", "HDFCBANK", "TCS", "UNREAD"]
    assert plan["blackout"] == ["RELIANCE"] and plan["watch"] == ["NIFTY 50"]
    assert plan["posture"] == "cautious"  # 5 of 7 tradable names sit out
    by_symbol = {item["symbol"]: item for item in plan["names"]}
    assert by_symbol["TCS"] == {
        "symbol": "TCS", "tradable": True, "regime": "trending_down", "daily_bars": 45, "playbook": "defensive",
        "floor": "0.65", "size_multiplier": "0.50", "probes_allowed": False, "blackout": None, "role": "standdown",
    }
    assert by_symbol["TRENT"]["probes_allowed"] is True and by_symbol["TRENT"]["floor"] == "0.55"
    assert by_symbol["UNREAD"]["playbook"] == "cautious_default"
    assert by_symbol["RELIANCE"]["role"] == "blackout" and by_symbol["RELIANCE"]["playbook"] == "trend_following"
    assert plan["exploration"] == {"max_per_day": 3, "eligible_names": 2}
    assert plan["yesterday"] is None and plan["lessons_in_force"] == 0 and plan["skill_weights"] == {}


def test_no_budget_means_no_probes_and_routing_off_means_the_plan_floor_everywhere() -> None:
    plan = strategist.build_session_plan(
        tenant_id=TENANT, now=utc(3), session_date=TODAY, policy=AtlasPolicy(regime_playbooks=False),
        names=(name("TCS", "trending_down"), name("COALINDIA", "high_volatility")),
    )
    # Routing off: every name trades at the plan floor, so every name is in focus.
    assert plan["posture"] == "normal" and plan["focus"] == ["COALINDIA", "TCS"] and plan["standdown"] == []
    assert all(item["playbook"] == "unrouted" and item["floor"] == "0.55" for item in plan["names"])
    assert all(item["probes_allowed"] is False for item in plan["names"])  # budget 0


def test_postures_span_from_normal_to_observe() -> None:
    def posture(*labels, tradable=True):
        plan = strategist.build_session_plan(
            tenant_id=TENANT, now=utc(3), session_date=TODAY, policy=AtlasPolicy(),
            names=tuple(name(f"S{i}", label, tradable=tradable) for i, label in enumerate(labels)),
        )
        return plan["posture"]
    assert posture("trending_up", "ranging") == "normal"
    assert posture("trending_up", "ranging", "trending_down") == "selective"
    assert posture("trending_up", "trending_down") == "cautious"
    assert posture("trending_down", "high_volatility") == "defensive"
    assert posture("trending_up", tradable=False) == "observe"
    assert posture() == "observe"


def test_yesterday_and_missed_summaries_quote_the_journal_and_the_report(tmp_path) -> None:
    rows = [
        {"decision_id": "a", "side": "BUY", "stance": "BUY", "governance": journal.GOVERNANCE_FILLED,
         "forward_return_60m": "0.01", "probe": 1},
        {"decision_id": "b", "side": None, "stance": "NEUTRAL", "governance": journal.GOVERNANCE_ABSTAINED,
         "forward_return_60m": "-0.01", "probe": 0},
        {"decision_id": "c", "side": "BUY", "stance": "BUY", "governance": journal.GOVERNANCE_REJECTED,
         "forward_return_60m": "-0.02", "probe": 0},
    ]
    summary = strategist.yesterday_summary(rows, date(2026, 9, 21))
    assert summary == {"session_date": "2026-09-21", "decisions": 3, "filled": 1, "rejected": 1, "abstained": 1,
                       "probes": 1, "evaluated_60m": 2, "hit_rate_60m": 0.5}
    missed_dir = tmp_path / "missed"
    missed_dir.mkdir()
    (missed_dir / "2026-09-21.json").write_text(json.dumps({
        "schema": "pramana.missed_opportunities.v1", "holds": 10, "missed": 2, "avoided": 1,
        "symbols": [{"symbol": "TRENT", "best": {"forward_return": 0.018, "decided_at": "2026-09-21T11:20:00+05:30",
                                                  "regime": "ranging", "agents": {}}},
                    {"symbol": "TCS", "best": None}],
    }))
    missed = strategist.missed_summary(missed_dir, "2026-09-21")
    assert missed["missed"] == 2 and missed["top"] == ["TRENT +1.80% at 11:20 IST, ranging, no specialist votes"]
    assert strategist.missed_summary(missed_dir, "2026-09-20") is None
    assert strategist.missed_summary(None, "2026-09-21") is None
    (missed_dir / "2026-09-19.json").write_text("{not json")
    assert strategist.missed_summary(missed_dir, "2026-09-19") is None


def test_the_morning_brief_reads_the_plan_out_in_a_few_lines() -> None:
    plan = strategist.build_session_plan(
        tenant_id=TENANT, now=utc(3), session_date=TODAY, policy=AtlasPolicy(exploration_max_per_day=3),
        names=(name("TRENT", "trending_up"), name("TCS", "trending_down"), name("RELIANCE", "ranging", blackout="event_blackout:earnings")),
        yesterday={"session_date": "2026-09-21", "decisions": 40, "filled": 2, "probes": 3, "evaluated_60m": 36, "hit_rate_60m": 0.5556},
        missed_yesterday={"missed": 2, "top": ["INFY +1.20% at 10:10 IST, ranging, specialists neutral"]},
        lessons_in_force=2, skill_weights={"technical": D("1.1000")},
    )
    assert strategist.morning_brief(plan).split("\n") == [
        "Atlas pre-open 2026-09-22: posture cautious, 1 focus, 1 stand-down, 1 blackout.",
        "Focus: TRENT trend_following",
        "Stand down: TCS trending_down",
        "Blackout: RELIANCE event_blackout:earnings",
        "Yesterday 2026-09-21: 40 decisions, 2 filled, 3 probes, 60m hit rate 0.56 on 36.",
        "Missed yesterday: 2 moves; INFY +1.20% at 10:10 IST, ranging, specialists neutral",
        "Probes 3/day across 1 eligible names. Lessons in force 2. Skill weights: technical x1.1000.",
    ]
    late = strategist.build_session_plan(tenant_id=TENANT, now=utc(5), session_date=TODAY, policy=AtlasPolicy(), names=(), late=True)
    assert strategist.morning_brief(late).startswith("Atlas pre-open 2026-09-22 (late): posture observe, 0 focus, 0 stand-down.")
    assert strategist.morning_brief(late).endswith("Probes off. Lessons in force 0. Skill weights: none.")
    text = strategist.render(plan)
    assert "SYMBOL" in text and "TCS" in text and "defensive" in text


def test_plan_files_round_trip_and_the_newest_is_found(tmp_path) -> None:
    plans = tmp_path / "session-plans"
    for day in ("2026-09-21", "2026-09-22"):
        plan = strategist.build_session_plan(tenant_id=TENANT, now=utc(3), session_date=day, policy=AtlasPolicy(), names=())
        strategist.write_plan(strategist.plan_path(plans, day), plan)
    (plans / "notes.json").write_text("{}")
    assert strategist.latest_plan(plans)["session_date"] == "2026-09-22"
    assert strategist.load_plan(plans / "2026-09-21.json")["session_date"] == "2026-09-21"
    assert strategist.load_plan(plans / "notes.json") is None
    assert strategist.latest_plan(tmp_path / "missing") is None


# ----------------------------------------------------------------- the daemon


class CaptureChannel:
    def __init__(self) -> None:
        self.items = []

    def send(self, notification) -> None:
        self.items.append(notification)


class History:
    """Closed daily bars: 45 rising sessions for INFY, none for anything else."""

    def fetch(self, instrument, now):
        if instrument.symbol != "INFY":
            return ()
        start = now - timedelta(days=80)
        return tuple(
            Candle(instrument, start + timedelta(days=i), D(100 + i), D(101 + i), D(99 + i), D(100 + i), D(1000))
            for i in range(45)
        )


def build_daemon(tmp_path, clock, *, plan_dir, broker=None, events=()):
    broker = broker or PaperBrokerService(tmp_path / "ledger.db", starting_capital=D(100000), slippage_bps=D(0))
    feed = IndiaSandboxMarketDataFeed()
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=SwarmPaperTradingService(broker=broker), history=History(),
    )
    scheduler = AutonomousCadenceScheduler(pipeline, calendar=MarketCalendar(holidays=default_holidays()))
    tracker = PortfolioTracker(broker, feed, tenant_id=TENANT)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D("0.82"), D("0.20"), expected_edge=D("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    channel = CaptureChannel()
    daemon = AutonomousTradingDaemon(
        scheduler, tracker, INFY, plan, quantity=10, country=country_for(INFY), tenant_id=TENANT,
        notifications=TradingNotificationDispatcher((channel,)), idle_sleep_seconds=0.01, clock=clock,
        instruments=(INFY, TCS, NIFTY), session_plan_dir=plan_dir,
        event_calendar=EventCalendar(tuple(events)) if events else None,
    )
    return daemon, broker, channel


def plans(channel):
    return [item for item in channel.items if item.code is TradingAlertCode.SESSION_PLAN_READY]


def test_the_daemon_writes_one_plan_per_trading_day_from_0830_ist_and_reads_it_out(tmp_path) -> None:
    plan_dir = tmp_path / "session-plans"
    daemon, broker, channel = build_daemon(
        tmp_path, lambda: utc(3), plan_dir=plan_dir,
        events=(ScheduledEvent(TODAY, TODAY, "earnings", "TCS"),),
    )
    # Yesterday's session in the journal, so the brief has numbers to quote.
    for index, forward in enumerate(("0.01", "-0.01", "0.02")):
        journal.insert_decision(broker, {
            "decision_id": f"y{index}", "tenant_id": TENANT, "symbol": "INFY", "market": "INDIA", "asset_class": "EQUITY",
            "decided_at": utc(5, 10 * index, day=21).isoformat(), "stance": "NEUTRAL", "side": None, "confidence": "0.5",
            "expected_return": "0", "expected_risk": "0", "reference_price": "1500", "stop_price": None,
            "take_profit_price": None, "regime": "ranging", "mode": "deterministic",
            "governance": journal.GOVERNANCE_ABSTAINED, "reason": "floor", "order_id": None, "agents": "{}",
            "forward_return_60m": forward,
        })
    asyncio.run(daemon.run_once(utc(2, 50)))          # 08:20 IST: too early
    assert not plan_dir.exists() or not list(plan_dir.glob("*.json"))
    asyncio.run(daemon.run_once(utc(3, 5)))           # 08:35 IST: the plan is built
    written = json.loads((plan_dir / "2026-09-22.json").read_text())
    assert written["late"] is False
    by_symbol = {item["symbol"]: item for item in written["names"]}
    assert by_symbol["INFY"]["regime"] == "trending_up" and by_symbol["INFY"]["role"] == "focus"
    assert by_symbol["TCS"]["regime"] == "insufficient_history" and by_symbol["TCS"]["role"] == "blackout"
    assert by_symbol["TCS"]["blackout"] == "event_blackout:earnings"
    assert by_symbol["NIFTY 50"]["role"] == "watch"
    assert written["posture"] == "cautious" and written["focus"] == ["INFY"]
    assert written["yesterday"]["session_date"] == "2026-09-21" and written["yesterday"]["decisions"] == 3
    (note,) = plans(channel)
    assert "Atlas pre-open 2026-09-22: posture cautious, 1 focus, 0 stand-down, 1 blackout." in note.message
    assert "Focus: INFY trend_following" in note.message
    assert "Yesterday 2026-09-21: 3 decisions, 0 filled, 0 probes" in note.message
    assert note.metadata["posture"] == "cautious" and note.metadata["late"] == "false"
    # Later ticks the same day, and a restart, never write or say it twice.
    asyncio.run(daemon.run_once(utc(3, 45)))          # pre-market
    asyncio.run(daemon.run_once(utc(5)))              # regular hours
    assert len(plans(channel)) == 1
    restarted, _, fresh = build_daemon(tmp_path, lambda: utc(6), plan_dir=plan_dir, broker=broker)
    asyncio.run(restarted.run_once(utc(6)))
    assert plans(fresh) == [] and json.loads((plan_dir / "2026-09-22.json").read_text()) == written


def test_a_late_boot_during_regular_hours_builds_a_late_plan_and_nothing_after_the_close(tmp_path) -> None:
    plan_dir = tmp_path / "session-plans"
    daemon, _broker, channel = build_daemon(tmp_path, lambda: utc(5), plan_dir=plan_dir)
    asyncio.run(daemon.run_once(utc(5)))               # 10:30 IST, no plan yet
    written = json.loads((plan_dir / "2026-09-22.json").read_text())
    assert written["late"] is True
    (note,) = plans(channel)
    assert note.message.startswith("[PRAMANA] Atlas pre-open 2026-09-22 (late):") or "Atlas pre-open 2026-09-22 (late):" in note.message
    # A boot after the close plans nothing for the day that is over.
    evening_dir = tmp_path / "evening"
    evening, _b, quiet = build_daemon(tmp_path, lambda: utc(10, 30), plan_dir=evening_dir)
    asyncio.run(evening.run_once(utc(10, 30)))         # 16:00 IST post-market
    asyncio.run(evening.run_once(utc(12)))             # 17:30 IST closed
    assert plans(quiet) == [] and (not evening_dir.exists() or not list(evening_dir.glob("*.json")))


def test_no_plan_on_a_weekend_or_without_a_directory(tmp_path) -> None:
    plan_dir = tmp_path / "session-plans"
    saturday = datetime(2026, 9, 26, 3, 5, tzinfo=timezone.utc)
    daemon, _b, channel = build_daemon(tmp_path, lambda: saturday, plan_dir=plan_dir)
    asyncio.run(daemon.run_once(saturday))
    assert plans(channel) == [] and (not plan_dir.exists() or not list(plan_dir.glob("*.json")))
    off, _b2, quiet = build_daemon(tmp_path, lambda: utc(3, 5), plan_dir=None)
    asyncio.run(off.run_once(utc(3, 5)))
    assert plans(quiet) == []


def test_the_operator_command_prints_the_newest_plan(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("pilot_ops", Path(__file__).parents[1] / "scripts/pilot_ops.py")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    monkeypatch.delenv("PRAMANA_SESSION_PLAN_DIR", raising=False)
    database = tmp_path / "pramana.db"
    database.write_bytes(b"")
    assert ops.plan(database).startswith("no session plan written under")
    plan_dir = tmp_path / "session-plans"
    plan = strategist.build_session_plan(tenant_id=TENANT, now=utc(3), session_date=TODAY, policy=AtlasPolicy(),
                                         names=(name("INFY", "trending_up"),))
    strategist.write_plan(strategist.plan_path(plan_dir, TODAY), plan)
    text = ops.plan(database)
    assert text.startswith("Atlas pre-open 2026-09-22: posture normal, 1 focus, 0 stand-down.")
    assert "INFY" in text and "trend_following" in text
    assert ops.plan(database, "2026-09-21").startswith("no session plan for 2026-09-21")
    assert PLAYBOOKS["trending_up"].name == "trend_following"
