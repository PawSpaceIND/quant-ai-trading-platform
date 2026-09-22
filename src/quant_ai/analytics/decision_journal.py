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
import sqlite3
from collections.abc import Mapping
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from quant_ai.agents.contracts import Stance
from quant_ai.domain.models import Side
from quant_ai.llm.diagnostics import normalized_diagnostic

LOGGER = logging.getLogger("quant_ai.decision_journal")

TABLE = "paper_decision_journal"
INDIA_TZ = ZoneInfo("Asia/Kolkata")

HORIZON_COLUMNS = (
    "forward_return_10m",
    "forward_return_30m",
    "forward_return_60m",
    "forward_return_close",
)

# What the fill was priced against, beside what the market was actually showing. The
# engine refuses an entry whose mark has left the reference price by more than the
# tolerance - and then fills the ones it approves AT that reference, never at the mark.
# So every approved entry books the drift the gate tolerated as a gain or a loss no broker
# would have given, and nothing recorded how much. These columns are that amount's inputs,
# kept per decision so it can be measured rather than argued about.
FILL_COLUMNS = (
    # The mark the snapshot froze at T0, and the mark the entry gate compared at submit.
    # reference_price (above) is the third: the last closed one-minute bar the gate used.
    "mark_at_t0",
    "mark_at_submit",
    # Signed, in basis points: (mark_at_submit / reference_price - 1). Recorded on
    # approvals as well as refusals - refusals alone are every drift above the tolerance
    # and none below it, which is a censored sample and cannot describe the path.
    "drift_bps",
    # The book as of T0. Null when the quote was one-sided, crossed or absent; never zero,
    # because "no quote" and "a zero spread" are different facts.
    "bid",
    "ask",
    "spread_bps",
    # What the paper ledger actually charged, and what priced it.
    "fill_price",
    "fill_anchor",
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
    "inference_status",
    "inference_failure_code",
    "governance",
    "reason",
    "order_id",
    "agents",
    "features",
    "feature_schema_version",
    "probe",
    "playbook",
    "forecast_probability_up",
    "forecast_horizon_seconds",
    "forecast_resolves_at",
    "forecast_cost_bps",
    "forecast_basis",
    *FILL_COLUMNS,
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
    inference_status TEXT,
    inference_failure_code TEXT,
    governance TEXT NOT NULL,
    reason TEXT,
    order_id TEXT,
    agents TEXT NOT NULL,
    features TEXT,
    feature_schema_version INTEGER,
    probe INTEGER,
    playbook TEXT,
    forecast_probability_up TEXT,
    forecast_horizon_seconds INTEGER,
    forecast_resolves_at TEXT,
    forecast_cost_bps TEXT,
    forecast_basis TEXT,
    mark_at_t0 TEXT,
    mark_at_submit TEXT,
    drift_bps TEXT,
    bid TEXT,
    ask TEXT,
    spread_bps TEXT,
    fill_price TEXT,
    fill_anchor TEXT,
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


# Columns added after the first release, as ``(name, declaration)``. ``CREATE TABLE IF NOT
# EXISTS`` does nothing to a table that already exists, so a ledger written by an earlier
# build keeps its old shape while ``insert_decision`` names every column in ``COLUMNS`` -
# and the insert fails. The daemon logs that failure and carries on, so the first symptom
# would be a journal that quietly stopped growing, which is the one failure this table
# exists to prevent. Every entry must be nullable: rows decided before a column existed
# genuinely have nothing to put in it, and NULL is the honest value.
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("inference_status", "TEXT"),
    ("inference_failure_code", "TEXT"),
    ("features", "TEXT"),
    ("feature_schema_version", "INTEGER"),
    # 1 when the decision was an exploration probe: a hold the specialists' lean turned
    # into a bounded entry under the policy's daily budget. The budget is counted from
    # this column, so a restart cannot reset it.
    ("probe", "INTEGER"),
    # The regime playbook the decision was judged under (quant_ai.agents.playbook), so
    # decision quality can score trend_following apart from defensive.
    ("playbook", "TEXT"),
    # The claim the decision made, written before its outcome exists. Probability that the
    # forward return over the horizon is positive, the horizon it refers to, the cost the
    # move must clear, and the named mapping that produced it. A refit ships a new basis so
    # a stored number's meaning never moves under it. See quant_ai.agents.forecast.
    ("forecast_probability_up", "TEXT"),
    ("forecast_horizon_seconds", "INTEGER"),
    ("forecast_resolves_at", "TEXT"),
    ("forecast_cost_bps", "TEXT"),
    ("forecast_basis", "TEXT"),
    # What priced the fill against what the market was showing. See FILL_COLUMNS: the
    # ledger anchors every fill to reference_price, so mark_at_submit beside it is the
    # only way to see the edge the drift tolerance hands the book on an approval.
    ("mark_at_t0", "TEXT"),
    ("mark_at_submit", "TEXT"),
    ("drift_bps", "TEXT"),
    ("bid", "TEXT"),
    ("ask", "TEXT"),
    ("spread_bps", "TEXT"),
    ("fill_price", "TEXT"),
    ("fill_anchor", "TEXT"),
)


def journal_columns(broker) -> set[str]:
    """The columns this ledger actually has, which is not always the ones this build names."""
    with broker._lock:
        return {row[1] for row in broker._connection.execute(f"PRAGMA table_info({TABLE})")}


def ensure_journal(broker) -> None:
    """Create the journal table lazily, and add any column an older ledger predates.

    A reader may hold this ledger open read-only - ``scripts/pilot_ops.py`` deliberately
    does, so an operator shell cannot write to the live book. Such a connection cannot run
    the DDL, and until now that turned every read of a ledger one migration behind into
    ``attempt to write a readonly database``. The window is real and badly placed: between
    a deploy that adds a column and the session's first journalled decision, which is the
    pre-open hour an operator is most likely to be reading.

    So the DDL is best-effort. A writer still migrates and still fails loudly if it cannot;
    a reader gets whatever columns exist, and ``load_rows`` fills the rest with None -
    which is the honest value, because rows written before a column existed have nothing
    to put in it.
    """
    try:
        with broker._lock, broker._connection as db:
            db.execute(SCHEMA)
            db.execute(INDEX)
            present = {row[1] for row in db.execute(f"PRAGMA table_info({TABLE})")}
            for column, declaration in MIGRATIONS:
                if column not in present:
                    # Cheap in SQLite: appends to the header, never rewrites the rows.
                    db.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {declaration}")
    except sqlite3.OperationalError as error:
        # Only the one cause is tolerated. A corrupt table or a locked writer must still
        # raise, because those are failures rather than a reader being ahead of a ledger.
        if "readonly" not in str(error).lower():
            raise
        LOGGER.debug("journal_migration_skipped_on_readonly_connection")


def aware(value: datetime) -> datetime:
    """Reject naive datetimes; normalise everything else to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


# Bumped whenever the set of features the pipeline publishes changes, so a training set
# assembled later can tell "this decision had no such feature" from "this feature was
# zero". Rows keep the version they were written under; nothing is ever back-filled.
FEATURE_SCHEMA_VERSION = 1

# A decision is judged on a bounded vector, and a ledger is not a place to discover that
# an upstream dict grew without limit.
MAX_FEATURES = 200
MAX_FEATURE_NAME = 64
MAX_FEATURE_TEXT = 200


def feature_json(features: Any) -> str | None:
    """The decision-time feature vector as exact text, or None when there is none.

    Values arrive as ``Decimal`` (every number the pipeline computes) or ``str`` (labels
    such as the regime). Decimals are stored as text for the same reason money is: a float
    round-trip changes the number, and a training set built from these rows would be
    learning against inputs the engine never actually saw. A non-finite value is dropped
    rather than written, because NaN in a feature column is indistinguishable from a
    feature that was genuinely absent.
    """
    if not isinstance(features, dict) or not features:
        return None
    encoded: dict[str, str] = {}
    for key, value in features.items():
        if len(encoded) >= MAX_FEATURES:
            break
        name = str(key)[:MAX_FEATURE_NAME]
        if isinstance(value, bool):
            # bool is an int subclass; storing True as "1" loses that it was a flag.
            encoded[name] = "true" if value else "false"
        elif isinstance(value, (Decimal, int, float)):
            text = decimal_text(value)
            if text is not None:
                encoded[name] = text
        elif isinstance(value, str):
            encoded[name] = value[:MAX_FEATURE_TEXT]
    if not encoded:
        return None
    return json.dumps(encoded, sort_keys=True, allow_nan=False)


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


def decision_provenance(result) -> dict:
    provenance = getattr(result.xai_trace, "provenance", None)
    if not isinstance(provenance, dict):
        provenance = getattr(result.proposal, "provenance", None)
    return provenance if isinstance(provenance, dict) else {}


def decision_mode(result, *, llm_available: bool) -> str:
    """Evidence mode of the decision, labelled the way the Atlas consensus labels its proofs."""
    provenance = decision_provenance(result)
    mode = provenance.get("mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()[:64]
    inference = provenance.get("inference")
    if isinstance(inference, dict):
        return MODE_BY_INFERENCE_STATUS.get(str(inference.get("status")), MODE_UNVERIFIED)
    return MODE_DETERMINISTIC if not llm_available else MODE_UNVERIFIED


def inference_diagnostic(result) -> tuple[str | None, str | None]:
    provenance = decision_provenance(result)
    inference = provenance.get("inference")
    if not isinstance(inference, dict):
        inference = {}
    status, code = inference.get("status"), inference.get("failure_code")
    if status in (None, "unverified") and provenance.get("mode") == "llm_invalid_schema":
        status = "invalid_schema"
    if not inference and provenance.get("mode") == MODE_DETERMINISTIC:
        status = "not_requested"
    return normalized_diagnostic(status, code)


def probe_of(proposal) -> bool:
    """Whether the proposal is an exploration probe, read from its provenance."""
    provenance = getattr(proposal, "provenance", None)
    exploration = provenance.get("exploration") if isinstance(provenance, dict) else None
    return isinstance(exploration, dict) and bool(exploration.get("probe"))


def forecast_of(proposal) -> dict[str, Any]:
    """The decision's recorded forecast, flattened to its journal columns."""
    from quant_ai.agents.forecast import SCHEMA as FORECAST_SCHEMA

    provenance = getattr(proposal, "provenance", None)
    block = provenance.get("forecast") if isinstance(provenance, dict) else None
    if not isinstance(block, dict) or block.get("schema") != FORECAST_SCHEMA:
        return {"forecast_probability_up": None, "forecast_horizon_seconds": None,
                "forecast_resolves_at": None, "forecast_cost_bps": None, "forecast_basis": None}
    horizon = block.get("horizon_seconds")
    return {
        "forecast_probability_up": _text(block.get("probability_up")),
        "forecast_horizon_seconds": horizon if isinstance(horizon, int) else None,
        "forecast_resolves_at": _text(block.get("resolves_at")),
        "forecast_cost_bps": _text(block.get("cost_bps")),
        "forecast_basis": _text(block.get("basis")),
    }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def playbook_of(proposal) -> str | None:
    """The regime playbook name the proposal's decision recorded, when it recorded one."""
    provenance = getattr(proposal, "provenance", None)
    playbook = provenance.get("playbook") if isinstance(provenance, dict) else None
    name = playbook.get("name") if isinstance(playbook, dict) else None
    return str(name)[:64] if isinstance(name, str) and name else None


def count_probes(broker, *, tenant_id: str, now: datetime) -> int:
    """Probes journaled in the IST session that contains ``now``.

    The exploration budget is a per-session count, and the journal is the only record
    that survives a restart, so Atlas asks here rather than trusting its own memory.
    """
    local = aware(now).astimezone(INDIA_TZ)
    start = datetime.combine(local.date(), time.min, tzinfo=INDIA_TZ)
    end = datetime.combine(local.date(), time.max, tzinfo=INDIA_TZ)
    rows = load_rows(broker, tenant_id=tenant_id, since=start, until=end, where="probe = 1")
    return len(rows)


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


# The paper ledger prices every fill from ``order.reference_price`` (see
# quant_ai.execution.friction: execution_price = reference_price +/- spread and slippage).
# It never reads the mark at submit. Recorded as a value rather than left implicit so that
# a later change of anchor is visible in the data as a different string, and pinned by a
# test against the ledger's real behaviour so the two cannot drift apart in silence.
FILL_ANCHOR_REFERENCE = "reference_price_at_t0"


def fill_marks(proposal: Any, fill: Any, marks: Any = None) -> dict[str, Any]:
    """What the fill was priced against, beside what the market was showing.

    Three marks describe one entry and they are routinely different:
    ``reference_price`` is the last closed one-minute bar the gate used, ``mark_at_t0`` is
    the tick the snapshot froze, and ``mark_at_submit`` is what the book showed when the
    order went in. The ledger charges against the first. An approval at fifteen basis
    points of drift therefore books fifteen basis points no broker would have given, and
    without these columns beside each other that amount cannot be computed after the fact.

    Everything absent stays None. A mark that was never observed is not a zero.
    """
    supplied = marks if isinstance(marks, Mapping) else {}
    provenance = getattr(proposal, "provenance", None)
    snapshot = provenance.get("feature_snapshot") if isinstance(provenance, dict) else None
    market = snapshot.get("market") if isinstance(snapshot, dict) else None
    book = market if isinstance(market, dict) else {}
    filled = fill is not None and getattr(fill, "average_price", None) is not None
    return {
        # Already strings in the snapshot; carried through rather than reformatted, so the
        # journal and the proof cannot disagree about what the decision saw.
        "mark_at_t0": _text_or_none(book.get("last_price")),
        "bid": _text_or_none(book.get("bid")),
        "ask": _text_or_none(book.get("ask")),
        "spread_bps": _text_or_none(book.get("spread_bps")),
        # Measured by the entry gate, which is the only place both numbers exist at once.
        "mark_at_submit": _text_or_none(supplied.get("mark_at_submit")),
        "drift_bps": _text_or_none(supplied.get("drift_bps")),
        "fill_price": decimal_text(fill.average_price) if filled else None,
        # Only meaningful when something actually filled.
        "fill_anchor": FILL_ANCHOR_REFERENCE if filled else None,
    }


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else decimal_text(value)
    return text if text else None


def decision_row(
    result,
    *,
    tenant_id: str,
    now: datetime,
    regime: Any = None,
    mode: str | None = None,
    llm_available: bool = False,
    features: Any = None,
    marks: Any = None,
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
    inference_status, inference_failure_code = inference_diagnostic(result)
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
        "inference_status": inference_status,
        "inference_failure_code": inference_failure_code,
        "governance": governance,
        "reason": reason[:200] if reason else None,
        "order_id": str(fill.order_id) if fill is not None and fill.order_id else None,
        "agents": json.dumps(agents_of(trace), sort_keys=True, allow_nan=False),
        # What the decision was made on, not what the agents concluded from it. ``agents``
        # already records the conclusions; without the inputs beside them no later work can
        # ask whether the conclusions were any good, and the inputs cannot be reconstructed
        # after the fact from anything the ledger keeps.
        "features": (encoded := feature_json(features)),
        "feature_schema_version": FEATURE_SCHEMA_VERSION if encoded else None,
        "probe": 1 if probe_of(proposal) else 0,
        "playbook": playbook_of(proposal),
        **forecast_of(proposal),
        **fill_marks(proposal, fill, marks),
    }


def insert_decision(broker, row: dict[str, Any]) -> bool:
    """Insert a journal row; a repeated ``decision_id`` is a no-op. True when inserted."""
    ensure_journal(broker)
    values = {column: row.get(column) for column in COLUMNS}
    values["inference_status"], values["inference_failure_code"] = normalized_diagnostic(
        values["inference_status"], values["inference_failure_code"]
    )
    # ``decision_row`` already encodes this, but a caller building a row by hand naturally
    # puts the mapping here, and SQLite refuses to bind a dict. That raise reaches the
    # daemon's journal guard, which logs and continues - so the mistake would cost the
    # whole evidence trail and show up only as a table that stopped growing. Encoded here
    # too, where it is one call and the failure mode is closed for every caller.
    if values.get("features") is not None and not isinstance(values["features"], str):
        values["features"] = feature_json(values["features"])
        if values["features"] is None:
            values["feature_schema_version"] = None
        elif values.get("feature_schema_version") is None:
            values["feature_schema_version"] = FEATURE_SCHEMA_VERSION
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
    features: Any = None,
    marks: Any = None,
) -> bool:
    """Journal one cadence decision. Idempotent on ``decision_id``; True when a row was added."""
    row = decision_row(
        result, tenant_id=tenant_id, now=now, regime=regime, mode=mode,
        llm_available=llm_available, features=features, marks=marks,
    )
    inserted = insert_decision(broker, row)
    if inserted:
        LOGGER.info(
            "decision_journaled decision_id=%s symbol=%s governance=%s reason=%s",
            row["decision_id"], row["symbol"], row["governance"], row["reason"],
        )
    return inserted


def ordered_decision_ids(broker, *, tenant_id: str) -> set[str]:
    """Decision ids that produced an order. Their proofs are fill evidence and are kept."""
    with broker._lock:
        rows = broker._connection.execute(
            f"SELECT decision_id FROM {TABLE} WHERE tenant_id = ? AND order_id IS NOT NULL",
            (tenant_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


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
    # Only the columns this ledger has. A reader newer than the ledger it is pointed at -
    # a read-only operator shell between a deploy and the first journalled decision - would
    # otherwise name a column the table does not carry and fail on the SELECT itself.
    present = journal_columns(broker)
    selected = [column for column in COLUMNS if column in present]
    absent = [column for column in COLUMNS if column not in present]
    with broker._lock:
        rows = broker._connection.execute(
            f"SELECT {', '.join(selected)} FROM {TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY decided_at, decision_id",
            tuple(values),
        ).fetchall()
    # Every caller still gets a full row. A column the ledger predates is None, not zero:
    # those decisions genuinely have nothing to put in it, and a zero would be a value.
    blanks = dict.fromkeys(absent)
    return [{**dict(zip(selected, tuple(row))), **blanks} for row in rows]
