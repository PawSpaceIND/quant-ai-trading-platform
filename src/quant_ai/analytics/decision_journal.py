"""Durable journal of every cadence decision, whether it filled, was rejected or abstained.

The journal is the raw material of the decision-quality layer: one row per instrument per
cadence tick, written into the paper ledger's SQLite database next to the fills it
describes. Forward returns and trade outcomes are filled in later by the outcome resolver;
the report and the post-mortem only ever read this table.

Paper-only. Money is stored as Decimal text and every timestamp is ISO-8601 with an offset.
The daemon wraps every call here: a journal failure is logged and the cadence continues.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from quant_ai.agents.contracts import Stance
from quant_ai.domain.models import Side

LOGGER = logging.getLogger("quant_ai.decision_journal")

TABLE = "paper_decision_journal"

HORIZON_COLUMNS = (
    "forward_return_10m",
    "forward_return_30m",
    "forward_return_60m",
    "forward_return_close",
)

COLUMNS = (
    "decision_id",
    "tenant_id",
    "symbol",
    "market",
    "asset_class",
    "decided_at",
    "stance",
    "side",
    "confidence",
    "expected_return",
    "expected_risk",
    "reference_price",
    "stop_price",
    "take_profit_price",
    "regime",
    "mode",
    "governance",
    "reason",
    "order_id",
    "agents",
    *HORIZON_COLUMNS,
    "resolved_at",
    "realized_net_pnl",
    "realized_gross_pnl",
    "realized_fees",
    "exit_trigger",
    "exit_at",
    "holding_minutes",
)

# Columns the resolver may write after the row exists. Everything else is immutable.
RESOLVABLE_COLUMNS = frozenset(
    (
        *HORIZON_COLUMNS,
        "resolved_at",
        "realized_net_pnl",
        "realized_gross_pnl",
        "realized_fees",
        "exit_trigger",
        "exit_at",
        "holding_minutes",
    )
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    decision_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    stance TEXT NOT NULL,
    side TEXT,
    confidence TEXT,
    expected_return TEXT,
    expected_risk TEXT,
    reference_price TEXT,
    stop_price TEXT,
    take_profit_price TEXT,
    regime TEXT,
    mode TEXT,
    governance TEXT NOT NULL,
    reason TEXT,
    order_id TEXT,
    agents TEXT NOT NULL,
    forward_return_10m TEXT,
    forward_return_30m TEXT,
    forward_return_60m TEXT,
    forward_return_close TEXT,
    resolved_at TEXT,
    realized_net_pnl TEXT,
    realized_gross_pnl TEXT,
    realized_fees TEXT,
    exit_trigger TEXT,
    exit_at TEXT,
    holding_minutes INTEGER
)
"""

INDEX = (
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_tenant_decided "
    f"ON {TABLE} (tenant_id, decided_at)"
)

GOVERNANCE_FILLED = "filled"
GOVERNANCE_REJECTED = "rejected"
GOVERNANCE_ABSTAINED = "abstained"

# A zero-quantity proposal that was never vetoed by a gate is the sizer or the CIO finding
# nothing to do, not a governance rejection.
ABSTAIN_REASONS = frozenset(
    {"position_sizer_no_capacity", "invalid_trade_proposal", "atlas_non_actionable_proposal"}
)

# Mirrors the labelling in the Atlas consensus path: an inference record's status decides
# whether an LLM opinion was actually used.
MODE_BY_INFERENCE_STATUS = {
    "completed": "llm",
    "unavailable": "llm_unavailable",
    "budget_exhausted": "llm_budget_exhausted",
}
MODE_DETERMINISTIC = "deterministic"
MODE_UNVERIFIED = "unverified_inference"


def ensure_journal(broker) -> None:
    """Create the journal table lazily through the broker's own connection and lock."""
    with broker._lock, broker._connection as db:
        db.execute(SCHEMA)
        db.execute(INDEX)


def aware(value: datetime) -> datetime:
    """Reject naive datetimes; normalise everything else to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def decimal_text(value: Any) -> str | None:
    """Store a Decimal (or None) as exact text; never a float."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return str(parsed) if parsed.is_finite() else None


def parse_decimal(text: Any) -> Decimal | None:
    """Read a stored Decimal text; None for missing or unusable values."""
    if text is None:
        return None
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def stance_of(proposal) -> str:
    """The proposal's stance label.

    The proposal contract carries a side, not the Atlas conviction, so a BUY/SELL side maps
    to BUY/SELL and an unactionable proposal to NEUTRAL. A ``stance`` attribute on the
    proposal, if one is ever added, is honoured as-is.
    """
    explicit = getattr(proposal, "stance", None)
    if isinstance(explicit, Stance):
        return explicit.value
    if isinstance(explicit, str) and explicit in {item.value for item in Stance}:
        return explicit
    side = getattr(proposal, "side", None)
    if side == Side.BUY:
        return Stance.BUY.value
    if side == Side.SELL:
        return Stance.SELL.value
    return Stance.NEUTRAL.value


def governance_of(result) -> tuple[str, str | None]:
    """Map a swarm execution result to ``filled`` / ``rejected`` / ``abstained`` and a reason."""
    risk = result.risk_decision
    approved = bool(getattr(risk, "approved", False))
    reason = None if approved else str(getattr(risk, "reason", "") or "") or None
    if result.fill is not None:
        return GOVERNANCE_FILLED, None
    proposal = result.proposal
    if proposal.side is None:
        return GOVERNANCE_ABSTAINED, reason
    if proposal.quantity <= 0 and (reason is None or reason in ABSTAIN_REASONS):
        return GOVERNANCE_ABSTAINED, reason
    return GOVERNANCE_REJECTED, reason or "unspecified"


def decision_mode(result, *, llm_available: bool) -> str:
    """Evidence mode of the decision, labelled the way the Atlas consensus labels its proofs."""
    provenance = getattr(result.xai_trace, "provenance", None)
    if not isinstance(provenance, dict):
        provenance = getattr(result.proposal, "provenance", None)
    if not isinstance(provenance, dict):
        provenance = {}
    mode = provenance.get("mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()[:64]
    inference = provenance.get("inference")
    if isinstance(inference, dict):
        return MODE_BY_INFERENCE_STATUS.get(str(inference.get("status")), MODE_UNVERIFIED)
    return MODE_DETERMINISTIC if not llm_available else MODE_UNVERIFIED


def agents_of(trace) -> dict[str, dict[str, str]]:
    """Specialist stances and confidences from the trace's input matrix."""
    agents: dict[str, dict[str, str]] = {}
    for row in getattr(trace, "input_matrix", None) or ():
        try:
            agents[str(row["agent_id"])] = {
                "stance": str(enum_value(row["stance"])),
                "confidence": str(row["confidence"]),
            }
        except (KeyError, TypeError):
            continue
    return agents


def decision_row(
    result,
    *,
    tenant_id: str,
    now: datetime,
    regime: Any = None,
    mode: str | None = None,
    llm_available: bool = False,
) -> dict[str, Any]:
    """Build the journal row for one ``SwarmExecutionResult`` without writing it."""
    proposal = result.proposal
    trace = result.xai_trace
    governance, reason = governance_of(result)
    regime_label = (
        regime
        or getattr(trace, "regime", None)
        or (getattr(proposal, "provenance", None) or {}).get("regime")
    )
    regime_label = enum_value(regime_label)
    fill = result.fill
    return {
        "decision_id": str(proposal.decision_id),
        "tenant_id": tenant_id,
        "symbol": str(proposal.symbol),
        "market": str(enum_value(proposal.market)),
        "asset_class": str(enum_value(proposal.asset_class)),
        "decided_at": aware(now).isoformat(),
        "stance": stance_of(proposal),
        "side": str(enum_value(proposal.side)) if proposal.side is not None else None,
        "confidence": decimal_text(proposal.confidence),
        "expected_return": decimal_text(proposal.expected_return),
        "expected_risk": decimal_text(proposal.expected_risk),
        "reference_price": decimal_text(proposal.reference_price),
        "stop_price": decimal_text(proposal.stop_price),
        "take_profit_price": decimal_text(proposal.take_profit_price),
        "regime": str(regime_label)[:64] if regime_label else None,
        "mode": (mode or decision_mode(result, llm_available=llm_available))[:64],
        "governance": governance,
        "reason": reason[:200] if reason else None,
        "order_id": str(fill.order_id) if fill is not None and fill.order_id else None,
        "agents": json.dumps(agents_of(trace), sort_keys=True, allow_nan=False),
    }


def insert_decision(broker, row: dict[str, Any]) -> bool:
    """Insert a journal row; a repeated ``decision_id`` is a no-op. True when inserted."""
    ensure_journal(broker)
    values = {column: row.get(column) for column in COLUMNS}
    placeholders = ", ".join("?" for _ in COLUMNS)
    with broker._lock, broker._connection as db:
        inserted = db.execute(
            f"INSERT OR IGNORE INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            tuple(values[column] for column in COLUMNS),
        )
        return inserted.rowcount == 1


def record_decision(
    broker,
    result,
    *,
    tenant_id: str,
    regime: Any = None,
    mode: str | None = None,
    now: datetime,
    llm_available: bool = False,
) -> bool:
    """Journal one cadence decision. Idempotent on ``decision_id``; True when a row was added."""
    row = decision_row(
        result, tenant_id=tenant_id, now=now, regime=regime, mode=mode, llm_available=llm_available
    )
    inserted = insert_decision(broker, row)
    if inserted:
        LOGGER.info(
            "decision_journaled decision_id=%s symbol=%s governance=%s reason=%s",
            row["decision_id"], row["symbol"], row["governance"], row["reason"],
        )
    return inserted


def update_decision(broker, decision_id: str, **fields: Any) -> bool:
    """Write resolver-owned columns of one row. Unknown columns are refused."""
    unknown = set(fields) - RESOLVABLE_COLUMNS
    if unknown:
        raise ValueError(f"not a resolvable journal column: {sorted(unknown)}")
    if not fields:
        return False
    assignments = ", ".join(f"{column} = ?" for column in fields)
    with broker._lock, broker._connection as db:
        updated = db.execute(
            f"UPDATE {TABLE} SET {assignments} WHERE decision_id = ?",
            (*fields.values(), decision_id),
        )
        return updated.rowcount == 1


def load_rows(
    broker,
    *,
    tenant_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    where: str = "",
    parameters: tuple = (),
) -> list[dict[str, Any]]:
    """Journal rows for a tenant, oldest first. ``where`` is an extra SQL clause with ``?``s."""
    ensure_journal(broker)
    clauses = ["tenant_id = ?"]
    values: list[Any] = [tenant_id]
    if since is not None:
        clauses.append("decided_at >= ?")
        values.append(aware(since).isoformat())
    if until is not None:
        clauses.append("decided_at <= ?")
        values.append(aware(until).isoformat())
    if where:
        clauses.append(f"({where})")
        values.extend(parameters)
    with broker._lock:
        rows = broker._connection.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY decided_at, decision_id",
            tuple(values),
        ).fetchall()
    return [dict(zip(COLUMNS, tuple(row))) for row in rows]
