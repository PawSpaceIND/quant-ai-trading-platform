"""Checking the friction model against what executions actually cost."""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from quant_ai.execution.cost_calibration import (
    MATERIAL_BIAS_BPS,
    RealisedFill,
    calibrate_costs,
    fills_needed_to_detect,
)


def fill(*, side="BUY", reference="100", filled="100.05", modelled="100.05", symbol="X"):
    return RealisedFill(
        symbol=symbol,
        side=side,
        quantity=Decimal(500),
        reference_price=Decimal(reference),
        fill_price=Decimal(filled),
        modelled_execution_price=Decimal(modelled),
        traded_on=date(2026, 9, 18),
    )


def test_no_fills_reports_no_evidence_rather_than_a_calibrated_model():
    report = calibrate_costs([])

    assert report.verdict == "insufficient_evidence"
    assert not report.model_is_usable
    assert not report.backtests_are_invalidated
    assert "never been checked against an execution" in report.reasons[0]


def test_a_handful_of_fills_is_refused_rather_than_judged():
    report = calibrate_costs([fill() for _ in range(5)])

    assert report.fills == 5
    assert report.verdict == "insufficient_evidence"
    assert "below the 30" in report.reasons[0]


def test_buying_above_reference_and_selling_below_it_are_both_costs():
    buy = fill(side="BUY", reference="100", filled="100.10")
    sell = fill(side="SELL", reference="100", filled="99.90")

    assert buy.realised_drag_bps == pytest.approx(10.0)
    assert sell.realised_drag_bps == pytest.approx(10.0)
    # A sell that filled above the reference is a gain, not a cost, and signs accordingly.
    assert fill(side="SELL", reference="100", filled="100.10").realised_drag_bps == pytest.approx(-10.0)


def test_a_model_that_charges_too_little_invalidates_the_backtests_it_priced():
    # Every fill cost 5bps more than the model said it would.
    fills = [fill(filled="100.10", modelled="100.05") for _ in range(40)]

    report = calibrate_costs(fills)

    assert report.verdict == "model_understates_cost"
    assert report.mean_error_bps == pytest.approx(5.0)
    assert report.backtests_are_invalidated
    assert not report.model_is_usable
    assert "priced too cheaply" in report.reasons[0]


def test_a_model_that_charges_too_much_is_flagged_as_the_safe_direction_to_be_wrong():
    fills = [fill(filled="100.02", modelled="100.08") for _ in range(40)]

    report = calibrate_costs(fills)

    assert report.verdict == "model_overstates_cost"
    assert report.mean_error_bps < 0
    assert report.model_is_usable  # conservative pricing does not invalidate a result
    assert not report.backtests_are_invalidated
    assert "safe direction" in report.reasons[0]


def test_a_model_that_matches_is_called_not_caught_wrong_rather_than_correct():
    fills = []
    for index in range(40):
        # Symmetric noise around a correct model: no bias, real dispersion.
        drift = "100.07" if index % 2 else "100.03"
        fills.append(fill(filled=drift, modelled="100.05"))

    report = calibrate_costs(fills)

    assert report.verdict == "calibrated"
    assert report.mean_error_bps == pytest.approx(0.0, abs=1e-9)
    assert report.model_is_usable
    assert "not confirmed correct, only not caught wrong" in report.reasons[0]


def test_the_test_is_paired_so_an_instruments_own_volatility_does_not_read_as_model_bias():
    # Drag varies hugely between fills - a thin book one day, a deep one the next - but the
    # model tracks each. An unpaired comparison of means would still net out here; what
    # would not is the dispersion, and a paired test keeps the t-statistic honest.
    fills = []
    for index in range(40):
        drag = Decimal(index % 8) / Decimal(100)
        fills.append(
            RealisedFill(
                symbol="X", side="BUY", quantity=Decimal(500),
                reference_price=Decimal(100),
                fill_price=Decimal(100) + drag,
                modelled_execution_price=Decimal(100) + drag,
                traded_on=date(2026, 9, 18),
            )
        )

    report = calibrate_costs(fills)

    assert report.realised_bps == pytest.approx(report.modelled_bps)
    assert report.error_dispersion_bps == pytest.approx(0.0)
    assert report.verdict == "calibrated"


def test_how_many_fills_are_needed_grows_with_the_square_of_the_noise():
    assert fills_needed_to_detect(0.0) == 1.0
    small = fills_needed_to_detect(10.0, bias_bps=MATERIAL_BIAS_BPS)
    doubled = fills_needed_to_detect(20.0, bias_bps=MATERIAL_BIAS_BPS)
    assert doubled == pytest.approx(small * 4.0)
    # A bias half the size takes four times the evidence to see.
    assert fills_needed_to_detect(10.0, bias_bps=2.5) == pytest.approx(small * 4.0)


def test_a_statistically_real_but_immaterial_bias_says_so():
    fills = []
    for index in range(400):
        # A consistent 1bp error: real, and far too small to change which strategies pass.
        jitter = "100.0600" if index % 2 else "100.0400"
        fills.append(fill(filled=jitter, modelled="100.0400"))

    report = calibrate_costs(fills)

    assert report.verdict.startswith("model_")
    assert abs(report.mean_error_bps) < MATERIAL_BIAS_BPS
    assert any("would change which strategies pass" in reason for reason in report.reasons)


def test_evidence_states_that_deterministic_charges_are_excluded():
    evidence = calibrate_costs([fill() for _ in range(40)]).as_evidence()

    assert "cannot be wrong" in evidence["limitation"]
    assert evidence["schema"] == "pramana.cost_calibration.v1"
    assert math.isfinite(evidence["fills_needed"] or 0.0)


def test_a_fill_that_could_not_have_happened_is_refused():
    with pytest.raises(ValueError, match="positive quantity"):
        RealisedFill("X", "BUY", Decimal(0), Decimal(100), Decimal(100), Decimal(100),
                     date(2026, 9, 18))
    with pytest.raises(ValueError, match="side must be"):
        RealisedFill("X", "HOLD", Decimal(1), Decimal(100), Decimal(100), Decimal(100),
                     date(2026, 9, 18))
