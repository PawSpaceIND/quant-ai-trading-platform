"""Scheduled corporate actions, and the guard for the ones nobody declared.

A split, bonus or large special dividend cuts the quoted price without changing what the
position is worth. The stored stop and average price refer to the pre-adjustment basis, so
for one session the two are not comparable. Left alone, the next protection sweep reads the
new price as a stop breach and liquidates the whole position against the old cost basis,
booking a fabricated loss large enough to trip the drawdown breaker and halt the pilot.

This module does not adjust the ledger. It makes the engine *stop* instead of acting on a
price it cannot interpret, which is the same discipline the exit engine already applies to
an unknown mark: refuse, tell the operator, and wait for a human. Adjusting a held position
is a ledger change and a separate decision.

Two layers, because the dangerous case is the action nobody recorded:

``CorporateActionCalendar``
    Operator-declared ex-dates, loaded from JSON. Authoritative when present.

``price_discontinuity``
    A safety net that needs no calendar. A quoted price that moves further in one step than
    the exchange's own band allows is not a market move; it is a re-based quote.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("quant_ai.corporate_actions")

IST = ZoneInfo("Asia/Kolkata")

# NSE price bands top out at 20% for most scrips, so a larger single-step move in a quoted
# price is a re-based quote (split, bonus, consolidation) rather than trading. Set well
# above any genuine band so a limit-hit move is still treated as a real, stoppable move.
DEFAULT_DISCONTINUITY_FRACTION = Decimal("0.20")

SCHEMA = "pramana.corporate_actions.v1"


@dataclass(frozen=True)
class ScheduledAction:
    """One declared ex-date. ``kind`` is free text for the operator's own record."""

    symbol: str
    ex_date: date
    kind: str


class CorporateActionCalendar:
    """Ex-dates the operator declared. Absent file means no declared actions, never a guess."""

    def __init__(self, actions: tuple[ScheduledAction, ...] = ()) -> None:
        self._by_symbol: dict[str, set[date]] = {}
        self._kinds: dict[tuple[str, date], str] = {}
        for item in actions:
            self._by_symbol.setdefault(item.symbol.upper(), set()).add(item.ex_date)
            self._kinds[(item.symbol.upper(), item.ex_date)] = item.kind

    def __len__(self) -> int:
        return sum(len(dates) for dates in self._by_symbol.values())

    def action_on(self, symbol: str, moment: datetime) -> str | None:
        """The declared action for this symbol on the IST session date, or None."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("moment must be timezone-aware")
        session = moment.astimezone(IST).date()
        key = symbol.upper()
        if session not in self._by_symbol.get(key, ()):
            return None
        return self._kinds.get((key, session), "corporate_action")

    @classmethod
    def from_file(cls, path: str | Path | None) -> CorporateActionCalendar:
        """Load declared ex-dates. A malformed file yields an empty calendar and one warning.

        Failing to an empty calendar is safe here only because ``price_discontinuity``
        catches an undeclared action independently. The calendar suppresses a false alarm
        on a *known* date; it is not the thing that prevents the fabricated loss.
        """
        if path is None:
            return cls()
        target = Path(path)
        if not target.exists():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            return cls(_parse(payload))
        except (OSError, ValueError, TypeError, KeyError):
            LOGGER.warning("corporate_action_calendar_unreadable path=%s", target, exc_info=True)
            return cls()


def _parse(payload: Any) -> tuple[ScheduledAction, ...]:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"corporate action calendar must declare schema {SCHEMA}")
    rows = payload.get("actions")
    if not isinstance(rows, list):
        raise TypeError("corporate action calendar needs an actions list")
    actions = []
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("each action must be an object")
        symbol = str(row["symbol"]).strip().upper()
        if not symbol:
            raise ValueError("each action needs a symbol")
        actions.append(
            ScheduledAction(symbol, date.fromisoformat(str(row["ex_date"])), str(row.get("kind", "corporate_action"))[:40])
        )
    return tuple(actions)


def price_discontinuity(
    previous: Decimal | None,
    current: Decimal,
    *,
    fraction: Decimal = DEFAULT_DISCONTINUITY_FRACTION,
) -> bool:
    """True when the step from ``previous`` to ``current`` is too large to be trading.

    The exchange bounds how far a price may move in a session, so a larger step means the
    quote was re-based and no longer refers to the same unit as the stored cost and stop.
    Returns False on the first observation, when either value is unusable, or on any
    arithmetic fault: a guard that raises would cost the sweep it was meant to protect.
    """
    try:
        if previous is None or previous <= 0 or current <= 0 or fraction <= 0:
            return False
        return abs(current - previous) / previous > fraction
    except (DecimalException, ArithmeticError, TypeError):
        return False
