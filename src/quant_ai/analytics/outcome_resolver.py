"""Fill in what happened after each journaled decision.

Forward returns are marked from the live feed at the first cadence at or after each horizon
elapses; a horizon whose cadence was missed is left null rather than back-filled with a
later mark, and a row that has waited three later sessions is finalised with whatever is
still null. Filled entries take their realised outcome from the ledger's flat-to-flat
episodes once the position closes, so a decision's PnL is the paper account's PnL after
fees, attributed pro rata by entry notional so nothing is counted twice.

Paper-only, Decimal money, tz-aware time. The daemon wraps the call: a failure here is
logged and the cadence continues.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Any, Callable
from zoneinfo import ZoneInfo

from quant_ai.analytics.decision_journal import (
    GOVERNANCE_FILLED,
    HORIZON_COLUMNS,
    TABLE,
    aware,
    ensure_journal,
    load_rows,
    update_decision,
)
from quant_ai.domain.models import Market
from quant_ai.execution.protection_state import positive_level
from quant_ai.execution.session import SESSIONS, GlobalVenue, MarketCalendar, MarketState

LOGGER = logging.getLogger("quant_ai.outcome_resolver")

MarkResolver = Callable[[str], Decimal | None]

MINUTE_HORIZONS = (
    (10, "forward_return_10m"),
    (30, "forward_return_30m"),
    (60, "forward_return_60m"),
)

# A horizon is marked at the first cadence at or after it elapses. Anything later than half
# a cadence is a missed cadence, and a later mark is not that horizon's mark.
DEFAULT_MAX_LAG = timedelta(minutes=5)
DEFAULT_EXPIRY_SESSIONS = 3
# Filled entries are checked for closure this long; older open positions are the ledger's
# problem, not the journal's.
TRADE_LOOKBACK = timedelta(days=90)

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_SWARM = "SWARM"
EXIT_OPERATOR = "operator"


def resolve_outcomes(
    broker,
    *,
    tenant_id: str,
    now: datetime,
    mark_for: MarkResolver,
    calendar: MarketCalendar,
    market: Market | None = None,
    max_lag: timedelta = DEFAULT_MAX_LAG,
    expiry_sessions: int = DEFAULT_EXPIRY_SESSIONS,
    trade_evidence: dict | None = None,
) -> dict[str, int]:
    """Resolve pending forward returns and closed-trade outcomes. Returns counters.

    ``mark_for(symbol)`` returns the current mark or None; None skips the row until the next
    cadence. ``market`` is the fallback venue for rows whose own market is unusable.
    ``trade_evidence`` is an already computed ``build_trade_evidence`` report; when omitted
    it is computed from the ledger only if a filled entry is waiting for its outcome.
    """
    current = aware(now)
    ensure_journal(broker)
    summary = {"horizons": 0, "close": 0, "finalized": 0, "expired": 0, "skipped": 0, "trades": 0}
    marks: dict[str, Decimal | None] = {}

    def mark(symbol: str) -> Decimal | None:
        if symbol not in marks:
            try:
                marks[symbol] = positive_level(mark_for(symbol))
            except (ValueError, RuntimeError, TimeoutError, ConnectionError, OSError,
                    DecimalException, AttributeError, TypeError) as error:
                LOGGER.warning("decision_mark_unavailable symbol=%s error=%s", symbol, error)
                marks[symbol] = None
        return marks[symbol]

    pending = load_rows(broker, tenant_id=tenant_id, where="resolved_at IS NULL")
    for row in pending:
        updates = _resolve_forward_returns(
            row, current, mark, calendar, market, max_lag, expiry_sessions, summary
        )
        if updates:
            update_decision(broker, row["decision_id"], **updates)

    filled = load_rows(
        broker,
        tenant_id=tenant_id,
        since=current - TRADE_LOOKBACK,
        where=(
            "governance = ? AND side = 'BUY' AND order_id IS NOT NULL "
            "AND exit_at IS NULL AND realized_net_pnl IS NULL"
        ),
        parameters=(GOVERNANCE_FILLED,),
    )
    if filled:
        summary["trades"] = _resolve_trade_outcomes(broker, tenant_id, filled, trade_evidence)
    return summary


# ----------------------------------------------------------------- forward returns


def _resolve_forward_returns(
    row: dict[str, Any],
    now: datetime,
    mark: Callable[[str], Decimal | None],
    calendar: MarketCalendar,
    fallback_market: Market | None,
    max_lag: timedelta,
    expiry_sessions: int,
    summary: dict[str, int],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    try:
        decided_at = aware(datetime.fromisoformat(row["decided_at"]))
    except (ValueError, TypeError):
        summary["finalized"] += 1
        return {"resolved_at": now.isoformat()}
    row_market = _market_of(row, fallback_market)
    reference = positive_level(row["reference_price"])
    if row_market is None or reference is None:
        # Nothing can be measured against a missing venue or price. Close the row out.
        summary["finalized"] += 1
        return {"resolved_at": now.isoformat()}

    for minutes, column in MINUTE_HORIZONS:
        if row[column] is not None:
            continue
        due = decided_at + timedelta(minutes=minutes)
        if now < due or now - due > max_lag:
            continue
        current_mark = mark(row["symbol"])
        if current_mark is None:
            summary["skipped"] += 1
            continue
        updates[column] = str((current_mark - reference) / reference)
        summary["horizons"] += 1

    if row["forward_return_close"] is None and _close_due(calendar, row_market, decided_at, now):
        current_mark = mark(row["symbol"])
        if current_mark is None:
            summary["skipped"] += 1
        else:
            updates["forward_return_close"] = str((current_mark - reference) / reference)
            summary["close"] += 1

    merged = {**row, **updates}
    if all(merged[column] is not None for column in HORIZON_COLUMNS):
        updates["resolved_at"] = now.isoformat()
        summary["finalized"] += 1
    elif sessions_opened_since(calendar, row_market, decided_at, now) >= expiry_sessions:
        # Too old to mark honestly: close the row with whatever is still null.
        updates["resolved_at"] = now.isoformat()
        summary["expired"] += 1
    return updates


def _market_of(row: dict[str, Any], fallback: Market | None) -> Market | None:
    try:
        market = Market(str(row["market"]))
    except (ValueError, TypeError):
        market = fallback
    if market is None or market == Market.GLOBAL:
        return fallback if fallback is not None and fallback != Market.GLOBAL else None
    return market


def _session(market: Market):
    return SESSIONS[GlobalVenue(market.value)]


def _close_due(calendar: MarketCalendar, market: Market, decided_at: datetime, now: datetime) -> bool:
    """True once the decision's own session is over and a mark can still honestly stand for it.

    The ordinary case is the first cadence after the venue leaves REGULAR_HOURS on the
    decision's own day. If the daemon was down for that cadence, the next session's first
    cadence is the last chance; after that the close return stays null rather than being
    marked from a price days removed from the session it is meant to describe.
    """
    if now <= decided_at:
        return False
    opened = sessions_opened_since(calendar, market, decided_at, now)
    if opened == 0:
        return calendar.state(market, now) != MarketState.REGULAR_HOURS
    return opened == 1


def sessions_opened_since(
    calendar: MarketCalendar, market: Market, decided_at: datetime, now: datetime, *, limit_days: int = 60
) -> int:
    """Number of the venue's trading sessions that opened after the decision's own session."""
    session = _session(market)
    zone = ZoneInfo(session.timezone)
    start = decided_at.astimezone(zone).date()
    end = now.astimezone(zone).date()
    count = 0
    day = start + timedelta(days=1)
    while day <= end and (day - start).days <= limit_days:
        opens_at = datetime.combine(day, session.regular_open, tzinfo=zone)
        if opens_at <= now and calendar.state(market, opens_at) == MarketState.REGULAR_HOURS:
            count += 1
        day += timedelta(days=1)
    return count


# ----------------------------------------------------------------- trade outcomes


def _resolve_trade_outcomes(
    broker, tenant_id: str, rows: list[dict[str, Any]], trade_evidence: dict | None
) -> int:
    if trade_evidence is None:
        from quant_ai.validation.trade_evidence import build_trade_evidence

        with broker._lock:
            trade_evidence = build_trade_evidence(broker._connection, tenant_id)
    if not isinstance(trade_evidence, dict) or trade_evidence.get("status") != "ok":
        reason = trade_evidence.get("reason") if isinstance(trade_evidence, dict) else "unavailable"
        LOGGER.info("decision_trade_outcomes_deferred reason=%s", reason)
        return 0
    episodes_by_order: dict[str, dict] = {}
    for episode in trade_evidence.get("episodes", ()):
        for order_id in episode.get("orderIds", ()):
            episodes_by_order[str(order_id)] = episode
    resolved = 0
    for row in rows:
        episode = episodes_by_order.get(str(row["order_id"]))
        if episode is None:
            continue  # still open, or an order the ledger does not know
        try:
            outcome = _episode_outcome(broker, tenant_id, row, episode)
        except (ValueError, TypeError, KeyError, DecimalException, sqlite3.Error) as error:
            LOGGER.warning(
                "decision_trade_outcome_unresolvable decision_id=%s error=%s",
                row["decision_id"], type(error).__name__,
            )
            continue
        if outcome and update_decision(broker, row["decision_id"], **outcome):
            resolved += 1
    return resolved


def _episode_outcome(broker, tenant_id: str, row: dict[str, Any], episode: dict) -> dict[str, Any]:
    order_ids = [str(item) for item in episode["orderIds"]]
    placeholders = ", ".join("?" for _ in order_ids)
    with broker._lock:
        fills = broker._connection.execute(
            f"""SELECT order_id, side, notional, created_at FROM paper_ledger
            WHERE tenant_id = ? AND order_id IN ({placeholders})""",
            (tenant_id, *order_ids),
        ).fetchall()
        closing_id = order_ids[-1]
        protective = broker._connection.execute(
            "SELECT payload FROM paper_protection_evidence WHERE order_id = ? AND tenant_id = ?",
            (closing_id, tenant_id),
        ).fetchone()
        swarm = broker._connection.execute(
            "SELECT 1 FROM paper_decision_evidence WHERE order_id = ? AND tenant_id = ?",
            (closing_id, tenant_id),
        ).fetchone()
    by_order = {str(fill["order_id"]): fill for fill in fills}
    entry = by_order.get(str(row["order_id"]))
    if entry is None or entry["side"] != "BUY":
        return {}
    entry_notional = Decimal(str(entry["notional"]))
    total_entry = sum(
        (Decimal(str(fill["notional"])) for fill in fills if fill["side"] == "BUY"), Decimal(0)
    )
    if entry_notional <= 0 or total_entry <= 0:
        return {}
    share = entry_notional / total_entry
    gross = Decimal(str(episode["grossPnl"])) * share
    fees = Decimal(str(episode["cashFees"])) * share
    net = Decimal(str(episode["netPnl"])) * share
    closed_at = aware(datetime.fromisoformat(str(episode["closedAt"])))
    opened_at = aware(datetime.fromisoformat(str(entry["created_at"])))
    holding = max(0, int((closed_at - opened_at).total_seconds() // 60))
    return {
        "realized_net_pnl": str(net),
        "realized_gross_pnl": str(gross),
        "realized_fees": str(fees),
        "exit_trigger": _exit_trigger(protective, swarm is not None),
        "exit_at": closed_at.isoformat(),
        "holding_minutes": holding,
    }


def _exit_trigger(protective_row, swarm_sell: bool) -> str:
    if protective_row is not None:
        try:
            trigger = json.loads(protective_row["payload"]).get("trigger")
        except (ValueError, TypeError, AttributeError):
            trigger = None
        if trigger in (EXIT_STOP_LOSS, EXIT_TAKE_PROFIT):
            return trigger
        return EXIT_OPERATOR
    return EXIT_SWARM if swarm_sell else EXIT_OPERATOR


def pending_counts(broker, *, tenant_id: str) -> dict[str, int]:
    """How many rows still wait for forward returns or trade outcomes (for diagnostics)."""
    ensure_journal(broker)
    with broker._lock:
        forward = broker._connection.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE tenant_id = ? AND resolved_at IS NULL",
            (tenant_id,),
        ).fetchone()[0]
        trades = broker._connection.execute(
            f"""SELECT COUNT(*) FROM {TABLE} WHERE tenant_id = ? AND governance = ?
            AND side = 'BUY' AND exit_at IS NULL""",
            (tenant_id, GOVERNANCE_FILLED),
        ).fetchone()[0]
    return {"forward_returns": int(forward), "trade_outcomes": int(trades)}
