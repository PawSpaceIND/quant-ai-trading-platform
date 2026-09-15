"""Calibration and quality report over the decision journal.

Everything here is arithmetic over journal rows: hit rates on the 60-minute horizon,
confidence calibration, closed-trade statistics and the same numbers cut by regime, hour,
specialist and evidence mode. The JSON schema is fixed (``pramana.decision_quality.v1``)
because the dashboard renders it directly. Numbers are computed in Decimal and rendered
as JSON numbers rounded to six places; undefined values are null, never zero.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_ai.analytics.decision_journal import (
    GOVERNANCE_ABSTAINED,
    GOVERNANCE_FILLED,
    GOVERNANCE_REJECTED,
    aware,
    load_rows,
    parse_decimal,
)
from quant_ai.analytics.metrics import (
    MINIMUM_SIGNIFICANCE_OBSERVATIONS,
    mean_return_significance,
)

SCHEMA = "pramana.decision_quality.v1"
HORIZON_MINUTES = 60
HORIZON_COLUMN = "forward_return_60m"
IST = ZoneInfo("Asia/Kolkata")
UP = frozenset({"BUY", "STRONG_BUY"})
DOWN = frozenset({"SELL", "STRONG_SELL"})
SESSION_HOURS_IST = tuple(range(9, 16))
RECENT_LIMIT = 50
CALIBRATION_BINS = 10
DEFAULT_SINCE_DAYS = 30
DEFAULT_MINIMUM_SAMPLE = 20
UNKNOWN = "unknown"


# ----------------------------------------------------------------- primitives


def number(value: Decimal | int | None) -> float | None:
    """Render a Decimal for JSON: six places, null when undefined."""
    if value is None:
        return None
    return round(float(str(value)), 6)


def ratio(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


def mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def direction(stance: Any) -> int:
    if stance in UP:
        return 1
    if stance in DOWN:
        return -1
    return 0


def directional_hit(row: dict[str, Any]) -> tuple[bool, Decimal] | None:
    """(hit, forward return) on the 60-minute horizon, or None when not evaluable.

    A hit is the forward return's sign matching the stance direction; a zero return is
    a miss. NEUTRAL and AVOID rows are never evaluated.
    """
    wanted = direction(row.get("stance"))
    if wanted == 0:
        return None
    forward = parse_decimal(row.get(HORIZON_COLUMN))
    if forward is None:
        return None
    observed = 1 if forward > 0 else -1 if forward < 0 else 0
    return observed == wanted, forward


def evaluated(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], bool, Decimal]]:
    result = []
    for row in rows:
        outcome = directional_hit(row)
        if outcome is not None:
            result.append((row, outcome[0], outcome[1]))
    return result


def decided_at_of(row: dict[str, Any]) -> datetime | None:
    try:
        return aware(datetime.fromisoformat(str(row["decided_at"])))
    except (ValueError, TypeError, KeyError):
        return None


def session_date_of(row: dict[str, Any]) -> str | None:
    """IST calendar date of the decision, the unit the burn-in counts sessions in."""
    moment = decided_at_of(row)
    return moment.astimezone(IST).date().isoformat() if moment else None


# ----------------------------------------------------------------- sections


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    governance = Counter(row.get("governance") for row in rows)
    return {
        "decisions": len(rows),
        "filled": governance.get(GOVERNANCE_FILLED, 0),
        "rejected": governance.get(GOVERNANCE_REJECTED, 0),
        "abstained": governance.get(GOVERNANCE_ABSTAINED, 0),
        "resolved_60m": sum(parse_decimal(row.get(HORIZON_COLUMN)) is not None for row in rows),
        "closed_trades": sum(parse_decimal(row.get("realized_net_pnl")) is not None for row in rows),
    }


def rejections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tally = Counter(
        str(row.get("reason") or UNKNOWN)
        for row in rows
        if row.get("governance") == GOVERNANCE_REJECTED
    )
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    ]


def directional(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = evaluated(rows)
    hits = sum(1 for _, hit, _ in scored if hit)
    return {
        "horizon_minutes": HORIZON_MINUTES,
        "evaluated": len(scored),
        "hit_rate": number(ratio(hits, len(scored))),
        "mean_forward_return": number(mean([forward for _, _, forward in scored])),
    }


def closed_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if parse_decimal(row.get("realized_net_pnl")) is not None]


def closed_pnls(rows: list[dict[str, Any]]) -> list[Decimal]:
    """Realised net P&L of every closed entry decision, in Decimal, oldest first."""
    return [parse_decimal(row["realized_net_pnl"]) for row in closed_trades(rows)]


def expectancy_of(pnls: list[Decimal]) -> Decimal | None:
    """Mean realised net P&L per closed trade; None when nothing closed."""
    return mean(pnls)


def profit_factor_of(pnls: list[Decimal]) -> Decimal | None:
    """Gross wins over gross losses; None when no losing trade defines the denominator."""
    gross_wins = sum((pnl for pnl in pnls if pnl > 0), Decimal(0))
    gross_losses = sum((-pnl for pnl in pnls if pnl < 0), Decimal(0))
    return gross_wins / gross_losses if gross_losses > 0 else None


def profitable_regime_count(rows: list[dict[str, Any]]) -> int:
    """Named regimes whose closed trades netted a profit. ``unknown`` never counts."""
    totals: dict[str, Decimal] = {}
    for row in closed_trades(rows):
        regime = str(row.get("regime") or UNKNOWN)
        if regime == UNKNOWN:
            continue
        totals[regime] = totals.get(regime, Decimal(0)) + parse_decimal(row["realized_net_pnl"])
    return sum(1 for total in totals.values() if total > 0)


def session_dates(rows: list[dict[str, Any]]) -> set[str]:
    """Distinct IST calendar dates the journal has decisions for."""
    return {date for row in rows if (date := session_date_of(row))}


def trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = closed_trades(rows)
    pnls = [parse_decimal(row["realized_net_pnl"]) for row in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [-pnl for pnl in pnls if pnl < 0]
    holding = [
        Decimal(int(row["holding_minutes"]))
        for row in closed
        if isinstance(row.get("holding_minutes"), int)
    ]
    gross = [parse_decimal(row.get("realized_gross_pnl")) for row in closed]
    fees = [parse_decimal(row.get("realized_fees")) for row in closed]
    exits = Counter(str(row.get("exit_trigger") or UNKNOWN) for row in closed)
    return {
        "closed": len(closed),
        "win_rate": number(ratio(len(wins), len(pnls))),
        "expectancy": number(expectancy_of(pnls)),
        "profit_factor": number(profit_factor_of(pnls)),
        "average_win": number(mean(wins)),
        # Magnitude of the mean losing trade, as the trade-evidence summary reports it.
        "average_loss": number(mean(losses)),
        "average_holding_minutes": number(mean(holding)),
        "net_pnl": number(sum(pnls, Decimal(0))),
        "gross_pnl": number(sum((item for item in gross if item is not None), Decimal(0))),
        "fees": number(sum((item for item in fees if item is not None), Decimal(0))),
        "exits": [
            {"trigger": trigger, "count": count}
            for trigger, count in sorted(exits.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def significance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One-sample t-statistics for the mean forward return and the mean closed-trade P&L.

    A hit rate above one half and a positive expectancy are descriptions of a sample. The
    t-statistic is the first question a reader should ask of them: how far is this mean
    from zero in units of its own standard error. Both are null when the sample is too
    small or has no dispersion, and neither is corrected for multiple testing - see
    ``multiple_testing_correction``.
    """
    forward = tuple(value for _, _, value in evaluated(rows))
    pnls = tuple(closed_pnls(rows))
    return {
        "minimum_observations": MINIMUM_SIGNIFICANCE_OBSERVATIONS,
        "multiple_testing_correction": "none",
        "forward_return_60m": _t_block(forward),
        "trade_net_pnl": _t_block(pnls),
    }


def _t_block(values: tuple[Decimal, ...]) -> dict[str, Any]:
    result = mean_return_significance(values)
    if result is None:
        return {
            "observations": len(values),
            "mean": None,
            "standard_error": None,
            "t_statistic": None,
        }
    return {
        "observations": result.observations,
        "mean": number(result.mean),
        "standard_error": number(result.standard_error),
        "t_statistic": number(result.t_statistic),
    }


def calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = []
    for row, hit, _ in evaluated(rows):
        confidence = parse_decimal(row.get("confidence"))
        if confidence is None:
            continue
        scored.append((max(Decimal(0), min(Decimal(1), confidence)), Decimal(1 if hit else 0)))
    brier = mean([(confidence - outcome) ** 2 for confidence, outcome in scored])
    bins = []
    for index in range(CALIBRATION_BINS):
        lower = Decimal(index) / CALIBRATION_BINS
        upper = Decimal(index + 1) / CALIBRATION_BINS
        last = index == CALIBRATION_BINS - 1
        members = [
            (confidence, outcome)
            for confidence, outcome in scored
            if lower <= confidence < upper or (last and confidence == upper)
        ]
        bins.append(
            {
                "lower": number(lower),
                "upper": number(upper),
                "decisions": len(members),
                "hit_rate": number(mean([outcome for _, outcome in members])),
                "mean_confidence": number(mean([confidence for confidence, _ in members])),
            }
        )
    return {"brier_score": number(brier), "bins": bins}


def _group_metrics(group: list[dict[str, Any]]) -> tuple[float | None, float]:
    scored = evaluated(group)
    hit_rate = ratio(sum(1 for _, hit, _ in scored if hit), len(scored))
    pnl = sum(
        (parse_decimal(row["realized_net_pnl"]) for row in closed_trades(group)), Decimal(0)
    )
    return number(hit_rate), number(pnl)


def by_regime(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("regime") or UNKNOWN), []).append(row)
    result = []
    for regime, group in groups.items():
        hit_rate, pnl = _group_metrics(group)
        result.append(
            {
                "regime": regime,
                "decisions": len(group),
                "filled": sum(row.get("governance") == GOVERNANCE_FILLED for row in group),
                "hit_rate": hit_rate,
                "net_pnl": pnl,
            }
        )
    return sorted(result, key=lambda item: (-item["decisions"], item["regime"]))


def by_hour_ist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = {hour: [] for hour in SESSION_HOURS_IST}
    for row in rows:
        moment = decided_at_of(row)
        if moment is None:
            continue
        groups.setdefault(moment.astimezone(IST).hour, []).append(row)
    result = []
    for hour in sorted(groups):
        hit_rate, pnl = _group_metrics(groups[hour])
        result.append(
            {"hour": hour, "decisions": len(groups[hour]), "hit_rate": hit_rate, "net_pnl": pnl}
        )
    return result


def by_agent(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tallies: dict[str, list[int]] = {}
    for row in rows:
        forward = parse_decimal(row.get(HORIZON_COLUMN))
        try:
            agents = json.loads(row.get("agents") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(agents, dict):
            continue
        for agent_id, vote in agents.items():
            tally = tallies.setdefault(str(agent_id), [0, 0])
            if forward is None or not isinstance(vote, dict):
                continue
            wanted = direction(vote.get("stance"))
            if wanted == 0:
                continue
            observed = 1 if forward > 0 else -1 if forward < 0 else 0
            tally[0] += 1
            tally[1] += int(observed == wanted)
    return [
        {
            "agent_id": agent_id,
            "evaluated": evaluated_count,
            "directional_accuracy": number(ratio(hits, evaluated_count)),
        }
        for agent_id, (evaluated_count, hits) in sorted(tallies.items())
    ]


def by_mode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tally = Counter(str(row.get("mode") or UNKNOWN) for row in rows)
    return [
        {"mode": mode, "decisions": count}
        for mode, count in sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    ]


def recent(rows: list[dict[str, Any]], limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    newest = sorted(rows, key=lambda row: (str(row.get("decided_at")), str(row.get("decision_id"))))
    return [
        {
            "decision_id": row.get("decision_id"),
            "decided_at": row.get("decided_at"),
            "symbol": row.get("symbol"),
            "stance": row.get("stance"),
            "confidence": number(parse_decimal(row.get("confidence"))),
            "regime": row.get("regime"),
            "mode": row.get("mode"),
            "governance": row.get("governance"),
            "reason": row.get("reason"),
            "order_id": row.get("order_id"),
            "forward_return_60m": number(parse_decimal(row.get(HORIZON_COLUMN))),
            "net_pnl": number(parse_decimal(row.get("realized_net_pnl"))),
            "exit_trigger": row.get("exit_trigger"),
        }
        for row in reversed(newest[-limit:])
    ]


def limitations(*, insufficient_sample: bool, minimum_sample: int) -> list[str]:
    notes = [
        (
            "Paper fills against live marks with modelled friction; no live orders were "
            "placed and no live fill price is implied."
        ),
        (
            "Forward returns are marked at the first cadence at or after each horizon "
            "elapses; a horizon whose cadence was missed stays null, never back-filled."
        ),
        (
            "A directional hit is the sign of the 60-minute forward return matching the "
            "stance; a zero return is a miss, and size, fees and slippage are ignored."
        ),
        (
            "Trade statistics cover closed entry decisions only; open positions and fills "
            "that were not journaled are excluded, and net PnL is after recorded cash fees."
        ),
        (
            "Calibration compares stated confidence with 60-minute direction, not with "
            "realised trade PnL; per-agent accuracy scores each specialist vote in isolation."
        ),
        (
            "Decisions inside a session are serially correlated; counts and rates do not "
            "establish a repeatable edge."
        ),
        (
            "The t-statistics test each mean against zero on the observations shown and "
            "are not corrected for multiple testing: they do not account for how many "
            "candidate strategies, windows or parameter settings were tried, and serial "
            "correlation inflates them further."
        ),
    ]
    if insufficient_sample:
        notes.append(
            f"Fewer than {minimum_sample} evaluated directional decisions: every rate in "
            "this report is dominated by sampling noise."
        )
    return notes


def summarize(
    rows: list[dict[str, Any]],
    *,
    tenant_id: str,
    now: datetime,
    since: datetime,
    minimum_sample: int = DEFAULT_MINIMUM_SAMPLE,
) -> dict[str, Any]:
    """The full report for an already loaded set of rows."""
    directional_section = directional(rows)
    insufficient = directional_section["evaluated"] < minimum_sample
    return {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "generated_at": aware(now).isoformat(),
        "window": {
            "since": aware(since).isoformat(),
            "until": aware(now).isoformat(),
            "sessions": len({date for row in rows if (date := session_date_of(row))}),
        },
        "minimum_sample": minimum_sample,
        "insufficient_sample": insufficient,
        "counts": counts(rows),
        "rejections": rejections(rows),
        "directional": directional_section,
        "trades": trades(rows),
        "significance": significance(rows),
        "calibration": calibration(rows),
        "by_regime": by_regime(rows),
        "by_hour_ist": by_hour_ist(rows),
        "by_agent": by_agent(rows),
        "by_mode": by_mode(rows),
        "recent": recent(rows),
        "limitations": limitations(insufficient_sample=insufficient, minimum_sample=minimum_sample),
    }


def build_report(
    broker,
    *,
    tenant_id: str,
    now: datetime,
    since_days: int = DEFAULT_SINCE_DAYS,
    minimum_sample: int = DEFAULT_MINIMUM_SAMPLE,
) -> dict[str, Any]:
    """Decision-quality report for the last ``since_days`` days of the tenant's journal."""
    if since_days < 1:
        raise ValueError("since_days must be at least one")
    if minimum_sample < 1:
        raise ValueError("minimum_sample must be at least one")
    current = aware(now)
    since = current - timedelta(days=since_days)
    rows = load_rows(broker, tenant_id=tenant_id, since=since, until=current)
    return summarize(rows, tenant_id=tenant_id, now=current, since=since, minimum_sample=minimum_sample)


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    """Atomically replace ``path`` with the report: temp file in the same directory, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, allow_nan=False)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target
