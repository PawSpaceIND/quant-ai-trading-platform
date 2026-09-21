"""Regime playbooks: the regime raises the floor, scales the entry and may withhold probes.

The specialists are the strategy; these tests pin how each deterministic regime label
holds them to account, that every playbook only tightens, that the runtime sizes a BUY
from what the decision recorded, and that the journal and the decision-quality report
can tell the playbooks apart.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant_ai.agents.atlas import (
    REGIME_PLAYBOOKS_ENV,
    AtlasInvestmentAgent,
    AtlasPolicy,
    atlas_policy_from_env,
)
from quant_ai.agents.contracts import EvidenceContext, Stance
from quant_ai.agents.playbook import (
    DEFAULT_PLAYBOOK,
    FLOOR_SIZE_SHARE,
    PLAYBOOKS,
    UNROUTED_PLAYBOOK,
    RegimePlaybook,
    conviction_multiplier,
    describe,
    playbook_for,
    regime_label_of,
    sized_from_provenance,
    sized_quantity,
)
from quant_ai.agents.swarm import TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import decision_quality as quality
from quant_ai.domain.models import AssetClass, Market, Side
from quant_ai.intelligence.regime import REGIME_LABELS
from quant_ai.operations.premarket import premarket_checks
from tests.test_exploration_budget import LEAN, NOW, budget, consensus_client, evidence
from tests.test_premarket_check import env, manifest, payload


def context(label: str | None) -> EvidenceContext | None:
    if label is None:
        return None
    return EvidenceContext(regime=(("label", label), ("timeframe", "1d"), ("trend_strength", Decimal("0.4"))))


# Three voters lean BUY at 0.60: above the 0.55 floor, under defensive (0.65) and crisis (0.70).
MODERATE = tuple(
    evidence(item.agent_id, item.domain, item.stance, "0.60" if item.stance is Stance.BUY else str(item.confidence))
    for item in LEAN
)


# ----------------------------------------------------------------- the table


def test_every_regime_label_has_a_playbook_and_every_playbook_only_tightens() -> None:
    assert set(PLAYBOOKS) == REGIME_LABELS
    names = [item.name for item in PLAYBOOKS.values()]
    assert len(set(names)) == len(names)
    for item in (*PLAYBOOKS.values(), DEFAULT_PLAYBOOK):
        assert item.floor_adjustment >= 0
        assert Decimal(0) < item.size_multiplier <= Decimal(1)
    assert PLAYBOOKS["trending_up"].floor_adjustment == 0 and PLAYBOOKS["trending_up"].size_multiplier == 1
    assert not PLAYBOOKS["trending_down"].probes_allowed
    assert not PLAYBOOKS["high_volatility"].probes_allowed
    assert UNROUTED_PLAYBOOK.floor_adjustment == 0 and UNROUTED_PLAYBOOK.size_multiplier == 1
    with pytest.raises(ValueError):
        RegimePlaybook("x", "loose", Decimal("-0.01"), Decimal(1), True, "")
    with pytest.raises(ValueError):
        RegimePlaybook("x", "oversized", Decimal(0), Decimal("1.01"), True, "")
    with pytest.raises(ValueError):
        RegimePlaybook("x", "two words", Decimal(0), Decimal(1), True, "")


def test_playbook_lookup_routes_known_labels_and_falls_back_cautiously() -> None:
    assert playbook_for("trending_up").name == "trend_following"
    assert playbook_for("ranging").name == "range_trading"
    assert playbook_for("trending_down").name == "defensive"
    assert playbook_for("high_volatility").name == "crisis_standdown"
    assert playbook_for("insufficient_history").name == "cautious_default"
    assert playbook_for(None) is DEFAULT_PLAYBOOK
    assert playbook_for("sideways_ish") is DEFAULT_PLAYBOOK
    assert playbook_for("high_volatility", enabled=False) is UNROUTED_PLAYBOOK
    assert regime_label_of(context("ranging")) == "ranging"
    assert regime_label_of(None) is None
    assert regime_label_of(EvidenceContext()) is None
    assert regime_label_of(SimpleNamespace(regime=(("label", ""),))) is None


def test_the_provenance_block_names_the_floor_the_decision_was_judged_against() -> None:
    assert PLAYBOOKS["trending_down"].provenance(Decimal("0.55")) == {
        "name": "defensive", "regime": "trending_down", "floor": "0.6500", "floor_adjustment": "0.1000",
        "size_multiplier": "0.5000", "probes_allowed": False,
    }
    line = describe()
    assert line.startswith("trending_up=trend_following x1.00; ranging=range_trading x0.75 floor+0.05;")
    assert "high_volatility=crisis_standdown x0.35 floor+0.15 no-probes" in line


# ----------------------------------------------------------------- sizing


@pytest.mark.parametrize(
    "confidence, expected",
    [("0.30", "0.5000"), ("0.55", "0.5000"), ("0.70", "0.7500"), ("0.85", "1.0000"), ("0.99", "1.0000")],
)
def test_conviction_runs_from_half_at_the_floor_to_full_at_0_85(confidence, expected) -> None:
    assert str(conviction_multiplier(Decimal(confidence), floor=Decimal("0.55"))) == expected


def test_a_floor_at_or_above_full_size_confidence_keeps_the_floor_share() -> None:
    assert conviction_multiplier(Decimal("0.90"), floor=Decimal("0.85")) == FLOOR_SIZE_SHARE


def test_sized_quantity_scales_down_only_and_keeps_one_unit_of_an_allowed_entry() -> None:
    quantity, sizing = sized_quantity(100, confidence=Decimal("0.70"), floor=Decimal("0.55"), size_multiplier=Decimal("0.50"))
    assert quantity == 37  # 100 x 0.75 conviction x 0.50 playbook, rounded down
    assert sizing == {"plan_quantity": 100, "conviction_multiplier": "0.7500", "playbook_multiplier": "0.5000", "quantity": 37}
    assert sized_quantity(1, confidence=Decimal("0.55"), floor=Decimal("0.55"), size_multiplier=Decimal("0.35"))[0] == 1
    assert sized_quantity(0, confidence=Decimal("0.99"), floor=Decimal("0.55"), size_multiplier=Decimal(1))[0] == 0
    assert sized_quantity(40, confidence=Decimal("0.99"), floor=Decimal("0.55"), size_multiplier=Decimal(1))[0] == 40


def test_sizing_from_provenance_needs_a_well_formed_playbook_block() -> None:
    block = {"playbook": PLAYBOOKS["ranging"].provenance(Decimal("0.55"))}
    quantity, sizing = sized_from_provenance(100, Decimal("0.85"), block)
    assert (quantity, sizing["conviction_multiplier"], sizing["playbook_multiplier"]) == (75, "1.0000", "0.7500")
    assert sized_from_provenance(100, Decimal("0.85"), None) is None
    assert sized_from_provenance(100, Decimal("0.85"), {}) is None
    assert sized_from_provenance(100, Decimal("0.85"), {"playbook": {"name": "x"}}) is None
    assert sized_from_provenance(100, Decimal("0.85"), {"playbook": {"floor": "abc", "size_multiplier": "1"}}) is None
    assert sized_from_provenance(100, Decimal("0.85"), {"playbook": {"floor": "0.55", "size_multiplier": "1.5"}}) is None


def proposal(quantity: int, confidence: str, provenance: dict | None, side: Side | None = Side.BUY) -> TradeProposal:
    return TradeProposal("d1", "TRENT", Market.INDIA, "India", AssetClass.EQUITY, side, quantity,
                         Decimal(5000), Decimal(4900), Decimal(5200), Decimal(confidence),
                         Decimal("0.01"), Decimal("0.02"), ("r",), provenance)


def test_the_runtime_sizes_a_buy_from_the_playbook_the_decision_recorded() -> None:
    routed = proposal(100, "0.70", {"playbook": PLAYBOOKS["trending_down"].provenance(Decimal("0.55"))})
    sized = SwarmPaperTradingService._conviction_sized(routed)
    assert sized.quantity == 31  # 100 x 0.625 conviction (0.70 a quarter of the way from 0.65 to 0.85) x 0.50
    assert sized.provenance["sizing"]["conviction_multiplier"] == "0.6250"
    assert sized.provenance["sizing"]["plan_quantity"] == 100
    assert sized.provenance["playbook"]["name"] == "defensive"
    # A proposal without a playbook (not an Atlas decision) is left exactly as it came.
    plain = proposal(100, "0.70", None)
    assert SwarmPaperTradingService._conviction_sized(plain) is plain
    # Full conviction under the trend playbook keeps the plan quantity.
    trend = proposal(100, "0.90", {"playbook": PLAYBOOKS["trending_up"].provenance(Decimal("0.55"))})
    assert SwarmPaperTradingService._conviction_sized(trend).quantity == 100


# ----------------------------------------------------------------- Atlas


def test_the_regime_raises_the_floor_a_moderate_lean_must_clear() -> None:
    atlas = AtlasInvestmentAgent()
    riding = atlas.decide("TRENT", MODERATE, NOW, evidence_context=context("trending_up"))
    assert riding.action is Stance.BUY
    assert "playbook=trend_following:floor=0.5500" in riding.rationale
    assert riding.provenance["playbook"]["name"] == "trend_following"
    assert riding.provenance["regime"] == "trending_up"

    ranging = atlas.decide("TRENT", MODERATE, NOW, evidence_context=context("ranging"))
    assert ranging.action is Stance.BUY  # 0.60 clears 0.60
    assert ranging.provenance["playbook"]["floor"] == "0.6000"

    falling = atlas.decide("TRENT", MODERATE, NOW, evidence_context=context("trending_down"))
    assert falling.action is Stance.NEUTRAL
    assert "playbook=defensive:floor=0.6500" in falling.rationale
    assert falling.provenance["playbook"] == PLAYBOOKS["trending_down"].provenance(Decimal("0.55"))
    assert falling.provenance["consensus"]["average_confidence"] == "0.6000"

    crisis = atlas.decide("TRENT", MODERATE, NOW, evidence_context=context("high_volatility"))
    assert crisis.action is Stance.NEUTRAL
    assert crisis.provenance["playbook"]["name"] == "crisis_standdown"

    unread = atlas.decide("TRENT", MODERATE, NOW)
    assert unread.action is Stance.BUY  # no regime read: cautious_default's 0.60 floor, met exactly
    assert unread.provenance["playbook"]["name"] == "cautious_default"
    assert unread.provenance["regime"] is None


def test_cautious_default_floor_is_met_at_exactly_the_adjusted_floor() -> None:
    # 0.60 average against 0.55 + 0.05: at the floor is enough, the comparison is strict below.
    decision = AtlasInvestmentAgent().decide("TRENT", MODERATE, NOW, evidence_context=context("insufficient_history"))
    assert decision.provenance["playbook"]["floor"] == "0.6000"
    assert decision.action is Stance.BUY


def test_routing_off_judges_every_regime_at_the_plan_floor() -> None:
    atlas = AtlasInvestmentAgent(policy=AtlasPolicy(regime_playbooks=False))
    falling = atlas.decide("TRENT", MODERATE, NOW, evidence_context=context("trending_down"))
    assert falling.action is Stance.BUY
    assert falling.provenance["playbook"] == UNROUTED_PLAYBOOK.provenance(Decimal("0.55"))
    assert falling.provenance["regime"] == "trending_down"  # the label is still recorded
    assert "playbook=unrouted:floor=0.5500" in falling.rationale


def test_hard_holds_carry_the_playbook_too() -> None:
    held = AtlasInvestmentAgent().decide("TRENT", LEAN[:2], NOW, evidence_context=context("ranging"))
    assert "insufficient_agent_coverage" in held.rationale
    assert held.provenance["playbook"]["name"] == "range_trading"
    assert "consensus" not in held.provenance


def test_defensive_and_crisis_playbooks_withhold_probes_and_say_so() -> None:
    atlas = AtlasInvestmentAgent(policy=budget(3))
    riding = atlas.decide("TRENT", LEAN, NOW, evidence_context=context("trending_up"))
    assert riding.action is Stance.BUY and riding.provenance["exploration"]["probe"] is True
    falling = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=5), evidence_context=context("trending_down"))
    assert falling.action is Stance.NEUTRAL
    assert "exploration_suppressed=playbook:defensive" in falling.rationale
    assert "exploration" not in falling.provenance
    assert atlas._probes_issued == {"2026-09-21": 1}  # the withheld probe spent nothing
    ranging = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=10), evidence_context=context("ranging"))
    assert ranging.provenance["exploration"]["budget_used"] == 2
    unread = atlas.decide("TRENT", LEAN, NOW + timedelta(minutes=15))
    assert "exploration_suppressed=playbook:cautious_default" in unread.rationale


def test_the_llm_path_carries_the_playbook_and_a_model_buy_is_sized_not_vetoed() -> None:
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.62))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW, evidence_context=context("trending_down")))
    assert decision.action is Stance.BUY and decision.provenance["mode"] == "llm"
    assert decision.provenance["playbook"]["name"] == "defensive"
    # The runtime then halves the plan quantity for the defensive book and sizes the
    # model's 0.62 against the 0.65 floor as if at it.
    sized = SwarmPaperTradingService._conviction_sized(proposal(100, "0.62", decision.provenance))
    assert sized.quantity == 25


# ----------------------------------------------------------------- env, journal, report, pre-market


def test_the_switch_reads_on_off_and_refuses_anything_else() -> None:
    assert atlas_policy_from_env({}).regime_playbooks is True
    assert atlas_policy_from_env({REGIME_PLAYBOOKS_ENV: "off"}).regime_playbooks is False
    assert atlas_policy_from_env({REGIME_PLAYBOOKS_ENV: "FALSE"}).regime_playbooks is False
    assert atlas_policy_from_env({REGIME_PLAYBOOKS_ENV: "on"}).regime_playbooks is True
    with pytest.raises(RuntimeError, match="must be on or off"):
        atlas_policy_from_env({REGIME_PLAYBOOKS_ENV: "maybe"})


def test_the_journal_records_the_playbook_and_the_report_scores_it(tmp_path) -> None:
    from quant_ai.execution.paper_ledger import PaperBrokerService

    broker = PaperBrokerService(tmp_path / "ledger.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    routed = SimpleNamespace(
        decision_id="d1", symbol="TRENT", market="INDIA", asset_class="EQUITY", side="BUY", quantity=10,
        reference_price=Decimal(5000), stop_price=None, take_profit_price=None, confidence=Decimal("0.70"),
        expected_return=Decimal("0.01"), expected_risk=Decimal("0.02"),
        provenance={"playbook": PLAYBOOKS["trending_up"].provenance(Decimal("0.55")), "mode": "deterministic"},
    )
    result = SimpleNamespace(proposal=routed, fill=None, risk_decision=SimpleNamespace(approved=False, reason="x"),
                             xai_trace=SimpleNamespace(input_matrix=(), regime="trending_up", provenance=None))
    row = journal.decision_row(result, tenant_id="ghost", now=NOW)
    assert row["playbook"] == "trend_following"
    plain = journal.decision_row(
        SimpleNamespace(**{**vars(result), "proposal": SimpleNamespace(**{**vars(routed), "provenance": {}, "decision_id": "d2"})}),
        tenant_id="ghost", now=NOW,
    )
    assert plain["playbook"] is None
    assert journal.insert_decision(broker, row) and journal.insert_decision(broker, plain)
    rows = journal.load_rows(broker, tenant_id="ghost", since=NOW - timedelta(hours=1), until=NOW + timedelta(hours=1))
    groups = quality.by_playbook(rows)
    assert [(g["playbook"], g["decisions"], g["probes"]) for g in groups] == [("trend_following", 1, 0), ("unknown", 1, 0)]
    assert quality.recent(rows)[0]["playbook"] in {"trend_following", None}
    # An older ledger gains the column on first use rather than refusing the insert.
    with broker._lock, broker._connection as db:
        db.execute("ALTER TABLE paper_decision_journal DROP COLUMN playbook")
    journal.ensure_journal(broker)
    assert journal.insert_decision(broker, {**row, "decision_id": "d3"})


def test_the_premarket_check_reports_the_routing_as_information() -> None:
    line = next(c for c in premarket_checks(payload(), manifest(), env(), NOW) if c.id == "regime_playbooks")
    assert line.state == "INFO"
    assert line.detail.startswith("on: trending_up=trend_following x1.00;")
    assert "conviction sizing x0.50 at the floor to x1 at 0.85" in line.detail
    off = next(c for c in premarket_checks(payload(), manifest(), {**env(), REGIME_PLAYBOOKS_ENV: "off"}, NOW)
               if c.id == "regime_playbooks")
    assert off.state == "INFO" and off.detail.startswith("off (PRAMANA_REGIME_PLAYBOOKS=off)")
    bad = next(c for c in premarket_checks(payload(), manifest(), {**env(), REGIME_PLAYBOOKS_ENV: "sometimes"}, NOW)
               if c.id == "regime_playbooks")
    assert bad.state == "FAIL" and "must be on or off" in bad.detail
