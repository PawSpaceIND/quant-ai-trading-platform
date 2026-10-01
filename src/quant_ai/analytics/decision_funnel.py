"""Read-only session denominators and exact order/evidence links; never trading policy."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_ai.analytics.calibrate import basis_integrity
from quant_ai.analytics.forecast_scoring import summarize as score_forecasts


def _object(value):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}


def _gate_reasons(evidence):
    reasons = _object(evidence).get("deterministic_gate_reasons")
    if isinstance(reasons, list) and all(isinstance(reason, str) and reason.strip() for reason in reasons):
        return reasons
    return ["unknown"]


def research_coverage(rows):
    """Describe recorded admission evidence, never reconstruct votes or trade permission."""
    states = Counter()
    stale = Counter()
    entries = 0
    for row in rows:
        admission = _object(row.get("funnel_evidence")).get("admission")
        valid = isinstance(admission, dict) and admission.get("schema") == "pramana.evidence_admission.v1"
        agents = admission.get("agents") if valid else None
        minimum = admission.get("minimum_voters") if valid else None
        valid = valid and type(minimum) is int and minimum > 0 and isinstance(agents, list)
        if valid:
            identities = []
            for agent in agents:
                if not isinstance(agent, dict):
                    valid = False
                    break
                identity = agent.get("agent_id")
                age, budget = agent.get("age_seconds"), agent.get("budget_seconds")
                if (not isinstance(identity, str) or not identity.strip()
                        or agent.get("role") not in ("gate", "voter")
                        or type(age) is not int or age < 0 or type(budget) is not int or budget < 0
                        or type(agent.get("coverage_candidate")) is not bool
                        or type(agent.get("over_atlas_age_budget")) is not bool
                        or agent["over_atlas_age_budget"] != (age > budget)
                        or (agent["role"] == "gate" and agent["coverage_candidate"])):
                    valid = False
                    break
                identities.append(identity)
            valid = valid and len(set(identities)) == len(identities)
        if not valid:
            states["unknown_admission"] += 1
            continue
        candidates = sum(a["coverage_candidate"] for a in agents)
        states["candidate_quorum_met" if candidates >= minimum else "candidate_quorum_below"] += 1
        entries += len(agents)
        # Count evaluations affected once, even if several inputs exceed their age budget.
        if any(a["over_atlas_age_budget"] for a in agents):
            stale["evaluations_with_over_age_input"] += 1
        stale["over_age_agent_entries"] += sum(a["over_atlas_age_budget"] for a in agents)
    return {"recorded_evaluations": len(rows),
            "admission_states": {name: states[name] for name in
                ("candidate_quorum_met", "candidate_quorum_below", "unknown_admission")},
            "validated_agent_entries": entries,
            "evaluations_with_over_age_input": stale["evaluations_with_over_age_input"],
            "over_age_agent_entries": stale["over_age_agent_entries"],
            "limitations": ["Coverage candidates are not votes cast or permission to trade.",
                            "Input age is recorded Atlas evidence age, not independent source authentication.",
                            "Unknown admission contributes no inferred fresh or stale inputs."]}


def report(database: Path, tenant: str, session_date: str) -> dict:
    """One IST date, half-open bounds and one read transaction across all evidence.

    Missing historical tables/columns remain unknown. No constructors or migrations.
    A journal fill is not a count of submissions; uncertain dispatch stays explicit.
    """
    day = date.fromisoformat(session_date)
    start = datetime.combine(day, time.min, ZoneInfo("Asia/Kolkata"))
    end = start + timedelta(days=1)
    bounds = (tenant, start.astimezone(timezone.utc).isoformat(),
              end.astimezone(timezone.utc).isoformat())
    db = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def session_rows(table, clock):
            if table not in tables:
                return None
            return [dict(r) for r in db.execute(
                f"SELECT * FROM {table} WHERE tenant_id=? AND julianday({clock})>=julianday(?) "
                f"AND julianday({clock})<julianday(?) ORDER BY {clock}", bounds)]

        rows = session_rows("paper_decision_journal", "decided_at")
        fills = session_rows("paper_ledger", "created_at")
        rows_list, fills_list = rows or [], fills or []
        outcomes = Counter(r.get("governance", "unknown") for r in rows_list)
        reasons = Counter(r.get("reason") or "unspecified" for r in rows_list
                          if r.get("governance") != "filled")
        states = Counter(_object(r.get("funnel_evidence")).get("order_state") or "unknown"
                         for r in rows_list)
        evaluations = []
        for r in rows_list:
            evidence = _object(r.get("funnel_evidence"))
            evaluations.append({
                "decision_id": r["decision_id"], "symbol": r["symbol"],
                "decided_at": r["decided_at"], "side": r.get("side"),
                "governance": r.get("governance"), "reason": r.get("reason"),
                "inference_status": r.get("inference_status"),
                "order_id": r.get("order_id"), "evidence": evidence or None,
            })
        links = []
        for fill in fills_list:
            order = fill["order_id"]
            journal = [r["decision_id"] for r in rows_list if r.get("order_id") == order]
            canonical = {}
            for table in ("paper_decision_evidence", "paper_protection_evidence"):
                if table in tables:
                    found = db.execute(f"SELECT payload FROM {table} WHERE tenant_id=? AND order_id=?",
                                       (tenant, order)).fetchone()
                    if found:
                        canonical[table] = _object(found[0]).get("decision_id")
            outbox = None
            if "paper_protective_fill_outbox" in tables:
                outbox = db.execute("SELECT 1 FROM paper_protective_fill_outbox "
                                    "WHERE tenant_id=? AND order_id=?", (tenant, order)).fetchone() is not None
            kinds = list(canonical)
            kind = ("conflict" if len(kinds) > 1 else "swarm" if "paper_decision_evidence" in kinds
                    else "protection" if "paper_protection_evidence" in kinds else "unattributed")
            links.append({"order_id": order, "symbol": fill["symbol"], "created_at": fill["created_at"],
                          "kind": kind, "journal_decision_ids": journal,
                          "canonical_decision_ids": canonical, "protective_outbox_present": outbox})
        return {
            "schema": "pramana.decision_funnel_report.v1", "tenant": tenant,
            "session_date": session_date, "timezone": "Asia/Kolkata",
            "window_start": bounds[1], "window_end_exclusive": bounds[2],
            "evaluations": len(rows_list) if rows is not None else None,
            "directional_proposals": sum(r.get("side") in {"BUY", "SELL"} for r in rows_list)
                if rows is not None else None,
            "outcomes": dict(outcomes), "hold_rejection_reasons": dict(reasons),
            "inference_statuses": dict(Counter(r.get("inference_status") or "unknown" for r in rows_list)),
            "deterministic_gate_reasons": dict(Counter(
                reason for r in rows_list
                for reason in _gate_reasons(r.get("funnel_evidence")))),
            "recorded_execution_states": dict(states),
            "ledger_fills": len(fills_list) if fills is not None else None,
            "fill_kinds": dict(Counter(r["kind"] for r in links)),
            "evaluation_records": evaluations, "fill_links": links,
            "forecast_scoring": score_forecasts(rows_list),
            "forecast_basis_integrity": basis_integrity(rows_list),
            "research_coverage": research_coverage(rows_list) if rows is not None else None,
            "limitations": ["Counts cover recorded evaluations, not scheduler ticks missing from the journal.",
                            "A side is a recorded directional proposal, not proof of submission.",
                            "Evidence presence/IDs are linkage, not cryptographic validation or fill conservation.",
                            "Protective fills have separate evidence and need not have a swarm evaluation.",
                            "Missing tables/legacy evidence are unknown; no trade or live-readiness verdict."],
        }
    finally:
        db.close()
