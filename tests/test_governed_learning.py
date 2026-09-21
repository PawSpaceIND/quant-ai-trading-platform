"""Governed learning: weekly specialist skill weights and post-close post-mortems.

The specialists are scored on every decision the resolver marked, once per IST week, inside
a fixed band and above a fixed sample; the report is written before a weight applies and
names the decisions it came from. The session post-mortem is built by the engine after the
close and waits, pending, for the operator's approval. These tests pin both.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import post_mortem as review
from quant_ai.analytics import specialist_skill as skill
from quant_ai.analytics.attribution import AgentAttributionEngine
from quant_ai.analytics.specialist_skill import REWEIGHTING_ENV, reweighting_enabled
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
from quant_ai.notifications.trading import TradingAlertCode
from quant_ai.operations.premarket import premarket_checks
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from tests.test_premarket_check import env, manifest, payload

D = Decimal
TENANT = "learning"
# Monday 21 September 2026, 10:30 IST. The week began Sunday 18:30 UTC.
NOW = datetime(2026, 9, 21, 5, 0, tzinfo=timezone.utc)
WEEK = datetime(2026, 9, 20, 18, 30, tzinfo=timezone.utc)


def utc(day: int, hour: int, minute: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def row(decision_id: str, decided_at: datetime, *, forward: str | None, votes: dict[str, str],
        tenant: str = TENANT, symbol: str = "INFY", side: str | None = None) -> dict:
    return {
        "decision_id": decision_id, "tenant_id": tenant, "symbol": symbol, "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": decided_at.isoformat(), "stance": "BUY" if side else "NEUTRAL",
        "side": side, "confidence": "0.6", "expected_return": "0.01", "expected_risk": "0.02",
        "reference_price": "1500", "stop_price": None, "take_profit_price": None, "regime": "ranging",
        "mode": "deterministic", "governance": journal.GOVERNANCE_FILLED if side else journal.GOVERNANCE_ABSTAINED,
        "reason": None if side else "consensus_below_floor", "order_id": None,
        "agents": json.dumps({name: {"stance": stance, "confidence": "0.6"} for name, stance in votes.items()}),
        "forward_return_60m": forward,
    }


def broker_for(tmp_path) -> PaperBrokerService:
    return PaperBrokerService(tmp_path / "ledger.db", starting_capital=D(100000), slippage_bps=D(0))


def seed_week(broker, *, tenant: str = TENANT) -> None:
    """Five sessions (14-18 Sep) with 8 scored rows each: 40 votes per specialist.

    ``technical`` is right on 28 of 40 (0.70), ``macro`` on 12 of 40 (0.30), ``news`` votes
    NEUTRAL throughout, ``flat`` is right on 20 of 40 with the other 20 flat returns (misses).
    """
    index = 0
    for day in (14, 15, 16, 17, 18):
        for slot in range(8):
            index += 1
            technical_right = index % 10 not in (0, 3, 6)        # 28 of 40
            macro_right = index % 10 in (1, 4, 7)                # 12 of 40
            forward = "0.01" if technical_right else "-0.01"
            technical = "BUY"
            macro = "BUY" if (macro_right == technical_right) else "SELL"
            journal.insert_decision(broker, row(
                f"d-{day}-{slot}", utc(day, 4) + timedelta(minutes=10 * slot), forward=forward,
                votes={"technical": technical, "macro": macro, "news": "NEUTRAL"}, tenant=tenant,
            ))
            journal.insert_decision(broker, row(
                f"f-{day}-{slot}", utc(day, 6) + timedelta(minutes=10 * slot), forward="0" if slot % 2 else "0.02",
                votes={"flat": "BUY"}, tenant=tenant,
            ))


# ----------------------------------------------------------------- the report


def test_the_week_starts_monday_midnight_ist() -> None:
    assert skill.week_start(NOW).astimezone(timezone.utc) == WEEK
    assert skill.week_start(datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)).astimezone(timezone.utc) == WEEK - timedelta(days=7)
    assert skill.week_start(datetime(2026, 9, 27, 18, 29, tzinfo=timezone.utc)).astimezone(timezone.utc) == WEEK


@pytest.mark.parametrize("accuracy, weight", [("0", "0.7500"), ("0.30", "0.9000"), ("0.50", "1.0000"), ("0.70", "1.1000"), ("1", "1.2500")])
def test_the_weight_is_the_attribution_band(accuracy, weight) -> None:
    assert str(skill.skill_weight(D(accuracy))) == weight


def test_the_report_scores_every_specialist_and_weights_only_a_sufficient_sample(tmp_path) -> None:
    broker = broker_for(tmp_path)
    seed_week(broker)
    # A row this week, a row with no resolved return and another tenant's row are not counted.
    journal.insert_decision(broker, row("this-week", NOW - timedelta(hours=1), forward="0.05", votes={"technical": "BUY"}))
    journal.insert_decision(broker, row("unresolved", utc(18, 8), forward=None, votes={"technical": "BUY"}))
    journal.insert_decision(broker, row("other", utc(18, 8, 30), forward="0.05", votes={"technical": "BUY"}, tenant="other"))

    report = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    assert report["schema"] == "pramana.specialist_skill.v1"
    assert report["week_start"] == skill.week_start(NOW).isoformat()
    assert report["window"]["session_dates"] == ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    assert report["window"]["rows"] == 81  # 80 scored plus the unresolved one; "this-week" and "other" are outside
    by_agent = {item["agent_id"]: item for item in report["agents"]}
    assert by_agent["technical"] == {"agent_id": "technical", "evaluated": 40, "hits": 28,
                                     "directional_accuracy": 0.7, "weight": "1.1000", "applied": True}
    assert by_agent["macro"]["weight"] == "0.9000" and by_agent["macro"]["hits"] == 12
    assert by_agent["news"] == {"agent_id": "news", "evaluated": 0, "hits": 0,
                                "directional_accuracy": None, "weight": "1", "applied": False}
    assert by_agent["flat"]["evaluated"] == 40 and by_agent["flat"]["hits"] == 20  # a flat return is a miss
    assert report["applied"] == 3
    assert skill.weights_of(report) == {"technical": D("1.1000"), "macro": D("0.9000"), "flat": D("1.0000")}
    assert len(report["basis_sha256"]) == 64
    assert "decision_ids" not in by_agent["technical"]
    assert any("flat return is a miss" in line for line in report["limitations"])

    # The same week from any later tick is the same report: the window ends where the week began.
    later = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW + timedelta(days=3))
    assert later["basis_sha256"] == report["basis_sha256"] and later["agents"] == report["agents"]
    # A higher sample floor unweights everyone without changing the scores.
    strict = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW, minimum_sample=41)
    assert strict["applied"] == 0 and skill.weights_of(strict) == {}
    assert {i["agent_id"]: i["weight"] for i in strict["agents"]}["technical"] == "1"


def test_an_empty_journal_gives_an_empty_report(tmp_path) -> None:
    report = skill.build_skill_report(broker_for(tmp_path), tenant_id=TENANT, now=NOW)
    assert report["agents"] == [] and report["applied"] == 0 and report["window"]["session_dates"] == []
    # The week is named by its Monday in IST, the date the founder reads.
    assert skill.notification_message(report, applied=True) == "Specialist skill, week of 2026-09-21: 0 weighted."


def test_weights_of_ignores_malformed_or_out_of_band_entries() -> None:
    report = {"agents": [
        {"agent_id": "a", "weight": "1.10", "applied": True},
        {"agent_id": "b", "weight": "1.40", "applied": True},   # out of band
        {"agent_id": "c", "weight": "1.10", "applied": False},  # not applied
        {"agent_id": "d", "weight": "abc", "applied": True},
        {"agent_id": 5, "weight": "1.00", "applied": True},
        "junk",
    ]}
    assert skill.weights_of(report) == {"a": D("1.10")}


def test_the_weekly_note_lists_weighted_specialists_largest_first() -> None:
    report = {"week_start": "2026-09-21T00:00:00+05:30", "minimum_sample": 30, "agents": [
        {"agent_id": "macro", "evaluated": 40, "directional_accuracy": 0.3, "weight": "0.9000", "applied": True},
        {"agent_id": "technical", "evaluated": 40, "directional_accuracy": 0.7, "weight": "1.1000", "applied": True},
        {"agent_id": "news", "evaluated": 3, "directional_accuracy": 1.0, "weight": "1", "applied": False},
    ]}
    assert skill.notification_message(report, applied=True).split("\n") == [
        "Specialist skill, week of 2026-09-21: 2 weighted, 1 under 30 votes.",
        "technical x1.1000 (0.70 on 40)",
        "macro x0.9000 (0.30 on 40)",
    ]
    assert skill.notification_message(report, applied=False).startswith(
        "Specialist skill, week of 2026-09-21: 2 scored, weights not applied (re-weighting off)"
    )


# ----------------------------------------------------------------- the engine


def evidence(agent: str, confidence: str) -> AgentEvidence:
    return AgentEvidence(agent, AgentDomain.TECHNICAL, "INFY", Stance.BUY, D(confidence), D("0.01"),
                        D("0.02"), ("declared",), NOW, 10)


def test_skill_weights_scale_confidence_inside_the_band_and_are_written_into_the_rationale() -> None:
    engine = AgentAttributionEngine()
    engine.apply_skill({"technical": D("1.10"), "macro": D("0.90")}, basis="abc123")
    weighted = engine.weight_evidence((evidence("technical", "0.50"), evidence("macro", "0.50"), evidence("news", "0.50")))
    by_agent = {item.agent_id: item for item in weighted}
    assert by_agent["technical"].confidence == D("0.5500")
    assert by_agent["macro"].confidence == D("0.4500")
    assert by_agent["news"].confidence == D("0.50")
    assert "skill_weight=1.10:directional_accuracy" in by_agent["technical"].rationale
    assert "skill_basis_sha256=abc123" in by_agent["technical"].rationale
    assert "combined_weight=1.10" in by_agent["technical"].rationale
    assert "attribution_weight=1:unscored" in by_agent["technical"].rationale
    assert not any(item.startswith("skill_") for item in by_agent["news"].rationale)
    # Realised credit and skill compound, but never past the band.
    engine.record(("technical",), D(100))
    engine.record(("technical",), D(100))
    compounded = engine.weight_evidence((evidence("technical", "0.50"),))[0]
    assert "attribution_weight=1.25:blended" in compounded.rationale
    assert "combined_weight=1.25" in compounded.rationale
    assert compounded.confidence == D("0.6250")
    engine.clear_skill()
    assert engine.skill_weights == {} and engine.skill_basis is None
    with pytest.raises(ValueError, match="out of band"):
        engine.apply_skill({"technical": D("1.30")}, basis="x")


def test_a_journal_refresh_keeps_the_skill_weights(tmp_path) -> None:
    from quant_ai.analytics.attribution import restore_from_journal

    engine = AgentAttributionEngine()
    engine.apply_skill({"technical": D("1.10")}, basis="abc")
    restore_from_journal(engine, broker_for(tmp_path), tenant_id=TENANT, now=NOW)
    assert engine.skill_weights == {"technical": D("1.10")}


# ----------------------------------------------------------------- the daemon


class CaptureChannel:
    def __init__(self) -> None:
        self.items = []

    def send(self, notification) -> None:
        self.items.append(notification)


INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def build_daemon(tmp_path, clock, *, post_mortem_dir, reweighting=True, broker=None):
    broker = broker or broker_for(tmp_path)
    feed = IndiaSandboxMarketDataFeed()
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=SwarmPaperTradingService(broker=broker),
    )
    scheduler = AutonomousCadenceScheduler(pipeline, calendar=MarketCalendar(holidays=default_holidays()))
    tracker = PortfolioTracker(broker, feed, tenant_id=TENANT)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D("0.82"), D("0.20"), expected_edge=D("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    channel = CaptureChannel()
    daemon = AutonomousTradingDaemon(
        scheduler, tracker, INFY, plan, quantity=10, country=country_for(INFY), tenant_id=TENANT,
        notifications=TradingNotificationDispatcher((channel,)), idle_sleep_seconds=0.01,
        clock=clock, post_mortem_dir=post_mortem_dir, specialist_reweighting=reweighting,
    )
    daemon.decision_quality_report_path = tmp_path / "reports" / "decision-quality.json"
    return daemon, broker, channel


def notes(channel, code):
    return [item for item in channel.items if item.code is code]


def test_the_daemon_recomputes_the_weights_once_a_week_and_applies_them(tmp_path) -> None:
    daemon, broker, channel = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seed_week(broker)
    asyncio.run(daemon.run_once(NOW))
    engine = daemon.scheduler.pipeline.runtime.attribution
    assert engine.skill_weights == {"technical": D("1.1000"), "macro": D("0.9000"), "flat": D("1.0000")}
    written = json.loads((tmp_path / "reports" / "specialist-skill.json").read_text())
    assert written["schema"] == "pramana.specialist_skill.v1" and engine.skill_basis == written["basis_sha256"]
    quality = json.loads((tmp_path / "reports" / "decision-quality.json").read_text())
    assert quality["specialist_skill"]["applied"] == 3 and quality["specialist_skill"]["applied_to_engine"] is True
    (note,) = notes(channel, TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED)
    assert note.message.split("\n")[0].endswith("Specialist skill, week of 2026-09-21: 3 weighted, 1 under 30 votes.")
    assert "technical x1.1000 (0.70 on 40)" in note.message
    assert written["week_start"] == "2026-09-21T00:00:00+05:30"
    # Another tick the same week: same weights, no second note, no recomputation.
    asyncio.run(daemon.run_once(NOW + timedelta(minutes=10)))
    assert len(notes(channel, TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED)) == 1
    # A restart in the same week reproduces the weights and stays quiet: the marker holds.
    restarted, _, fresh = build_daemon(tmp_path, lambda: NOW + timedelta(days=2), post_mortem_dir=None, broker=broker)
    asyncio.run(restarted.run_once(NOW + timedelta(days=2)))
    assert restarted.scheduler.pipeline.runtime.attribution.skill_weights == engine.skill_weights
    assert notes(fresh, TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED) == []
    # The next week recomputes over a window that now includes this week's rows, and says so.
    next_week = NOW + timedelta(days=7)
    asyncio.run(restarted.run_once(next_week))
    assert restarted._skill_week == skill.week_start(next_week).isoformat()
    assert len(notes(fresh, TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED)) == 1


def test_re_weighting_off_writes_the_report_and_applies_nothing(tmp_path) -> None:
    daemon, broker, channel = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None, reweighting=False)
    seed_week(broker)
    asyncio.run(daemon.run_once(NOW))
    assert daemon.scheduler.pipeline.runtime.attribution.skill_weights == {}
    assert (tmp_path / "reports" / "specialist-skill.json").exists()
    quality = json.loads((tmp_path / "reports" / "decision-quality.json").read_text())
    assert quality["specialist_skill"]["applied_to_engine"] is False
    (note,) = notes(channel, TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED)
    assert "weights not applied (re-weighting off)" in note.message


def test_the_daemon_builds_the_post_mortem_after_the_close_and_asks_once_for_approval(tmp_path) -> None:
    reviews = tmp_path / "post-mortems"
    daemon, broker, channel = build_daemon(tmp_path, lambda: utc(21, 9), post_mortem_dir=reviews)
    # Today's session: decisions 13:30-14:15 IST (08:00-08:45 UTC); NSE closes 15:30 IST (10:00 UTC).
    for slot in range(4):
        journal.insert_decision(broker, row(f"today-{slot}", utc(21, 8, 15 * slot), forward="0.01" if slot % 2 else "-0.01",
                                            votes={"technical": "BUY"}, side="BUY" if slot == 0 else None))
    calendar = daemon.scheduler.calendar
    assert calendar.state(Market.INDIA, utc(21, 9, 50), exchange="NSE") == MarketState.REGULAR_HOURS
    assert calendar.state(Market.INDIA, utc(21, 10, 10), exchange="NSE") != MarketState.REGULAR_HOURS

    asyncio.run(daemon.run_once(utc(21, 9, 50)))      # open: nothing yet, and this tick journals a decision
    assert not reviews.exists() or not list(reviews.glob("*.json"))
    asyncio.run(daemon.run_once(utc(21, 10, 5)))      # closed, but only 15 minutes after that decision
    assert not list(reviews.glob("*.json")) if reviews.exists() else True
    # An hour after the last in-session decision. The post-close holds journaled by the
    # ticks above must not push the review back another hour.
    asyncio.run(daemon.run_once(utc(21, 10, 55)))
    assert (reviews / "2026-09-21.json").exists()
    written = json.loads((reviews / "2026-09-21.json").read_text())
    assert written["status"] == "pending" and written["session_date"] == "2026-09-21"
    (note,) = notes(channel, TradingAlertCode.POST_MORTEM_PENDING)
    assert f"Post-mortem 2026-09-21 written: {len(written['lessons'])} lesson" in note.message
    assert "pramana post-mortem --approve 2026-09-21" in note.message
    assert note.metadata["session_date"] == "2026-09-21"

    asyncio.run(daemon.run_once(utc(21, 11, 5)))
    assert len(notes(channel, TradingAlertCode.POST_MORTEM_PENDING)) == 1
    # A restart after the close sees the file and stays quiet; an approved file is never touched.
    review.approve_post_mortem(reviews, "2026-09-21", utc(21, 11))
    restarted, _, fresh = build_daemon(tmp_path, lambda: utc(21, 11, 30), post_mortem_dir=reviews, broker=broker)
    asyncio.run(restarted.run_once(utc(21, 11, 30)))
    assert notes(fresh, TradingAlertCode.POST_MORTEM_PENDING) == []
    assert json.loads((reviews / "2026-09-21.json").read_text())["status"] == "approved"


def test_the_post_mortem_waits_an_hour_after_the_last_decision(tmp_path) -> None:
    reviews = tmp_path / "post-mortems"
    daemon, broker, channel = build_daemon(tmp_path, lambda: utc(21, 10, 5), post_mortem_dir=reviews)
    journal.insert_decision(broker, row("late", utc(21, 9, 55), forward=None, votes={"technical": "BUY"}))
    asyncio.run(daemon.run_once(utc(21, 10, 5)))       # closed, 10 minutes after the last decision
    assert not list(reviews.glob("*.json")) if reviews.exists() else True
    asyncio.run(daemon.run_once(utc(21, 10, 56)))      # 61 minutes after
    assert (reviews / "2026-09-21.json").exists()
    assert len(notes(channel, TradingAlertCode.POST_MORTEM_PENDING)) == 1


def test_a_daemon_without_a_directory_or_decisions_builds_nothing(tmp_path) -> None:
    daemon, _broker, channel = build_daemon(tmp_path, lambda: utc(21, 10, 30), post_mortem_dir=None)
    asyncio.run(daemon.run_once(utc(21, 10, 30)))
    assert notes(channel, TradingAlertCode.POST_MORTEM_PENDING) == []
    reviews = tmp_path / "post-mortems"
    empty, _b, quiet = build_daemon(tmp_path, lambda: utc(21, 10, 30), post_mortem_dir=reviews)
    asyncio.run(empty.run_once(utc(21, 10, 30)))
    assert notes(quiet, TradingAlertCode.POST_MORTEM_PENDING) == []
    assert not reviews.exists() or not list(reviews.glob("*.json"))


# ----------------------------------------------------------------- switch and pre-market


def test_the_switch_reads_on_off_and_refuses_anything_else() -> None:
    assert reweighting_enabled({}) is True
    assert reweighting_enabled({REWEIGHTING_ENV: "off"}) is False
    assert reweighting_enabled({REWEIGHTING_ENV: "ON"}) is True
    with pytest.raises(RuntimeError, match="must be on or off"):
        reweighting_enabled({REWEIGHTING_ENV: "weekly"})


def test_the_premarket_check_reports_learning_as_information() -> None:
    line = next(c for c in premarket_checks(payload(), manifest(), env(), NOW) if c.id == "learning")
    assert line.state == "INFO"
    assert line.detail.startswith("specialist re-weighting on: weekly, last 10 sessions, >= 30 scored votes, band 0.75-1.25")
    assert "post-mortems built after the close, approval manual" in line.detail
    off = next(c for c in premarket_checks(payload(), manifest(), {**env(), REWEIGHTING_ENV: "off"}, NOW) if c.id == "learning")
    assert off.detail.startswith("specialist re-weighting off (report still written)")
    bad = next(c for c in premarket_checks(payload(), manifest(), {**env(), REWEIGHTING_ENV: "daily"}, NOW) if c.id == "learning")
    assert bad.state == "FAIL"
