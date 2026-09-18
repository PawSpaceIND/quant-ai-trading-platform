"""Evidence has to survive being checked.

Three claims are exercised here. A promotion review must recompute what it can recompute:
digests from the retained files, promotion figures from the retained ledger. ``resume``
must release an operator pause and refuse a fault. A reported statistic must match the
interval it was sampled at, and say nothing when the sample cannot support it.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_ai.analytics import decision_quality as quality
from quant_ai.analytics.decision_journal import COLUMNS, TABLE
from quant_ai.analytics.decision_journal import SCHEMA as JOURNAL_SCHEMA
from quant_ai.analytics.metrics import (
    MINIMUM_RATIO_OBSERVATIONS,
    annualisation_periods,
    maximum_drawdown,
    mean_return_significance,
    sharpe_ratio,
    summarize_performance,
)
from quant_ai.domain.models import Market
from quant_ai.execution.risk_state import SQLiteRiskStateStore
from quant_ai.execution.session import regular_session_length
from quant_ai.governance.derived_evidence import derive_strategy_evidence
from quant_ai.governance.pilot_review import (
    HUMAN_REVIEW_FLAGS,
    PINNED_DIGESTS,
    SINGLE_FILE_DIGESTS,
    review,
    sign_review,
)
from quant_ai.operations.evidence_log import append_record, read_records, verify_chain
from quant_ai.operations.pilot_gate import evidence_bundle_digest
from quant_ai.validation.trial_register import record_trials, register_summary

TENANT = "pilot"
REVISION = "a" * 40
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))
SESSIONS = 30
TRADES_PER_SESSION = 4

# The fixture ledger, hand-computed. 120 closed trades over 30 IST sessions: three wins of
# 100 and one loss of 50 per session, alternating between two named regimes that both end
# the window in profit.
EXPECTED_TRADES = SESSIONS * TRADES_PER_SESSION
EXPECTED_EXPECTANCY = Decimal("62.5")
EXPECTED_PROFIT_FACTOR = Decimal(6)
EXPECTED_REGIMES = 2
# Daily equity rises by 250 a session, peaks at 103750 and dips once to 96000.
EXPECTED_DRAWDOWN = (Decimal("103750.00") - Decimal("96000.00")) / Decimal("103750.00")


# --------------------------------------------------------------- ledger fixture


def _journal_rows() -> list[dict[str, object]]:
    rows = []
    start = datetime(2026, 1, 5, 10, 0, tzinfo=IST_OFFSET)
    for index in range(EXPECTED_TRADES):
        session = index // TRADES_PER_SESSION
        decided = start + timedelta(days=session, minutes=15 * (index % TRADES_PER_SESSION))
        pnl = Decimal("-50.00") if index % 4 == 3 else Decimal("100.00")
        rows.append(
            {
                "decision_id": f"decision-{index:04d}",
                "tenant_id": TENANT,
                "symbol": "RELIANCE",
                "market": "INDIA",
                "asset_class": "EQUITY",
                "decided_at": decided.isoformat(),
                "stance": "BUY",
                "side": "BUY",
                "confidence": "0.6",
                "regime": "trending_up" if index % 2 == 0 else "ranging",
                "mode": "deterministic",
                "governance": "filled",
                "order_id": f"order-{index:04d}",
                "agents": "{}",
                "forward_return_60m": "0.001" if index % 3 else "-0.0005",
                "realized_net_pnl": str(pnl),
                "realized_gross_pnl": str(pnl + Decimal("5.00")),
                "realized_fees": "5.00",
                "exit_trigger": "take_profit",
                "holding_minutes": 30,
            }
        )
    return rows


def _build_ledger(path: Path) -> None:
    """A ledger carrying exactly the two things the gate recomputes from: journal and equity."""
    connection = sqlite3.connect(str(path))
    connection.execute(JOURNAL_SCHEMA)
    placeholders = ", ".join("?" for _ in COLUMNS)
    connection.executemany(
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
        [tuple(row.get(column) for column in COLUMNS) for row in _journal_rows()],
    )
    connection.commit()
    connection.close()
    state = SQLiteRiskStateStore(path)
    for session in range(SESSIONS):
        equity = Decimal("96000.00") if session == 15 else Decimal(100000 + 250 * (session + 1))
        state.record_equity(TENANT, date(2026, 1, 5) + timedelta(days=session), equity)
    state.close()


def _evidence_directory(root: Path) -> dict[str, object]:
    """Write a complete retained-evidence bundle and return the artifact that pins it."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "holdout_evidence_sha256": ["holdout.json"],
        "forward_paper_evidence_sha256": ["forward-paper.json"],
        "execution_stress_evidence_sha256": ["execution-stress.json"],
        "calibration_evidence_sha256": ["calibration.json"],
        "strategy_config_sha256": ["strategy-config.json"],
        "strategy_evidence_sha256": ["episodes.json", "episodes-index.json"],
        "ledger_evidence_sha256": ["retained-ledger.sqlite"],
        "trial_register_sha256": ["trial-register.jsonl"],
    }
    for name, members in files.items():
        for member in members:
            if member.endswith(".json"):
                (root / member).write_text(json.dumps({"evidence": name, "file": member}))
    _build_ledger(root / "retained-ledger.sqlite")
    record_trials(
        root / "trial-register.jsonl",
        study="paper-pilot",
        candidate_trials=4,
        configuration={"windows": [5, 10, 20, 50]},
        data_sha256="b" * 64,
        now=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    artifact = {
        "schema": "pramana.strategy.review.v1",
        "tenant_id": TENANT,
        "release_revision": REVISION,
        "strategy_id": "synthetic-test-only",
        "evidence_references": ["synthetic-unit-fixture"],
        "evidence_files": files,
        "holdout_reviewed": True,
        "costs_reviewed": True,
        "trial_register_reviewed": True,
        "ai_calibration_reviewed": True,
        "forward_paper_reviewed": True,
        "execution_stress_reviewed": True,
        "sample_trades": EXPECTED_TRADES,
        "expectancy": str(EXPECTED_EXPECTANCY),
        "max_drawdown": str(EXPECTED_DRAWDOWN),
        "profit_factor": str(EXPECTED_PROFIT_FACTOR),
        "profitable_regimes": EXPECTED_REGIMES,
        "paper_days": SESSIONS,
        # The statistical gate result the study published, now a promotion input rather
        # than a number the reviewer reads and discards.
        "deflated_sharpe": "0.97",
        "universe_verdict": "plausible",
    }
    for name, members in files.items():
        artifact[name] = evidence_bundle_digest(list(members), root)
    return artifact


def _fabricated_artifact() -> dict[str, object]:
    """The artifact that used to be approved: every digest a constant, pointing at nothing."""
    artifact = {
        "schema": "pramana.strategy.review.v1",
        "tenant_id": TENANT,
        "release_revision": REVISION,
        "strategy_id": "synthetic-test-only",
        "evidence_references": ["synthetic-unit-fixture"],
        "holdout_reviewed": True,
        "costs_reviewed": True,
        "trial_register_reviewed": True,
        "ai_calibration_reviewed": True,
        "forward_paper_reviewed": True,
        "execution_stress_reviewed": True,
        "sample_trades": 100,
        "expectancy": "1",
        "max_drawdown": "0.05",
        "profit_factor": "1.5",
        "profitable_regimes": 2,
        "paper_days": 30,
    }
    for name in (
        "holdout_evidence_sha256", "forward_paper_evidence_sha256",
        "execution_stress_evidence_sha256", "calibration_evidence_sha256",
        "strategy_config_sha256", "strategy_evidence_sha256",
        "ledger_evidence_sha256", "trial_register_sha256",
    ):
        artifact[name] = "a" * 64
    return artifact


# --------------------------------------------------------------- the gate


def test_fabricated_evidence_is_refused(tmp_path):
    """Digests of ``a`` * 64 over no files with typed numbers must not approve anything."""
    root = tmp_path / "review"
    root.mkdir()
    artifact = _fabricated_artifact()
    with pytest.raises(ValueError, match="list the retained files"):
        review(artifact, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)

    # The same artifact refuses end to end, signature path included.
    document = root / "review.json"
    document.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="list the retained files"):
        sign_review(document, gate="strategy", tenant=TENANT, revision=REVISION,
                    reviewer="QA fixture", secret="x" * 32)

    # Naming files that do not exist is refused too: a digest is only worth the bytes it
    # was taken over, and there are none.
    named = {**artifact, "evidence_files": {name: [f"{name}.json"] for name in
                                            ("holdout_evidence_sha256",)}}
    with pytest.raises(ValueError, match="unreadable or unsafe"):
        review(named, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)


def test_review_without_an_evidence_directory_is_refused(tmp_path):
    artifact = _evidence_directory(tmp_path / "review")
    with pytest.raises(ValueError, match="retained evidence directory"):
        review(artifact, "strategy", tenant=TENANT, revision=REVISION)


def test_genuine_evidence_is_accepted_and_the_signature_covers_the_derived_figures(tmp_path):
    root = tmp_path / "review"
    artifact = _evidence_directory(root)

    derived = review(artifact, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)
    assert derived["sample_trades"] == EXPECTED_TRADES
    assert Decimal(derived["expectancy"]) == EXPECTED_EXPECTANCY
    assert Decimal(derived["profit_factor"]) == EXPECTED_PROFIT_FACTOR
    assert Decimal(derived["max_drawdown"]) == EXPECTED_DRAWDOWN
    assert derived["profitable_regimes"] == EXPECTED_REGIMES
    assert derived["paper_days"] == SESSIONS
    assert derived["candidate_trials"] == 4
    assert derived["multiple_testing_correction"] == "none"

    document = root / "review.json"
    document.write_text(json.dumps(artifact))
    signed = sign_review(document, gate="strategy", tenant=TENANT, revision=REVISION,
                         reviewer="QA fixture", secret="x" * 32,
                         now=datetime(2026, 3, 1, tzinfo=timezone.utc))
    payload = json.loads(signed["payload"])
    assert payload["derived"]["sample_trades"] == EXPECTED_TRADES
    assert len(signed["signature"]) == 64


def test_a_digest_that_does_not_describe_the_file_is_refused(tmp_path):
    root = tmp_path / "review"
    artifact = _evidence_directory(root)
    (root / "holdout.json").write_text(json.dumps({"evidence": "edited after review"}))
    with pytest.raises(ValueError, match="Recomputed digest for holdout_evidence_sha256"):
        review(artifact, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)


def test_derived_figures_beat_typed_ones_when_they_disagree(tmp_path):
    root = tmp_path / "review"
    artifact = _evidence_directory(root)

    # A ledger with four closed trades cannot be talked up to a hundred and twenty.
    thin = tmp_path / "thin"
    thin.mkdir()
    connection = sqlite3.connect(str(thin / "retained-ledger.sqlite"))
    connection.execute(JOURNAL_SCHEMA)
    placeholders = ", ".join("?" for _ in COLUMNS)
    connection.executemany(
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
        [tuple(row.get(column) for column in COLUMNS) for row in _journal_rows()[:4]],
    )
    connection.commit()
    connection.close()
    state = SQLiteRiskStateStore(thin / "retained-ledger.sqlite")
    state.record_equity(TENANT, date(2026, 1, 5), Decimal("100000.00"))
    state.record_equity(TENANT, date(2026, 1, 6), Decimal("100100.00"))
    state.close()
    (root / "retained-ledger.sqlite").write_bytes((thin / "retained-ledger.sqlite").read_bytes())
    thin_artifact = {
        **artifact,
        "ledger_evidence_sha256": evidence_bundle_digest(["retained-ledger.sqlite"], root),
    }
    with pytest.raises(ValueError, match="insufficient_trade_sample"):
        review(thin_artifact, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)


def test_typed_figures_that_disagree_with_a_passing_ledger_are_refused(tmp_path):
    root = tmp_path / "review"
    artifact = _evidence_directory(root)
    inflated = {**artifact, "expectancy": "900", "paper_days": 400}
    with pytest.raises(ValueError, match="disagree with the retained ledger"):
        review(inflated, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)


def test_an_empty_trial_register_refuses_the_review(tmp_path):
    root = tmp_path / "review"
    artifact = _evidence_directory(root)
    (root / "trial-register.jsonl").write_text("")
    empty = {
        **artifact,
        "trial_register_sha256": evidence_bundle_digest(["trial-register.jsonl"], root),
    }
    with pytest.raises(ValueError, match="no research trials"):
        review(empty, "strategy", tenant=TENANT, revision=REVISION, evidence_root=root)


def test_a_ledger_without_equity_history_cannot_supply_a_drawdown(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(str(ledger))
    connection.execute(JOURNAL_SCHEMA)
    placeholders = ", ".join("?" for _ in COLUMNS)
    connection.executemany(
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
        [tuple(row.get(column) for column in COLUMNS) for row in _journal_rows()],
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="maximum drawdown"):
        derive_strategy_evidence(ledger, tenant_id=TENANT)


def test_derivation_matches_the_hand_computed_fixture(tmp_path):
    root = tmp_path / "review"
    _evidence_directory(root)
    evidence = derive_strategy_evidence(root / "retained-ledger.sqlite", tenant_id=TENANT)
    assert evidence.sample_trades == EXPECTED_TRADES
    assert evidence.expectancy == EXPECTED_EXPECTANCY
    assert evidence.profit_factor == EXPECTED_PROFIT_FACTOR
    assert evidence.max_drawdown == EXPECTED_DRAWDOWN
    assert evidence.profitable_regimes == EXPECTED_REGIMES
    assert evidence.paper_days == SESSIONS


# --------------------------------------------------------------- resume


def _halted_ledger(tmp_path, monkeypatch, reason: str) -> Path:
    ledger = tmp_path / "ledger.sqlite"
    state = SQLiteRiskStateStore(ledger)
    state.set_kill_switch("ghost", True, reason)
    state.close()
    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("PRAMANA_TENANT_ID", "ghost")
    monkeypatch.setenv("PRAMANA_HALT_FILE", str(tmp_path / "PRAMANA_HALT"))
    return ledger


def test_resume_refuses_a_drawdown_halt_and_clears_it_only_on_the_record(
    tmp_path, monkeypatch, capsys
):
    from quant_ai.cli import main as cli_main

    ledger = _halted_ledger(tmp_path, monkeypatch, "portfolio_drawdown_limit")

    assert cli_main(["resume"]) == 2
    output = capsys.readouterr().out
    assert "refusing to clear fault halt" in output
    assert "portfolio_drawdown_limit" in output
    still = SQLiteRiskStateStore(ledger)
    assert still.kill_switch_state("ghost") == (True, "portfolio_drawdown_limit")
    still.close()

    assert cli_main(["resume", "--clear-fault-halt", "--operator", "QA fixture"]) == 0
    output = capsys.readouterr().out
    assert "fault halt cleared by operator override" in output
    assert "portfolio_drawdown_limit" in output
    cleared = SQLiteRiskStateStore(ledger)
    assert cleared.kill_switch_state("ghost") == (False, None)
    cleared.close()

    records = read_records(tmp_path / "halt-overrides.jsonl")
    assert verify_chain(records)
    assert len(records) == 1
    assert records[0]["event_type"] == "fault_halt_override"
    assert records[0]["payload"]["halt_reason"] == "portfolio_drawdown_limit"
    assert records[0]["payload"]["operator"] == "QA fixture"


@pytest.mark.parametrize(
    "reason",
    ["paper_ledger_reconciliation_failed", "protective_exit_failed", "cadence_halted"],
)
def test_resume_refuses_every_named_fault_halt(tmp_path, monkeypatch, reason):
    from quant_ai.cli import main as cli_main

    ledger = _halted_ledger(tmp_path, monkeypatch, reason)
    assert cli_main(["resume"]) == 2
    still = SQLiteRiskStateStore(ledger)
    assert still.kill_switch_state("ghost") == (True, reason)
    still.close()


def test_resume_still_releases_an_operator_halt_without_a_flag(tmp_path, monkeypatch):
    from quant_ai.cli import main as cli_main

    ledger = _halted_ledger(tmp_path, monkeypatch, "operator_halt_file: founder review")
    marker = tmp_path / "PRAMANA_HALT"
    marker.write_text("founder review", encoding="utf-8")
    assert cli_main(["resume"]) == 0
    assert not marker.exists()
    released = SQLiteRiskStateStore(ledger)
    assert released.kill_switch_state("ghost") == (False, None)
    released.close()
    assert read_records(tmp_path / "halt-overrides.jsonl") == []


# --------------------------------------------------------------- statistics

# 40 observations: 21 gains of a tenth of a percent and 19 losses of the same size.
SERIES = tuple([Decimal("0.001")] * 21 + [Decimal("-0.001")] * 19)
SERIES_MEAN = (21 * Decimal("0.001") + 19 * Decimal("-0.001")) / 40
SERIES_POPULATION_VARIANCE = (
    21 * (Decimal("0.001") - SERIES_MEAN) ** 2 + 19 * (Decimal("-0.001") - SERIES_MEAN) ** 2
) / 40
SERIES_SAMPLE_VARIANCE = (
    21 * (Decimal("0.001") - SERIES_MEAN) ** 2 + 19 * (Decimal("-0.001") - SERIES_MEAN) ** 2
) / 39
TOLERANCE = Decimal("0.000001")


def test_one_minute_returns_annualise_with_one_minute_periods():
    session = regular_session_length(Market.INDIA)
    assert session == timedelta(minutes=375)
    periods = annualisation_periods(timedelta(minutes=1), session)
    assert periods == 375 * 252

    expected = float(SERIES_MEAN) / math.sqrt(float(SERIES_POPULATION_VARIANCE)) * math.sqrt(periods)
    assert abs(sharpe_ratio(SERIES, periods=periods) - Decimal(str(expected))) < TOLERANCE

    # The old daily default understates the interval by exactly the bars in a session.
    daily = sharpe_ratio(SERIES, periods=252)
    assert abs(sharpe_ratio(SERIES, periods=periods) / daily - Decimal(str(math.sqrt(375)))) < TOLERANCE


def test_sharpe_refuses_a_sample_that_cannot_support_one():
    thin = SERIES[: MINIMUM_RATIO_OBSERVATIONS - 1]
    assert len(thin) == 29
    assert sharpe_ratio(thin, periods=375 * 252) is None
    assert summarize_performance(thin, (Decimal(100),), thin).sharpe is None
    assert summarize_performance(thin, (Decimal(100),), thin).sortino is None
    assert sharpe_ratio(SERIES, periods=0) is None


def test_t_statistic_matches_the_hand_computed_value():
    result = mean_return_significance(SERIES)
    assert result is not None
    assert result.observations == 40
    assert result.multiple_testing_correction == "none"
    expected = float(SERIES_MEAN) / (math.sqrt(float(SERIES_SAMPLE_VARIANCE)) / math.sqrt(40))
    assert abs(result.t_statistic - Decimal(str(expected))) < TOLERANCE
    assert mean_return_significance(SERIES[:29]) is None
    assert mean_return_significance(tuple([Decimal("0.001")] * 40)) is None


def test_the_quality_report_carries_a_t_statistic_and_says_it_is_uncorrected(tmp_path):
    rows = _journal_rows()
    section = quality.significance(rows)
    assert section["multiple_testing_correction"] == "none"
    assert section["forward_return_60m"]["observations"] == len(rows)
    assert section["trade_net_pnl"]["observations"] == len(rows)
    assert section["trade_net_pnl"]["t_statistic"] is not None

    report = quality.summarize(
        rows,
        tenant_id=TENANT,
        now=datetime(2026, 3, 1, tzinfo=timezone.utc),
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert report["significance"]["trade_net_pnl"]["observations"] == len(rows)
    assert any("not corrected for multiple testing" in note for note in report["limitations"])
    # The dashboard's required keys are all still present and unrenamed.
    assert set(report["trades"]) >= {"closed", "win_rate", "expectancy", "profit_factor", "exits"}


def test_equity_drawdown_uses_the_recorded_marks(tmp_path):
    curve = (Decimal("100000.00"), Decimal("103750.00"), Decimal("96000.00"))
    assert maximum_drawdown(curve) == EXPECTED_DRAWDOWN


# --------------------------------------------------------------- trial register


def test_the_trial_register_records_every_window_and_detects_tampering(tmp_path):
    register = tmp_path / "trial-register.jsonl"
    record_trials(register, study="sweep", candidate_trials=4,
                  configuration={"window": "2025-H1"}, data_sha256="c" * 64)
    first = register_summary(register, study="sweep")
    assert first["runs"] == 1
    assert first["candidate_trials"] == 4

    record_trials(register, study="sweep", candidate_trials=4,
                  configuration={"window": "2025-H2"}, data_sha256="d" * 64)
    second = register_summary(register, study="sweep")
    assert second["runs"] == 2
    assert second["candidate_trials"] == 8
    assert second["distinct_datasets"] == 2

    lines = register.read_text(encoding="utf-8").splitlines()
    register.write_text(lines[0].replace('"candidate_trials":4', '"candidate_trials":1') + "\n")
    with pytest.raises(ValueError, match="chain is broken"):
        register_summary(register)
    with pytest.raises(ValueError, match="chain is broken"):
        record_trials(register, study="sweep", candidate_trials=1,
                      configuration={}, data_sha256="e" * 64)


def test_a_trial_needs_a_study_a_candidate_and_a_dataset(tmp_path):
    register = tmp_path / "trial-register.jsonl"
    with pytest.raises(ValueError, match="study name"):
        record_trials(register, study="  ", candidate_trials=1, configuration={},
                      data_sha256="c" * 64)
    with pytest.raises(ValueError, match="at least one candidate"):
        record_trials(register, study="sweep", candidate_trials=0, configuration={},
                      data_sha256="c" * 64)
    with pytest.raises(ValueError, match="pin the sha256"):
        record_trials(register, study="sweep", candidate_trials=1, configuration={},
                      data_sha256="not-a-digest")


def test_the_evidence_log_chains_and_refuses_a_naive_timestamp(tmp_path):
    log = tmp_path / "log.jsonl"
    first = append_record(log, "example", {"value": 1})
    second = append_record(log, "example", {"value": 2})
    assert second["previous_sha256"] == first["sha256"]
    assert verify_chain(read_records(log))
    with pytest.raises(ValueError, match="timezone-aware"):
        append_record(log, "example", {"value": 3}, now=datetime.fromisoformat("2026-01-01T00:00:00"))


def test_backtest_registers_every_window_it_is_run_over(tmp_path, monkeypatch, capsys):
    """Two runs over different windows leave two records, and the tearsheet says so."""
    from quant_ai.cli import main as cli_main

    data = tmp_path / "bars.csv"
    rows = ["timestamp,open,high,low,close,volume"]
    start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    for index in range(80):
        moment = start + timedelta(minutes=index)
        price = 100 + (index % 7)
        rows.append(
            f"{moment.isoformat()},{price},{price + 1},{price - 1},{price + Decimal('0.5')},1000"
        )
    data.write_text("\n".join(rows) + "\n")

    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("PRAMANA_PROOF_DIR", str(tmp_path / "proofs"))
    monkeypatch.setenv("PRAMANA_TENANT_ID", "backtest")

    # The CSV declares no instrument, so the market it is scored in has to be stated.
    assert cli_main(["backtest", "--data", str(data), "--market", "us"]) == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["registered_runs"] == 1
    assert first["registered_candidate_trials"] == 1
    assert "not corrected for multiple testing" in first["significance_note"]

    assert cli_main(
        ["backtest", "--data", str(data), "--market", "us", "--start", "2026-01-05"]
    ) == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["registered_runs"] == 2
    assert second["registered_candidate_trials"] == 2

    records = read_records(tmp_path / "trial-register.jsonl")
    assert verify_chain(records)
    assert len(records) == 2


def test_the_research_experiment_registers_its_candidate_trials(tmp_path, monkeypatch, capsys):
    """Two runs over two windows are two register entries, and the report carries the total."""
    from quant_ai.validation.experiment import main as experiment_main

    register = tmp_path / "trial-register.jsonl"
    prices = [100.0 + (index % 11) + index * 0.01 for index in range(200)]

    def write_csv(name: str, series: list[float]) -> Path:
        target = tmp_path / name
        rows = ["timestamp,close"]
        moment = datetime(2026, 1, 5, tzinfo=timezone.utc)
        for index, price in enumerate(series):
            rows.append(f"{(moment + timedelta(days=index)).isoformat()},{price}")
        target.write_text("\n".join(rows) + "\n")
        return target

    for run, (name, series) in enumerate(
        (("first.csv", prices), ("second.csv", prices[::-1])), start=1
    ):
        source = write_csv(name, series)
        monkeypatch.setattr(
            "sys.argv",
            ["experiment", "--data", str(source), "--output", str(tmp_path / f"report-{run}.json"),
             "--trial-register", str(register)],
        )
        experiment_main()
        printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert printed["promotion_approved"] is False
        report = json.loads((tmp_path / f"report-{run}.json").read_text())
        assert report["trial_register"]["runs"] == run
        assert report["trial_register"]["candidate_trials"] == report["candidate_trials"] * run
        assert printed["cumulative_candidate_trials"] == report["candidate_trials"] * run

    assert len(read_records(register)) == 2
    assert verify_chain(read_records(register))


def test_the_shipped_review_template_names_every_field_the_gate_demands():
    """An operator fills in the example; the example must not omit a required field.

    The gate refuses a review that leaves out a pinned digest or the listing of files
    behind it. A template missing those keys sends the operator to a refusal they cannot
    diagnose from the document in front of them, so the template is checked here rather
    than discovered during a promotion review.
    """
    template = json.loads(
        (Path(__file__).resolve().parents[1] / "deploy/review-strategy.example.json").read_text()
    )
    for name in PINNED_DIGESTS:
        assert name in template, f"template omits the pinned digest {name}"
        assert template["evidence_files"].get(name), f"template omits the files behind {name}"
    for name in SINGLE_FILE_DIGESTS:
        assert len(template["evidence_files"][name]) == 1, f"{name} pins exactly one file"
    assert set(template["evidence_files"]) == set(PINNED_DIGESTS)
    for flag in HUMAN_REVIEW_FLAGS:
        assert template[flag] is False, "a template must not pre-tick a human review flag"
