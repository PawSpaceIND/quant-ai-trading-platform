"""The exploration budget: bounded probe entries below the conviction floor, paper only.

On 21 September 2026, the first twelve-name session, every one of the day's decisions was
a hold: the specialists leaned, never with the conviction the 0.55 floor demands, and the
decision-quality report had nothing directional to score. A budget of a few probes a day
at a fraction of equity turns such a lean into a directional decision the outcome resolver
can grade. Every gate downstream still applies; these tests pin the policy itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.agents.atlas import (
    EXPLORATION_FRACTION_ENV,
    EXPLORATION_MAX_ENV,
    EXPLORATION_MIN_CONFIDENCE_ENV,
    EXPLORATION_MIN_SCORE_ENV,
    AtlasInvestmentAgent,
    AtlasPolicy,
    atlas_policy_from_env,
    probe_quantity,
)
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, EvidenceContext, Stance
from quant_ai.analytics import decision_journal as journal
from quant_ai.governance.runtime_manifest import describe
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.operations.premarket import premarket_checks

NOW = datetime(2026, 9, 21, 5, 0, tzinfo=timezone.utc)  # 10:30 IST


def evidence(agent: str, domain: AgentDomain, stance: Stance, confidence: str) -> AgentEvidence:
    return AgentEvidence(agent, domain, "TRENT", stance, Decimal(confidence), Decimal("0.01"),
                         Decimal("0.02"), ("declared",), NOW, 10)


# Three voters lean BUY at 0.45: weighted score 1.0, average confidence 0.45, under the floor.
LEAN = (
    evidence("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY, "0.45"),
    evidence("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY, "0.45"),
    evidence("indian-equities", AgentDomain.COUNTRY, Stance.BUY, "0.45"),
    evidence("risk-desk", AgentDomain.RISK, Stance.NEUTRAL, "0.70"),
)
FLAT = tuple(
    evidence(item.agent_id, item.domain, Stance.NEUTRAL, str(item.confidence)) for item in LEAN
)
SELL_LEAN = tuple(
    evidence(item.agent_id, item.domain, Stance.SELL if item.stance is Stance.BUY else item.stance,
             str(item.confidence)) for item in LEAN
)


def budget(max_per_day: int = 3, **overrides) -> AtlasPolicy:
    return AtlasPolicy(exploration_max_per_day=max_per_day, **overrides)


# The regime the probe tests run in: a trend, whose playbook allows probes. Defensive and
# crisis playbooks withhold probes (test_regime_playbooks); an unread regime tightens nothing.
TREND = EvidenceContext(regime=(("label", "trending_up"), ("timeframe", "1d")))


def test_policy_defaults_are_off_and_bounds_are_enforced() -> None:
    policy = AtlasPolicy()
    assert policy.exploration_max_per_day == 0
    assert (policy.exploration_min_confidence, policy.exploration_min_weighted_score,
            policy.exploration_notional_fraction) == (Decimal("0.40"), Decimal("0.45"), Decimal("0.01"))
    for bad in (
        {"exploration_max_per_day": -1},
        {"exploration_min_confidence": Decimal("0.56")},
        {"exploration_min_confidence": Decimal(0)},
        {"exploration_min_weighted_score": Decimal(0)},
        {"exploration_min_weighted_score": Decimal("0.46")},
        {"exploration_notional_fraction": Decimal("0.06")},
        {"exploration_notional_fraction": Decimal(0)},
    ):
        with pytest.raises(ValueError):
            AtlasPolicy(**bad)


def test_without_a_budget_a_lean_below_the_floor_stays_a_hold_and_records_the_consensus() -> None:
    decision = AtlasInvestmentAgent().decide("TRENT", LEAN, NOW)
    assert decision.action is Stance.NEUTRAL
    assert decision.provenance["consensus"] == {
        "weighted_score": "1.0000", "average_confidence": "0.4500", "expected_risk": "0.0200", "participating": 3,
    }
    assert "exploration" not in decision.provenance
    assert not any(item.startswith("exploration") for item in decision.rationale)


def test_a_budget_turns_the_lean_into_a_labelled_probe_and_counts_it() -> None:
    atlas = AtlasInvestmentAgent(policy=budget(3))
    first = atlas.decide("TRENT", LEAN, NOW, evidence_context=TREND)
    assert first.action is Stance.BUY
    assert first.confidence == Decimal("0.45")
    assert first.rationale[0] == "exploration_probe:weighted_consensus=1.0000;average_confidence=0.4500;budget=1/3"
    assert first.provenance["exploration"] == {
        "probe": True, "weighted_score": "1.0000", "average_confidence": "0.4500",
        "budget_used": 1, "budget_max": 3, "notional_fraction": "0.01",
        "min_weighted_score": "0.4500", "overrode": "NEUTRAL",
    }
    assert first.provenance["mode"] == "deterministic"
    second = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=10), evidence_context=TREND)
    third = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=20), evidence_context=TREND)
    assert (second.provenance["exploration"]["budget_used"], third.provenance["exploration"]["budget_used"]) == (2, 3)
    fourth = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=30), evidence_context=TREND)
    assert fourth.action is Stance.NEUTRAL
    assert "exploration_budget_exhausted=3/3" in fourth.rationale
    assert "exploration" not in fourth.provenance
    # A new IST session starts the count again.
    tomorrow = atlas.decide("TRENT", LEAN, NOW + timedelta(days=1), evidence_context=TREND)
    assert tomorrow.provenance["exploration"]["budget_used"] == 1


@pytest.mark.parametrize("case", ["flat", "sell", "low_confidence", "risky", "hard_hold", "conviction"])
def test_only_a_buy_lean_at_the_minimum_confidence_under_the_floor_is_probed(case) -> None:
    atlas = AtlasInvestmentAgent(policy=budget(3))
    if case == "flat":
        decision = atlas.decide("TRENT", FLAT, NOW)
        assert decision.action is Stance.NEUTRAL
    elif case == "sell":
        decision = atlas.decide("TRENT", SELL_LEAN, NOW)
        assert decision.action is Stance.NEUTRAL  # the floor holds it; no SELL probe exists
    elif case == "low_confidence":
        thin = tuple(evidence(i.agent_id, i.domain, i.stance, "0.30") for i in LEAN)
        decision = atlas.decide("TRENT", thin, NOW)
        assert decision.action is Stance.NEUTRAL
    elif case == "risky":
        risky = tuple(AgentEvidence(i.agent_id, i.domain, "TRENT", i.stance, i.confidence, i.expected_return,
                                    Decimal("0.20"), i.rationale, NOW, 10) for i in LEAN)
        decision = atlas.decide("TRENT", risky, NOW)
        assert decision.action is Stance.NEUTRAL
    elif case == "hard_hold":
        decision = atlas.decide("TRENT", LEAN[:2], NOW)  # two voters: coverage floor
        assert decision.action is Stance.NEUTRAL
        assert "insufficient_agent_coverage" in decision.rationale
        assert "consensus" not in decision.provenance
    else:
        strong = tuple(evidence(i.agent_id, i.domain, i.stance, "0.80") for i in LEAN)
        decision = atlas.decide("TRENT", strong, NOW)
        assert decision.action is Stance.BUY  # above the floor: a conviction trade, not a probe
        assert not decision.rationale[0].startswith("exploration")
    assert "exploration" not in decision.provenance
    assert atlas._probes_issued == {}


def test_the_journal_count_seeds_the_budget_and_an_unreadable_count_spends_nothing() -> None:
    seeded = AtlasInvestmentAgent(policy=budget(3), exploration_used=lambda now: 2)
    decision = seeded.decide("TRENT", LEAN, NOW, evidence_context=TREND)
    assert decision.provenance["exploration"]["budget_used"] == 3
    assert seeded.decide("TRENT", LEAN, NOW + timedelta(minutes=10), evidence_context=TREND).action is Stance.NEUTRAL

    def broken(now):
        raise OSError("ledger unreadable")

    fail_closed = AtlasInvestmentAgent(policy=budget(3), exploration_used=broken)
    assert fail_closed.decide("TRENT", LEAN, NOW, evidence_context=TREND).action is Stance.NEUTRAL


def consensus_client(stance: str, confidence: float = 0.5) -> AnthropicSwarmClient:
    payload = {"stance": stance, "confidence": confidence, "expected_return": 0.01, "expected_risk": 0.01,
               "rationale": ["model view"],
               "xai_proof": {"summary": "paper-only", "supporting_factors": ["a"], "risk_factors": ["b"]}}
    block = SimpleNamespace(type="tool_use", name="trading_consensus", input=payload)
    usage = SimpleNamespace(input_tokens=100, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    response = SimpleNamespace(model="m", id="r", stop_reason="tool_use", content=[block], usage=usage)
    return AnthropicSwarmClient(client=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response))), model="m")


def test_a_model_hold_over_a_specialist_lean_becomes_a_probe_and_a_model_buy_is_left_alone() -> None:
    held = AtlasInvestmentAgent(policy=budget(3), llm_client=consensus_client("NEUTRAL"))
    decision = asyncio.run(held.decide_with_llm("TRENT", LEAN, NOW, evidence_context=TREND))
    assert decision.action is Stance.BUY
    assert decision.provenance["mode"] == "llm"
    assert decision.provenance["exploration"]["overrode"] == "NEUTRAL"
    assert decision.rationale[0].startswith("exploration_probe:")
    assert "anthropic_model=m" in decision.rationale

    # A model BUY over a swarm the floor held no longer stands on its own. The model may
    # only tighten, so the deterministic NEUTRAL is the action, and the exploration budget
    # is what turns it into a BUY - small, labelled and counted. Before this the model's
    # own stance became a full-sized entry on conviction no specialist had reached.
    bought = AtlasInvestmentAgent(policy=budget(3), llm_client=consensus_client("BUY", 0.7))
    decision = asyncio.run(bought.decide_with_llm("TRENT", LEAN, NOW, evidence_context=TREND))
    assert decision.action is Stance.BUY
    assert decision.provenance["model_view"] == {
        "stance": "BUY", "confidence": "0.7000", "expected_risk": "0.0100",
        "applied": "advisory", "deterministic_stance": "NEUTRAL",
    }
    assert decision.provenance["exploration"]["overrode"] == "NEUTRAL"
    assert bought._probes_issued != {}


def test_probe_quantity_is_the_fraction_of_equity_in_whole_units_or_zero() -> None:
    assert probe_quantity(Decimal(100000), "0.01", Decimal(320)) == 3
    assert probe_quantity(Decimal(100000), Decimal("0.01"), Decimal(5100)) == 0
    assert probe_quantity(Decimal(100000), "0.01", Decimal(0)) == 0
    assert probe_quantity(Decimal(0), "0.01", Decimal(100)) == 0
    assert probe_quantity(Decimal(100000), "not-a-number", Decimal(100)) == 0


def test_the_environment_arms_the_budget_and_refuses_a_malformed_value() -> None:
    assert atlas_policy_from_env({}) == AtlasPolicy()
    armed = atlas_policy_from_env({EXPLORATION_MAX_ENV: "3", EXPLORATION_MIN_CONFIDENCE_ENV: "0.40",
                                   EXPLORATION_FRACTION_ENV: "0.02"})
    assert (armed.exploration_max_per_day, armed.exploration_notional_fraction) == (3, Decimal("0.02"))
    assert armed.exploration_min_weighted_score == Decimal("0.45"), "unset keeps the full-entry lean"
    lowered = atlas_policy_from_env({EXPLORATION_MAX_ENV: "3", EXPLORATION_MIN_SCORE_ENV: "0.35"})
    assert lowered.exploration_min_weighted_score == Decimal("0.35")
    for bad in ({EXPLORATION_MAX_ENV: "three"}, {EXPLORATION_FRACTION_ENV: "0.5"}, {EXPLORATION_MIN_CONFIDENCE_ENV: "x"},
                {EXPLORATION_MIN_SCORE_ENV: "0.5"}, {EXPLORATION_MIN_SCORE_ENV: "0"}, {EXPLORATION_MIN_SCORE_ENV: "x"}):
        with pytest.raises(RuntimeError, match="unsupported exploration budget setting"):
            atlas_policy_from_env(bad)


def test_the_manifest_carries_the_budget_so_arming_it_is_a_recorded_change() -> None:
    issues: list[str] = []
    described = describe(AtlasInvestmentAgent(policy=budget(3)), issues)
    assert issues == []
    policy = described["parameters"]["policy"]
    assert policy["exploration_max_per_day"] == 3
    assert policy["exploration_notional_fraction"] == "0.01"


def test_the_journal_records_the_probe_and_counts_the_session(tmp_path) -> None:
    from quant_ai.execution.paper_ledger import PaperBrokerService

    broker = PaperBrokerService(tmp_path / "ledger.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    proposal = SimpleNamespace(
        decision_id="d1", symbol="TRENT", market="INDIA", asset_class="EQUITY", side=None, quantity=0,
        reference_price=Decimal(5000), stop_price=None, take_profit_price=None, confidence=Decimal("0.45"),
        expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        provenance={"exploration": {"probe": True}, "mode": "deterministic"},
    )
    result = SimpleNamespace(proposal=proposal, fill=None, risk_decision=SimpleNamespace(approved=False, reason="x"),
                             xai_trace=SimpleNamespace(input_matrix=(), regime="ranging", provenance=None))
    row = journal.decision_row(result, tenant_id="ghost", now=NOW)
    assert row["probe"] == 1
    plain = journal.decision_row(SimpleNamespace(**{**vars(result), "proposal": SimpleNamespace(**{**vars(proposal), "provenance": {}, "decision_id": "d2"})}),
                                 tenant_id="ghost", now=NOW)
    assert plain["probe"] == 0
    journal.insert_decision(broker, row)
    journal.insert_decision(broker, plain)
    yesterday = {**row, "decision_id": "d0", "decided_at": (NOW - timedelta(days=1)).isoformat()}
    journal.insert_decision(broker, yesterday)
    assert journal.count_probes(broker, tenant_id="ghost", now=NOW) == 1
    assert journal.count_probes(broker, tenant_id="ghost", now=NOW - timedelta(days=1)) == 1
    assert journal.count_probes(broker, tenant_id="other", now=NOW) == 0
    # An older ledger gains the column on first use rather than refusing the insert.
    with broker._lock, broker._connection as db:
        db.execute("ALTER TABLE paper_decision_journal DROP COLUMN probe")
    journal.ensure_journal(broker)
    assert journal.count_probes(broker, tenant_id="ghost", now=NOW) == 0
    journal.insert_decision(broker, {**row, "decision_id": "d3"})
    assert journal.count_probes(broker, tenant_id="ghost", now=NOW) == 1


def test_the_premarket_check_reports_the_budget_as_information(tmp_path) -> None:
    from test_premarket_check import env, manifest, payload  # pytest puts tests/ on sys.path

    checks = premarket_checks(payload(), manifest(), env(), datetime(2026, 9, 21, 3, 20, tzinfo=timezone.utc))
    line = next(c for c in checks if c.id == "exploration")
    assert (line.state, line.detail) == ("INFO", "off (set PRAMANA_EXPLORATION_MAX_PER_DAY)")
    armed = premarket_checks(payload(), manifest(), env(**{EXPLORATION_MAX_ENV: "3"}),
                             datetime(2026, 9, 21, 3, 20, tzinfo=timezone.utc))
    line = next(c for c in armed if c.id == "exploration")
    assert line.detail == ("up to 3 probes/day at 1.00% of equity when specialists lean BUY "
                           "(score >= 0.45) at >= 0.40 confidence")
    lowered = premarket_checks(payload(), manifest(), env(**{EXPLORATION_MAX_ENV: "3", EXPLORATION_MIN_SCORE_ENV: "0.35"}),
                               datetime(2026, 9, 21, 3, 20, tzinfo=timezone.utc))
    assert "(score >= 0.35)" in next(c for c in lowered if c.id == "exploration").detail
    broken = premarket_checks(payload(), manifest(), env(**{EXPLORATION_MAX_ENV: "many"}),
                              datetime(2026, 9, 21, 3, 20, tzinfo=timezone.utc))
    assert next(c for c in broken if c.id == "exploration").state == "FAIL"
    json.dumps([c.__dict__ for c in armed])  # the report stays serialisable


# 24 September 2026: the strongest lean all day was 0.37 at 0.72 confidence - one voter
# saying BUY a little more confidently than two saying NEUTRAL - against a 0.45 bar.
WEAK_LEAN = (
    evidence("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY, "0.80"),
    evidence("geopolitical-analyst", AgentDomain.NEWS, Stance.NEUTRAL, "0.68"),
    evidence("indian-equities", AgentDomain.COUNTRY, Stance.NEUTRAL, "0.68"),
    evidence("risk-desk", AgentDomain.RISK, Stance.NEUTRAL, "0.70"),
)


def test_a_lowered_probe_bar_probes_the_weak_lean_the_default_holds() -> None:
    held = AtlasInvestmentAgent(policy=budget(3)).decide("TRENT", WEAK_LEAN, NOW, evidence_context=TREND)
    assert held.action is Stance.NEUTRAL and "exploration" not in held.provenance
    assert held.provenance["consensus"]["weighted_score"] == "0.3704"

    lowered = budget(3, exploration_min_weighted_score=Decimal("0.35"))
    probe = AtlasInvestmentAgent(policy=lowered).decide("TRENT", WEAK_LEAN, NOW, evidence_context=TREND)
    assert probe.action is Stance.BUY
    assert probe.provenance["exploration"]["weighted_score"] == "0.3704"
    assert probe.provenance["exploration"]["min_weighted_score"] == "0.3500"


def test_the_lowered_bar_is_still_a_bar_and_still_needs_a_budget() -> None:
    lowered = budget(3, exploration_min_weighted_score=Decimal("0.35"))
    # One BUY and two NEUTRAL at equal confidence is a 0.33 lean: under 0.35, a hold.
    even = tuple(evidence(item.agent_id, item.domain, item.stance, "0.72") for item in WEAK_LEAN)
    held = AtlasInvestmentAgent(policy=lowered).decide("TRENT", even, NOW, evidence_context=TREND)
    assert held.provenance["consensus"]["weighted_score"] == "0.3333"
    assert held.action is Stance.NEUTRAL and "exploration" not in held.provenance
    # The bar alone trades nothing: with no budget the weak lean stays a hold.
    off = AtlasPolicy(exploration_min_weighted_score=Decimal("0.35"))
    assert AtlasInvestmentAgent(policy=off).decide("TRENT", WEAK_LEAN, NOW, evidence_context=TREND).action is Stance.NEUTRAL


def test_compose_forwards_the_probe_bar_with_the_full_entry_default() -> None:
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy/docker-compose.yml").read_text())
    ghost = compose["services"]["pramana-ghost"]["environment"]
    assert ghost[EXPLORATION_MIN_SCORE_ENV] == "${PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE:-0.45}"
