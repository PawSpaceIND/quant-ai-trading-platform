"""The decision-quality report says how little it knows before it says anything else.

Three things the dashboard reads from it, each pinned here:

- the promotion gate: the verdict ``pilot_ops.py calibrate`` reaches, over the report's
  window. The page reads its edge rule only when this passes;
- why the book held: a hard hold, a silent roster, a deadlock or a one-sided lean the
  floor refused. #239's roster shapes, split from the holds that never weighed a lean;
- the sample behind every rate, so a hit rate over one decision is never shown as one.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from test_calibrate import calibrated_book
from test_calibrate import row as forecast_row
from test_governed_learning import NOW, build_daemon

from quant_ai.analytics import calibrate as calibration
from quant_ai.analytics import decision_quality as quality
from quant_ai.execution.daemon import AutonomousTradingDaemon

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 1, tzinfo=timezone.utc)


def roster(**votes: str) -> str:
    return json.dumps({agent: {"stance": stance, "confidence": "0.7"} for agent, stance in votes.items()})


def hold(decision_id: str, *, lean: str | None, agents: str, stance: str = "NEUTRAL") -> dict:
    return {"decision_id": decision_id, "stance": stance, "side": None, "governance": "abstained",
            "consensus_weighted_score": lean, "agents": agents}


# ------------------------------------------------------------------ why the book held


def test_holds_are_split_by_why_they_held() -> None:
    rows = [
        # A veto or stale evidence stopped it before any lean was weighed, whatever the
        # roster looked like. Never a conviction-floor hold, and never probed.
        hold("veto", lean=None, agents=roster(tech="BUY", macro="NEUTRAL")),
        hold("avoid", lean=None, agents=roster(risk="AVOID"), stance="AVOID"),
        hold("silent", lean="0.0000", agents=roster(tech="NEUTRAL", macro="NEUTRAL")),
        # 22 September, INDIGO: one BUY, one SELL, the rest neutral. A deadlock, not a floor.
        hold("split", lean="0.0500", agents=roster(geo="BUY", equities="SELL", tech="NEUTRAL")),
        hold("lean", lean="0.3704", agents=roster(tech="BUY", macro="NEUTRAL", flow="NEUTRAL")),
        hold("lean-down", lean="-0.3000", agents=roster(tech="SELL", macro="NEUTRAL")),
        # A consensus with no readable roster is unknown, not silent.
        hold("unread", lean="0.2000", agents="{}"),
        # Directional decisions, a probe among them, are not holds at all.
        {"decision_id": "probe", "stance": "BUY", "side": "BUY", "probe": "1",
         "consensus_weighted_score": "0.3704", "agents": roster(tech="BUY")},
        {"decision_id": "sell", "stance": "SELL", "side": "SELL",
         "consensus_weighted_score": "-0.6000", "agents": roster(tech="SELL")},
    ]
    assert quality.hold_causes(rows) == {
        "holds": 7, "hard_hold": 2, "silent": 1, "deadlock": 1, "conviction_floor": 2,
        "roster_unrecorded": 1,
    }


def test_an_empty_journal_has_no_holds_rather_than_no_answer() -> None:
    assert quality.hold_causes([]) == {
        "holds": 0, "hard_hold": 0, "silent": 0, "deadlock": 0, "conviction_floor": 0,
        "roster_unrecorded": 0,
    }


# ------------------------------------------------------------------ the promotion gate


def gate(rows, **drawdown):
    return quality.promotion_gate(rows, **drawdown)


def test_the_gate_is_the_calibrate_verdict_on_the_same_rows() -> None:
    book = calibrated_book()
    inputs = {"realised_max_drawdown": "0.01", "policy_max_drawdown": "0.10"}
    passed = gate(book, **inputs)
    assert passed["verdict"] == calibration.calibrate(book, **inputs)["verdict"] == "pass"
    assert passed["promotion_authorized"] is True
    assert passed["minimum_resolved"] == 200 and passed["resolved_forecast_count"] == 240
    assert passed["schema"] == "pramana.promotion_report.v1" and passed["missing_inputs"] == []


def test_a_missing_drawdown_refuses_the_gate_even_on_a_passing_book() -> None:
    refused = gate(calibrated_book())
    assert refused["verdict"] == "missing_inputs" and refused["promotion_authorized"] is False
    assert "drawdown_or_policy_unavailable" in refused["missing_inputs"]


def test_under_two_hundred_resolved_forecasts_the_gate_is_insufficient() -> None:
    thin = gate(calibrated_book(199), realised_max_drawdown="0.01", policy_max_drawdown="0.10")
    assert thin["verdict"] == "insufficient_sample" and thin["promotion_authorized"] is False
    assert thin["resolved_forecast_count"] == 199


def test_one_row_from_a_second_mapping_fails_the_gate_whatever_the_rest_scores() -> None:
    mixed = calibrated_book() + [forecast_row(999, mode="llm", p="0.9500", ws="0.3470", conf="0.5000")]
    failed = gate(mixed, realised_max_drawdown="0.01", policy_max_drawdown="0.10")
    assert failed["verdict"] == "basis_mixed" and failed["promotion_authorized"] is False


def test_the_report_carries_the_gate_and_the_hold_causes() -> None:
    report = quality.summarize(calibrated_book(), tenant_id="ghost", now=UNTIL, since=SINCE)
    assert report["promotion"]["verdict"] == "missing_inputs"
    assert report["holds"]["holds"] == 72  # three in ten leaned down and were held
    assert any("No edge is read below 200 clean resolved forecasts" in item
               for item in report["limitations"])
    passed = quality.summarize(calibrated_book(), tenant_id="ghost", now=UNTIL, since=SINCE,
                               realised_max_drawdown="0.01", policy_max_drawdown="0.10")
    assert passed["promotion"]["verdict"] == "pass"


# ------------------------------------------------------------------ the sample behind a rate


def test_every_grouped_hit_rate_carries_the_decisions_it_was_taken_over() -> None:
    rows = [
        {"decision_id": "a", "stance": "BUY", "regime": "ranging", "playbook": "range_trading",
         "decided_at": "2026-09-24T05:00:00+00:00", "forward_return_60m": "0.01"},
        {"decision_id": "b", "stance": "NEUTRAL", "regime": "ranging", "playbook": "range_trading",
         "decided_at": "2026-09-24T05:10:00+00:00", "forward_return_60m": "0.02"},
        {"decision_id": "c", "stance": "BUY", "regime": "ranging", "playbook": "range_trading",
         "decided_at": "2026-09-24T05:20:00+00:00", "forward_return_60m": None},
    ]
    (regime,) = quality.by_regime(rows)
    (playbook,) = quality.by_playbook(rows)
    # Three decisions and one evaluated: a hit rate of 1.0 over one call.
    assert (regime["decisions"], regime["evaluated"], regime["hit_rate"]) == (3, 1, 1.0)
    assert (playbook["decisions"], playbook["evaluated"]) == (3, 1)
    hours = {item["hour"]: item for item in quality.by_hour_ist(rows)}
    assert (hours[10]["decisions"], hours[10]["evaluated"]) == (3, 1)
    assert hours[9]["evaluated"] == 0 and hours[9]["hit_rate"] is None


# ------------------------------------------------------------------ the daemon's inputs


def stub(broker, max_drawdown):
    return SimpleNamespace(
        tracker=SimpleNamespace(broker=broker), tenant_id="ghost",
        plan=SimpleNamespace(max_drawdown_fraction=max_drawdown), _logger=logging.getLogger("test"),
    )


def test_the_daemon_hands_the_gate_the_breakers_own_limit(tmp_path) -> None:
    from quant_ai.execution.paper_ledger import PaperBrokerService

    broker = PaperBrokerService(tmp_path / "ledger.sqlite", starting_capital=Decimal(100000))
    # No equity history yet: the realised drawdown is unknown, not zero.
    assert AutonomousTradingDaemon._promotion_drawdown(stub(broker, Decimal("0.05"))) == (None, Decimal("0.05"))
    # The breaker never runs looser than 0.10, and neither does the gate.
    assert AutonomousTradingDaemon._promotion_drawdown(stub(broker, Decimal("0.20")))[1] == Decimal("0.10")


def test_an_unreadable_ledger_leaves_the_drawdown_missing_and_the_cadence_running(caplog) -> None:
    class Broken:
        _lock = None

    realised, limit = AutonomousTradingDaemon._promotion_drawdown(stub(Broken(), Decimal("0.05")))
    assert (realised, limit) == (None, Decimal("0.05"))
    assert "promotion_drawdown_unavailable" in caplog.text


def test_the_written_report_is_gated_on_the_drawdown_the_daemon_supplied(tmp_path, monkeypatch) -> None:
    daemon, _, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seen = {}
    real = quality.build_report

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(quality, "build_report", spy)
    asyncio.run(daemon.run_once(NOW))
    assert seen["policy_max_drawdown"] == min(daemon.plan.max_drawdown_fraction, Decimal(".10"))
    assert "realised_max_drawdown" in seen
    written = json.loads((tmp_path / "reports" / "decision-quality.json").read_text())
    assert written["promotion"]["promotion_authorized"] is False
    assert set(written["holds"]) == {"holds", "hard_hold", "silent", "deadlock", "conviction_floor",
                                     "roster_unrecorded"}


def test_the_session_post_mortem_states_the_sample_behind_its_hit_rate(tmp_path) -> None:
    from test_decision_journal import SESSION, TENANT, losing_session, pilot_broker

    from quant_ai.analytics import post_mortem as pm

    broker = pilot_broker(tmp_path)
    losing_session(broker)
    report = pm.build_post_mortem(broker, tenant_id=TENANT, now=SESSION + timedelta(hours=8))
    assert (report["summary"]["evaluated_60m"], report["summary"]["hit_rate_60m"]) == (8, 0.25)
    assert all("evaluated" in item for item in report["summary"]["by_regime"])
