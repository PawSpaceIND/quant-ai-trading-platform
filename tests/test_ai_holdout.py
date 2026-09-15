import pytest

from quant_ai.validation.ai_holdout import evaluate_ai_holdout


def provenance():
    return {
        "model": "atlas",
        "model_version": "2026-09-15",
        "strategy_hash": "a" * 64,
        "data_sha256": "b" * 64,
        "decision_cutoff": "2026-01-01T00:00:00+00:00",
    }


def test_ai_holdout_is_after_costs_and_never_promotes():
    prices = [100 + i for i in range(45)]
    report = evaluate_ai_holdout(prices, [1] * len(prices), holdout_start=20, cost_bps=10, provenance=provenance())
    assert report["schema"] == "pramana.ai_holdout.v1"
    assert report["holdout"]["observations"] == 25
    assert report["promotion_approved"] is False
    assert report["provenance"]["strategy_hash"] == "a" * 64


def test_ai_holdout_rejects_missing_provenance_or_tuning_on_holdout():
    prices = [100 + i for i in range(40)]
    with pytest.raises(ValueError, match="missing holdout provenance"):
        evaluate_ai_holdout(prices, [0] * len(prices), holdout_start=20, cost_bps=10, provenance={})
    with pytest.raises(ValueError, match="unknown holdout provenance"):
        evaluate_ai_holdout(prices, [0] * len(prices), holdout_start=20, cost_bps=10, provenance={**provenance(), "score": "tuned"})


def test_ai_holdout_rejects_invalid_alignment_and_positions():
    with pytest.raises(ValueError, match="aligned observations"):
        evaluate_ai_holdout([100] * 39, [0] * 40, holdout_start=20, cost_bps=10, provenance=provenance())
    with pytest.raises(ValueError, match="binary"):
        evaluate_ai_holdout([100 + i for i in range(40)], [0] * 39 + [2], holdout_start=20, cost_bps=10, provenance=provenance())
