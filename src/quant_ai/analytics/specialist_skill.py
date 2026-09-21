"""Weekly specialist skill weights from every scored decision, bounded and evidenced.

The attribution engine credits a specialist only when a filled entry closes with a realised
result. A paper book that holds all day never closes an entry, so that engine learns nothing
from a session like 21 September 2026, although every decision that day was scored against
its 60-minute forward return. This module reads that scoring: per specialist, how often the
direction it voted matched the forward return over the last ``DEFAULT_SESSIONS`` IST
sessions before the current week. A specialist scored at least ``MINIMUM_SAMPLE`` times
gets a weight in the same 0.75 to 1.25 band the attribution engine uses; fewer votes keep
the weight at one. The weights are recomputed once per IST week from rows strictly before
the week started, so a restart mid-week reproduces the same numbers, and the report that
carries them names every decision they came from through ``basis_sha256``.

Governed: the band is fixed, the sample floor is fixed, the horizon is the resolver's, the
report is written before the weights apply, and the switch that applies them is an
operator setting. Nothing here trains a model, changes a risk limit or places an order.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    basis = hashlib.sha256(json.dumps(
        [SCHEMA, tenant_id, HORIZON_COLUMN, [item["decision_ids"] for item in agents]],
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
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
