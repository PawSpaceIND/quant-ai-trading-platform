"""The vector a decision was made from, frozen at the instant it described.

A decision proof already carries what Atlas concluded. It did not carry what Atlas was
looking at. So a forecast could be scored, and a refusal could be read, but neither could
be explained: the metrics the specialists voted on, the regime they voted under and the
state of the book at that moment were computed, used and discarded.

That gap is why the drift gate was unexplainable. ``pilot_price_moved_during_analysis``
compares the mark now against the reference price the analysis reasoned over, and until
this module there was no record of *when* that reference was taken, so the refusal could
not be told apart from a slow analysis or an early snapshot.

Two rules shape what is stored.

**The snapshot is the input, not a recomputation.** Every value here is one the decision
actually used, carried through unchanged. Recomputing a feature at write time would
produce a vector that looks like the decision's and is not, which is worse than none:
a model trained on it would learn from inputs no decision ever saw.

**An absent value is None, never zero.** "No quote was on the book" and "the spread was
zero" are different facts, and a trainer that cannot tell them apart will learn the
difference as signal. Every named slot is present on every snapshot for the same reason.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from quant_ai.llm.provenance import content_hash

SCHEMA = "pramana.feature_snapshot.v1"
# The frozen metric map is bounded so a proof cannot grow without limit. The pipeline
# writes a few dozen; a map past this is recorded truncated and says so, because silently
# dropping the tail would leave a vector that claims to be complete and is not.
MAX_METRICS = 160
MAX_NAME_CHARS = 80
# Specialists are few by construction; the bound is a guard, not a policy.
MAX_SPECIALISTS = 32
BASIS_POINT = Decimal(10000)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    """Decimals and numbers as exact strings; anything else as itself, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    number = _decimal(value)
    return str(number) if number is not None else None


def _instant(value: Any) -> str | None:
    """An ISO timestamp, or None when the caller's clock was not a usable datetime.

    A decision made on a malformed clock is still a decision, and the engine already
    refuses it through the governed path with ``governed_knowledge_invalid``. Recording
    must never turn that refusal into an exception: a caller that asked for a decision
    and got a TypeError has been told neither yes nor no.
    """
    try:
        return value.isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def timings(*, started_at: Any, frozen_at: Any, decided_at: Any) -> dict[str, Any]:
    """When the evidence was frozen, when the decision finished, and the gap between.

    ``analysis_latency_ms`` covers freeze to decision ONLY. The entry drift gate compares
    the live mark against the last closed one-minute bar as of T0, so the window the
    market actually had to move in opens before the freeze and closes at submit. This is
    the middle leg of it. Reading a drift in basis points against this number alone would
    blame a slow decision for a reference that was already a minute old.
    """
    try:
        latency = int((decided_at - frozen_at).total_seconds() * 1000)
    except (AttributeError, TypeError, ValueError):
        # A naive T0 against an aware clock, or no clock at all. Unknown, never raised.
        latency = None
    return {
        "analysis_started_at": _instant(started_at),
        "features_frozen_at": _instant(frozen_at),
        "decision_at": _instant(decided_at),
        "analysis_latency_ms": latency,
    }


def spread_basis_points(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    """The quoted spread against its own mid, or None when the book was not two-sided.

    A one-sided or crossed book has no meaningful spread, and reporting one would put a
    number where the decision had none.
    """
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / Decimal(2)
    return ((ask - bid) / mid * BASIS_POINT) if mid > 0 else None


def _metrics(metrics: Mapping[str, Any] | None) -> tuple[dict[str, str], bool]:
    """The frozen vector as exact strings, sorted, with an explicit truncation flag."""
    if not isinstance(metrics, Mapping):
        return {}, False
    usable = sorted(
        (name, _text(value))
        for name, value in metrics.items()
        if isinstance(name, str) and 0 < len(name) <= MAX_NAME_CHARS
    )
    kept = [(name, value) for name, value in usable if value is not None]
    return dict(kept[:MAX_METRICS]), len(kept) > MAX_METRICS


def _specialists(evidence: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Each voter's stance and conviction as it stood, in a stable order.

    Recorded whether or not the agent participated: an abstention is the reason a
    consensus was thin, and a snapshot that omits abstainers cannot show that.
    """
    rows = []
    for item in (evidence or ())[:MAX_SPECIALISTS]:
        stance = getattr(getattr(item, "stance", None), "name", None)
        domain = getattr(getattr(item, "domain", None), "name", None)
        rows.append({
            "agent_id": _text(getattr(item, "agent_id", None)),
            "domain": domain,
            "stance": stance,
            "confidence": _text(getattr(item, "confidence", None)),
            "freshness_seconds": getattr(item, "source_freshness_seconds", None),
        })
    rows.sort(key=lambda row: (row["agent_id"] or "", row["domain"] or ""))
    return rows


def freeze(
    *,
    subject: str,
    frozen_at: Any,
    metrics: Mapping[str, Any] | None = None,
    evidence: Sequence[Any] | None = None,
    market_tick: Any | None = None,
    regime: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    forecast: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The immutable record of what one decision was looking at, at ``frozen_at``.

    ``frozen_at`` is T0: the instant the evidence describes, and the instant the entry
    drift gate later measures the mark against. It is not the instant this function ran.
    """
    bid = _decimal(getattr(market_tick, "bid", None))
    ask = _decimal(getattr(market_tick, "ask", None))
    spread = spread_basis_points(bid, ask)
    regime_pairs = dict(regime) if regime is not None else {}
    frozen, truncated = _metrics(metrics)
    return {
        "schema": SCHEMA,
        "subject": subject,
        "frozen_at": _instant(frozen_at),
        # The vector itself, exactly as the specialists were given it.
        "metrics": frozen,
        "metrics_sha256": content_hash(frozen),
        "metrics_truncated": truncated,
        "regime": {
            "label": _text(regime_pairs.get("label")),
            "timeframe": _text(regime_pairs.get("timeframe")),
            "trend_strength": _text(regime_pairs.get("trend_strength")),
            "range_fraction": _text(regime_pairs.get("range_fraction")),
        },
        # The quote the reference price came from. `spread_bps` is None on a one-sided
        # book rather than zero, so a later fill model can tell "no quote" from "no spread".
        "market": {
            "last_price": _text(getattr(market_tick, "ltp", None)),
            "volume": _text(getattr(market_tick, "volume", None)),
            "bid": _text(bid),
            "ask": _text(ask),
            "spread_bps": _text(spread),
            "source": _text(getattr(market_tick, "source", None)),
            "observed_at": _text(getattr(getattr(market_tick, "observed_at", None), "isoformat", lambda: None)()),
        },
        "specialists": _specialists(evidence),
        # Mirrors the decision's own forecast block so the snapshot is self-contained for
        # a trainer. A test pins that the two agree; they are never computed twice.
        "forecast": {
            "probability_up": _text((forecast or {}).get("probability_up")),
            "basis": _text((forecast or {}).get("basis")),
        },
    }
