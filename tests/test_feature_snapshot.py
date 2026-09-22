"""What a decision was looking at, frozen at the instant it described.

A proof carried what Atlas concluded and not what it saw. So a forecast could be scored
and a refusal could be read, but neither could be explained: the metrics the specialists
voted on, the regime they voted under and the state of the book were computed, used and
discarded. This is what closed that, and these tests pin the two rules that make the
record worth trusting - the snapshot is the input rather than a recomputation, and a
value the decision did not have is null rather than zero.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Sibling test modules by name: pytest puts tests/ on sys.path, the repository root it
# does not (the console script CI runs), so a ``tests.`` package import fails there.
from test_exploration_budget import NOW, consensus_client, evidence
from test_regime_playbooks import MODERATE, context

from quant_ai.agents import feature_snapshot as snapshots
from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, Stance
from quant_ai.marketdata.ticker_stream import LiveTick

LATER = NOW + timedelta(milliseconds=250)
VECTOR = {"rsi": Decimal(50), "momentum": Decimal("0.004"), "book_gross_exposure_fraction": Decimal("0.12")}


def _tick(bid: str | None = "99.95", ask: str | None = "100.05") -> LiveTick:
    return LiveTick(
        symbol="TRENT", ltp=Decimal("100.00"), volume=Decimal(12345),
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        observed_at=NOW, source="zerodha",
    )


def _atlas() -> AtlasInvestmentAgent:
    """A fixed clock, so the one timestamp that is not T0 is still deterministic."""
    return AtlasInvestmentAgent(clock=lambda: LATER)


def test_the_snapshot_carries_the_vector_the_decision_was_given_unchanged() -> None:
    decision = _atlas().decide("TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR)
    block = decision.provenance["feature_snapshot"]
    assert block["schema"] == snapshots.SCHEMA
    assert block["frozen_at"] == NOW.isoformat()
    # Exact strings, not floats: a Decimal that survives the journal must survive this.
    assert block["metrics"] == {"book_gross_exposure_fraction": "0.12", "momentum": "0.004", "rsi": "50"}
    assert block["metrics_truncated"] is False


def test_a_vector_that_differs_gets_a_different_hash() -> None:
    """The hash is over the vector, so it can prove which inputs a decision ran on."""
    atlas = _atlas()
    first = atlas.decide("TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR)
    second = atlas.decide("TRENT", MODERATE, NOW, market_tick=_tick(), features={**VECTOR, "rsi": Decimal(51)})
    assert (first.provenance["feature_snapshot"]["metrics_sha256"]
            != second.provenance["feature_snapshot"]["metrics_sha256"])


def test_a_book_with_no_quote_reports_no_spread_rather_than_a_zero_one() -> None:
    """"No quote was on the book" and "the spread was zero" are different facts.

    A trainer that cannot tell them apart learns the difference as signal, and a fill
    model that reads a zero spread will price a touch that was never there.
    """
    quoted = _atlas().decide("TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR)
    # 0.10 wide on a 100.00 mid is exactly 10 bps, and Decimal keeps it exact.
    assert quoted.provenance["feature_snapshot"]["market"]["spread_bps"] == "10.000"
    bare = _atlas().decide("TRENT", MODERATE, NOW, market_tick=_tick(bid=None, ask=None), features=VECTOR)
    market = bare.provenance["feature_snapshot"]["market"]
    assert market["spread_bps"] is None and market["bid"] is None
    # The trade price itself was known, and is still recorded.
    assert market["last_price"] == "100.00"


def test_a_crossed_or_one_sided_book_is_refused_a_spread() -> None:
    assert snapshots.spread_basis_points(Decimal(101), Decimal(100)) is None  # crossed
    assert snapshots.spread_basis_points(None, Decimal(100)) is None
    assert snapshots.spread_basis_points(Decimal(0), Decimal(100)) is None


def test_a_hold_records_who_abstained() -> None:
    """The case that most needs the snapshot.

    "insufficient_agent_coverage" is unreadable without the roster: the whole question is
    which specialists were there and what they had to say, and a proof that drops them
    leaves an operator with a reason code and no way to check it.
    """
    thin = (evidence("technical-quant-mas", AgentDomain.TECHNICAL, Stance.NEUTRAL, "0"),)
    decision = _atlas().decide("TRENT", thin, NOW, market_tick=_tick(), features=VECTOR)
    assert decision.action is Stance.NEUTRAL
    specialists = decision.provenance["feature_snapshot"]["specialists"]
    assert specialists == [{
        "agent_id": "technical-quant-mas", "domain": "TECHNICAL",
        "stance": "NEUTRAL", "confidence": "0", "freshness_seconds": 10,
    }]


def test_the_snapshot_mirrors_the_forecast_it_was_recorded_beside() -> None:
    decision = _atlas().decide("TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR)
    mirrored = decision.provenance["feature_snapshot"]["forecast"]
    recorded = decision.provenance["forecast"]
    assert mirrored["probability_up"] == recorded["probability_up"]
    assert mirrored["basis"] == recorded["basis"]


def test_the_timings_say_how_long_the_book_had_to_move() -> None:
    """T0 is what the drift gate measures against, so the gap to the decision is the cost."""
    decision = _atlas().decide("TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR)
    timings = decision.provenance["timings"]
    assert timings["features_frozen_at"] == NOW.isoformat()
    assert timings["decision_at"] == LATER.isoformat()
    assert timings["analysis_latency_ms"] == 250


def test_the_overlay_path_snapshots_the_same_frozen_vector() -> None:
    """The model runs after the freeze, so it cannot change what was frozen."""
    atlas = AtlasInvestmentAgent(clock=lambda: LATER, llm_client=consensus_client("AVOID", 0.80))
    decision = asyncio.run(atlas.decide_with_llm(
        "TRENT", MODERATE, NOW, market_tick=_tick(),
        evidence_context=context("trending_down"), features=VECTOR,
    ))
    assert decision.action is Stance.AVOID
    block = decision.provenance["feature_snapshot"]
    assert block["metrics"]["rsi"] == "50"
    assert block["regime"]["label"] == "trending_down"
    assert block["frozen_at"] == NOW.isoformat()


def test_an_analysis_start_the_caller_knows_is_kept_apart_from_t0() -> None:
    started = NOW - timedelta(seconds=3)
    decision = _atlas().decide(
        "TRENT", MODERATE, NOW, market_tick=_tick(), features=VECTOR, analysis_started_at=started,
    )
    timings = decision.provenance["timings"]
    assert timings["analysis_started_at"] == started.isoformat()
    assert timings["features_frozen_at"] == NOW.isoformat()


def test_a_decision_without_a_vector_still_snapshots_what_it_had() -> None:
    """Fail open on the record, never on the decision: a missing map is an empty one."""
    decision = _atlas().decide("TRENT", MODERATE, datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc))
    block = decision.provenance["feature_snapshot"]
    assert block["metrics"] == {} and block["market"]["last_price"] is None
    assert block["specialists"], "the roster is known even when the metric map is not"


def test_a_clock_the_timings_cannot_subtract_reports_no_latency_rather_than_raising() -> None:
    """Recording must never turn a decision into an exception.

    Caught by the knowledge-binding suite: the first version of this module subtracted a
    naive T0 from an aware clock read and raised, so a caller that asked for a decision
    got a TypeError - told neither yes nor no, which is strictly worse than a decision
    with no record. An unusable instant is now reported absent.

    The narrower cases that suite covers - a clock of None or a string - are refused
    earlier by the governed knowledge path and never reach here; this module does not
    widen that contract.
    """
    naive = NOW.replace(tzinfo=None)
    decision = _atlas().decide("TRENT", MODERATE, naive, market_tick=_tick(), features=VECTOR)
    timings = decision.provenance["timings"]
    # The snapshot is written in full; only the gap it could not compute is absent.
    assert decision.provenance["feature_snapshot"]["metrics"]["rsi"] == "50"
    assert timings["features_frozen_at"] == naive.isoformat()
    assert timings["decision_at"] == LATER.isoformat()
    # A gap that cannot be computed is unknown, never a fabricated zero.
    assert timings["analysis_latency_ms"] is None
