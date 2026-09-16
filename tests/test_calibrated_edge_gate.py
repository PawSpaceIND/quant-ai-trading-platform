from decimal import Decimal

from quant_ai.decision.edge import CalibratedEdgeGate, EdgeEvidence, EdgePolicy

D = Decimal
H = "a" * 64


def evidence(**changes):
    values = {
        "calibrated_probability": D("0.65"),
        "calibration_error": D("0.03"),
        "probability_uncertainty": D("0.02"),
        "average_win_return": D("0.04"),
        "average_loss_return": D("0.02"),
        "expected_cost_return": D("0.002"),
        "resolved_samples": 100,
        "source_sha256": H,
    }
    values.update(changes)
    return EdgeEvidence(**values)


def test_edge_uses_conservative_probability_and_after_cost_expectancy():
    decision = CalibratedEdgeGate().evaluate(evidence())
    assert decision.conservative_probability == D("0.60")
    assert decision.break_even_probability == D("0.3666666666666666666666666667")
    assert decision.expected_after_cost_return == D("0.014")
    assert decision.approved
    assert D(0) < decision.recommended_risk_fraction <= D("0.02")


def test_high_raw_probability_can_still_fail_after_calibration_uncertainty_haircut_and_costs():
    decision = CalibratedEdgeGate().evaluate(evidence(
        calibrated_probability=D("0.75"), calibration_error=D("0.12"),
        probability_uncertainty=D("0.10"), expected_cost_return=D("0.015"),
    ))
    assert not decision.approved
    assert "edge_calibration_error_too_high" in decision.reasons
    assert decision.recommended_risk_fraction == 0


def test_asymmetric_payoff_changes_break_even_probability_not_just_win_rate():
    gate = CalibratedEdgeGate(EdgePolicy(min_conservative_edge=D(0)))
    two_to_one = gate.evaluate(evidence(
        calibrated_probability=D("0.55"), calibration_error=D(0),
        probability_uncertainty=D(0), average_win_return=D("0.04"),
        average_loss_return=D("0.02"), expected_cost_return=D(0),
    ))
    half_to_one = gate.evaluate(evidence(
        calibrated_probability=D("0.55"), calibration_error=D(0),
        probability_uncertainty=D(0), average_win_return=D("0.01"),
        average_loss_return=D("0.02"), expected_cost_return=D(0),
    ))
    assert two_to_one.approved
    assert not half_to_one.approved
    assert two_to_one.break_even_probability < half_to_one.break_even_probability


def test_tiny_sample_or_uncertain_model_cannot_get_risk_even_with_positive_point_estimate():
    gate = CalibratedEdgeGate()
    sample = gate.evaluate(evidence(resolved_samples=5))
    assert not sample.approved and sample.recommended_risk_fraction == 0
    uncertain = gate.evaluate(evidence(probability_uncertainty=D("0.20")))
    assert not uncertain.approved and uncertain.recommended_risk_fraction == 0


def test_fractional_kelly_is_hard_capped_by_risk_policy():
    decision = CalibratedEdgeGate(EdgePolicy(
        min_conservative_edge=D(0), kelly_fraction=D("0.5"), max_risk_fraction=D("0.01")
    )).evaluate(evidence(
        calibrated_probability=D("0.9"), calibration_error=D(0), probability_uncertainty=D(0),
        average_win_return=D("0.10"), average_loss_return=D("0.01"), expected_cost_return=D(0),
    ))
    assert decision.approved
    assert decision.raw_kelly_fraction > D("0.5")
    assert decision.recommended_risk_fraction == D("0.01")
