"""Deterministic session post-mortem with human approval.

A post-mortem is a file per IST session date: the session's numbers plus at most eight
one-line lessons derived only from those numbers (no LLM, never from fewer than three
observations), each backed by the decision ids it came from. It is written ``pending``;
an operator approves it, and only approved lessons are ever handed to the LLM path as
bounded, untrusted evidence via ``approved_lessons``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.analytics.decision_journal import (
    GOVERNANCE_REJECTED,
    aware,
    load_rows,
    parse_decimal,
)
from quant_ai.analytics.decision_quality import (
    IST,
    by_hour_ist,
    by_regime,
    closed_trades,
    counts,
    direction,
    evaluated,
    mean,
    number,
    ratio,
    rejections,
    session_date_of,
    trades,
    write_report,
)

SCHEMA = "pramana.post_mortem.v1"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
MAX_LESSONS = 8
MAX_LESSON_CHARS = 200
MIN_OBSERVATIONS = 3
MAX_EVIDENCE_IDS = 500
DATE_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FALLBACK_MODES = frozenset(
    {"llm_unavailable", "llm_budget_exhausted", "unverified_inference", "llm_invalid_schema"}
)


class PostMortemApprovedError(RuntimeError):
    """An approved post-mortem is immutable; re-running the build must not overwrite it."""


# ----------------------------------------------------------------- text hygiene


def sanitize_line(text: Any, max_chars: int = MAX_LESSON_CHARS) -> str:
    """One printable line of at most ``max_chars`` characters; empty when nothing survives."""
    if max_chars < 1:
        return ""
    raw = "".join(ch if ch.isprintable() else " " for ch in str(text))
    collapsed = " ".join(raw.split())
    return collapsed[:max_chars]


def _label(value: Any) -> str:
    return sanitize_line(str(value).lower().replace("_", " "), 40) or "unknown"


# ----------------------------------------------------------------- session selection


def session_dates(broker, *, tenant_id: str) -> list[date]:
    """IST session dates with at least one decision, oldest first."""
    seen = set()
    for row in load_rows(broker, tenant_id=tenant_id):
        stamp = session_date_of(row)
        if stamp:
            seen.add(date.fromisoformat(stamp))
    return sorted(seen)


def latest_session_date(broker, *, tenant_id: str) -> date | None:
    dates = session_dates(broker, tenant_id=tenant_id)
    return dates[-1] if dates else None


def session_rows(broker, *, tenant_id: str, session_date: date) -> list[dict[str, Any]]:
    start = datetime.combine(session_date, datetime.min.time(), tzinfo=IST)
    end = start + timedelta(days=1)
    rows = load_rows(broker, tenant_id=tenant_id, since=start, until=end)
    stamp = session_date.isoformat()
    return [row for row in rows if session_date_of(row) == stamp]


# ----------------------------------------------------------------- lessons


def derive_lessons(rows: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Deterministic lessons from the session's numbers: (sentence, decision ids)."""
    candidates: list[tuple[int, int, str, list[str]]] = []

    def add(priority: int, weight: int, sentence: str, ids: list[str]) -> None:
        if len(ids) < MIN_OBSERVATIONS:
            return
        text = sanitize_line(sentence, MAX_LESSON_CHARS)
        if text:
            candidates.append((priority, -weight, text, sorted(set(ids))[:MAX_EVIDENCE_IDS]))

    scored = evaluated(rows)

    # Regime x stance on the 60-minute horizon.
    groups: dict[tuple[str, str], list[tuple[dict, bool]]] = {}
    for row, hit, _ in scored:
        groups.setdefault((str(row.get("regime") or "unknown"), str(row["stance"])), []).append((row, hit))
    for (regime, stance), members in sorted(groups.items()):
        ids = [row["decision_id"] for row, _ in members]
        misses = sum(1 for _, hit in members if not hit)
        hits = len(members) - misses
        if Decimal(misses) / len(members) >= Decimal("0.6"):
            add(0, len(members), (
                f"In {_label(regime)} regime, {misses} of {len(members)} {stance} decisions did "
                f"not move with the call within 60 minutes; consider abstaining there."), ids)
        elif Decimal(hits) / len(members) >= Decimal("0.7"):
            add(8, len(members), (
                f"In {_label(regime)} regime, {hits} of {len(members)} {stance} decisions moved "
                f"with the call within 60 minutes."), ids)

    # Calibration: stated confidence against the realised hit rate.
    confident = [(row, hit, parse_decimal(row.get("confidence"))) for row, hit, _ in scored]
    confident = [item for item in confident if item[2] is not None]
    if len(confident) >= MIN_OBSERVATIONS:
        hit_rate = ratio(sum(1 for _, hit, _ in confident if hit), len(confident))
        confidence = mean([item[2] for item in confident])
        if confidence - hit_rate >= Decimal("0.20"):
            add(1, len(confident), (
                f"Stated confidence averaged {confidence:.2f} but the 60-minute hit rate was "
                f"{hit_rate:.2f} across {len(confident)} directional decisions; confidence "
                f"overstated the edge."), [row["decision_id"] for row, _, _ in confident])

    # Exit mix and net result over closed trades.
    closed = closed_trades(rows)
    if len(closed) >= MIN_OBSERVATIONS:
        ids = [row["decision_id"] for row in closed]
        exits = Counter(str(row.get("exit_trigger") or "unknown") for row in closed)
        holding = mean([Decimal(int(row["holding_minutes"])) for row in closed
                        if isinstance(row.get("holding_minutes"), int)])
        held = f"; average holding time {holding:.0f} minutes" if holding is not None else ""
        stops = exits.get("STOP_LOSS", 0)
        targets = exits.get("TAKE_PROFIT", 0)
        if Decimal(stops) / len(closed) >= Decimal("0.5"):
            add(2, len(closed), f"{stops} of {len(closed)} closed trades ended at the stop loss{held}.", ids)
        if Decimal(targets) / len(closed) >= Decimal("0.5"):
            add(9, len(closed), f"{targets} of {len(closed)} closed trades reached the take-profit target{held}.", ids)
        statistics = trades(rows)
        pnls = [parse_decimal(row["realized_net_pnl"]) for row in closed]
        add(7, len(closed), (
            f"Closed trades netted {sum(pnls, Decimal(0)):.2f} after fees over {len(closed)} "
            f"trades (win rate {Decimal(str(statistics['win_rate'])):.2f}, expectancy "
            f"{Decimal(str(statistics['expectancy'])):.2f} per trade)."), ids)

    # Hour of day.
    by_hour: dict[int, list[tuple[dict, bool]]] = {}
    for row, hit, _ in scored:
        try:
            hour = aware(datetime.fromisoformat(str(row["decided_at"]))).astimezone(IST).hour
        except (ValueError, TypeError):
            continue
        by_hour.setdefault(hour, []).append((row, hit))
    for hour, members in sorted(by_hour.items()):
        misses = sum(1 for _, hit in members if not hit)
        if Decimal(misses) / len(members) >= Decimal("0.6"):
            add(3, len(members), (
                f"Between {hour:02d}:00 and {hour:02d}:59 IST, {misses} of {len(members)} "
                f"directional decisions did not move with the call within 60 minutes."),
                [row["decision_id"] for row, _ in members])

    # Specialist accuracy.
    votes: dict[str, list[tuple[str, bool]]] = {}
    for row in rows:
        forward = parse_decimal(row.get("forward_return_60m"))
        if forward is None:
            continue
        try:
            agents = json.loads(row.get("agents") or "{}")
        except (ValueError, TypeError):
            continue
        observed = 1 if forward > 0 else -1 if forward < 0 else 0
        for agent_id, vote in (agents.items() if isinstance(agents, dict) else ()):
            wanted = direction(vote.get("stance")) if isinstance(vote, dict) else 0
            if wanted:
                votes.setdefault(str(agent_id), []).append((row["decision_id"], observed == wanted))
    for agent_id, calls in sorted(votes.items()):
        misses = sum(1 for _, hit in calls if not hit)
        if Decimal(misses) / len(calls) >= Decimal("0.6"):
            add(4, len(calls), (
                f"Specialist {sanitize_line(agent_id, 40)} was wrong on {misses} of {len(calls)} "
                f"directional calls on the 60-minute horizon."), [decision_id for decision_id, _ in calls])

    # Governance: the dominant rejection reason and the LLM fallback share.
    rejected = [row for row in rows if row.get("governance") == GOVERNANCE_REJECTED]
    if rejected:
        reason, count = Counter(str(row.get("reason") or "unknown") for row in rejected).most_common(1)[0]
        add(5, count, f"{count} of {len(rows)} decisions were rejected for {sanitize_line(reason, 80)}.",
            [row["decision_id"] for row in rejected if str(row.get("reason") or "unknown") == reason])
    fallback = [row for row in rows if str(row.get("mode")) in FALLBACK_MODES]
    if fallback:
        modes = ", ".join(sorted({sanitize_line(row.get("mode"), 32) for row in fallback}))
        add(6, len(fallback), (
            f"{len(fallback)} of {len(rows)} decisions ran without a completed LLM consensus "
            f"({modes}); their outcomes reflect the deterministic fallback."),
            [row["decision_id"] for row in fallback])

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(text, ids) for _, _, text, ids in candidates[:MAX_LESSONS]]


# ----------------------------------------------------------------- build / persist


def build_post_mortem(
    broker, *, tenant_id: str, now: datetime, session_date: date | None = None
) -> dict[str, Any]:
    """Post-mortem for one IST session (default: the latest session with decisions)."""
    current = aware(now)
    if session_date is None:
        session_date = latest_session_date(broker, tenant_id=tenant_id)
        if session_date is None:
            raise ValueError("no journaled decisions to review")
    rows = session_rows(broker, tenant_id=tenant_id, session_date=session_date)
    lessons = derive_lessons(rows)
    scored = evaluated(rows)
    return {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "session_date": session_date.isoformat(),
        "generated_at": current.isoformat(),
        "status": STATUS_PENDING,
        "approved_at": None,
        "summary": {
            "counts": counts(rows),
            "net_pnl": number(sum(
                (parse_decimal(row["realized_net_pnl"]) for row in closed_trades(rows)), Decimal(0)
            )),
            "hit_rate_60m": number(ratio(sum(1 for _, hit, _ in scored if hit), len(scored))),
            "exits": trades(rows)["exits"],
            "rejections": rejections(rows),
            "by_regime": by_regime(rows),
            "by_hour_ist": by_hour_ist(rows),
        },
        "lessons": [text for text, _ in lessons],
        "evidence": {str(index): ids for index, (_, ids) in enumerate(lessons)},
    }


def post_mortem_path(directory: str | Path, session_date: date | str) -> Path:
    stamp = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    if not DATE_STEM.match(stamp):
        raise ValueError("session date must be YYYY-MM-DD")
    date.fromisoformat(stamp)
    return Path(directory) / f"{stamp}.json"


def load_post_mortem(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_post_mortem(directory: str | Path, report: dict[str, Any]) -> Path:
    """Write ``<directory>/<session_date>.json``; a pending file is replaced, an approved one is kept."""
    path = post_mortem_path(directory, report["session_date"])
    existing = load_post_mortem(path) if path.exists() else None
    if existing is not None and existing.get("status") == STATUS_APPROVED:
        raise PostMortemApprovedError(f"post-mortem already approved: {path}")
    return write_report(path, report)


def approve_post_mortem(directory: str | Path, session_date: date | str, now: datetime) -> dict[str, Any]:
    """Mark a written post-mortem approved. Idempotent for an already approved file."""
    path = post_mortem_path(directory, session_date)
    report = load_post_mortem(path)
    if report is None or report.get("schema") != SCHEMA:
        raise FileNotFoundError(f"no post-mortem to approve at {path}")
    if report.get("status") == STATUS_APPROVED and report.get("approved_at"):
        return report
    approved = {**report, "status": STATUS_APPROVED, "approved_at": aware(now).isoformat()}
    write_report(path, approved)
    return approved


def approved_lessons(
    directory: str | Path,
    now: datetime,
    *,
    max_sessions: int = 5,
    max_items: int = MAX_LESSONS,
    max_chars: int = MAX_LESSON_CHARS,
) -> tuple[str, ...]:
    """Lessons from the newest approved post-mortems, newest session first, bounded.

    Files are untrusted input: anything that is not an approved post-mortem with a list of
    strings is ignored, and every lesson is reduced to one line of at most ``max_chars``.
    """
    root = Path(directory)
    if max_sessions < 1 or max_items < 1 or max_chars < 1 or not root.is_dir():
        return ()
    today = aware(now).astimezone(IST).date()
    dated: list[tuple[date, Path]] = []
    for path in root.glob("*.json"):
        if not DATE_STEM.match(path.stem):
            continue
        try:
            session = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if session <= today:
            dated.append((session, path))
    collected: list[str] = []
    sessions_used = 0
    for _, path in sorted(dated, key=lambda item: item[0], reverse=True):
        payload = load_post_mortem(path)
        if (
            payload is None
            or payload.get("schema") != SCHEMA
            or payload.get("status") != STATUS_APPROVED
            or not isinstance(payload.get("lessons"), list)
        ):
            continue
        sessions_used += 1
        for lesson in payload["lessons"]:
            if not isinstance(lesson, str):
                continue
            text = sanitize_line(lesson, max_chars)
            if text:
                collected.append(text)
            if len(collected) >= max_items:
                return tuple(collected)
        if sessions_used >= max_sessions:
            break
    return tuple(collected)
