"""Whether the forecast has earned the right to gate a trade, and why not yet.

Arming the EV gate is an operator act that needs evidence, not confidence. This is the
function that supplies it, and these tests pin the property that makes it worth reading:
it fails closed. A report that quietly omits the drift distribution and returns "pass"
would be read as the thing that checked it.
"""
from __future__ import annotations

from decimal import Decimal

from quant_ai.analytics.promotion import MINIMUM_RESOLVED, SCHEMA, promotion_report

BASIS = "weighted_lean_times_confidence.v1"


def row(
    *,
    probability: str,
    forward: str | None,
    governance: str = "filled",
    side: str = "BUY",
    net_pnl: str | None = "125.00",
    cost_bps: str = "10",
    basis: str = BASIS,
    expected_return: str = "0.01",
    expected_risk: str = "0.02",
) -> dict[str, object]:
    return {
        "forecast_probability_up": probability,
        "forecast_horizon_seconds": 3600,
        "forecast_cost_bps": cost_bps,
        "forecast_basis": basis,
        "forward_return_60m": forward,
        "governance": governance,
        "side": side,
        "realized_net_pnl": net_pnl,
        "expected_return": expected_return,
        "expected_risk": expected_risk,
    }


def skilful(count: int) -> list[dict[str, object]]:
    """A forecaster that is right more often than a coin, at honest probabilities."""
    rows = []
    for index in range(count):
        up = index % 10 < 7  # 70% of the sample resolves up
        rows.append(row(
            probability="0.7000" if up else "0.3000",
            forward="0.0200" if up else "-0.0200",
        ))
    return rows


def test_a_thin_sample_is_refused_before_any_metric_is_believed() -> None:
    report = promotion_report(skilful(MINIMUM_RESOLVED - 1))
    assert report["schema"] == SCHEMA
    assert report["verdict"] == "insufficient_sample"
    assert report["promotion_authorized"] is False
    assert report["resolved_forecast_count"] == MINIMUM_RESOLVED - 1


def test_a_missing_input_is_named_and_refuses_rather_than_being_skipped() -> None:
    """The property that makes the report worth reading.

    Entry drift and realised drawdown are not journaled yet. A report that scored the
    fields it had and returned "pass" would be read as having checked the ones it did not.
    """
    report = promotion_report(skilful(MINIMUM_RESOLVED))
    assert report["verdict"] == "missing_inputs"
    assert report["promotion_authorized"] is False
    assert "entry_drift_not_journaled" in report["missing_inputs"]
    assert "drawdown_or_policy_unavailable" in report["missing_inputs"]
    # The metrics it could compute are still reported; refusing is not the same as blank.
    assert report["resolved_forecast_count"] == MINIMUM_RESOLVED
    assert report["brier_score"] is not None


def test_a_complete_and_profitable_sample_passes() -> None:
    report = promotion_report(
        skilful(MINIMUM_RESOLVED),
        entry_drift_bps=["12.0", "-8.0", "15.0"],
        realised_max_drawdown="0.02",
        policy_max_drawdown="0.10",
    )
    assert report["missing_inputs"] == []
    assert report["verdict"] == "pass"
    assert report["promotion_authorized"] is True
    assert Decimal(report["brier_score"]) < Decimal(report["brier_baseline_coin_flip"])
    assert report["median_abs_entry_drift_bps"] == "12.0"


def test_a_forecaster_no_better_than_a_coin_is_refused() -> None:
    """Every row states 0.5: the coin's own score, so nothing was added."""
    rows = [row(probability="0.5000", forward="0.0200" if index % 2 else "-0.0200")
            for index in range(MINIMUM_RESOLVED)]
    report = promotion_report(
        rows, entry_drift_bps=["10.0"], realised_max_drawdown="0.01", policy_max_drawdown="0.10",
    )
    assert report["verdict"] == "fail"
    assert report["promotion_authorized"] is False


def test_a_drawdown_past_policy_is_refused_however_good_the_forecast() -> None:
    report = promotion_report(
        skilful(MINIMUM_RESOLVED),
        entry_drift_bps=["10.0"], realised_max_drawdown="0.15", policy_max_drawdown="0.10",
    )
    assert report["verdict"] == "fail"


def test_losing_fills_are_refused_even_when_the_direction_was_right() -> None:
    """A forecast can be well calibrated and still lose money after costs."""
    rows = [dict(item, realized_net_pnl="-40.00") for item in skilful(MINIMUM_RESOLVED)]
    report = promotion_report(
        rows, entry_drift_bps=["10.0"], realised_max_drawdown="0.01", policy_max_drawdown="0.10",
    )
    assert report["verdict"] == "fail"
    assert Decimal(report["mean_net_ev_filled_buys"]) < 0


def test_a_refusal_scores_the_forecast_but_is_never_fill_profit() -> None:
    """Refusals are not losses of the book; they are still evidence about the mapping."""
    rows = [dict(item, governance="rejected", realized_net_pnl=None)
            for item in skilful(MINIMUM_RESOLVED)]
    report = promotion_report(
        rows, entry_drift_bps=["10.0"], realised_max_drawdown="0.01", policy_max_drawdown="0.10",
    )
    assert report["resolved_forecast_count"] == MINIMUM_RESOLVED  # still scored
    assert report["filled_buys"] == 0
    assert report["mean_net_ev_filled_buys"] is None
    assert "no_settled_filled_buy" in report["missing_inputs"]
    assert report["verdict"] == "missing_inputs"


def test_payoffs_that_decide_every_expected_value_are_named_as_such() -> None:
    """A book silenced by its payoffs must not look like a book silenced by its forecast.

    Break-even probability is (risk + cost) / (win + risk) - a property of the declared
    payoffs alone. At 1% win against 2% risk that is 0.70, past what this mapping reaches,
    so every expected value is negative whatever the forecast says.
    """
    # Deliberately NOT the skilful fixture: its 0.70 sits exactly on the break-even, so
    # those rows price at zero rather than below it. These are the probabilities the live
    # book actually produces - a lean of 0.35 at a conviction of 0.58 lands near 0.60.
    rows = [row(probability="0.6000" if index % 2 else "0.5500",
                forward="0.0200" if index % 3 else "-0.0200")
            for index in range(MINIMUM_RESOLVED)]
    report = promotion_report(rows)
    payoffs = report["payoffs"]
    assert payoffs["assessed"] is True
    assert payoffs["payoffs_look_declared_not_measured"] is True
    assert Decimal(payoffs["negative_expected_value_share"]) >= Decimal("0.90")


def test_payoffs_that_actually_pay_are_not_flagged() -> None:
    rows = [dict(item, expected_return="0.05") for item in skilful(MINIMUM_RESOLVED)]
    report = promotion_report(rows)
    assert report["payoffs"]["payoffs_look_declared_not_measured"] is False


def test_the_report_is_deterministic_over_its_inputs() -> None:
    """Two readers of one journal must never disagree about what it says."""
    rows = skilful(MINIMUM_RESOLVED)
    first = promotion_report(rows, entry_drift_bps=["9.0"], realised_max_drawdown="0.01",
                             policy_max_drawdown="0.10")
    second = promotion_report(list(rows), entry_drift_bps=["9.0"], realised_max_drawdown="0.01",
                              policy_max_drawdown="0.10")
    assert first == second


def test_each_row_is_scored_at_the_cost_it_was_decided_under() -> None:
    """A change of cost policy must not rescore a decision made under the old one.

    Pinned by a probability where the cost alone decides the sign: at 0.68 against a 1%
    win and a 2% risk, EV is +0.000400 before cost and -0.000600 after the 10 bps stored
    on the row. A scorer that reached for a current setting instead of the row's own
    would silently restate every historical verdict every time that setting moved.
    """
    def sample(cost_bps: str) -> dict[str, object]:
        rows = [row(probability="0.6800", forward="0.0200" if index % 3 else "-0.0200",
                    cost_bps=cost_bps)
                for index in range(MINIMUM_RESOLVED)]
        return promotion_report(rows)["payoffs"]

    assert sample("10")["negative_expected_value_share"] == "1"
    assert sample("0")["negative_expected_value_share"] == "0"
    # And the flag follows the cost, because the flag is about the payoffs beside the
    # probability rather than about the probability.
    assert sample("10")["payoffs_look_declared_not_measured"] is True
    assert sample("0")["payoffs_look_declared_not_measured"] is False
