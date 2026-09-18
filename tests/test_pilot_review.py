import json
from datetime import datetime, timezone

import pytest

from quant_ai.governance.pilot_review import review, sign_review


def test_baseline_cannot_approve_ai_strategy():
    with pytest.raises(ValueError, match="strategy-specific"):
        review({"tenant_id":"pilot","release_revision":"a"*40,"schema":"pramana.research.v1"},"strategy",tenant="pilot",revision="a"*40)


def test_review_is_release_bound_and_refuses_incomplete_recovery(tmp_path):
    artifact={"schema":"pramana.recovery.review.v1","tenant_id":"pilot","release_revision":"a"*40,"target_host":"qa","evidence_references":["isolated-fixture"]}
    with pytest.raises(ValueError, match="incomplete"):
        review(artifact,"recovery",tenant="pilot",revision="a"*40)
    artifact.update({k:True for k in ("clean_start","private_access","token_renewal","independent_alert_received","full_bundle_restored","rollback_verified")})
    source=tmp_path/'review.json';source.write_text(json.dumps(artifact))
    result=sign_review(source,gate="recovery",tenant="pilot",revision="a"*40,reviewer="QA fixture",secret="x"*32,now=datetime(2000,1,1,tzinfo=timezone.utc))
    assert len(result["signature"])==64
    assert json.loads(result["payload"])["expires_at"].startswith("2000-01-08")
    with pytest.raises(ValueError,match="exact release"):
        sign_review(source,gate="recovery",tenant="pilot",revision="b"*40,reviewer="QA",secret="x"*32)


def test_strategy_review_still_rejects_invalid_typed_metrics_and_never_approves_them_alone(tmp_path):
    """The typed figures are still validated - and on their own they no longer approve."""
    artifact={"schema":"pramana.strategy.review.v1","tenant_id":"pilot","release_revision":"a"*40,"strategy_id":"synthetic-test-only","strategy_config_sha256":"b"*64,"strategy_evidence_sha256":"c"*64,"holdout_evidence_sha256":"d"*64,"forward_paper_evidence_sha256":"e"*64,"execution_stress_evidence_sha256":"f"*64,"calibration_evidence_sha256":"0"*64,"ledger_evidence_sha256":"1"*64,"trial_register_sha256":"2"*64,"evidence_references":["synthetic-unit-fixture"],"holdout_reviewed":True,"costs_reviewed":True,"trial_register_reviewed":True,"ai_calibration_reviewed":True,"forward_paper_reviewed":True,"execution_stress_reviewed":True,"sample_trades":100,"expectancy":"1","max_drawdown":"0.05","profit_factor":"1.5","profitable_regimes":2,"paper_days":30}
    with pytest.raises(ValueError,match="finite"):
        review({**artifact,"profit_factor":"Infinity"},"strategy",tenant="pilot",revision="a"*40,evidence_root=tmp_path)
    with pytest.raises(ValueError,match="nonnegative integers"):
        review({**artifact,"sample_trades":-1},"strategy",tenant="pilot",revision="a"*40,evidence_root=tmp_path)
    with pytest.raises(ValueError,match="list the retained files"):
        review(artifact,"strategy",tenant="pilot",revision="a"*40,evidence_root=tmp_path)


def test_strategy_review_requires_the_retained_ledger_and_trial_register_digests():
    artifact={"schema":"pramana.strategy.review.v1","tenant_id":"pilot","release_revision":"a"*40,"strategy_id":"synthetic-test-only","strategy_config_sha256":"b"*64,"strategy_evidence_sha256":"c"*64,"holdout_evidence_sha256":"d"*64,"forward_paper_evidence_sha256":"e"*64,"execution_stress_evidence_sha256":"f"*64,"calibration_evidence_sha256":"0"*64,"evidence_references":["synthetic-unit-fixture"],"holdout_reviewed":True,"costs_reviewed":True,"trial_register_reviewed":True,"ai_calibration_reviewed":True,"forward_paper_reviewed":True,"execution_stress_reviewed":True,"sample_trades":100,"expectancy":"1","max_drawdown":"0.05","profit_factor":"1.5","profitable_regimes":2,"paper_days":30}
    with pytest.raises(ValueError,match="ledger evidence digest"):
        review(artifact,"strategy",tenant="pilot",revision="a"*40)
    with pytest.raises(ValueError,match="trial register digest"):
        review({**artifact,"ledger_evidence_sha256":"1"*64},"strategy",tenant="pilot",revision="a"*40)


def test_strategy_review_requires_bound_forward_and_quality_evidence():
    artifact={"schema":"pramana.strategy.review.v1","tenant_id":"pilot","release_revision":"a"*40,"strategy_id":"synthetic-test-only","strategy_config_sha256":"b"*64,"strategy_evidence_sha256":"c"*64,"evidence_references":["synthetic-unit-fixture"],"holdout_reviewed":True,"costs_reviewed":True,"trial_register_reviewed":True,"ai_calibration_reviewed":True,"forward_paper_reviewed":True,"execution_stress_reviewed":True,"sample_trades":100,"expectancy":"1","max_drawdown":"0.05","profit_factor":"1.5","profitable_regimes":2,"paper_days":30}
    with pytest.raises(ValueError,match="holdout evidence digest"):
        review(artifact,"strategy",tenant="pilot",revision="a"*40)


def test_strategy_review_requires_the_statistical_gate_result():
    """The register's candidate count was loaded and discarded before this existed.

    _selection_evidence is what turns it into a promotion input, so the study has to type
    its deflated sharpe and universe verdict alongside the performance figures.
    """
    from quant_ai.governance.pilot_review import _selection_evidence

    trials = {"candidate_trials": 47}
    # Absent returns None so evaluate_promotion reports selection_bias_uncorrected alongside
    # any other defect, instead of masking it with a different error raised earlier.
    assert _selection_evidence({}, trials) is None
    assert _selection_evidence({"deflated_sharpe": 0.97}, trials) is None
    # A field that is present but malformed is still a hard artifact error.
    with pytest.raises(ValueError, match="must be a number"):
        _selection_evidence({"deflated_sharpe": "not-a-number", "universe_verdict": "plausible"}, trials)
    with pytest.raises(ValueError, match=r"probability and must lie in \[0, 1\]"):
        _selection_evidence({"deflated_sharpe": 1.4, "universe_verdict": "plausible"}, trials)

    selection = _selection_evidence(
        {"deflated_sharpe": "0.97", "universe_verdict": "plausible"}, trials
    )
    # The count comes from the retained register, never from the artifact the operator typed.
    assert selection.candidate_trials == 47
    assert selection.deflated_sharpe == 0.97
    assert selection.universe_verdict == "plausible"


def test_the_reviewer_actually_passes_selection_to_the_promotion_gate():
    """Structural, because every other test in this file stops at an earlier rejection.

    Without this, removing `selection=` from the promotion call in _review_strategy leaves
    the whole suite green while silently restoring approval on uncorrected statistics.
    """
    from pathlib import Path

    source = Path(__import__("quant_ai.governance.pilot_review", fromlist=["x"]).__file__).read_text()
    assert "evaluate_promotion(evidence, selection=_selection_evidence(artifact, trials))" in source, (
        "pilot_review no longer threads selection evidence into evaluate_promotion"
    )
    assert "require_selection_correction=False" not in source, (
        "pilot_review disables the selection correction it is supposed to enforce"
    )
