"""Which watched names stopped updating, when, and what their ticks were doing meanwhile.

On 24 and 25 September 2026 the engine latched ``protection_unreachable:COALINDIA`` in
the middle of a session. Both times the question that decided the fix could not be
answered afterwards: did the whole feed stall, or only COALINDIA, and did its ticks stop
arriving or arrive and get refused? The halt names only held symbols, so "COALINDIA" said
nothing about the other eleven; the tick counters lived in memory and a restart had
already cleared them.

The engine now writes one ``pramana.feed_minute.v1`` row a minute while a watched market
is open (``quant_ai.execution.telemetry``). This reads those rows for one IST session
and says, per symbol, each run of stale minutes and what the counters did across it:

    silent    no tick arrived at all - the feed stopped sending this name
    refused   ticks arrived and every one was rejected, with the reasons
    delayed   ticks were accepted but carried old exchange timestamps
    restart   the process restarted inside the run, so the counts do not compare

and whether every watched name was stale in the same minutes, which is a feed or
connection stall rather than one instrument. Read-only; it decides nothing.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = "pramana.feed_gaps.v1"
FEED_SCHEMA = "pramana.feed_minute.v1"
IST = ZoneInfo("Asia/Kolkata")
MINUTE = timedelta(minutes=1)
ACCEPTED = "accepted"
SILENT, REFUSED, DELAYED, RESTART = "silent", "refused", "delayed", "restart"
# A connect failure can carry the websocket URL, and Kite's URL carries the session.
SECRET = re.compile(r"(?i)(wss?|https?)://\S+|(access_token|api_key|enctoken|token)=[^&\s\"']+")


def _instant(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment.astimezone(timezone.utc) if moment.utcoffset() is not None else None


def session_bounds(session_date: date) -> tuple[str, str]:
    """The IST calendar day as UTC ISO bounds, in the table's own minute format."""
    start = datetime.combine(session_date, time.min, IST).astimezone(timezone.utc)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def load_minutes(connection, tenant_id: str, session_date: date) -> list[tuple[datetime, dict]]:
    """The session's feed minutes, oldest first. A row that does not parse is skipped."""
    since, until = session_bounds(session_date)
    rows = connection.execute(
        "SELECT minute, payload FROM pilot_feed_minutes WHERE tenant_id=? AND minute>=? AND minute<? ORDER BY minute",
        (tenant_id, since, until),
    ).fetchall()
    minutes = []
    for minute, payload in rows:
        moment = _instant(minute)
        try:
            record = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if moment is None or not isinstance(record, dict) or record.get("schema") != FEED_SCHEMA:
            continue
        if isinstance(record.get("symbols"), dict):
            minutes.append((moment, record))
    return minutes


def _counts(record: dict, symbol: str) -> dict[str, int]:
    entry = (record.get("symbols") or {}).get(symbol) or {}
    ticks = entry.get("ticks") if isinstance(entry, dict) else None
    if not isinstance(ticks, dict):
        return {}
    return {str(key): value for key, value in ticks.items() if isinstance(value, int) and value >= 0}


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int] | None:
    """What arrived between two cumulative snapshots; None when a counter went backwards."""
    delta = {}
    for key in set(before) | set(after):
        change = after.get(key, 0) - before.get(key, 0)
        if change < 0:
            return None
        if change:
            delta[key] = change
    return delta


def _verdict(delta: dict[str, int] | None) -> str:
    if delta is None:
        return RESTART
    if delta.get(ACCEPTED, 0) > 0:
        return DELAYED
    return REFUSED if delta else SILENT


def _runs(minutes: list[tuple[datetime, dict]], is_stale) -> list[tuple[int, int]]:
    """Index ranges of consecutive stale minutes. A missing minute ends a run."""
    runs, start = [], None
    for index, (moment, record) in enumerate(minutes):
        contiguous = index > 0 and moment - minutes[index - 1][0] == MINUTE
        stale = is_stale(record)
        if stale and start is not None and not contiguous:
            runs.append((start, index - 1))
            start = index
        elif stale and start is None:
            start = index
        elif not stale and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(minutes) - 1))
    return runs


def _symbol_stale(symbol: str):
    def stale(record: dict) -> bool:
        entry = (record.get("symbols") or {}).get(symbol)
        return isinstance(entry, dict) and entry.get("fresh") is not True
    return stale


def _all_stale(record: dict) -> bool:
    entries = [entry for entry in (record.get("symbols") or {}).values() if isinstance(entry, dict)]
    return bool(entries) and all(entry.get("fresh") is not True for entry in entries)


def report(minutes: list[tuple[datetime, dict]], *, session_date: date) -> dict[str, Any]:
    """Every stale run per symbol, and the minutes in which every watched name was stale."""
    symbols = sorted({name for _, record in minutes for name in (record.get("symbols") or {})})
    gaps = []
    for symbol in symbols:
        for first, last in _runs(minutes, _symbol_stale(symbol)):
            start, end = minutes[first][0], minutes[last][0]
            entry = (minutes[first][1].get("symbols") or {}).get(symbol) or {}
            # A name turns stale 120 s after its last tick, so the minute before the run
            # would miss what arrived in between. The baseline is the minute that held
            # the last good tick; without one in the record there is nothing to compare.
            last_tick = _instant(entry.get("lastTickAt"))
            cutoff = last_tick.replace(second=0, microsecond=0) if last_tick else start - MINUTE
            base = next((index for index in range(first - 1, -1, -1) if minutes[index][0] <= cutoff), None)
            after = _counts(minutes[last][1], symbol)
            span = minutes[base if base is not None else first:last + 1]
            restarted = len({record.get("countsSince") for _, record in span}) > 1
            delta = None if restarted or base is None else _delta(_counts(minutes[base][1], symbol), after)
            gaps.append({
                "symbol": symbol, "from": start.isoformat(), "to": end.isoformat(),
                "minutes": last - first + 1,
                "last_tick_at": entry.get("lastTickAt"),
                "ticks_during": delta,
                "verdict": RESTART if restarted else (_verdict(delta) if base is not None else None),
            })
    whole = [
        {"from": minutes[first][0].isoformat(), "to": minutes[last][0].isoformat(), "minutes": last - first + 1}
        for first, last in _runs(minutes, _all_stale)
    ] if len(symbols) > 1 else []
    return {
        "schema": SCHEMA, "session_date": session_date.isoformat(), "symbols": symbols,
        "minutes_recorded": len(minutes),
        "first_minute": minutes[0][0].isoformat() if minutes else None,
        "last_minute": minutes[-1][0].isoformat() if minutes else None,
        "gaps": sorted(gaps, key=lambda item: (item["from"], item["symbol"])),
        "whole_feed": whole,
    }


def _ist(value: Any) -> str:
    moment = _instant(value)
    return moment.astimezone(IST).strftime("%H:%M") if moment else "-"


READING = {
    SILENT: "no tick arrived: the feed stopped sending this name",
    REFUSED: "ticks arrived and every one was rejected",
    DELAYED: "ticks were accepted but carried old exchange timestamps",
    RESTART: "the process restarted inside this gap; its counts do not compare",
    None: "the gap opens the record, so there is no count to compare against",
}


def render(built: dict[str, Any], *, halt: Iterable | None = None, stream_events: Iterable[str] = ()) -> str:
    lines = [f"feed gaps  session {built['session_date']} (times IST)"]
    if halt is not None:
        lines.append(f"halt       {list(halt)}")
    if not built["minutes_recorded"]:
        lines.append("no feed minutes recorded for this session: the engine wrote none "
                     "(closed market, engine down, or a build before this record existed)")
    else:
        lines.append(f"recorded   {built['minutes_recorded']} minutes, {_ist(built['first_minute'])}-"
                     f"{_ist(built['last_minute'])}, {len(built['symbols'])} names")
    events = list(stream_events)
    lines.append("websocket  " + ("; ".join(events) if events else "no disconnect recorded"))
    if built["whole_feed"]:
        for run in built["whole_feed"]:
            lines.append(f"WHOLE FEED {_ist(run['from'])}-{_ist(run['to'])}  {run['minutes']} min: "
                         "every watched name stale at once - a feed or connection stall, not one symbol")
    elif built["gaps"]:
        lines.append("whole feed never stale at once: each gap below is specific to its names")
    if not built["gaps"]:
        lines.append("no stale minute for any watched name")
    for gap in built["gaps"]:
        ticks = gap["ticks_during"]
        shown = ", ".join(f"{key} {value}" for key, value in sorted(ticks.items())) if ticks else "none"
        lines.append(
            f"  {gap['symbol']:<12} {_ist(gap['from'])}-{_ist(gap['to'])}  {gap['minutes']:>3} min  "
            f"last tick {_ist(gap['last_tick_at'])}  ticks during: {shown}  -> {READING[gap['verdict']]}"
        )
    return "\n".join(lines)


def stream_events(lines: Iterable[str], session_date: date) -> list[str]:
    """Websocket events from the engine's JSON event log, for one IST session, as text."""
    since, until = session_bounds(session_date)
    found = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        name = str(event.get("event", "")) if isinstance(event, dict) else ""
        stamp = str(event.get("generated_at", "")) if isinstance(event, dict) else ""
        moment = _instant(stamp)
        if not name.startswith("websocket") or moment is None or not since <= moment.isoformat() < until:
            continue
        # Kite's close reason, with anything URL- or token-shaped removed, then capped.
        reason = SECRET.sub("[redacted]", str(event.get("error", "")))[:80]
        found.append(f"{_ist(stamp)} {name} {reason}".strip())
    return found
