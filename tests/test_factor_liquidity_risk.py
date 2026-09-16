from decimal import Decimal

from quant_ai.risk.factor_liquidity import (
    FactorLiquidityFirewall,
    FactorLiquidityPolicy,
    FactorLiquidityPosition,
    FactorScenario,
    measure_factor_liquidity,
    scenario_pnl,
)

D = Decimal


def position(symbol="INFY", value="20000", quantity=100, adv="1000", stress_volume="0.5", cost="0.01", market="1.0", tech="0.5"):
    return FactorLiquidityPosition(
        symbol, D(value), quantity, D("200"), {"MARKET": D(market), "TECH": D(tech)},
        D(adv), D(stress_volume), D(cost),
    )


def policy(**kwargs):
    base = {
        "factor_caps": {"MARKET": D("0.40"), "TECH": D("0.30")},
        "max_daily_participation": D("0.10"),
        "max_liquidation_days": D("5"),
        "max_total_liquidation_cost_fraction": D("0.02"),
    }
    base.update(kwargs)
    return FactorLiquidityPolicy(**base)


def test_factor_exposure_and_stressed_liquidation_are_measured_from_supplied_evidence():
    measure = measure_factor_liquidity((position(),), equity=D("100000"), policy=policy())
    assert measure.factor_exposure["MARKET"] == D("20000")
    assert measure.factor_fraction["TECH"] == D("0.10")
    # 1000 ADV x 50% stress volume x 10% allowed participation = 50 units/day.
    assert measure.positions[0].stressed_daily_capacity == D("50")
    assert measure.positions[0].liquidation_days == D("2")
    assert measure.total_stressed_execution_cost_fraction == D("0.002")


def test_missing_factor_loading_fails_closed_instead_of_becoming_zero():
    incomplete = FactorLiquidityPosition(
        "INFY", D("20000"), 100, D("200"), {"MARKET": D(1)},
        D("1000"), D("0.5"), D("0.01"),
    )
    decision = FactorLiquidityFirewall(policy()).evaluate((incomplete,), equity=D("100000"))
    assert not decision.approved
    assert decision.reason.startswith("factor_liquidity_measure_unavailable:factor_loading_missing")


def test_factor_cap_liquidation_horizon_and_stressed_cost_are_independent_gates():
    factor = FactorLiquidityFirewall(policy()).evaluate(
        (position(value="50000"),), equity=D("100000")
    )
    assert not factor.approved and factor.reason == "factor_exposure_limit:MARKET"
    horizon = FactorLiquidityFirewall(policy()).evaluate(
        (position(quantity=300, adv="1000", market="0.1", tech="0.1"),), equity=D("100000")
    )
    assert not horizon.approved and horizon.reason == "liquidation_horizon_limit:INFY"
    cost = FactorLiquidityFirewall(policy(max_total_liquidation_cost_fraction=D("0.001"))).evaluate(
        (position(market="0.1", tech="0.1"),), equity=D("100000")
    )
    assert not cost.approved and cost.reason == "liquidation_cost_limit"


def test_explicit_factor_scenario_combines_factor_and_symbol_specific_shocks():
    positions = (
        position("INFY", value="20000", market="1", tech="0.5"),
        position("TCS", value="10000", market="0.8", tech="0.7"),
    )
    scenario = FactorScenario(
        "market down / tech shock",
        {"MARKET": D("-0.10"), "TECH": D("-0.20")},
        {"INFY": D("-0.02")},
    )
    # INFY: 20k*(-10%-10%-2%)=-4400; TCS:10k*(-8%-14%)=-2200.
    assert scenario_pnl(positions, scenario) == D("-6600")


def test_healthy_book_is_approved():
    decision = FactorLiquidityFirewall(policy()).evaluate(
        (position(market="0.5", tech="0.5"),), equity=D("100000")
    )
    assert decision.approved and decision.reason == "approved_factor_liquidity"
