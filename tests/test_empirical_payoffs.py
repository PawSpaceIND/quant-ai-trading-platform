"""What a decision won and lost, against what its specialists said it would.

Every specialist declares the same payoff on every instrument in every regime: 1% against
2%. Break-even probability is ``(e_risk + cost) / (e_win + e_risk)`` - a property of those
declared numbers alone - so at 10 basis points it is 0.70, and the swarm produces 0.59 to
0.65. Every entry is negative expected value at its own stated payoffs, and no improvement
in the forecast can change that, because the forecast is not what is wrong.

These tests pin the measurement of the other half, and the three rules that keep it from
becoming a quiet substitution: it uses the cost each row stored rather than a current one,
it is recorded beside the declared figure and never in place of it, and a refit ships a new
id instead of moving what an old one meant.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.expected_value import DECLARED
from quant_ai.agents.expected_value import record as ev_record
from quant_ai.analytics import empirical_payoffs as payoffs

FORECAST = {"probability_up": "0.6500", "cost_bps": "10", "basis": "weighted_lean_times_confidence.v1"}


def row(symbol="INDIGO", forward="0.0100", cost="10", playbook="range_trading",
        regime="ranging", at="2026-09-22T05:00:00+00:00", horizon=3600) -> dict:
    return {"symbol": symbol, "playbook": playbook, "regime": regime, "decided_at": at,
            "forecast_horizon_seconds": horizon, "forecast_cost_bps": cost,
            "forward_return_60m": forward}


def sample(wins: int, losses: int, **overrides) -> list[dict]:
    return ([row(forward="0.0100", **overrides) for _ in range(wins)]
            + [row(forward="-0.0050", **overrides) for _ in range(losses)])


# ------------------------------------------------------------------ the measurement


def test_a_move_is_measured_net_of_the_cost_that_row_itself_stored() -> None:
    """The rule the Brier score already applies, keeping the magnitude it throws away.

    A later change of cost policy must not restate what an old decision won.
    """
    assert payoffs.net_move(row(forward="0.0100", cost="10")) == Decimal("0.0090")
    assert payoffs.net_move(row(forward="0.0100", cost="50")) == Decimal("0.0050")
    # A row scored under no cost is not a row scored at zero cost.
    assert payoffs.net_move(row(cost=None)) is None
    assert payoffs.net_move(row(cost="-1")) is None


def test_a_horizon_with_no_resolver_column_is_not_scored_against_another_one() -> None:
    assert payoffs.net_move(row(horizon=900)) is None
    assert payoffs.net_move(row(horizon=None)) is None
    assert payoffs.net_move(row(forward=None)) is None  # has not resolved yet
    assert payoffs.net_move("not a row") is None


def test_the_measured_payoffs_are_the_two_means_and_the_break_even_they_imply() -> None:
    """The number the module exists to produce, against the declared 0.70."""
    group = payoffs.artifact(sample(20, 20))["by_symbol"]["INDIGO"]
    # +1% and -0.5% gross, each net of its own 10 bps.
    assert (group["e_win_hat"], group["e_loss_hat"]) == ("0.009000", "0.006000")
    assert (group["wins"], group["losses"], group["n"]) == (20, 20, 40)
    # (0.006 + 0.001) / (0.009 + 0.006) = 0.4667, against 0.70 declared. That gap is the
    # whole finding: the forecast is asked to clear a bar its own payoffs invented.
    assert group["break_even_probability"] == "0.4667"


def test_a_move_of_exactly_zero_won_nothing_and_lost_nothing() -> None:
    """Counting it either way moves a mean it has no claim on."""
    group = payoffs.artifact(sample(10, 10) + [row(forward="0.0010")] * 5)["by_symbol"]["INDIGO"]
    assert (group["wins"], group["losses"], group["flat"], group["n"]) == (10, 10, 5, 25)


def test_an_empty_or_unresolved_sample_measures_nothing_rather_than_zero() -> None:
    """Zero is a payoff. Absence is not one."""
    for rows in ([], [row(forward=None)], [row(horizon=900)]):
        group = payoffs.artifact(rows)["overall"]
        assert group["e_win_hat"] is None and group["e_loss_hat"] is None
        assert group["break_even_probability"] is None and group["n"] == 0


def test_a_group_below_the_sample_floor_is_flagged_and_never_applied() -> None:
    """Eighteen rows is what one session of one name produces. It is not a measurement."""
    thin = payoffs.artifact(sample(9, 9))["by_symbol"]["INDIGO"]
    assert thin["thin"] is True and thin["n"] == 18
    assert payoffs.group_for(payoffs.artifact(sample(9, 9)), symbol="INDIGO") is None
    enough = payoffs.artifact(sample(20, 20))
    assert enough["by_symbol"]["INDIGO"]["thin"] is False
    assert payoffs.group_for(enough, symbol="INDIGO") is not None


def test_the_window_says_which_rows_the_measurement_was_taken_over() -> None:
    rows = sample(15, 15, at="2026-09-21T05:00:00+00:00") + sample(15, 15, at="2026-09-22T05:00:00+00:00")
    group = payoffs.artifact(rows)["by_symbol"]["INDIGO"]
    assert group["first_decided_at"] == "2026-09-21T05:00:00+00:00"
    assert group["last_decided_at"] == "2026-09-22T05:00:00+00:00"


# ------------------------------------------------------------------ the artifact id


def test_the_id_is_the_measurement_and_a_refit_ships_a_new_one() -> None:
    """``empirical:<id>`` has to name one measurement over one window, permanently."""
    rows = sample(20, 20)
    first = payoffs.artifact(rows)
    assert payoffs.artifact(list(rows))["id"] == first["id"]
    assert payoffs.artifact(rows[:-1])["id"] != first["id"]
    assert payoffs.source_label(first) == f"empirical:{first['id']}"


def test_the_clock_is_outside_the_id() -> None:
    """The same rows measured twice are the same artifact.

    An id that moved with the wall clock would make the source label useless as a
    statement about which numbers a decision was scored under.
    """
    rows = sample(20, 20)
    morning = payoffs.artifact(rows, generated_at=datetime(2026, 9, 22, 4, tzinfo=timezone.utc))
    evening = payoffs.artifact(rows, generated_at=datetime(2026, 9, 22, 16, tzinfo=timezone.utc))
    assert morning["id"] == evening["id"]
    assert morning["generated_at"] != evening["generated_at"]


def test_an_artifact_with_no_id_is_not_a_source() -> None:
    """Naming it one would let a decision claim a measurement nobody can look up."""
    for absent in (None, {}, {"id": ""}, "empirical", 7):
        assert payoffs.source_label(absent) == DECLARED


def test_a_symbol_measurement_is_preferred_to_its_playbook_and_both_to_nothing() -> None:
    rows = sample(20, 20) + sample(20, 20, symbol="TRENT")
    found = payoffs.artifact(rows)
    assert payoffs.group_for(found, symbol="INDIGO") is found["by_symbol"]["INDIGO"]
    # A name with no measurement of its own falls back to the playbook it ran under.
    fallback = payoffs.group_for(found, symbol="UNSEEN", playbook="range_trading", regime="ranging")
    assert fallback is found["by_playbook"]["range_trading:ranging"]
    assert payoffs.group_for(found, symbol="UNSEEN", playbook="crisis", regime="high_vol") is None


# ------------------------------------------------------------------ the live path


def test_the_declared_expected_value_is_byte_identical_with_and_without_a_measurement() -> None:
    """The rule that makes this safe to land. No silent swap.

    A measurement that changed the live number would restate what every expected value
    already written had meant, and it would do it before anything had decided the
    measurement was worth trusting.
    """
    declared = ev_record(forecast=FORECAST, expected_return="0.01", expected_risk="0.02")
    measured = ev_record(forecast=FORECAST, expected_return="0.01", expected_risk="0.02",
                         empirical={"e_win_hat": "0.009", "e_loss_hat": "0.006", "n": 40,
                                    "source": "empirical:abc123"})
    assert declared["expected_value"] == measured["expected_value"] == "-0.001500"
    assert declared["e_win_source"] == declared["e_loss_source"] == DECLARED
    assert "ev_empirical" not in declared
    # And the live source still says declared even WITH a measurement attached. This is
    # the assertion that makes "no silent swap" checkable: relabelling the top-level
    # source leaves every number identical and would otherwise pass unnoticed, while the
    # journal would then claim every decision had been priced off a measurement.
    assert measured["e_win_source"] == measured["e_loss_source"] == DECLARED
    # And the second opinion is recorded beside it, on the same probability and cost.
    assert measured["ev_empirical"]["expected_value"] == "0.002750"
    assert measured["ev_empirical"]["e_win_source"] == "empirical:abc123"
    assert measured["ev_empirical"]["sample"] == 40


def test_an_unusable_measurement_records_nothing_rather_than_the_declared_one_renamed() -> None:
    """An empirical block that fell back to declared numbers is the declared EV in a wig.

    It would read as a second, agreeing opinion and be the first one restated.
    """
    for broken in (None, {}, "empirical", {"e_win_hat": "0.009", "e_loss_hat": "0.006"},
                   {"e_win_hat": "0.009", "e_loss_hat": "0.006", "source": ""},
                   {"e_win_hat": None, "e_loss_hat": "0.006", "source": "empirical:x"},
                   {"e_win_hat": "nonsense", "e_loss_hat": "0.006", "source": "empirical:x"}):
        assert "ev_empirical" not in ev_record(
            forecast=FORECAST, expected_return="0.01", expected_risk="0.02", empirical=broken)


def test_a_measurement_never_rewrites_a_row_that_was_scored_under_an_earlier_one() -> None:
    """Nothing back-fills. Two ids are two statements, not one corrected statement."""
    first = ev_record(forecast=FORECAST, expected_return="0.01", expected_risk="0.02",
                      empirical={"e_win_hat": "0.009", "e_loss_hat": "0.006", "source": "empirical:aaa"})
    second = ev_record(forecast=FORECAST, expected_return="0.01", expected_risk="0.02",
                       empirical={"e_win_hat": "0.012", "e_loss_hat": "0.004", "source": "empirical:bbb"})
    assert first["ev_empirical"]["e_win_source"] == "empirical:aaa"
    assert second["ev_empirical"]["e_win_source"] == "empirical:bbb"
    assert first["ev_empirical"]["expected_value"] != second["ev_empirical"]["expected_value"]
    # The declared figure is the same in both, because it never depended on either.
    assert first["expected_value"] == second["expected_value"]


# ------------------------------------------------------------------ the agent wiring


def _lean():
    from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
    moment = datetime(2026, 9, 22, 5, tzinfo=timezone.utc)
    return tuple(
        AgentEvidence(agent, domain, "TRENT", Stance.BUY, Decimal("0.60"), Decimal("0.01"),
                      Decimal("0.02"), ("declared",), moment, 10)
        for agent, domain in (("technical-quant-mas", AgentDomain.TECHNICAL),
                              ("geopolitical-analyst", AgentDomain.NEWS),
                              ("indian-equities", AgentDomain.COUNTRY))
    )


def _decide(**kwargs):
    from quant_ai.agents.atlas import AtlasInvestmentAgent
    moment = datetime(2026, 9, 22, 5, tzinfo=timezone.utc)
    decision = AtlasInvestmentAgent(**kwargs).decide("TRENT", _lean(), moment)
    return decision, decision.provenance["expected_value"]


def test_the_live_path_carries_no_measurement_until_an_operator_supplies_one() -> None:
    """Off by default, like the gate. A measurement is a decision, not a discovery."""
    _, block = _decide()
    assert block["e_win_source"] == block["e_loss_source"] == DECLARED
    assert "ev_empirical" not in block and block["expected_value"] == "-0.001500"


def test_a_supplied_measurement_is_recorded_beside_the_declared_number() -> None:
    """Same probability, same cost, different payoffs - and the sign flips.

    That is the finding in one row: the entry is refused by arithmetic the specialists
    invented, not by anything the market did.
    """
    measured = {"e_win_hat": "0.009000", "e_loss_hat": "0.006000", "n": 40,
                "source": "empirical:abc123"}
    _, block = _decide(empirical_payoffs=lambda **_: measured)
    assert block["expected_value"] == "-0.001500"           # declared, untouched
    assert block["ev_empirical"]["expected_value"] == "0.002750"
    assert block["ev_empirical"]["e_win_source"] == "empirical:abc123"


def test_an_artifact_that_raises_cannot_take_down_the_cadence() -> None:
    """Evidence capture is never a reason for a decision to fail."""
    def explode(**_kwargs):
        raise RuntimeError("artifact unreadable")

    decision, block = _decide(empirical_payoffs=explode)
    assert decision.action is not None and "ev_empirical" not in block
    assert block["expected_value"] == "-0.001500"


def test_the_agent_asks_for_the_decisions_own_symbol_playbook_and_regime() -> None:
    """A measurement fetched for the wrong group is worse than none."""
    seen: dict = {}

    def capture(**kwargs):
        seen.update(kwargs)

    _decide(empirical_payoffs=capture)
    assert seen["symbol"] == "TRENT"
    assert set(seen) == {"symbol", "playbook", "regime"}
