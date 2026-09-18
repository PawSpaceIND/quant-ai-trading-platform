"""Journal-bound specialist credit, not counterfactual skill or model promotion.

The resolved decision journal is the outcome authority. This module does not
independently attest to broker fills or statutory costs. Only positive-confidence
supporters of a filled BUY entry share that entry's resolved net result.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.analytics.decision_journal import aware, load_rows, parse_decimal

POLICY = "pramana.entry_supporter_credit.v1"
TABLE = "paper_specialist_feedback"
UP = frozenset({"BUY", "STRONG_BUY"})
STANCES = UP | {"SELL", "STRONG_SELL", "NEUTRAL", "AVOID"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_agent_field")
        result[key] = value
    return result


def _id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("identity_missing_or_invalid")
    return value


def _event(row, tenant, now):
    if row.get("tenant_id") != tenant:
        raise ValueError("tenant_mismatch")
    if row.get("governance") != "filled" or row.get("side") != "BUY" or row.get("stance") not in UP:
        raise ValueError("not_a_filled_long_entry")
    decided = aware(datetime.fromisoformat(row["decided_at"]))
    exited = aware(datetime.fromisoformat(row["exit_at"]))
    if exited < decided or exited > now:
        raise ValueError("outcome_time_invalid_or_future")
    pnl = parse_decimal(row.get("realized_net_pnl"))
    if pnl is None:
        raise ValueError("net_outcome_invalid")
    agents = json.loads(row.get("agents") or "{}", object_pairs_hook=_unique)
    if not isinstance(agents, dict) or not agents:
        raise ValueError("agent_opinions_missing")
    supporters, ignored = {}, {}
    for name, vote in agents.items():
        _id(name)
        if not isinstance(vote, dict) or vote.get("stance") not in STANCES:
            raise ValueError("agent_opinion_invalid")
        confidence = parse_decimal(vote.get("confidence"))
        if confidence is None or not Decimal(0) <= confidence <= Decimal(1):
            raise ValueError("agent_confidence_invalid")
        target = supporters if vote["stance"] in UP and confidence > 0 else ignored
        target[name] = {"stance": vote["stance"], "confidence": str(confidence)}
    if not supporters:
        raise ValueError("no_eligible_supporter")
    regime = row.get("regime") or ""
    if not isinstance(regime, str) or len(regime) > 64:
        raise ValueError("regime_invalid")
    return {
        "policy": POLICY, "tenant_id": tenant,
        "decision_id": _id(row.get("decision_id")), "order_id": _id(row.get("order_id")),
        "symbol": _id(row.get("symbol")), "market": _id(row.get("market")),
        "asset_class": _id(row.get("asset_class")), "regime": regime,
        "decided_at": decided.isoformat(), "exit_at": exited.isoformat(),
        "net_pnl": str(pnl), "supporters": supporters, "uncredited": ignored,
    }


def _persist(broker, events, *, tenant, now, since):
    db = broker._connection
    if db.in_transaction:
        raise ValueError("feedback_requires_own_transaction")
    with db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            tenant_id TEXT NOT NULL, decision_id TEXT NOT NULL, order_id TEXT NOT NULL,
            payload TEXT NOT NULL, sha256 TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,decision_id), UNIQUE(tenant_id,order_id))""")
        for verb in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_{verb.lower()}_blocked "
                       f"BEFORE {verb} ON {TABLE} BEGIN "
                       "SELECT RAISE(ABORT,'Specialist feedback is append-only'); END")
        existing = db.execute(
            f"SELECT decision_id,payload,sha256 FROM {TABLE} WHERE tenant_id=?", (tenant,)
        ).fetchall()
        incoming = {item["decision_id"]: _canonical(item) for item in events}
        for old in existing:
            raw, digest = old[1], old[2]
            if hashlib.sha256(raw.encode()).hexdigest() != digest:
                raise ValueError("feedback_digest_mismatch")
            payload = json.loads(raw)
            if (payload.get("policy") != POLICY or payload.get("tenant_id") != tenant
                    or payload.get("decision_id") != old[0]):
                raise ValueError("feedback_identity_mismatch")
            if since is not None and aware(datetime.fromisoformat(payload["decided_at"])) < since:
                continue
            if aware(datetime.fromisoformat(payload["exit_at"])) > now:
                continue
            if incoming.get(old[0]) != raw:
                raise ValueError("feedback_source_changed_or_missing")
        for event in events:
            raw = incoming[event["decision_id"]]
            db.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES(?,?,?,?,?,?)", (
                tenant, event["decision_id"], event["order_id"], raw,
                hashlib.sha256(raw.encode()).hexdigest(), now.isoformat(),
            ))
            stored = db.execute(
                f"SELECT payload FROM {TABLE} WHERE tenant_id=? AND decision_id=?",
                (tenant, event["decision_id"]),
            ).fetchone()
            if stored is None or stored[0] != raw:
                raise ValueError("feedback_entry_reused")


def refresh_feedback(engine, broker, *, tenant_id, now=None, since=None, upper_bound=None):
    """Rebuild from validated closed entries; repeated refresh never increments twice.

    Source audit records are immutable. Before/after weights are a deterministic
    replay ordered by exit time, not a claim that a late-resolved result was known
    earlier. Actual uses remain in decision traces. Any inconsistency clears
    adaptive weights rather than retaining an unverifiable score.
    """
    from quant_ai.analytics.attribution import AgentAttributionEngine

    status = {
        "schema": POLICY, "tenant_id": tenant_id, "status": "unavailable",
        "credited_entries": 0, "skipped": {}, "events": [], "agents": [],
    }
    engine._records = {}
    engine.feedback = status
    try:
        at = aware(now or datetime.now(timezone.utc))
        if upper_bound is not None:
            at = min(at, aware(upper_bound))
        status["checked_at"] = at.isoformat()
        cutoff = aware(since) if since is not None else None
        with broker._lock:
            if broker._connection.in_transaction:
                raise ValueError("feedback_requires_own_transaction")
            rows = load_rows(broker, tenant_id=tenant_id, since=cutoff, until=at)
            events, skipped = [], Counter()
            for row in rows:
                if row.get("realized_net_pnl") is None:
                    continue
                try:
                    events.append(_event(row, tenant_id, at))
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    skipped["invalid_or_ineligible_closed_entry"] += 1
            if len({e["order_id"] for e in events}) != len(events):
                raise ValueError("duplicate_closed_entry_order")
            events.sort(key=lambda item: (item["exit_at"], item["decision_id"]))
            _persist(broker, events, tenant=tenant_id, now=at, since=cutoff)
            replay = AgentAttributionEngine()
            audit = []
            for event in events:
                names = tuple(sorted(event["supporters"]))
                before = {name: str(replay.weight_for(name, event["regime"])[0]) for name in names}
                replay.record(names, Decimal(event["net_pnl"]), event["regime"])
                audit.append({
                    "decision_id": event["decision_id"], "order_id": event["order_id"],
                    "exit_at": event["exit_at"], "net_pnl": event["net_pnl"],
                    "source_sha256": hashlib.sha256(_canonical(event).encode()).hexdigest(),
                    "credited_agents": list(names), "uncredited_agents": sorted(event["uncredited"]),
                    "replay_before": before,
                    "replay_after": {name: str(replay.weight_for(name, event["regime"])[0]) for name in names},
                })
            engine._records = replay._records
            status.update(
                status="partial" if skipped else "applied" if events else "no_closed_entries",
                credited_entries=len(events), skipped=dict(skipped), events=audit[-50:],
                event_count=len(audit),
                basis_sha256=hashlib.sha256(_canonical([POLICY, tenant_id, [item["source_sha256"] for item in audit]]).encode()).hexdigest(),
                agents=[{"agent_id": item.agent_id, "observations": item.observations,
                         "weight": str(item.conviction_weight)} for item in replay.attribution()],
            )
            return len(events)
    except Exception as error:  # noqa: BLE001 - never leak evidence or break protection
        engine._records = {}
        status.update(status="refused", reason=type(error).__name__)
        return 0
