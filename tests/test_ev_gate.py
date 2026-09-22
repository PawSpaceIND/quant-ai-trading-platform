"""The expected value of a decision, and the gate that is not yet allowed to use it.

A probability is not a reason. 0.65 is a good bet at three-to-one and a bad one at the
payoffs this book actually declares: with a 1% win, a 2% loss and 10 bps of cost, an entry
does not break even until 0.70, and the live swarm has been producing 0.59 to 0.65. That
is the finding this module records - and deliberately does not act on.

So there are two separate guarantees here, and the tests keep them apart:

**Every decision carries its expected value.** Computed from the decision's own consensus
payoffs and the cost stored with its own forecast, so a later change of cost policy cannot
rescore a decision made under the old one. Recorded whether or not anything reads it.

**Nothing reads it unless an operator armed it.** ``PRAMANA_EV_GATE`` is off. The v1
probability mapping is a hypothesis, not a calibration, and a gate on an uncalibrated
probability is a confident, wrong number given veto power over the book. The arming
decision belongs to ``promotion_report()``, on evidence that does not exist yet.

The arithmetic below is exact and deliberate. Three specialists leaning BUY at 0.60
produce p=0.6500 and EV -0.001500: today's book, and the entry the gate would hold. The
same three at 0.90 produce p=0.7250 and EV +0.000750: what clearing the cost requires.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

# Sibling test modules by name: pytest puts tests/ on sys.path, the repository root it
# does not (the console script CI runs), so a ``tests.`` package import fails there.
from test_exploration_budget import NOW, TREND, consensus_client, evidence

from quant_ai.agents import expected_value as valuation
from quant_ai.agents.atlas import (
    EV_GATE_ENV,
    AtlasInvestmentAgent,
    AtlasPolicy,
    atlas_policy_from_env,
)
from quant_ai.agents.contracts import AgentDomain, Stance

DOMAINS = (AgentDomain.TECHNICAL, AgentDomain.NEWS, AgentDomain.COUNTRY, AgentDomain.MACRO)
AGENTS = ("technical-quant-mas", "geopolitical-analyst", "indian-equities", "macro-strategist")


def lean(stance: Stance, confidence: str, *, count: int = 3) -> tuple:
    """``count`` voters agreeing at one confidence, each declaring the book's 1%/2% payoff."""
    return tuple(
        evidence(AGENTS[index], DOMAINS[index], stance, confidence) for index in range(count)
    )


# Today's book: a clean BUY by every deterministic rule, and a losing bet at its own
# declared payoffs. weighted_score 1, confidence 0.60, p 0.6500, EV -0.001500.
THIN = lean(Stance.BUY, "0.60")
# What clearing 10 bps against a 1%/2% payoff actually takes. p 0.7250, EV +0.000750.
CONVINCED = lean(Stance.BUY, "0.90")
# One STRONG_BUY among three BUYs at the floor: weighted_score exactly 1.25, so the action
# is STRONG_BUY, while p is only 0.6719 and EV -0.000843. A louder stance is not a better bet.
LOUD = lean(Stance.STRONG_BUY, "0.55", count=1) + lean(Stance.BUY, "0.55")[:3]
# The defensive side of the same book. p 0.3500, EV -0.010500 - far worse than the entry
# the gate holds, which is exactly why the gate must not read it.
EXIT = lean(Stance.SELL, "0.60")


def armed(**overrides) -> AtlasPolicy:
    return AtlasPolicy(ev_gate=True, **overrides)


def test_every_decision_records_what_it_expects_to_be_worth() -> None:
    """The inputs, not just the answer: the arithmetic must be checkable without the source."""
    decision = AtlasInvestmentAgent().decide("TRENT", THIN, NOW)
    block = decision.provenance["expected_value"]
    assert block["schema"] == valuation.SCHEMA
    assert block["probability_up"] == "0.6500"
    # The consensus payoffs, not a house assumption. The losing leg is signed.
    assert (block["expected_win"], block["expected_loss"]) == ("0.01", "-0.02")
    assert block["cost_bps"] == "10"
    assert block["expected_value"] == "-0.001500"
    # The mapping the probability came from. An EV means nothing without the basis under it.
    assert block["basis"] == "weighted_lean_times_confidence.v1"


def test_the_gate_is_off_by_default_so_a_losing_bet_is_still_taken() -> None:
    """The finding is recorded; the book is unchanged.

    This is the whole posture of the PR. An EV of -0.0015 on a decision the engine still
    calls BUY is the evidence that the declared payoffs and the probability disagree. It
    is not yet evidence that the probability is the one to trust.
    """
    assert AtlasPolicy().ev_gate is False
    decision = AtlasInvestmentAgent().decide("TRENT", THIN, NOW)
    assert decision.action is Stance.BUY
    assert decision.provenance["expected_value"]["expected_value"] == "-0.001500"
    assert "ev_gate" not in decision.provenance


def test_an_armed_gate_holds_an_entry_that_does_not_clear_its_own_cost() -> None:
    decision = AtlasInvestmentAgent(policy=armed()).decide("TRENT", THIN, NOW)
    assert decision.action is Stance.NEUTRAL
    # The number that refused it, in the reason, so an operator reading the proof does not
    # have to recompute it from the payoffs to know how far short it fell.
    assert decision.rationale[0] == "ev_gate_held:-0.001500"
    assert decision.provenance["ev_gate"] == {
        "armed": True, "expected_value": "-0.001500", "held": True,
    }


def test_an_armed_gate_admits_an_entry_that_clears_it() -> None:
    """A gate that held everything would prove nothing about the gate."""
    decision = AtlasInvestmentAgent(policy=armed()).decide("TRENT", CONVINCED, NOW)
    assert decision.action is Stance.BUY
    assert decision.provenance["expected_value"]["expected_value"] == "0.000750"
    assert "ev_gate" not in decision.provenance


def test_a_strong_buy_is_held_on_the_same_terms_as_a_buy() -> None:
    """The gate reads risk added, not the name of the stance.

    A STRONG_BUY at a 0.55 book is a louder claim on a worse bet than the BUY above: the
    weighted lean clears 1.25 while the probability sits at 0.6719, under break-even.
    """
    assert AtlasInvestmentAgent().decide("TRENT", LOUD, NOW).action is Stance.STRONG_BUY
    decision = AtlasInvestmentAgent(policy=armed()).decide("TRENT", LOUD, NOW)
    assert decision.action is Stance.NEUTRAL
    assert decision.rationale[0] == "ev_gate_held:-0.000843"


def test_a_protective_exit_is_never_held_by_the_gate() -> None:
    """The one case the gate must get wrong if it is naive about direction.

    This exit's EV is -0.010500 - seven times worse than the entry the gate just refused.
    Reading it the same way would hold the book's defence open in exactly the conditions
    it exists for. The gate may tighten a stance; it may never loosen one.
    """
    decision = AtlasInvestmentAgent(policy=armed()).decide("TRENT", EXIT, NOW)
    assert decision.action is Stance.SELL
    assert decision.provenance["expected_value"]["expected_value"] == "-0.010500"
    assert "ev_gate" not in decision.provenance


def test_an_entry_whose_expected_value_could_not_be_computed_is_refused() -> None:
    """Unmeasured is not the same as measured and fine.

    ``record()`` returns None when the probability or the payoff could not be read. A gate
    that admitted that case would pass exactly the decisions it knows least about.
    """
    atlas = AtlasInvestmentAgent(policy=armed())
    original = valuation.record
    try:
        valuation.record = lambda **_: None
        decision = atlas.decide("TRENT", CONVINCED, NOW)
    finally:
        valuation.record = original
    assert decision.action is Stance.NEUTRAL
    assert decision.rationale[0] == "ev_gate_held:unavailable"
    assert "expected_value" not in decision.provenance
    assert decision.provenance["ev_gate"] == {
        "armed": True, "expected_value": None, "held": True,
    }


def test_a_hold_the_swarm_never_leaned_into_is_left_exactly_as_it_was() -> None:
    """A hard hold has no forecast and no EV, and the gate must not relabel it.

    "insufficient_agent_coverage" and "ev_gate_held" are different facts about a decision.
    A gate that overwrote the first with the second would erase the reason the engine
    actually stopped.
    """
    decision = AtlasInvestmentAgent(policy=armed()).decide("TRENT", lean(Stance.BUY, "0.60", count=2), NOW)
    assert decision.action is Stance.NEUTRAL
    assert decision.rationale[0] == "insufficient_agent_coverage"
    assert "ev_gate" not in decision.provenance and "expected_value" not in decision.provenance


def test_a_gate_held_entry_may_still_be_probed_and_the_hold_is_kept_in_the_proof() -> None:
    """Deliberate, and the reason the gate is survivable at all.

    A gate that also stopped probes would stop the resolved sample that decides whether
    the gate was ever justified - it would silence the book to protect a mapping nobody
    has yet scored. So the budget may still buy a labelled probe at a fraction of equity,
    and the proof carries both facts: the gate refused this, and a probe was taken anyway.
    """
    atlas = AtlasInvestmentAgent(policy=armed(exploration_max_per_day=1))
    decision = atlas.decide("TRENT", THIN, NOW, evidence_context=TREND)
    assert decision.action is Stance.BUY
    assert decision.rationale[0].startswith("exploration_probe:")
    assert decision.provenance["exploration"]["overrode"] == "NEUTRAL"
    assert decision.provenance["ev_gate"]["held"] is True
    # A probe is 1% of equity; the refused entry would have been sized on conviction.
    assert decision.provenance["exploration"]["notional_fraction"] == "0.01"


def test_the_gate_sits_after_the_model_overlay_on_the_llm_path_too() -> None:
    """Both paths, one gate.

    The model asks for more risk than the swarm earned, the overlay refuses it (it may
    only tighten), and the gate then holds the deterministic BUY that was left. A gate
    wired into only one of the two ``decide`` paths would be armed for the replay and
    absent from the live daemon.
    """
    atlas = AtlasInvestmentAgent(policy=armed(), llm_client=consensus_client("STRONG_BUY", 0.99))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", THIN, NOW, evidence_context=TREND))
    assert decision.provenance["mode"] == "llm"
    assert decision.provenance["model_view"]["applied"] == "advisory"
    assert decision.action is Stance.NEUTRAL
    assert decision.rationale[0] == "ev_gate_held:-0.001500"


def test_arming_the_gate_takes_an_explicit_switch_and_nothing_else() -> None:
    assert atlas_policy_from_env({}).ev_gate is False
    for raw in ("on", "true", "1", "yes", "ON"):
        assert atlas_policy_from_env({EV_GATE_ENV: raw}).ev_gate is True
    for raw in ("off", "false", "0", "no", ""):
        assert atlas_policy_from_env({EV_GATE_ENV: raw}).ev_gate is False
    # A typo must not read as off. Arming is a decision; failing to arm silently is not.
    with pytest.raises(RuntimeError):
        atlas_policy_from_env({EV_GATE_ENV: "maybe"})


def test_the_losing_leg_is_a_loss_and_the_cost_is_paid_either_way() -> None:
    """The two sign errors that would make every EV look better than it is."""
    # p=1 still pays the cost: a certain 1% win nets 0.99%.
    assert valuation.expected_value(
        Decimal(1), expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        cost_bps=Decimal(10),
    ) == Decimal("0.009000")
    # p=0 loses the risk rather than earning it.
    assert valuation.expected_value(
        Decimal(0), expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        cost_bps=Decimal(10),
    ) == Decimal("-0.021000")
    # Break-even for this book is 0.70, not 0.50. The whole finding, in one assertion.
    assert valuation.expected_value(
        Decimal("0.70"), expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        cost_bps=Decimal(10),
    ) == Decimal(0)


def test_a_probability_outside_zero_to_one_is_clamped_rather_than_trusted() -> None:
    """A weight that escaped its bounds must not manufacture EV out of arithmetic."""
    absurd = valuation.expected_value(
        Decimal(2), expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        cost_bps=Decimal(10),
    )
    assert absurd == valuation.expected_value(
        Decimal(1), expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        cost_bps=Decimal(10),
    )


def test_an_incomplete_input_has_no_expected_value_rather_than_a_zero_one() -> None:
    """Zero is a claim that the bet is exactly fair. Absence is not that claim."""
    complete = {"probability_up": "0.65", "cost_bps": "10", "basis": "b"}
    assert valuation.record(
        forecast=complete, expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
    )["expected_value"] == "-0.001500"
    for broken in ({}, None, "forecast", {"probability_up": "0.65"}, {"cost_bps": "10"},
                   {"probability_up": "nonsense", "cost_bps": "10"}):
        assert valuation.record(
            forecast=broken, expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        ) is None
    for missing in (None, "1%", True):
        assert valuation.record(
            forecast=complete, expected_return=missing, expected_risk=Decimal("0.02"),
        ) is None


def test_the_premarket_check_states_the_gate_on_every_run_including_when_it_is_off() -> None:
    """The morning line the operator reads before the session.

    Off is the designed state, and the check still prints it, because the failure mode
    worth catching is not a gate that breaks - it is one that was armed quietly on a
    probability nobody has scored, and then blamed for a silent book.
    """
    import json
    from datetime import datetime, timezone

    from test_premarket_check import env, manifest, payload  # pytest puts tests/ on sys.path

    from quant_ai.operations.premarket import premarket_checks

    opening = datetime(2026, 9, 21, 3, 20, tzinfo=timezone.utc)
    line = next(c for c in premarket_checks(payload(), manifest(), env(), opening) if c.id == "ev_gate")
    assert line.state == "INFO" and line.detail.startswith("off (PRAMANA_EV_GATE)")

    checks = premarket_checks(payload(), manifest(), env(**{EV_GATE_ENV: "on"}), opening)
    line = next(c for c in checks if c.id == "ev_gate")
    assert line.detail.startswith("ARMED (PRAMANA_EV_GATE)")
    # The line names what it does not touch, so nobody reads an armed gate as a stopped book.
    assert "Protective exits and probes are unaffected" in line.detail
    assert "200 resolved forecasts" in line.detail

    broken = premarket_checks(payload(), manifest(), env(**{EV_GATE_ENV: "maybe"}), opening)
    assert next(c for c in broken if c.id == "ev_gate").state == "FAIL"
    json.dumps([c.__dict__ for c in checks])  # the report stays serialisable
