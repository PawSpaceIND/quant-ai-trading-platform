"""Operator-supplied grouping and engine-sourced daily closes for book risk.

Two inputs the cross-position controls need and the engine did not previously
carry:

``sector map``
    A symbol-to-group mapping. There is no security master in this repository and
    no defensible way to infer one, so the mapping is operator-supplied data: a
    founder directives field, an inline JSON environment variable or a JSON file.
    Absent, there is no grouping and the sector limit does not apply - it is not
    silently replaced with a guess.

``daily closes``
    The engine already fetches closed daily bars per instrument for regime
    context (``marketdata.timeframes.DailyHistoryProvider``). Those bars are the
    only per-symbol return history the engine holds: ``paper_live_valuations``
    rows are minute-bucketed book valuations written as floats, and the tick
    buffer holds a bounded window of intraday ticks, neither of which is a daily
    return series. ``DailyCloseHistory`` adapts the provider the pipeline already
    owns into the series the risk measure reads, keyed by the venue-local session
    date so instruments on different venues align by trading day rather than by
    UTC instant.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument
from quant_ai.execution.session import SESSIONS
from quant_ai.marketdata.timeframes import session_close_at, venue_for

SECTOR_MAP_JSON_ENV = "PRAMANA_SECTOR_MAP_JSON"
SECTOR_MAP_FILE_ENV = "PRAMANA_SECTOR_MAP_FILE"


def normalize_sector_map(mapping: Mapping[str, str] | None) -> dict[str, str]:
    """Upper-cased ``symbol -> group``; blank entries are dropped, not guessed."""
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise TypeError("sector_map_must_be_object")
    normalized: dict[str, str] = {}
    for symbol, group in mapping.items():
        if not isinstance(symbol, str) or not isinstance(group, str):
            raise TypeError("sector_map_requires_string_pairs")
        key = symbol.strip().upper()
        value = group.strip().upper()
        if key in normalized:
            raise ValueError("sector_map_duplicate_symbol")
        if key and value:
            normalized[key] = value
    return normalized


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("sector_map_duplicate_symbol")
        result[key] = value
    return result


def sector_map_from_env() -> dict[str, str]:
    """Operator-supplied grouping from the environment, or ``{}`` when unset."""
    inline = os.getenv(SECTOR_MAP_JSON_ENV, "").strip()
    if inline:
        return normalize_sector_map(json.loads(inline, object_pairs_hook=_unique_pairs))
    location = os.getenv(SECTOR_MAP_FILE_ENV, "").strip()
    if location:
        return normalize_sector_map(
            json.loads(Path(location).expanduser().read_text(encoding="utf-8"),
                       object_pairs_hook=_unique_pairs)
        )
    return {}


def _session_key(timestamp: datetime, zone) -> str:
    """The venue-local trading date a daily bar belongs to.

    Two instruments on different venues close at different instants on the same
    trading day, so keying by the local date is what lets them share one session
    axis. A bar without a timezone is read as UTC rather than as this host's
    local time, which would make the axis depend on where the engine runs.
    """
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(zone).date().isoformat()


class DailyCloseHistory:
    """Aligned-by-trading-day daily closes for the symbols the book holds.

    Reads the closed daily bars the pipeline's history provider already caches
    once per instrument per UTC day, so arming the book controls adds no new
    market-data request beyond the ones the regime context makes. A symbol with
    no configured instrument yields no series at all, which makes the measure
    unavailable rather than quietly measuring a smaller book.
    """

    def __init__(
        self,
        provider,
        instruments: Iterable[Instrument],
        clock: Callable[[], datetime] | None = None,
        *, max_age: timedelta | None = None,
    ) -> None:
        self.provider = provider
        self.instruments = {item.symbol.strip().upper(): item for item in instruments}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if max_age is not None and max_age <= timedelta(0):
            raise ValueError("book_history_max_age_must_be_positive")
        self.max_age = max_age
        self._observed = {}

    def _rows(self, instrument, bars, now):
        if now.utcoffset() is None:
            raise ValueError("book_history_clock_must_be_aware")
        venue = venue_for(instrument.market)
        zone = ZoneInfo(SESSIONS[venue].timezone) if venue is not None else timezone.utc
        rows = {}
        for bar in bars:
            if self.max_age is not None:
                if bar.timestamp.utcoffset() is None or bar.timestamp > now:
                    raise ValueError("book_history_timestamp_invalid")
                if bar.instrument != instrument:
                    raise ValueError("book_history_instrument_mismatch")
                if session_close_at(bar.timestamp, venue) > now:
                    raise ValueError("book_history_unclosed_session")
                if _session_key(bar.timestamp, zone) in rows:
                    raise ValueError("book_history_duplicate_session")
            rows[_session_key(bar.timestamp, zone)] = bar.close
        if self.max_age is not None and rows:
            latest = datetime.fromisoformat(max(rows)).replace(tzinfo=zone)
            if now - latest > self.max_age:
                raise ValueError("book_history_stale")
        return tuple((key, rows[key]) for key in sorted(rows))

    def __call__(self, symbols: Sequence[str]) -> dict[str, tuple[tuple[str, Decimal], ...]]:
        now = self.clock()
        series = {}
        for symbol in symbols:
            key = symbol.strip().upper()
            instrument = self.instruments.get(key)
            if instrument is None:
                continue
            # Clear old observations before a failed fetch so telemetry cannot retain green.
            self._observed.pop(key, None)
            bars = self.provider.fetch(instrument, now)
            rows = self._rows(instrument, bars, now)
            self._observed[key] = (now, tuple(bars))
            if rows:
                series[symbol] = rows
        return series

    def readiness(self, symbols: Sequence[str], now: datetime) -> dict:
        """Read cached observations only; never perform network I/O under the broker lock.

        `dataReady` is the sample/coverage check, not approval of a proposed allocation.
        The firewall still calculates risk for the exact projected book on every entry.
        """
        from quant_ai.risk.portfolio_risk import MIN_TAIL_INTERVALS, align_daily_closes
        result = {"dataReady": False, "records": 0, "coveredSymbols": 0,
                  "alignedIntervals": 0, "reason": "history_not_observed",
                  "source": getattr(self.provider, "provider_id", type(self.provider).__name__)}
        try:
            series = {}
            for symbol in symbols:
                instrument = self.instruments.get(symbol.upper())
                if instrument is None:
                    continue
                # Regime history and risk reuse the same cached source. No extra fetch.
                cached = getattr(self.provider, "cached", None)
                if callable(cached):
                    bars = cached(instrument, now)
                else:
                    observed = self._observed.get(symbol.upper())
                    bars = (observed[1] if observed and observed[0].date() == now.date()
                            and observed[0] <= now else ())
                rows = self._rows(instrument, bars or (), now)
                if rows:
                    series[symbol] = rows
            result.update(records=sum(len(rows) for rows in series.values()),
                          coveredSymbols=len(series))
            if set(series) != set(symbols) or not symbols:
                result["reason"] = "history_scope_incomplete"
                return result
            aligned, reason = align_daily_closes(series)
            intervals = len(next(iter(aligned.values()), ())) - 1
            result["alignedIntervals"] = max(0, intervals)
            result["reason"] = reason or ("ready" if intervals >= MIN_TAIL_INTERVALS
                                          else "insufficient_tail_history")
            result["dataReady"] = not reason and intervals >= MIN_TAIL_INTERVALS
        except (ValueError, TypeError, AttributeError, ArithmeticError, OSError, RuntimeError):
            result["reason"] = "history_invalid_or_stale"
        return result
