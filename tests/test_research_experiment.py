import pytest

from quant_ai.validation.experiment import block_paths, evaluate, strategy_returns


def test_holdout_never_changes_training_selection():
    prices = [100 + i * .1 + (i % 7) for i in range(160)]
    modified = prices[:120] + [1 + (i % 10) * 50 for i in range(40)]
    a, b = evaluate(prices), evaluate(modified)
    assert a["folds"] == b["folds"]
    assert a["holdout"]["selected_window"] == b["holdout"]["selected_window"]
    assert not a["promotion_approved"]
    assert a["holdout"]["range"] == [120, 160]


def test_signal_uses_previous_close_and_charges_entry_exit():
    prices = [100., 100., 100., 110., 121.]
    # At t=3 the previously flat market cannot anticipate the jump to 110.
    assert strategy_returns(prices, 3, 4, 3, 0) == [0]
    result = strategy_returns(prices, 4, 5, 3, 10)
    assert result[0] == pytest.approx(.1 - .002)


def test_path_bootstrap_is_reproducible_and_measures_drawdown():
    sample = [.02, -.1, -.05, .01, .01, -.02]
    assert block_paths(sample, 50) == block_paths(sample, 50)
    assert block_paths(sample, 50)["max_drawdown_p95"] > .1
    with pytest.raises(ValueError):
        evaluate([100.] * 30)
