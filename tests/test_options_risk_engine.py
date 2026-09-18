from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.derivatives.chain import OptionChainSnapshot, OptionQuote
from quant_ai.derivatives.lifecycle import (
    SettlementStyle,
    SpreadMarginEvidence,
    expiry_obligation,
)
from quant_ai.derivatives.options import OptionContract, OptionType
from quant_ai.derivatives.portfolio import (
    OptionRiskPosition,
    aggregate_option_greeks,
    delta_gamma_scenario_pnl,
)
from quant_ai.derivatives.valuation import (
    ExerciseStyle,
    OptionPricingInputs,
    implied_volatility,
    option_greeks,
    value_option,
)

D = Decimal
NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def contract(option_type=OptionType.CALL, strike="100"):
    return OptionContract(
        "TEST26DEC", "TEST", option_type, D(strike), "2026-12-16", D("50"), "INR", "NFO"
    )


def inputs(vol="0.25", style=ExerciseStyle.EUROPEAN):
    return OptionPricingInputs(D("100"), D(vol), D("0.05"), D("0.01"), date(2026, 9, 16), style, 100)


def test_crr_price_is_above_intrinsic_and_american_put_is_not_cheaper_than_european():
    call = value_option(contract(), inputs())
    assert call.price > call.intrinsic_value == D(0)
    put = contract(OptionType.PUT, "110")
    european = value_option(put, inputs(style=ExerciseStyle.EUROPEAN)).price
    american = value_option(put, inputs(style=ExerciseStyle.AMERICAN)).price
    assert american >= european
    assert american >= put.intrinsic_value(D("100"))


def test_implied_volatility_round_trips_the_same_model_without_guessing_a_rate():
    c = contract()
    actual = D("0.32")
    premium = value_option(c, inputs(vol=str(actual))).price
    solved = implied_volatility(c, inputs(vol="0.20"), market_premium=premium, tolerance=D("0.00001"))
    assert abs(solved - actual) < D("0.002")
    with pytest.raises(ValueError, match="below_intrinsic"):
        implied_volatility(contract(OptionType.PUT, "120"), inputs(), market_premium=D("1"))


def test_greeks_have_expected_local_direction_and_portfolio_aggregation_respects_multiplier_and_sign():
    c = contract()
    greeks = option_greeks(c, inputs())
    assert D(0) < greeks.delta < D(1)
    assert greeks.gamma > 0
    assert greeks.vega > 0
    exposure = aggregate_option_greeks((
        OptionRiskPosition(c, 2, greeks),
        OptionRiskPosition(c, -1, greeks),
    ))
    assert exposure.delta == greeks.delta * D("50")
    scenario = delta_gamma_scenario_pnl(exposure, underlying_move=D("2"))
    assert scenario > exposure.delta * D("2")


def test_chain_requires_exact_unique_contract_provider_identity_and_freshness():
    c1 = contract(strike="100")
    c2 = contract(strike="105")
    q1 = OptionQuote(c1, D("5"), D("6"), D("5.5"), 100, 1000, "provider-1", NOW)
    q2 = OptionQuote(c2, D("3"), D("4"), D("3.5"), 50, 800, "provider-2", NOW)
    chain = OptionChainSnapshot("TEST", D("100"), NOW, "qualified option source", (q1, q2))
    chain.assert_fresh(NOW + timedelta(seconds=10), 30)
    with pytest.raises(ValueError, match="option_chain_stale"):
        chain.assert_fresh(NOW + timedelta(seconds=31), 30)
    with pytest.raises(ValueError, match="duplicate_option_contract_identity"):
        OptionChainSnapshot("TEST", D("100"), NOW, "source", (q1, q1))


def test_expiry_economics_require_explicit_exercise_and_explicit_settlement_style():
    c = contract(OptionType.CALL, "90")
    nothing = expiry_obligation(
        c, signed_contracts=2, settlement_spot=D("100"),
        settlement_style=SettlementStyle.PHYSICAL, exercise_or_assign=False,
    )
    assert not nothing.exercised and nothing.cash_delta == 0
    physical = expiry_obligation(
        c, signed_contracts=2, settlement_spot=D("100"),
        settlement_style=SettlementStyle.PHYSICAL, exercise_or_assign=True,
    )
    assert physical.underlying_delta == D("100")
    assert physical.cash_delta == D("-9000")
    short_cash = expiry_obligation(
        c, signed_contracts=-2, settlement_spot=D("100"),
        settlement_style=SettlementStyle.CASH, exercise_or_assign=True,
    )
    assert short_cash.cash_delta == D("-1000")


def test_spread_margin_is_evidence_not_theoretical_max_loss():
    evidence = SpreadMarginEvidence(
        "bull-call-1", D("12500"), "broker margin snapshot", NOW
    )
    assert evidence.required_margin == D("12500")
    with pytest.raises(ValueError, match="option_spread_margin_must_be_positive"):
        SpreadMarginEvidence("spread", D(0), "broker", NOW)


def test_expired_option_values_at_intrinsic_without_time_value():
    c = contract(OptionType.PUT, "110")
    expired = value_option(
        c,
        OptionPricingInputs(D("100"), D("0.20"), D("0.05"), D(0), date(2026, 12, 16), ExerciseStyle.EUROPEAN, 100),
    )
    assert expired.price == expired.intrinsic_value == D("10")
    assert expired.time_value == 0
