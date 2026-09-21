"""Every decision states what it expects, before the outcome exists.

Atlas recorded a stance and a confidence, neither of which is a forecast: nothing said
what would have to happen for the decision to be right, by when, or net of what cost. So
there was no track record to score. These tests pin the claim's shape, the mapping that
produces it, and the property that matters most: a decision the floor HELD still records
what it thought would happen, so the cost of the floor itself becomes measurable.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

# Sibling modules by name: pytest puts tests/ on sys.path, not the repository root.
from test_exploration_budget import FLAT, LEAN, NOW, TREND, budget, consensus_client

from quant_ai.agents import forecast
from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import Stance
from quant_ai.analytics import decision_journal as journal
from quant_ai.execution.paper_ledger import PaperBrokerService


def decide(policy: AtlasPolicy, evidence=LEAN, context=TREND):
    return AtlasInvestmentAgent(policy=policy).decide("TRENT", evidence, NOW, evidence_context=context)


# ------------------------------------------------------------------ the mapping


def test_a_neutral_book_claims_nothing_and_says_so_as_one_half() -> None:
    assert forecast.probability_up(Decimal(0), Decimal("0.62")) == Decimal("0.5000")
    # Conviction cannot manufacture a direction the specialists did not take.
    assert forecast.probability_up(Decimal(0), Decimal(1)) == Decimal("0.5000")


def test_the_mapping_is_monotone_symmetric_and_never_asserts_certainty() -> None:
    weak = forecast.probability_up(Decimal("0.5"), Decimal("0.5"))
    strong = forecast.probability_up(Decimal("1.5"), Decimal("0.5"))
    surer = forecast.probability_up(Decimal("0.5"), Decimal("0.9"))
    assert Decimal("0.5") < weak < strong
    assert weak < surer
    # A down-lean is the mirror of the same up-lean.
    assert forecast.probability_up(Decimal("-1.5"), Decimal("0.5")) == Decimal(1) - strong
    # Certainty is never claimed: a log score on 0 or 1 is infinite and the claim is false.
    assert forecast.probability_up(Decimal(9), Decimal(9)) == forecast.CEILING
    assert forecast.probability_up(Decimal(-9), Decimal(9)) == forecast.FLOOR


def test_a_record_carries_its_horizon_cost_and_invalidation() -> None:
    record = forecast.record(subject="TRENT", weighted_score=Decimal(1),
                             confidence=Decimal("0.45"), now=NOW, stop_price=Decimal(100))
    assert record["schema"] == forecast.SCHEMA
    # The basis names the mapping, so a refit ships a new id rather than moving the
    # meaning of numbers already written.
    assert record["basis"] == forecast.BASIS
    assert record["horizon_seconds"] == forecast.HORIZON_SECONDS
    assert record["resolves_at"] > record["made_at"]
    assert record["outcome_definition"] == "forward_return_net_of_cost_positive"
    assert record["cost_bps"] == str(forecast.COST_BPS)
    assert any(item.startswith("horizon_elapsed_at:") for item in record["invalidation"])
    assert "stop_price_touched:100" in record["invalidation"]
    with pytest.raises(ValueError):
        forecast.record(subject="TRENT", weighted_score=Decimal(1), confidence=Decimal("0.45"),
                        now=NOW, horizon_seconds=0)


# ------------------------------------------------------------------ on a decision


def test_a_decision_the_floor_held_still_records_what_it_expected() -> None:
    # Three BUY voters at 0.45 sit under the consensus floor, so the action is NEUTRAL.
    decision = decide(budget(max_per_day=0))
    assert decision.action is Stance.NEUTRAL
    recorded = forecast.probability_of(decision.provenance)
    # The claim survives the hold. Without this the only scoreable decisions would be the
    # handful that clear the floor, and the floor's own cost would stay invisible.
    assert recorded is not None and recorded > Decimal("0.5")


def test_an_all_neutral_book_records_exactly_one_half() -> None:
    assert forecast.probability_of(decide(budget(max_per_day=0), evidence=FLAT).provenance) == Decimal("0.5000")


def test_a_hold_before_any_consensus_records_no_forecast() -> None:
    # Too few voters to form a consensus: there is no claim to make, so none is stored.
    decision = decide(budget(max_per_day=0), evidence=LEAN[:1])
    assert forecast.probability_of(decision.provenance) is None


def test_the_model_path_records_the_model_s_own_claim() -> None:
    agent = AtlasInvestmentAgent(policy=AtlasPolicy(), llm_client=consensus_client("BUY", 0.80))
    decision = asyncio.run(agent.decide_with_llm("TRENT", LEAN, NOW, evidence_context=TREND))
    recorded = forecast.probability_of(decision.provenance)
    assert recorded is not None and recorded > Decimal("0.5")


# ------------------------------------------------------------------ in the journal


def test_the_journal_stores_the_forecast_beside_the_decision(tmp_path) -> None:
    decision = decide(budget(max_per_day=0))
    columns = journal.forecast_of(decision)
    assert columns["forecast_basis"] == forecast.BASIS
    assert columns["forecast_horizon_seconds"] == forecast.HORIZON_SECONDS
    assert Decimal(columns["forecast_probability_up"]) > Decimal("0.5")
    assert columns["forecast_cost_bps"] == str(forecast.COST_BPS)
    assert columns["forecast_resolves_at"]


def test_a_stored_forecast_survives_the_round_trip_through_the_ledger(tmp_path) -> None:
    """The columns exist on the table and the insert actually names them.

    Adding a column to MIGRATIONS alone gives the table the column and never writes to it:
    ``insert_decision`` and ``load_rows`` both name exactly ``COLUMNS``, so the forecast
    would be computed on every decision and dropped on the way to disk, with nothing
    failing. A forecast nobody stored cannot be scored, which is the whole point of it.
    """
    broker = PaperBrokerService(database=str(tmp_path / "ledger.db"))
    columns = journal.forecast_of(decide(budget(max_per_day=0)))
    assert set(columns) <= set(journal.COLUMNS), "forecast columns must be insertable"
    journal.insert_decision(broker, {
        "decision_id": "f1", "tenant_id": "pilot", "symbol": "TRENT", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": NOW.isoformat(), "stance": "BUY",
        "governance": "abstained", "agents": "{}", **columns,
    })
    stored = next(r for r in journal.load_rows(broker, tenant_id="pilot") if r["decision_id"] == "f1")
    for name, value in columns.items():
        assert stored[name] == value, f"{name} did not survive the round trip"
    assert forecast.probability_of({"forecast": None}) is None


def test_a_decision_without_a_forecast_stores_nulls_rather_than_a_guess() -> None:
    assert journal.forecast_of(decide(budget(max_per_day=0), evidence=LEAN[:1])) == {
        "forecast_probability_up": None, "forecast_horizon_seconds": None,
        "forecast_resolves_at": None, "forecast_cost_bps": None, "forecast_basis": None,
    }
