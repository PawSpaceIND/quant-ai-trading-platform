"""Scheduled events that suppress new entries, from an operator-supplied file.

Some days are known in advance to be bad days to open a position: the morning a company
reports, the afternoon a central bank decides, budget day. The engine cannot discover
those dates — it has no earnings feed and no policy calendar — so it does not guess
them. An operator writes them down, the file is validated once at boot, and the pilot
pre-submit path refuses new entries on them.

Configured with ``PRAMANA_EVENT_CALENDAR``, a path to a JSON file::

    {
      "timezone": "Asia/Kolkata",
      "events": [
        {"date": "2026-10-15", "category": "earnings", "symbol": "INFY"},
        {"date": "2026-10-01", "category": "rbi_policy"},
        {"date": "2026-02-01", "category": "budget_day"},
        {"date": "2026-03-17", "through": "2026-03-18", "category": "fed_decision"}
      ]
    }

An event with a ``symbol`` blacks out that instrument; an event without one is
index-level and blacks out every instrument. Dates are exchange-local calendar days,
compared in the file's timezone (NSE's by default), never UTC.

The rules this module holds to:

* **No file, no blackouts.** An unset variable means the engine invents no events. It
  never derives an earnings date from a filing, a ticker or the shape of a URL.
* **Fail closed on malformed input.** A file that exists but does not validate raises,
  so the daemon refuses to boot rather than trading on a calendar nobody has read. A
  half-understood blackout calendar is worse than none: it silently stops vetoing.
* **Entries only.** ``blackout_reason`` answers for a new entry. Nothing here can force,
  delay or block an exit — protective exits do not consult it and must not.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from quant_ai.execution.session import SESSIONS, GlobalVenue

LOGGER = logging.getLogger("quant_ai.event_calendar")

EVENT_CALENDAR_ENV = "PRAMANA_EVENT_CALENDAR"
# The blackout reasons the pre-submit path can return. A closed set keeps the veto
# strings stable for dashboards and post-mortems, and stops a typo in the operator file
# from becoming a new, silently-accepted reason code.
EARNINGS = "earnings"
EVENT_CATEGORIES = frozenset(
    {EARNINGS, "rbi_policy", "fed_decision", "budget_day", "data_release"}
)
BLACKOUT_PREFIX = "event_blackout"
MAX_EVENTS = 2000
# The pilot trades NSE cash only, so calendar days are read in the exchange's own
# timezone rather than a constant invented here.
DEFAULT_TIMEZONE = SESSIONS[GlobalVenue.INDIA].timezone


class EventCalendarError(ValueError):
    """Raised when an operator-supplied event calendar cannot be trusted."""


@dataclass(frozen=True)
class ScheduledEvent:
    """One scheduled event over a closed range of exchange-local calendar days."""

    start: date
    end: date
    category: str
    symbol: str | None = None

    def __post_init__(self) -> None:
        if self.category not in EVENT_CATEGORIES:
            raise EventCalendarError(
                f"unsupported event category {self.category!r}; "
                f"expected one of {sorted(EVENT_CATEGORIES)}"
            )
        if self.end < self.start:
            raise EventCalendarError("event 'through' date precedes its 'date'")
        if self.symbol is not None and not self.symbol:
            raise EventCalendarError("event symbol must not be empty")

    def covers(self, symbol: str, day: date) -> bool:
        if not self.start <= day <= self.end:
            return False
        return self.symbol is None or self.symbol == symbol.strip().upper()


@dataclass(frozen=True)
class EventCalendar:
    """Deterministic blackout lookup over a validated set of scheduled events."""

    events: tuple[ScheduledEvent, ...] = ()
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        if len(self.events) > MAX_EVENTS:
            raise EventCalendarError(f"event calendar holds at most {MAX_EVENTS} events")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise EventCalendarError(f"unknown event calendar timezone {self.timezone!r}") from error

    def local_day(self, moment: datetime) -> date:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise EventCalendarError("event calendar timestamps must be timezone-aware")
        return moment.astimezone(ZoneInfo(self.timezone)).date()

    def blackout_reason(self, symbol: str, moment: datetime) -> str | None:
        """``event_blackout:<category>`` when a new entry is suppressed, else ``None``.

        The most specific instrument-level event wins over an index-level one, so a
        proof says ``earnings`` rather than the policy meeting that happened to share
        the day. Only ever consulted for entries.
        """
        day = self.local_day(moment)
        matches = [event for event in self.events if event.covers(symbol, day)]
        if not matches:
            return None
        matches.sort(key=lambda event: (event.symbol is None, event.category))
        return f"{BLACKOUT_PREFIX}:{matches[0].category}"

    def events_on(self, symbol: str, moment: datetime) -> tuple[ScheduledEvent, ...]:
        day = self.local_day(moment)
        return tuple(event for event in self.events if event.covers(symbol, day))


def event_calendar_from_json(payload: Any) -> EventCalendar:
    """Validate an operator calendar payload. Anything unexpected raises."""
    if not isinstance(payload, Mapping):
        raise EventCalendarError("event calendar must be a JSON object")
    unknown = set(payload) - {"events", "timezone"}
    if unknown:
        raise EventCalendarError(f"unknown event calendar fields: {sorted(unknown)}")
    zone = payload.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(zone, str) or not zone.strip():
        raise EventCalendarError("event calendar timezone must be a string")
    raw = payload.get("events", [])
    if not isinstance(raw, list):
        raise EventCalendarError("event calendar 'events' must be a list")
    return EventCalendar(tuple(_event_from_json(item) for item in raw), zone.strip())


def _event_from_json(item: Any) -> ScheduledEvent:
    if not isinstance(item, Mapping):
        raise EventCalendarError("each event must be a JSON object")
    unknown = set(item) - {"date", "through", "category", "symbol", "note"}
    if unknown:
        raise EventCalendarError(f"unknown event fields: {sorted(unknown)}")
    category = item.get("category")
    if not isinstance(category, str):
        raise EventCalendarError("event category must be a string")
    start = _iso_date(item.get("date"), "date")
    end = _iso_date(item["through"], "through") if item.get("through") is not None else start
    symbol = item.get("symbol")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise EventCalendarError("event symbol must be a non-empty string when present")
    if category.strip().lower() == EARNINGS and symbol is None:
        # An earnings blackout with no instrument would halt the whole book; that is
        # almost certainly a typo, and guessing which name was meant is not an option.
        raise EventCalendarError("an earnings event requires the symbol it belongs to")
    return ScheduledEvent(
        start, end, category.strip().lower(), symbol.strip().upper() if symbol else None
    )


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise EventCalendarError(f"event {field} must be an ISO-8601 date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise EventCalendarError(f"event {field} is not an ISO-8601 date: {value!r}") from error


def event_calendar_from_env(
    environ: Mapping[str, str] | None = None,
) -> EventCalendar | None:
    """The configured calendar, or ``None`` when the operator supplied no file.

    A configured path that cannot be read or parsed raises: the daemon must not come up
    believing it is honouring blackouts it never loaded.
    """
    environ = os.environ if environ is None else environ
    configured = environ.get(EVENT_CALENDAR_ENV, "").strip()
    if not configured:
        return None
    location = Path(configured).expanduser()
    try:
        raw = location.read_text(encoding="utf-8")
    except OSError as error:
        raise EventCalendarError(
            f"{EVENT_CALENDAR_ENV} is set to {configured!r} but the file cannot be read: {error}"
        ) from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EventCalendarError(f"{configured!r} is not valid JSON: {error}") from error
    calendar = event_calendar_from_json(payload)
    LOGGER.info(
        "event_calendar_loaded path=%s events=%s timezone=%s",
        configured, len(calendar.events), calendar.timezone,
    )
    return calendar
