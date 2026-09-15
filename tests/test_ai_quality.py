import pytest

from quant_ai.validation.ai_quality import assess_calibration_drift


def test_calibration_and_drift_are_reproducible_and_non_promoting():
    probabilities = [0.2] * 10 + [0.8] * 10 + [0.3] * 10 + [0.7] * 10
    outcomes = [0] * 10 + [1] * 10 + [0] * 10 + [1] * 10
    report = assess_calibration_drift(probabilities, outcomes, window=10)
    assert report["schema"] == "pramana.ai_quality.v1"
    assert report["observations"] == 40
    assert report["promotion_approved"] is False
    assert report["latest"]["brier_score"] < 0.1


def test_calibration_rejects_short_or_invalid_inputs():
    with pytest.raises(ValueError, match="two aligned windows"):
        assess_calibration_drift([0.5] * 19, [0] * 19, window=10)
    with pytest.raises(ValueError, match="between zero and one"):
        assess_calibration_drift([0.5] * 20 + [1.2] * 20, [0] * 40, window=10)
    with pytest.raises(ValueError, match="binary"):
        assess_calibration_drift([0.5] * 40, [0] * 39 + [2], window=10)
