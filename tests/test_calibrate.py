"""A calibration score describes one mapping, so it is computed only over rows one mapping made.

Until #235 the LLM overlay rewrote the forecast from the model's own stance and confidence,
under the SAME basis id as the deterministic mapping, whenever the model answered. The
paper host ran that code until 22 September 2026. A Brier score over every row labelled
``weighted_lean_times_confidence.v1`` is therefore the calibration of a blend no code path
ever ran - and the label cannot say which rows are which, because the label is exactly
what the overlay kept.

The journal now records the two inputs the v1 mapping reads, so a stored probability is
recomputed rather than trusted. These tests pin the five outcomes of that check, that a
proven mix fails the whole report, and that the rest of the report - the drawdown limit,
the payoffs, the operator command - measures what the engine actually ran under.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant_ai.agents.forecast import BASIS, probability_up
from quant_ai.analytics import calibrate as calibration
from quant_ai.analytics.decision_journal import consensus_of
from quant_ai.domain.models import RiskMode
from quant_ai.governance.directives import FounderDirectives

ROOT = Path(__file__).resolve().parents[1]


def row(index: int = 0, *, mode: str = "deterministic", p: str = "0.7000", ws: str | None = "1.0000",
        conf: str | None = "0.8000", forward: str | None = "0.0200", basis: str = BASIS,
        filled: bool = False, drift: str | None = None) -> dict:
    return {
        # Every NOT NULL column the journal declares. The insert is OR IGNORE - there for
        # idempotency on decision_id - and SQLite applies that to a NOT NULL violation too,
        # so a fixture missing one of these is dropped without an error rather than refused.
        "decision_id": f"d{index}", "tenant_id": "ghost", "symbol": "INDIGO", "mode": mode,
        "market": "INDIA", "asset_class": "EQUITY", "agents": "{}",
        "decided_at": f"2026-09-{15 + index // 400:02d}T05:{index % 60:02d}:00+00:00",
        "stance": "BUY" if filled else "NEUTRAL", "side": "BUY" if filled else None,
        "governance": "filled" if filled else "held",
        "realized_net_pnl": "125.00" if filled else None, "drift_bps": drift,
        "forecast_probability_up": p, "forecast_basis": basis, "forecast_horizon_seconds": 3600,
        "forecast_cost_bps": "10", "forward_return_60m": forward,
        "expected_return": "0.01", "expected_risk": "0.02",
        "consensus_weighted_score": ws, "consensus_confidence": conf,
        "playbook": "range_trading", "regime": "ranging",
    }


def calibrated_book(count: int = 240) -> list[dict]:
    """A book one mapping provably made, that beats the coin and made money on its fills.

    p=0.70 is v1 at a lean of 1.0 and a conviction of 0.8; p=0.30 is the mirror. Seven in
    ten resolve the way they leaned, so the forecast is honest and better than a coin.
    """
    rows = []
    for index in range(count):
        up = index % 10 < 7
        rows.append(row(index, p="0.7000" if up else "0.3000", ws="1.0000" if up else "-1.0000",
                        forward="0.0200" if up else "-0.0200", filled=up, drift="12.0" if up else None))
    return rows


# ------------------------------------------------------------------ the five outcomes


def test_a_probability_its_own_inputs_reproduce_is_verified() -> None:
    assert calibration.integrity_of(row()) == calibration.VERIFIED


def test_the_pre_235_overlay_fingerprint_is_caught_as_contamination() -> None:
    """The row this check exists for, built the way the old overlay actually wrote it.

    The specialists leaned 0.347 at 0.50, which v1 maps to 0.5434. The model answered
    STRONG_BUY at 0.99, and the overlay stored v1 applied to the MODEL's stance instead:
    STANCE_SCORE 2 at 0.99, clamped to 0.95 - under the same basis id.
    """
    honest = probability_up(Decimal("0.3470"), Decimal("0.5000"))
    rewritten = probability_up(Decimal(2), Decimal("0.99"))
    assert (honest, rewritten) == (Decimal("0.5434"), Decimal("0.9500"))
    fingerprint = row(mode="llm", p=str(rewritten), ws="0.3470", conf="0.5000")
    assert calibration.integrity_of(fingerprint) == calibration.CONTAMINATED


def test_a_row_from_a_single_producer_mode_is_clean_by_path() -> None:
    """Only the deterministic mapping ever wrote on these modes, before and after #235."""
    for mode in ("deterministic", "llm_invalid_schema"):
        assert calibration.integrity_of(row(mode=mode, ws=None, conf=None)) == calibration.BY_PATH


def test_a_model_answered_row_without_inputs_is_unverifiable_not_assumed_clean() -> None:
    """It might be the overlay's; it might not. Nothing on the row can say, so it is not scored."""
    for mode in ("llm", "llm_unavailable", "llm_budget_exhausted", "unverified_inference", None):
        assert calibration.integrity_of(row(mode=mode, ws=None, conf=None)) == calibration.UNVERIFIABLE


def test_a_basis_this_build_cannot_reproduce_is_unregistered_not_scored_against_v1() -> None:
    assert calibration.integrity_of(row(basis="refit.v2")) == calibration.UNREGISTERED


def test_a_row_without_a_forecast_is_not_part_of_the_check() -> None:
    assert calibration.integrity_of(row(p=None)) is None
    assert calibration.integrity_of("not a row") is None


def test_honest_rounding_is_one_quantum_and_two_is_a_second_mapping() -> None:
    """The journal holds four-place inputs. Measured worst case: exactly one step of output.

    A tolerance tighter than that would call rounding a contamination; one looser would
    let a second mapping that happens to land close slip through.
    """
    assert calibration.integrity_of(row(p="0.7001")) == calibration.VERIFIED
    assert calibration.integrity_of(row(p="0.6999")) == calibration.VERIFIED
    assert calibration.integrity_of(row(p="0.7002")) == calibration.CONTAMINATED


# ------------------------------------------------------------------ the report


def test_a_proven_mix_fails_the_report_even_when_the_clean_rows_would_pass() -> None:
    """Dropping the contaminated rows and passing the rest would certify an id that meant two things."""
    book = calibrated_book()
    clean = calibration.calibrate(book, realised_max_drawdown="0.02", policy_max_drawdown="0.10")
    assert clean["verdict"] == "pass" and clean["promotion_authorized"] is True
    mixed = calibration.calibrate(book + [row(999, mode="llm", p="0.9500", ws="0.3470", conf="0.5000")],
                                  realised_max_drawdown="0.02", policy_max_drawdown="0.10")
    assert mixed["verdict"] == "basis_mixed" and mixed["promotion_authorized"] is False
    assert mixed["integrity"][BASIS]["mixed"] is True
    # Refusing is not the same as blank: the clean figures are still reported.
    assert mixed["promotion"]["verdict"] == "pass"


def test_unverifiable_rows_are_excluded_and_counted_never_blended() -> None:
    """They cannot fail the report - they prove nothing - but they must not be scored."""
    book = calibrated_book()
    # Unverifiable rows that would wreck the Brier score if they were blended in.
    noise = [row(1000 + index, mode="llm", p="0.9500", ws=None, conf=None, forward="-0.0200")
             for index in range(200)]
    report = calibration.calibrate(book + noise, realised_max_drawdown="0.02", policy_max_drawdown="0.10")
    assert report["excluded"][calibration.UNVERIFIABLE] == 200
    assert report["scored_rows"] == len(book)
    assert report["promotion"]["brier_score"] == calibration.calibrate(
        book, realised_max_drawdown="0.02", policy_max_drawdown="0.10")["promotion"]["brier_score"]
    assert report["verdict"] == "pass"


def test_a_thin_clean_sample_never_passes() -> None:
    report = calibration.calibrate(calibrated_book(150), realised_max_drawdown="0.02",
                                   policy_max_drawdown="0.10")
    assert report["verdict"] == "insufficient_sample" and report["promotion_authorized"] is False


def test_the_report_does_not_edit_the_rows_it_reads() -> None:
    import json
    book = calibrated_book(40)
    before = json.dumps(book, sort_keys=True)
    calibration.calibrate(book)
    assert json.dumps(book, sort_keys=True) == before


# ------------------------------------------------------------------ drawdown


def test_the_drawdown_limit_is_the_tighter_of_the_plan_and_the_baseline() -> None:
    """The warden's own rule. The 0.10 baseline alone is the loosest limit the book can have."""
    limits = {mode: calibration.live_drawdown_limit(FounderDirectives(risk_mode=mode)) for mode in RiskMode}
    assert limits[RiskMode.CONSERVATIVE] == Decimal("0.06")   # the plan is tighter
    assert limits[RiskMode.BALANCED] == Decimal("0.10")
    assert limits[RiskMode.AGGRESSIVE] == Decimal("0.10")     # the baseline caps it
    assert calibration.live_drawdown_limit() == Decimal("0.10")  # no directives: the default plan


def test_realised_drawdown_comes_from_the_ledgers_own_daily_equity(tmp_path) -> None:
    from quant_ai.execution.risk_state import SQLiteRiskStateStore
    store = SQLiteRiskStateStore(tmp_path / "ledger.sqlite")
    connection = store._connection
    # No mark at all refuses rather than reading as a book that never fell.
    assert calibration.realised_max_drawdown(connection, "ghost") is None
    # One session is already a curve: the table keeps that day's opening and its last mark.
    store.record_equity("ghost", date(2026, 9, 21), Decimal(100000))
    assert calibration.realised_max_drawdown(connection, "ghost") == Decimal(0)
    store.record_equity("ghost", date(2026, 9, 22), Decimal(97000))
    assert calibration.realised_max_drawdown(connection, "ghost") == Decimal("0.03")
    assert calibration.realised_max_drawdown(connection, "other-tenant") is None


# ------------------------------------------------------------------ the recorded inputs


def test_the_inputs_are_carried_through_as_the_decision_wrote_them() -> None:
    """Rebuilt at journal time they would test the arithmetic against itself."""
    proposal = SimpleNamespace(provenance={"consensus": {"weighted_score": "0.3470",
                                                          "average_confidence": "0.5000"}})
    assert consensus_of(proposal) == {"consensus_weighted_score": "0.3470",
                                      "consensus_confidence": "0.5000"}
    for absent in (SimpleNamespace(provenance=None), SimpleNamespace(provenance={}),
                   SimpleNamespace(provenance={"consensus": "x"})):
        assert consensus_of(absent) == {"consensus_weighted_score": None, "consensus_confidence": None}


def test_a_real_atlas_decision_journals_inputs_that_reproduce_its_forecast() -> None:
    """The whole write path, on both the deterministic and the model-answered path.

    The model says STRONG_BUY at 0.99. Before #235 that would have been stored as 0.95.
    Now the stored forecast is the specialists' own, and its recorded inputs prove it.
    """
    import asyncio
    from datetime import datetime, timezone

    from test_exploration_budget import TREND, consensus_client

    from quant_ai.agents.atlas import AtlasInvestmentAgent
    from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance

    moment = datetime(2026, 9, 22, 5, tzinfo=timezone.utc)
    book = tuple(AgentEvidence(agent, domain, "TRENT", stance, Decimal(conf), Decimal("0.01"),
                               Decimal("0.02"), ("x",), moment, 10)
                 for agent, domain, stance, conf in (
                     ("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY, "0.62"),
                     ("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY, "0.57"),
                     ("indian-equities", AgentDomain.COUNTRY, Stance.SELL, "0.49"),
                     ("macro-strategist", AgentDomain.MACRO, Stance.NEUTRAL, "0.44")))
    decisions = (
        AtlasInvestmentAgent().decide("TRENT", book, moment, evidence_context=TREND),
        asyncio.run(AtlasInvestmentAgent(llm_client=consensus_client("STRONG_BUY", 0.99))
                    .decide_with_llm("TRENT", book, moment, evidence_context=TREND)),
    )
    for decision in decisions:
        journaled = {
            **consensus_of(SimpleNamespace(provenance=decision.provenance)),
            "mode": decision.provenance.get("mode"),
            "forecast_probability_up": decision.provenance["forecast"]["probability_up"],
            "forecast_basis": decision.provenance["forecast"]["basis"],
        }
        assert calibration.integrity_of(journaled) == calibration.VERIFIED
    assert decisions[1].provenance["mode"] == "llm"


# ------------------------------------------------------------------ the operator command


def _ledger_with_history(path: Path) -> None:
    from quant_ai.analytics import decision_journal as journal
    from quant_ai.execution.risk_state import SQLiteRiskStateStore

    store = SQLiteRiskStateStore(path)
    store.record_equity("ghost", date(2026, 9, 21), Decimal(100000))
    store.record_equity("ghost", date(2026, 9, 22), Decimal(98800))
    broker = SimpleNamespace(_lock=store._lock, _connection=store._connection)
    for item in calibrated_book(60) + [row(5000, mode="llm", ws=None, conf=None)]:
        assert journal.insert_decision(broker, item), "fixture row was silently dropped"


def _run(*arguments: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PRAMANA_FOUNDER_DIRECTIVES")}
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment.update(env_overrides or {})
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "pilot_ops.py"), *arguments],
                          capture_output=True, text=True, env=environment, check=False)


def test_the_operator_command_reads_a_live_ledger_read_only(tmp_path) -> None:
    """The way it is run on the host: through pilot_ops, against the book, mode=ro."""
    ledger = tmp_path / "pramana.db"
    _ledger_with_history(ledger)
    before = ledger.read_bytes()
    result = _run("calibrate", "--database", str(ledger), "--tenant", "ghost")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert text.startswith("Forecast calibration  verdict=insufficient_sample")
    assert "verified=60" in text and "unverifiable=1" in text
    assert "realised max drawdown            0.012" in text
    assert "drawdown limit (live)            0.10" in text
    # Read-only is a property, not a promise: the ledger's bytes did not move.
    assert ledger.read_bytes() == before


def test_the_command_grades_against_the_limit_the_directives_set(tmp_path) -> None:
    ledger = tmp_path / "pramana.db"
    _ledger_with_history(ledger)
    result = _run("calibrate", "--database", str(ledger), "--tenant", "ghost",
                  env_overrides={"PRAMANA_FOUNDER_DIRECTIVES_JSON": '{"risk_mode": "CONSERVATIVE"}'})
    assert result.returncode == 0, result.stderr
    assert "drawdown limit (live)            0.06" in result.stdout


def test_unreadable_directives_report_the_limit_missing_rather_than_the_loosest(tmp_path) -> None:
    """Falling back to 0.10 would grade the book against a limit looser than its own."""
    ledger = tmp_path / "pramana.db"
    _ledger_with_history(ledger)
    result = _run("calibrate", "--database", str(ledger), "--tenant", "ghost",
                  env_overrides={"PRAMANA_FOUNDER_DIRECTIVES_FILE": str(tmp_path / "absent.json")})
    assert result.returncode == 0, result.stderr
    assert "drawdown limit (live)            -" in result.stdout
    assert "drawdown limit unavailable" in result.stdout


def test_the_payoff_artifact_is_written_privately_and_never_replaced(tmp_path) -> None:
    ledger = tmp_path / "pramana.db"
    _ledger_with_history(ledger)
    destination = tmp_path / "payoffs.json"
    first = _run("empirical-payoffs", "--database", str(ledger), "--tenant", "ghost",
                 "--destination", str(destination))
    assert first.returncode == 0, first.stderr
    assert "source=empirical:" in first.stdout and "declared 1% / 2%" in first.stdout
    assert destination.stat().st_mode & 0o777 == 0o600
    written = destination.read_bytes()
    again = _run("empirical-payoffs", "--database", str(ledger), "--tenant", "ghost",
                 "--destination", str(destination))
    assert again.returncode != 0 and destination.read_bytes() == written


def test_the_payoff_command_refuses_without_a_destination(tmp_path) -> None:
    ledger = tmp_path / "pramana.db"
    _ledger_with_history(ledger)
    result = _run("empirical-payoffs", "--database", str(ledger), "--tenant", "ghost")
    assert result.returncode != 0 and "requires --destination" in result.stderr


@pytest.mark.parametrize("action", ["calibrate"])
def test_a_ledger_that_never_journaled_is_reported_not_crashed(tmp_path, action) -> None:
    from quant_ai.execution.risk_state import SQLiteRiskStateStore
    ledger = tmp_path / "empty.db"
    SQLiteRiskStateStore(ledger)
    result = _run(action, "--database", str(ledger), "--tenant", "ghost")
    assert result.returncode == 0, result.stderr
    assert "no forecasts journaled" in result.stdout


def test_a_sample_below_the_scoring_floor_prints_no_skill() -> None:
    """The first host run printed a skill of -114.9 from two rows. That is an accident, not a finding.

    Two rows that moved the same way pin the base rate at its 0.95 clamp, so its Brier is
    0.0025 and any forecast looks catastrophically worse than it. The report still carries
    the numbers; the operator text shows only what the sample can support.
    """
    two = [row(0, p="0.7000", forward="0.0200"), row(1, p="0.7000", forward="0.0200")]
    text = calibration.render(calibration.calibrate(two))
    assert "(2 scored, below 30: no claim)" in text
    assert "skill vs base rate               -" in text
    assert "Brier, base rate                 -" in text
    # The bounded scores are still shown: a Brier cannot run away the way a ratio can.
    assert "Brier, coin (p=0.5)              0.25" in text

    enough = calibration.render(calibration.calibrate(calibrated_book(40)))
    assert "no claim" not in enough
    assert "skill vs base rate               -" not in enough
