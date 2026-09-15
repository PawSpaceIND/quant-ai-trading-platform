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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument
from quant_ai.execution.session import SESSIONS
from quant_ai.marketdata.timeframes import venue_for

SECTOR_MAP_JSON_ENV = "PRAMANA_SECTOR_MAP_JSON"
SECTOR_MAP_FILE_ENV = "PRAMANA_SECTOR_MAP_FILE"


def normalize_sector_map(mapping: Mapping[str, str] | None) -> dict[str, str]:
    """Upper-cased ``symbol -> group``; blank entries are dropped, not guessed."""
    if not mapping:
        return {}
    normalized: dict[str, str] = {}
    for symbol, group in mapping.items():
        key = str(symbol).strip().upper()
        value = str(group).strip().upper()
        if key and value:
            normalized[key] = value
    return normalized


def sector_map_from_env() -> dict[str, str]:
    """Operator-supplied grouping from the environment, or ``{}`` when unset."""
    inline = os.getenv(SECTOR_MAP_JSON_ENV, "").strip()
    if inline:
        return normalize_sector_map(json.loads(inline))
    location = os.getenv(SECTOR_MAP_FILE_ENV, "").strip()
    if location:
        return normalize_sector_map(
            json.loads(Path(location).expanduser().read_text(encoding="utf-8"))
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
    ) -> None:
        self.provider = provider
        self.instruments = {item.symbol.strip().upper(): item for item in instruments}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, symbols: Sequence[str]) -> dict[str, tuple[tuple[str, Decimal], ...]]:
        now = self.clock()
        series: dict[str, tuple[tuple[str, Decimal], ...]] = {}
        for symbol in symbols:
            instrument = self.instruments.get(symbol.strip().upper())
            if instrument is None:
                continue
            venue = venue_for(instrument.market)
            zone = ZoneInfo(SESSIONS[venue].timezone) if venue is not None else timezone.utc
            rows: dict[str, Decimal] = {}
            for bar in self.provider.fetch(instrument, now):
                rows[_session_key(bar.timestamp, zone)] = bar.close
            if rows:
                series[symbol] = tuple((key, rows[key]) for key in sorted(rows))
        return series
