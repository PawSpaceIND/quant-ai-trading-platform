"""What the swarm held through: the up-moves after a hold that a long-only book could have bought.

On 21 September 2026, the first twelve-name session, every decision was a hold. The
decision-quality report has nothing to say about a day like that: hit rates and calibration
score directional calls, and there were none. This report scores the holds instead, against
the forward returns the outcome resolver marks on every journal row. A hold followed by an
up-move of at least the threshold is a *missed* move; a hold followed by a down-move of at
least the threshold is an *avoided* one; a hold whose horizon is still null is unresolved
and scores nothing. One file per IST session date, so the founder can open the day and see
which names moved without Atlas, at what time, and what the specialists were saying.

Forward returns are the signed fractions the resolver stores, ``(mark - reference) /
reference``: 0.018 is an up-move of 1.8 percent. The threshold is in the same unit. The
JSON schema is fixed (``pramana.missed_opportunities.v1``) because the dashboard renders it
directly; Decimals become JSON numbers rounded to six places, undefined values are null.
Paper-only: nothing here is a claim that a trade would have been taken or filled.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.analytics.decision_journal import (
    HORIZON_COLUMNS,
    aware,
    load_rows,
    parse_decimal,
)
from quant_ai.analytics.decision_quality import (
    IST,
    decided_at_of,
    number,
    session_date_of,
)
from quant_ai.analytics.decision_quality import write_report as _write_json

SCHEMA = "pramana.missed_opportunities.v1"
DEFAULT_THRESHOLD = Decimal("0.01")
DEFAULT_HORIZON = "forward_return_60m"
HORIZON_LABELS = {
    "forward_return_10m": "10-minute",
    "forward_return_30m": "30-minute",
    "forward_return_60m": "60-minute",
    "forward_return_close": "session-close",
}
# Lines in the end-of-session note and the terminal verdict. Twelve names hold at most a
# dozen missed moves a day; five is what a phone screen shows without scrolling.
TOP_LINES = 5
NEUTRAL = "NEUTRAL"


# ----------------------------------------------------------------- primitives


def is_hold(row: dict[str, Any]) -> bool:
    """A hold is a decision that produced no order side: stance NEUTRAL, nothing to fill."""
    return row.get("side") is None


def agents_of_row(row: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The specialists' stances and confidences journaled with the decision; {} when unreadable."""
    try:
        parsed = json.loads(row.get("agents") or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(agent): vote for agent, vote in parsed.items() if isinstance(vote, dict)}


def stances_summary(agents: dict[str, dict[str, str]]) -> str:
    """One clause on what the specialists said: ``specialists neutral`` or the dissenters by name."""
    if not agents:
        return "no specialist votes"
    dissent = sorted(
        (agent, str(vote.get("stance", "?")))
        for agent, vote in agents.items()
        if str(vote.get("stance", NEUTRAL)) != NEUTRAL
    )
    if not dissent:
        return "specialists neutral"
    clause = ", ".join(f"{agent} {stance}" for agent, stance in dissent)
    quiet = len(agents) - len(dissent)
    return f"{clause}, {quiet} neutral" if quiet else clause


def signed_percent(fraction: Any) -> str:
    """A stored fraction as ``+1.80%``; the dash for nothing."""
    value = parse_decimal(fraction)
    return f"{value * 100:+.2f}%" if value is not None else "-"


def percent(fraction: Any) -> str:
    value = parse_decimal(fraction)
    return f"{value * 100:.2f}%" if value is not None else "-"


def ist_clock(iso_timestamp: Any) -> str:
    """``HH:MM`` in IST from a stored ISO timestamp; ``--:--`` when unreadable."""
    try:
        return aware(datetime.fromisoformat(str(iso_timestamp))).astimezone(IST).strftime("%H:%M")
    except (ValueError, TypeError):
        return "--:--"


def session_bounds(session_date: str) -> tuple[datetime, datetime]:
    """The IST calendar day as a half-open UTC interval ``[start, end)``."""
    day = date.fromisoformat(session_date)
    start = datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


# ----------------------------------------------------------------- report


def _best_block(row: dict[str, Any], forward: Decimal) -> dict[str, Any]:
    moment = decided_at_of(row)
    return {
        "decision_id": row.get("decision_id"),
        "decided_at": moment.astimezone(IST).isoformat() if moment else None,
        "reference_price": number(parse_decimal(row.get("reference_price"))),
        "forward_return": number(forward),
        "regime": row.get("regime"),
        "mode": row.get("mode"),
        "reason": row.get("reason"),
        "agents": agents_of_row(row),
    }


def by_symbol(
    holds: list[dict[str, Any]], *, threshold: Decimal, horizon: str
) -> list[dict[str, Any]]:
    """Per-symbol counts and the largest missed move, symbols with a missed move first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in holds:
        groups.setdefault(str(row.get("symbol")), []).append(row)
    result = []
    for symbol, rows in groups.items():
        missed = avoided = evaluated = 0
        best: tuple[Decimal, dict[str, Any]] | None = None
        for row in rows:
            forward = parse_decimal(row.get(horizon))
            if forward is None:
                continue
            evaluated += 1
            if forward >= threshold:
                missed += 1
                if best is None or forward > best[0]:
                    best = (forward, row)
            elif forward <= -threshold:
                avoided += 1
        result.append(
            {
                "symbol": symbol,
                "missed": missed,
                "avoided": avoided,
                "evaluated": evaluated,
                "best": _best_block(best[1], best[0]) if best else None,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            0 if item["best"] else 1,
            -Decimal(str(item["best"]["forward_return"])) if item["best"] else Decimal(0),
            item["symbol"],
        ),
    )


def limitations(*, threshold: Decimal, horizon: str) -> list[str]:
    label = HORIZON_LABELS.get(horizon, horizon)
    return [
        (
            f"A missed move is a hold whose {label} forward return reached "
            f"{percent(threshold)}, measured on the feed's last traded price at the first "
            "cadence at or after the horizon; it ignores costs, slippage and whether size "
            "was available."
        ),
        (
            "It is evidence about what the swarm saw and passed on, not a claim that the "
            "trade would have been proposed, approved by the gates or filled at that price."
        ),
        (
            "An avoided move is the mirror case, a hold ahead of a down-move of the same "
            "size; holds between the two thresholds are neither, and a hold whose horizon "
            "is still null is unresolved and scores nothing."
        ),
        (
            "Paper decisions on live marks; no order was placed and no live fill is implied."
        ),
    ]


def summarize(
    rows: list[dict[str, Any]],
    *,
    tenant_id: str,
    now: datetime,
    session_date: str,
    threshold: Decimal = DEFAULT_THRESHOLD,
    horizon: str = DEFAULT_HORIZON,
) -> dict[str, Any]:
    """The full report for an already loaded set of the session's rows."""
    holds = [row for row in rows if is_hold(row)]
    symbols = by_symbol(holds, threshold=threshold, horizon=horizon)
    evaluated = sum(item["evaluated"] for item in symbols)
    return {
        "schema": SCHEMA,
        "generated_at": aware(now).isoformat(),
        "tenant_id": tenant_id,
        "session_date": session_date,
        "threshold": number(threshold),
        "horizon": horizon,
        "decisions": len(rows),
        "holds": len(holds),
        "evaluated": evaluated,
        "missed": sum(item["missed"] for item in symbols),
        "avoided": sum(item["avoided"] for item in symbols),
        "unresolved": len(holds) - evaluated,
        "symbols": symbols,
        "limitations": limitations(threshold=threshold, horizon=horizon),
    }


def build_report(
    broker,
    *,
    tenant_id: str,
    now: datetime,
    session_date: str | None = None,
    threshold: Decimal = DEFAULT_THRESHOLD,
    horizon: str = DEFAULT_HORIZON,
) -> dict[str, Any]:
    """Missed-opportunity report for one IST session date (default: the IST date of ``now``)."""
    if horizon not in HORIZON_COLUMNS:
        raise ValueError(f"not a forward-return horizon: {horizon}")
    if not isinstance(threshold, Decimal) or not threshold.is_finite() or threshold <= 0:
        raise ValueError("threshold must be a positive Decimal fraction")
    current = aware(now)
    session_date = session_date or current.astimezone(IST).date().isoformat()
    since, until = session_bounds(session_date)
    # The query bounds are the day in UTC; the filter is the row's own IST date, so a row
    # written with a different offset representation still lands on the right session.
    rows = [
        row
        for row in load_rows(broker, tenant_id=tenant_id, since=since, until=until)
        if session_date_of(row) == session_date
    ]
    return summarize(
        rows, tenant_id=tenant_id, now=current, session_date=session_date,
        threshold=threshold, horizon=horizon,
    )


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    """Atomically replace ``path`` with the report, creating its directory."""
    return _write_json(path, report)


# ----------------------------------------------------------------- text


def best_detail(best: dict[str, Any]) -> str:
    """``+1.80% at 11:20 IST, BULL_TRENDING, specialists neutral``."""
    return (
        f"{signed_percent(best['forward_return'])} at {ist_clock(best['decided_at'])} IST, "
        f"{best.get('regime') or 'regime unknown'}, {stances_summary(best.get('agents') or {})}"
    )


def move_line(item: dict[str, Any]) -> str:
    """``RELIANCE +1.80% at 11:20 IST, BULL_TRENDING, specialists neutral``."""
    return f"{item['symbol']} {best_detail(item['best'])}"


def summary_lines(report: dict[str, Any], limit: int = TOP_LINES) -> list[str]:
    """The largest missed moves, one line each, largest first."""
    return [move_line(item) for item in report["symbols"] if item["best"]][:limit]


def verdict(report: dict[str, Any]) -> str:
    return (
        f"{report['evaluated']} holds evaluated, {report['missed']} missed moves above "
        f"{percent(report['threshold'])}"
    )


def notification_message(report: dict[str, Any], limit: int = TOP_LINES) -> str:
    """The end-of-session note: date, counts and the top missed lines."""
    label = HORIZON_LABELS.get(report["horizon"], report["horizon"])
    missed = report["missed"]
    head = (
        f"Missed moves {report['session_date']}: {report['evaluated']} holds evaluated, "
        f"{missed if missed else 'none'} above {percent(report['threshold'])} on the {label} "
        f"horizon"
    )
    if report["unresolved"]:
        head += f" ({report['unresolved']} unresolved)"
    return "\n".join([head + ".", *summary_lines(report, limit)])


def render(report: dict[str, Any]) -> str:
    """Plain-text table for the terminal."""
    lines = [
        (
            f"Missed opportunities {report['session_date']} tenant={report['tenant_id']} "
            f"threshold={percent(report['threshold'])} horizon={report['horizon']}"
        ),
        (
            f"decisions={report['decisions']} holds={report['holds']} "
            f"evaluated={report['evaluated']} missed={report['missed']} "
            f"avoided={report['avoided']} unresolved={report['unresolved']}"
        ),
    ]
    symbols = report["symbols"]
    if symbols:
        width = max(len("SYMBOL"), *(len(item["symbol"]) for item in symbols))
        lines.append(f"{'SYMBOL':<{width}}  MISSED  AVOIDED  EVALUATED  BEST MISSED MOVE")
        for item in symbols:
            detail = best_detail(item["best"]) if item["best"] else "-"
            lines.append(
                f"{item['symbol']:<{width}}  {item['missed']:>6}  {item['avoided']:>7}  "
                f"{item['evaluated']:>9}  {detail}"
            )
    else:
        lines.append("no holds journaled for this session")
    lines.append(verdict(report))
    return "\n".join(lines)
