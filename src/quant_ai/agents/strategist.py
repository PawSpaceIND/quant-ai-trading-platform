"""The pre-open strategist: what Atlas will focus on today, decided before the bell.

Before every session Atlas reads what is already known and writes one plan for the day:
the regime each name opens in and the playbook that regime selects, the names an operator
event blacks out, how the last session went and what it let go by, the probes the budget
allows, the lessons in force and the specialist weights that apply this week. The plan
names a focus list (names whose playbook trades at the plan floor), a stand-down list
(names whose regime demands more than the day is likely to give) and a posture for the
book. It is written to ``session-plans/<IST date>.json`` and read out to the founder as
the morning brief.

Deterministic on purpose. Every line comes from the same code the decisions run on
(``quant_ai.intelligence.regime`` and ``quant_ai.agents.playbook``), so the brief can
never promise a stance the engine would not take. The plan informs the founder and the
record; it changes no gate, no size and no floor. A model-written narrative can join it
later as data; nothing here calls one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.agents.atlas import AtlasPolicy
from quant_ai.agents.playbook import RegimePlaybook, playbook_for
from quant_ai.analytics.decision_journal import aware, parse_decimal
from quant_ai.analytics.decision_quality import (
    IST,
    counts,
    evaluated,
    number,
    ratio,
    write_report,
)
from quant_ai.intelligence.regime import INSUFFICIENT_HISTORY

SCHEMA = "pramana.session_plan.v1"
# The earliest local time a plan is built on a trading day; the pre-open bell is 09:00 IST.
PLAN_FROM = time(8, 30)
# Playbooks that trade the day: at the plan floor (trend, or routing off) or a touch above it.
FOCUS_PLAYBOOKS = frozenset({"trend_following", "range_trading", "unrouted"})
ROLE_FOCUS, ROLE_STANDDOWN, ROLE_BLACKOUT, ROLE_WATCH = "focus", "standdown", "blackout", "watch"
# No regime read yet: the engine decides at the plan floor (plan_default), but the plan
# cannot call a name it has not read a focus; it is listed apart and counts as sitting out.
ROLE_UNREAD = "unread"
POSTURES = ("normal", "selective", "cautious", "defensive", "observe")
MAX_BRIEF_NAMES = 12


@dataclass(frozen=True)
class NameInputs:
    """What the strategist knows about one watched name before the open."""

    symbol: str
    tradable: bool
    regime: str | None          # the deterministic daily label, None when nothing was read
    daily_bars: int
    blackout: str | None = None  # ``event_blackout:<category>`` from the operator's calendar


def role_of(
    playbook: RegimePlaybook, *, blackout: str | None, tradable: bool, regime: str | None = "read",
) -> str:
    if not tradable:
        return ROLE_WATCH
    if blackout:
        return ROLE_BLACKOUT
    if regime is None or regime == INSUFFICIENT_HISTORY:
        return ROLE_UNREAD
    return ROLE_FOCUS if playbook.name in FOCUS_PLAYBOOKS else ROLE_STANDDOWN


def posture_of(names: list[dict[str, Any]]) -> str:
    """How much of the tradable book the day asks Atlas to sit out."""
    tradable = [item for item in names if item["role"] != ROLE_WATCH]
    if not tradable:
        return "observe"
    sitting = sum(1 for item in tradable if item["role"] != ROLE_FOCUS)
    share = Decimal(sitting) / Decimal(len(tradable))
    if share == 0:
        return "normal"
    if share < Decimal("0.5"):
        return "selective"
    if share < 1:
        return "cautious"
    return "defensive"


def yesterday_summary(rows: list[dict[str, Any]], session_date: date | str) -> dict[str, Any]:
    """The last session in the numbers the plan quotes: counts and the 60-minute hit rate."""
    tally = counts(rows)
    scored = evaluated(rows)
    return {
        "session_date": session_date.isoformat() if isinstance(session_date, date) else str(session_date),
        "decisions": tally["decisions"],
        "filled": tally["filled"],
        "rejected": tally["rejected"],
        "abstained": tally["abstained"],
        "probes": tally["probes"],
        "evaluated_60m": len(scored),
        "hit_rate_60m": number(ratio(sum(1 for _, hit, _ in scored if hit), len(scored))),
    }


def missed_summary(directory: str | Path | None, session_date: date | str) -> dict[str, Any] | None:
    """What the last session's missed-opportunity file says, when one was written."""
    if directory is None:
        return None
    stamp = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    path = Path(directory) / f"{stamp}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "pramana.missed_opportunities.v1":
        return None
    from quant_ai.analytics.missed_opportunities import summary_lines

    try:
        top = summary_lines(payload, limit=3)
    except (KeyError, TypeError, ValueError):
        top = []
    return {
        "holds": payload.get("holds"),
        "missed": payload.get("missed"),
        "avoided": payload.get("avoided"),
        "top": top,
    }


def build_session_plan(
    *,
    tenant_id: str,
    now: datetime,
    session_date: date | str,
    names: tuple[NameInputs, ...],
    policy: AtlasPolicy,
    yesterday: Mapping[str, Any] | None = None,
    missed_yesterday: Mapping[str, Any] | None = None,
    lessons_in_force: int = 0,
    skill_weights: Mapping[str, Decimal] | None = None,
    late: bool = False,
) -> dict[str, Any]:
    """One plan for the session, from the same regime and playbook code the decisions use."""
    stamp = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    floor = policy.min_consensus_confidence
    rows = []
    for item in sorted(names, key=lambda entry: entry.symbol):
        playbook = playbook_for(item.regime, enabled=policy.regime_playbooks)
        rows.append({
            "symbol": item.symbol,
            "tradable": item.tradable,
            "regime": item.regime,
            "daily_bars": item.daily_bars,
            "playbook": playbook.name,
            "floor": str(playbook.floor(floor).quantize(Decimal("0.01"))),
            "size_multiplier": str(playbook.size_multiplier),
            "probes_allowed": playbook.probes_allowed and policy.exploration_max_per_day > 0,
            "blackout": item.blackout,
            "role": role_of(playbook, blackout=item.blackout, tradable=item.tradable, regime=item.regime),
        })
    focus = [item["symbol"] for item in rows if item["role"] == ROLE_FOCUS]
    standdown = [item["symbol"] for item in rows if item["role"] == ROLE_STANDDOWN]
    blackout = [item["symbol"] for item in rows if item["role"] == ROLE_BLACKOUT]
    unread = [item["symbol"] for item in rows if item["role"] == ROLE_UNREAD]
    watch = [item["symbol"] for item in rows if item["role"] == ROLE_WATCH]
    return {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "session_date": stamp,
        "generated_at": aware(now).isoformat(),
        "late": bool(late),
        "posture": posture_of(rows),
        "focus": focus,
        "standdown": standdown,
        "blackout": blackout,
        "unread": unread,
        "watch": watch,
        "names": rows,
        "exploration": {
            "max_per_day": policy.exploration_max_per_day,
            "eligible_names": sum(1 for item in rows if item["role"] == ROLE_FOCUS and item["probes_allowed"]),
        },
        "regime_playbooks": policy.regime_playbooks,
        "consensus_floor": str(floor),
        "yesterday": dict(yesterday) if yesterday else None,
        "missed_yesterday": dict(missed_yesterday) if missed_yesterday else None,
        "lessons_in_force": int(lessons_in_force),
        "skill_weights": {str(k): str(v) for k, v in sorted((skill_weights or {}).items())},
        "limitations": [
            (
                "The plan is read from closed daily bars and the operator's calendar before the "
                "open; the intraday regime, the live tape and every gate still rule each decision."
            ),
            (
                "Focus means the regime's playbook trades at the plan floor, not that a trade will "
                "be proposed, approved or filled; stand-down names still trade on an exceptional "
                "consensus."
            ),
            "Paper decisions on live marks; no order is placed and no live fill is implied.",
        ],
    }


def plan_path(directory: str | Path, session_date: date | str) -> Path:
    stamp = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    date.fromisoformat(stamp)
    return Path(directory) / f"{stamp}.json"


def write_plan(path: str | Path, plan: Mapping[str, Any]) -> Path:
    return write_report(path, dict(plan))


def load_plan(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema") == SCHEMA else None


def latest_plan(directory: str | Path) -> dict[str, Any] | None:
    root = Path(directory)
    if not root.is_dir():
        return None
    for path in sorted(root.glob("????-??-??.json"), reverse=True):
        plan = load_plan(path)
        if plan is not None:
            return plan
    return None


def _named(plan: Mapping[str, Any], role: str) -> str:
    items = [f"{item['symbol']} {item['playbook']}" if role == ROLE_FOCUS else
             f"{item['symbol']} {item['regime']}" if role == ROLE_STANDDOWN else
             f"{item['symbol']} {item['blackout']}" if role == ROLE_BLACKOUT else
             f"{item['symbol']} {item['daily_bars']} bars" if role == ROLE_UNREAD else item["symbol"]
             for item in plan["names"] if item["role"] == role]
    shown = ", ".join(items[:MAX_BRIEF_NAMES])
    return shown + (f", +{len(items) - MAX_BRIEF_NAMES}" if len(items) > MAX_BRIEF_NAMES else "")


def morning_brief(plan: Mapping[str, Any]) -> str:
    """The founder's pre-open note: posture, focus, stand-down, blackouts, yesterday, budget."""
    lines = [
        f"Atlas pre-open {plan['session_date']}{' (late)' if plan.get('late') else ''}: "
        f"posture {plan['posture']}, {len(plan['focus'])} focus, {len(plan['standdown'])} stand-down"
        + (f", {len(plan['blackout'])} blackout" if plan["blackout"] else "")
        + (f", {len(plan.get('unread') or ())} unread" if plan.get("unread") else "") + "."
    ]
    if plan["focus"]:
        lines.append(f"Focus: {_named(plan, ROLE_FOCUS)}")
    if plan["standdown"]:
        lines.append(f"Stand down: {_named(plan, ROLE_STANDDOWN)}")
    if plan["blackout"]:
        lines.append(f"Blackout: {_named(plan, ROLE_BLACKOUT)}")
    if plan.get("unread"):
        lines.append(f"Unread (plan floor until the bars exist): {_named(plan, ROLE_UNREAD)}")
    yesterday = plan.get("yesterday")
    if yesterday:
        hit = yesterday.get("hit_rate_60m")
        shown = f"{hit:.2f}" if isinstance(hit, (int, float)) else "-"
        lines.append(
            f"Yesterday {yesterday['session_date']}: {yesterday['decisions']} decisions, "
            f"{yesterday['filled']} filled, {yesterday['probes']} probes, 60m hit rate {shown} "
            f"on {yesterday['evaluated_60m']}."
        )
    missed = plan.get("missed_yesterday")
    if missed and missed.get("missed"):
        lines.append(f"Missed yesterday: {missed['missed']} moves; " + "; ".join(missed.get("top") or [])[:300])
    exploration = plan["exploration"]
    budget = (f"{exploration['max_per_day']}/day across {exploration['eligible_names']} eligible names"
              if exploration["max_per_day"] else "off")
    weights = plan.get("skill_weights") or {}
    weighted = ", ".join(f"{k} x{v}" for k, v in list(weights.items())[:6]) or "none"
    lines.append(f"Probes {budget}. Lessons in force {plan['lessons_in_force']}. Skill weights: {weighted}.")
    return "\n".join(lines)


def render(plan: Mapping[str, Any]) -> str:
    """Plain-text table for the terminal."""
    lines = [morning_brief(plan), ""]
    width = max(len("SYMBOL"), *(len(item["symbol"]) for item in plan["names"])) if plan["names"] else 6
    lines.append(f"{'SYMBOL':<{width}}  ROLE       REGIME                PLAYBOOK          FLOOR  SIZE   PROBES  BLACKOUT")
    for item in plan["names"]:
        lines.append(
            f"{item['symbol']:<{width}}  {item['role']:<9}  {(item['regime'] or 'unread'):<20}  "
            f"{item['playbook']:<16}  {item['floor']:<5}  {item['size_multiplier']:<5}  "
            f"{'yes' if item['probes_allowed'] else 'no':<6}  {item['blackout'] or '-'}"
        )
    return "\n".join(lines)


def hit_rate_of(rows: list[dict[str, Any]]) -> float | None:
    """Convenience for callers that only want the number the brief quotes."""
    scored = evaluated(rows)
    return number(ratio(sum(1 for _, hit, _ in scored if hit), len(scored)))


__all__ = [
    "IST",
    "PLAN_FROM",
    "SCHEMA",
    "NameInputs",
    "build_session_plan",
    "hit_rate_of",
    "latest_plan",
    "load_plan",
    "missed_summary",
    "morning_brief",
    "parse_decimal",
    "plan_path",
    "posture_of",
    "render",
    "role_of",
    "write_plan",
    "yesterday_summary",
]
