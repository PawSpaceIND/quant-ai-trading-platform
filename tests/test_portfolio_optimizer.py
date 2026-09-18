from decimal import Decimal

import pytest

from quant_ai.portfolio.optimizer import (
    PortfolioOptimizationPolicy,
    StrategyOpportunity,
    StrategyPortfolioOptimizer,
)

D = Decimal


def opportunity(name, expected="0.12", vol="0.20", drawdown="0.06", evidence="0.9", capacity="1", max_weight="1"):
    return StrategyOpportunity(
        name, D(expected), D(vol), D(drawdown), D(evidence), D(capacity), D(max_weight)
    )


def test_optimizer_respects_gross_strategy_capacity_and_volatility_limits():
    policy = PortfolioOptimizationPolicy(
        max_gross_weight=D("0.60"), max_strategy_weight=D("0.30"),
        max_portfolio_volatility=D("0.12"), risk_aversion=D("0.5"),
        turnover_penalty=D(0), weight_step=D("0.01"),
    )
    items = (opportunity("trend"), opportunity("meanrev", capacity="0.25"))
    result = StrategyPortfolioOptimizer().optimize(
        items, correlations={("trend", "meanrev"): D("0.2")}, policy=policy
    )
    weights = result.weights()
    assert result.gross_weight <= D("0.60")
    assert weights["trend"] <= D("0.30")
    assert weights["meanrev"] <= D("0.25")
    assert result.portfolio_volatility <= D("0.12")
    assert result.cash_weight == D(1) - result.gross_weight


def test_negative_edge_weak_evidence_and_excess_drawdown_are_excluded_not_rescued_by_optimizer():
    items = (
        opportunity("negative", expected="-0.01"),
        opportunity("weak", evidence="0.3"),
        opportunity("drawdown", drawdown="0.20"),
        opportunity("good"),
    )
    result = StrategyPortfolioOptimizer().optimize(items, correlations={})
    assert set(result.weights()) == {"good"}
    assert set(result.excluded) == {
        ("negative", "after_cost_expectancy_not_positive"),
        ("weak", "evidence_score_below_minimum"),
        ("drawdown", "strategy_drawdown_above_limit"),
    }


def test_missing_or_asymmetric_correlation_never_assumes_free_diversification():
    items = (opportunity("a"), opportunity("b"))
    with pytest.raises(ValueError, match="strategy_correlation_missing"):
        StrategyPortfolioOptimizer().optimize(items, correlations={})
    with pytest.raises(ValueError, match="strategy_correlation_asymmetric"):
        StrategyPortfolioOptimizer().optimize(
            items, correlations={("a", "b"): D("0.2"), ("b", "a"): D("0.3")}
        )


def test_lower_correlation_can_earn_more_budget_when_edge_and_volatility_match():
    policy = PortfolioOptimizationPolicy(
        max_gross_weight=D("0.60"), max_strategy_weight=D("0.30"),
        max_portfolio_volatility=D("0.30"), risk_aversion=D(10),
        turnover_penalty=D(0), weight_step=D("0.01"),
    )
    items = (opportunity("a"), opportunity("b"), opportunity("c"))
    correlations = {
        ("a", "b"): D("0.95"),
        ("a", "c"): D("0.0"),
        ("b", "c"): D("0.0"),
    }
    weights = StrategyPortfolioOptimizer().optimize(
        items, correlations=correlations, policy=policy
    ).weights()
    assert weights["c"] >= weights["a"]
    assert weights["c"] >= weights["b"]


def test_turnover_penalty_prefers_existing_good_strategy_when_marginal_edge_is_close():
    policy = PortfolioOptimizationPolicy(
        max_gross_weight=D("0.20"), max_strategy_weight=D("0.20"),
        max_portfolio_volatility=D("0.30"), risk_aversion=D("0.01"),
        turnover_penalty=D("0.03"), weight_step=D("0.01"),
    )
    items = (opportunity("held", expected="0.10"), opportunity("new", expected="0.105"))
    correlations = {("held", "new"): D("0.0")}
    result = StrategyPortfolioOptimizer().optimize(
        items, correlations=correlations, current_weights={"held": D("0.20")}, policy=policy
    )
    assert result.weights().get("held", D(0)) >= result.weights().get("new", D(0))


def test_bad_covariance_that_produces_negative_variance_refuses():
    items = (opportunity("a"), opportunity("b"))
    # |rho|<=1 is individually plausible, but the optimizer still refuses a trial if the
    # resulting quadratic form becomes negative instead of taking sqrt of nonsense.
    result = StrategyPortfolioOptimizer().optimize(
        items, correlations={("a", "b"): D("-1")},
        policy=PortfolioOptimizationPolicy(
            max_gross_weight=D("0.2"), max_strategy_weight=D("0.2"),
            max_portfolio_volatility=D("0.3"), risk_aversion=D("0.1"),
            turnover_penalty=D(0), weight_step=D("0.01"),
        ),
    )
    assert result.portfolio_volatility >= 0
