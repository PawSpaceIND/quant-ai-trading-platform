"""Weekly specialist skill weights from every scored decision, bounded and evidenced.

The attribution engine credits a specialist only when a filled entry closes with a realised
result. A paper book that holds all day never closes an entry, so that engine learns nothing
from a session like 21 September 2026, although every decision that day was scored against
its 60-minute forward return. This module reads that scoring: per specialist, how often the
direction it voted matched the forward return over the last ``DEFAULT_SESSIONS`` IST
sessions before the current week. A specialist scored at least ``MINIMUM_SAMPLE`` times
gets a weight in the same 0.75 to 1.25 band the attribution engine uses; fewer votes keep
the weight at one. The daemon freezes one accepted report per tenant and IST week, so a
restart cannot silently incorporate corrected history. ``basis_sha256`` binds the input
values and scoring policy, not just the decision identifiers.

Governed: the band is fixed, the sample floor is fixed, the horizon is the resolver's, the
report is written before the weights apply, and the switch that applies them is an
operator setting. Nothing here trains a model, changes a risk limit or places an order.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.analytics.decision_journal import aware, load_rows, parse_decimal
from quant_ai.analytics.decision_quality import (
    HORIZON_COLUMN,
    IST,
    direction,
    number,
    session_date_of,
    write_report,
)

SCHEMA = "pramana.specialist_skill.v1"
BASIS_SCHEMA = "pramana.specialist_skill_basis.v2"
ACCEPTED_SCHEMA = "pramana.accepted_specialist_skill.v1"
REPORT_NAME = "specialist-skill.json"
DEFAULT_SESSIONS = 10
MINIMUM_SAMPLE = 30
WEIGHT_FLOOR = Decimal("0.75")
WEIGHT_CEILING = Decimal("1.25")
# The window is bounded in days as well as sessions so a long-idle ledger is not read whole.
MAX_LOOKBACK_DAYS = 45
REWEIGHTING_ENV = "PRAMANA_SPECIALIST_REWEIGHTING"
_SWITCH = {"on": True, "true": True, "1": True, "yes": True,
           "off": False, "false": False, "0": False, "no": False}
PLACES = Decimal("0.0001")


def reweighting_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The operator switch; on unless named off, a malformed value refuses to boot."""
    source = os.environ if environ is None else environ
    raw = source.get(REWEIGHTING_ENV, "").strip().lower()
    if not raw:
        return True
    if raw not in _SWITCH:
        raise RuntimeError(f"unsupported learning setting: {REWEIGHTING_ENV} must be on or off")
    return _SWITCH[raw]


def week_start(now: datetime) -> datetime:
    """Monday 00:00 IST of the week that contains ``now``, as an aware UTC instant."""
    local = aware(now).astimezone(IST)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, time.min, tzinfo=IST).astimezone(IST)


def skill_weight(accuracy: Decimal) -> Decimal:
    """The attribution engine's band: 0.75 + accuracy / 2, clamped to [0.75, 1.25]."""
    raw = WEIGHT_FLOOR + accuracy * Decimal("0.5")
    return min(WEIGHT_CEILING, max(WEIGHT_FLOOR, raw)).quantize(PLACES)


def window_rows(broker, *, tenant_id: str, until: datetime, sessions: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows from the last ``sessions`` IST session dates strictly before ``until``."""
    upper = aware(until)
    rows = load_rows(broker, tenant_id=tenant_id, since=upper - timedelta(days=MAX_LOOKBACK_DAYS), until=upper)
    dated: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        stamp = session_date_of(row)
        moment = aware(datetime.fromisoformat(str(row["decided_at"]))) if row.get("decided_at") else None
        if stamp is None or moment is None or moment >= upper:
            continue
        dated.setdefault(stamp, []).append(row)
    kept = sorted(dated)[-sessions:] if sessions > 0 else []
    return [row for stamp in kept for row in dated[stamp]], kept


def score_agents(rows: list[dict[str, Any]], *, minimum_sample: int = MINIMUM_SAMPLE) -> list[dict[str, Any]]:
    """Per specialist: directional votes scored on the horizon, hits, accuracy and weight.

    A vote counts when the specialist took a direction and the row's forward return
    resolved; the hit rule is the report's: the sign of the return matches the vote, and
    a flat return is a miss. NEUTRAL votes are neither right nor wrong and are not counted.
    """
    tallies: dict[str, dict[str, Any]] = {}
    for row in rows:
        forward = parse_decimal(row.get(HORIZON_COLUMN))
        try:
            agents = json.loads(row.get("agents") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(agents, dict):
            continue
        for agent_id, vote in agents.items():
            tally = tallies.setdefault(str(agent_id), {"evaluated": 0, "hits": 0, "decision_ids": []})
            if forward is None or not isinstance(vote, dict):
                continue
            wanted = direction(vote.get("stance"))
            if wanted == 0:
                continue
            observed = 1 if forward > 0 else -1 if forward < 0 else 0
            tally["evaluated"] += 1
            tally["hits"] += int(observed == wanted)
            tally["decision_ids"].append(str(row.get("decision_id")))
    result = []
    for agent_id, tally in sorted(tallies.items()):
        evaluated = tally["evaluated"]
        accuracy = Decimal(tally["hits"]) / Decimal(evaluated) if evaluated else None
        applied = evaluated >= minimum_sample and accuracy is not None
        result.append({
            "agent_id": agent_id,
            "evaluated": evaluated,
            "hits": tally["hits"],
            "directional_accuracy": number(accuracy) if accuracy is not None else None,
            "weight": str(skill_weight(accuracy)) if applied else "1",
            "applied": applied,
            "decision_ids": sorted(tally["decision_ids"]),
        })
    return result


def limitations(*, sessions: int, minimum_sample: int) -> list[str]:
    return [
        (
            f"Directional accuracy over the last {sessions} IST sessions before the week "
            f"began, on the {HORIZON_COLUMN.replace('forward_return_', '')} forward return the "
            "outcome resolver stores; a flat return is a miss and NEUTRAL votes are not scored."
        ),
        (
            f"A weight applies only after {minimum_sample} scored votes and stays inside "
            f"{WEIGHT_FLOOR} to {WEIGHT_CEILING}; below the sample the weight is 1."
        ),
        (
            "Accuracy on paper marks is not demonstrated skill, costs and fills are not "
            "counted, and the weight only scales a specialist's stated confidence; it never "
            "changes a gate, a limit or the consensus floor."
        ),
    ]


def build_skill_report(
    broker,
    *,
    tenant_id: str,
    now: datetime,
    sessions: int = DEFAULT_SESSIONS,
    minimum_sample: int = MINIMUM_SAMPLE,
    until: datetime | None = None,
) -> dict[str, Any]:
    """The weekly skill report: computed from rows strictly before ``until`` (the week start)."""
    if sessions < 1 or minimum_sample < 1:
        raise ValueError("sessions and minimum_sample must be positive")
    current = aware(now)
    # Named by its Monday in IST, the date the founder reads, whatever zone the caller used.
    boundary = (aware(until) if until is not None else week_start(current)).astimezone(IST)
    rows, dates = window_rows(broker, tenant_id=tenant_id, until=boundary, sessions=sessions)
    agents = score_agents(rows, minimum_sample=minimum_sample)
    basis = _digest({
        "schema": BASIS_SCHEMA, "tenant_id": tenant_id, "horizon": HORIZON_COLUMN,
        "week_start": boundary.isoformat(), "sessions": sessions, "minimum_sample": minimum_sample,
        "policy": {"floor": str(WEIGHT_FLOOR), "ceiling": str(WEIGHT_CEILING),
                   "places": str(PLACES), "formula": "floor + accuracy / 2; flat is a miss"},
        "rows": sorted([
            {key: row.get(key) for key in ("decision_id", "decided_at", "agents", HORIZON_COLUMN)}
            for row in rows
        ], key=lambda row: (str(row["decision_id"]), str(row["decided_at"]))),
    })
    return {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "generated_at": current.isoformat(),
        "week_start": boundary.isoformat(),
        "horizon": HORIZON_COLUMN,
        "window": {
            "sessions": sessions,
            "session_dates": dates,
            "since": dates[0] if dates else None,
            "until": boundary.isoformat(),
            "rows": len(rows),
        },
        "minimum_sample": minimum_sample,
        "band": {"floor": str(WEIGHT_FLOOR), "ceiling": str(WEIGHT_CEILING)},
        "agents": [{k: v for k, v in item.items() if k != "decision_ids"} for item in agents],
        "applied": sum(1 for item in agents if item["applied"]),
        "basis_sha256": basis,
        "basis_schema": BASIS_SCHEMA,
        "limitations": limitations(sessions=sessions, minimum_sample=minimum_sample),
    }


def weights_of(report: Mapping[str, Any]) -> dict[str, Decimal]:
    """The applied weights in a report; anything malformed or out of band is left out."""
    weights: dict[str, Decimal] = {}
    for item in report.get("agents") or ():
        if not isinstance(item, dict) or not item.get("applied"):
            continue
        try:
            weight = Decimal(str(item["weight"]))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if WEIGHT_FLOOR <= weight <= WEIGHT_CEILING and isinstance(item.get("agent_id"), str):
            weights[item["agent_id"]] = weight
    return weights


def summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """The part of the report the decision-quality page carries."""
    return {
        "week_start": report.get("week_start"),
        "sessions": (report.get("window") or {}).get("session_dates"),
        "minimum_sample": report.get("minimum_sample"),
        "applied": report.get("applied"),
        "agents": [
            {k: item.get(k) for k in ("agent_id", "evaluated", "directional_accuracy", "weight", "applied")}
            for item in report.get("agents") or ()
        ],
        "basis_sha256": report.get("basis_sha256"),
        "basis_schema": report.get("basis_schema", "legacy_decision_ids_only"),
    }


def notification_message(report: Mapping[str, Any], *, applied: bool) -> str:
    """One line per weighted specialist for the weekly note."""
    week = str(report.get("week_start", ""))[:10]
    scored = [item for item in report.get("agents") or () if item.get("applied")]
    unscored = [item for item in report.get("agents") or () if not item.get("applied")]
    head = f"Specialist skill, week of {week}: " + (
        f"{len(scored)} weighted" if applied else f"{len(scored)} scored, weights not applied (re-weighting off)"
    )
    if unscored:
        head += f", {len(unscored)} under {report.get('minimum_sample')} votes"
    lines = [head + "."]
    for item in sorted(scored, key=lambda entry: (-Decimal(str(entry["weight"])), entry["agent_id"])):
        accuracy = item.get("directional_accuracy")
        shown = f"{accuracy:.2f}" if isinstance(accuracy, (int, float)) else "-"
        lines.append(f"{item['agent_id']} x{item['weight']} ({shown} on {item['evaluated']})")
    return "\n".join(lines)


def report_path(decision_quality_report: Path) -> Path:
    return Path(decision_quality_report).parent / REPORT_NAME


def write_skill_report(path: str | Path, report: Mapping[str, Any]) -> Path:
    return write_report(path, dict(report))


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def accepted_path(path: Path, tenant_id: str, now: datetime) -> Path:
    """Tenant-scoped, path-safe history; the latest display file is not the authority."""
    tenant = hashlib.sha256(tenant_id.encode()).hexdigest()
    return path.parent / "specialist-skill-history" / tenant / f"{week_start(now).date()}.json"


def _validate_report(report: Any, *, tenant_id: str, now: datetime) -> dict:
    """Reject partial/foreign policies before any stored weight can reach the engine."""
    if not isinstance(report, dict) or (
        report.get("schema"), report.get("tenant_id"), report.get("week_start"), report.get("horizon")
    ) != (SCHEMA, tenant_id, week_start(now).isoformat(), HORIZON_COLUMN):
        raise ValueError("specialist_skill_identity_invalid")
    minimum = report.get("minimum_sample")
    if type(minimum) is not int or minimum != MINIMUM_SAMPLE or report.get("band") != {
        "floor": str(WEIGHT_FLOOR), "ceiling": str(WEIGHT_CEILING),
    }:
        raise ValueError("specialist_skill_policy_invalid")
    window = report.get("window")
    if (not isinstance(window, dict) or window.get("sessions") != DEFAULT_SESSIONS
            or window.get("until") != week_start(now).isoformat()
            or report.get("basis_schema") not in (None, "legacy_decision_ids_only", BASIS_SCHEMA)):
        raise ValueError("specialist_skill_window_invalid")
    basis = report.get("basis_sha256")
    if not isinstance(basis, str) or len(basis) != 64 or any(c not in "0123456789abcdef" for c in basis):
        raise ValueError("specialist_skill_basis_invalid")
    agents = report.get("agents")
    if not isinstance(agents, list):
        raise TypeError("specialist_skill_agents_invalid")
    seen = set()
    for agent in agents:
        if not isinstance(agent, dict):
            raise TypeError("specialist_skill_agent_invalid")
        name, evaluated, hits = (agent.get(key) for key in ("agent_id", "evaluated", "hits"))
        if (not isinstance(name, str) or not name or name in seen
                or type(evaluated) is not int or type(hits) is not int or not 0 <= hits <= evaluated):
            raise ValueError("specialist_skill_scores_invalid")
        seen.add(name)
        applied = evaluated >= minimum
        accuracy = Decimal(hits) / Decimal(evaluated) if evaluated else None
        weight = skill_weight(accuracy) if applied else Decimal(1)
        if (agent.get("applied") is not applied or parse_decimal(agent.get("weight")) != weight
                or agent.get("directional_accuracy") != (number(accuracy) if accuracy is not None else None)):
            raise ValueError("specialist_skill_weight_invalid")
    if type(report.get("applied")) is not int or report["applied"] != sum(a["applied"] for a in agents):
        raise ValueError("specialist_skill_count_invalid")
    return report


def accepted_weekly_report(path: Path, broker, *, tenant_id: str, now: datetime) -> dict:
    """Publish once, atomically; restarts and competing processes load the accepted winner.

    A matching legacy display report is adopted on upgrade to preserve weights already
    in force. Its weaker ID-only basis is explicitly marked, never relabelled as v2.
    Corrupt accepted reports are errors, not permission to recompute a different policy.
    """
    target = accepted_path(path, tenant_id, now)

    def read() -> dict:
        envelope = json.loads(target.read_text(encoding="utf-8"))
        if (not isinstance(envelope, dict) or envelope.get("schema") != ACCEPTED_SCHEMA
                or envelope.get("report_sha256") != _digest(envelope.get("report"))):
            raise ValueError("specialist_skill_archive_integrity_invalid")
        return _validate_report(envelope["report"], tenant_id=tenant_id, now=now)

    if target.exists():
        return read()
    report = None
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(previous, dict) and previous.get("tenant_id") == tenant_id and previous.get("week_start") == week_start(now).isoformat():
            report = _validate_report(previous, tenant_id=tenant_id, now=now)
            report.setdefault("basis_schema", "legacy_decision_ids_only")
    if report is None:
        report = build_skill_report(broker, tenant_id=tenant_id, now=now)
    _validate_report(report, tenant_id=tenant_id, now=now)
    envelope = {"schema": ACCEPTED_SCHEMA, "report_sha256": _digest(report), "report": report}
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".skill-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, allow_nan=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Unlike replace(), link() cannot overwrite a concurrently accepted week.
            os.link(temporary, target)
        except FileExistsError:
            pass
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)
    return read()
