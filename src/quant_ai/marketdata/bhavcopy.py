"""Parse exchange bhavcopy files: the daily record of everything that actually traded.

A bhavcopy is one CSV per exchange per trading day, listing every security that traded on
it. That property is what makes it valuable here and it is worth stating plainly: the file
is **point-in-time by construction**. A company appears in the file for 14 March 2016
because it traded on 14 March 2016. When it is delisted it stops appearing, and the files
from before that day are unchanged. Nothing has to "retain" the dead names — they were
never removed, because each file is a snapshot that was never rewritten.

This is the opposite of a symbol-list API (Yahoo, a broker instrument master), which
answers *what is tradeable now* and so silently deletes every failure. A universe
reconstructed from those is survivor-only and every backtest run on it is biased upward.

This module does one job: bytes on disk to typed rows. It does not fetch, reconstruct
listings or adjust prices — :mod:`quant_ai.marketdata.listing_reconstruction` and
:mod:`quant_ai.marketdata.action_reconciliation` do those.

Three formats are understood, detected from the header rather than the filename:

``nse-legacy``
    NSE's format until mid-2024. ``SYMBOL,SERIES,OPEN,...,TIMESTAMP,TOTALTRADES,ISIN``.
``bse-legacy``
    BSE's classic format. ``SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,...,ISIN_CODE,TRADING_DATE``.
    Keyed by numeric scrip code, not a ticker; older vintages omit the date column
    entirely, which is why ``trading_day`` can be supplied from the filename.
``udiff``
    The unified format both exchanges moved to from 2024. Covers NSE and BSE with one
    parser, which is the point of it.

**Nothing is fabricated.** A row that cannot yield a coherent bar — a suspended scrip
quoted at 0.00, a low above its high, an unparseable number — is *rejected and counted*,
never coerced to zero and never silently dropped. :attr:`BhavcopyFile.rejected` carries
every one with the reason, because a parser that quietly discards 8% of its input while
reporting success is how a dataset acquires holes nobody can see.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "pramana.bhavcopy.v1"

#: NSE rolling-settlement equity. The default, because the friction and impact models
#: elsewhere in this repository assume normal rolling settlement.
NSE_NORMAL_SERIES = ("EQ",)
#: NSE trade-to-trade (``BE``) and surveillance trade-to-trade (``BZ``). These are real
#: equities but compulsory-delivery with no intraday netting, so their microstructure and
#: effective cost differ from ``EQ``. Parseable, and excluded by default on purpose.
NSE_RESTRICTED_SERIES = ("BE", "BZ")
#: NSE SME platform. Thin books; the square-root impact model does not describe them.
NSE_SME_SERIES = ("SM", "ST")
#: NSE's same-day-settlement segment, introduced in 2024. Excluded by default for two
#: reasons found in real data rather than assumed.
#:
#: It is *the same security again*: a T0 row carries the same ISIN as the EQ row, so
#: keeping both yields two rows for one security on one day. Reconstruction dedupes those,
#: keeps the first and reports the rest as ``rejected_rows`` — correct, but arbitrary about
#: which survives, and there is no reason to put it in that position.
#:
#: Its OHLC is also not internally consistent. Observed on SBIN, 2024-09-05: the T0 row had
#: a single trade of one share at 820.00, so open, high and low were all 820.00, while
#: ``ClsPric`` carried 818.75 — the regular segment's close, identical to the settlement
#: price. A close below the day's low is refused by ``BhavRow``, which is the right
#: outcome: the row does not describe a session anything actually traded through.
NSE_SAME_DAY_SETTLEMENT_SERIES = ("T0",)

#: BSE ``SC_TYPE`` for equity. Bonds (``B``), debentures (``D``) and preference shares
#: share the file and are not equities.
BSE_EQUITY_TYPE = "Q"
#: BSE groups under normal rolling settlement.
BSE_NORMAL_GROUPS = ("A", "B")
#: BSE trade-to-trade (``T``) and non-compliant (``Z``) groups.
BSE_RESTRICTED_GROUPS = ("T", "Z")

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_NSE_LEGACY_MARKERS = frozenset({"SYMBOL", "SERIES", "TOTTRDQTY", "TIMESTAMP"})
_BSE_LEGACY_MARKERS = frozenset({"SC_CODE", "SC_GROUP", "SC_TYPE", "NO_OF_SHRS"})
_UDIFF_MARKERS = frozenset({"TRADDT", "TCKRSYMB", "SCTYSRS", "CLSPRIC"})


class BhavcopyFormatError(ValueError):
    """The file is not a bhavcopy this module understands, or its shape is incoherent.

    Raised for whole-file problems only. A single unusable row is a rejection, not an
    exception: one suspended scrip must not cost us the other 1,800 rows in the file.
    """


@dataclass(frozen=True)
class RejectedRow:
    """A row that could not become a bar, and why. Counted, never silently dropped."""

    line: int
    symbol: str
    reason: str


@dataclass(frozen=True)
class BhavRow:
    """One security's trading on one day, as the exchange recorded it.

    ``previous_close`` is the exchange's own figure and is load-bearing: on an ex-date
    the exchange states it *already adjusted* for the corporate action, so comparing it
    with the prior session's close recovers the adjustment factor the exchange applied.
    :mod:`quant_ai.marketdata.action_reconciliation` relies on that.

    ``isin`` is the identity that survives a rename; ``symbol`` does not. It is ``""``
    when the source file carries none, and the caller is told how many such rows there
    were rather than being left to discover it in a broken price series.
    """

    exchange: str
    trading_day: date
    symbol: str
    isin: str
    series: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal
    volume: Decimal
    turnover: Decimal
    trades: int | None = None
    security_name: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("a bhavcopy row needs a symbol")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError(f"{self.symbol}: prices must be positive")
        if self.low > self.high:
            raise ValueError(f"{self.symbol}: low exceeds high")
        if not self.low <= self.open <= self.high:
            raise ValueError(f"{self.symbol}: open outside the day's range")
        if not self.low <= self.close <= self.high:
            raise ValueError(f"{self.symbol}: close outside the day's range")
        if self.volume < 0 or self.turnover < 0:
            raise ValueError(f"{self.symbol}: volume and turnover cannot be negative")

    @property
    def key(self) -> str:
        """The identity to group a price series by: ISIN when known, else the symbol.

        Keying by symbol makes a rename look like one company dying and another being
        born on the same day, which shows up as a spurious delisting *and* a spurious
        listing. ISIN does not move when a company renames.
        """
        return self.isin or f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class BhavcopyFile:
    """Every usable row of one file, plus an honest account of what was not usable."""

    exchange: str
    trading_day: date
    source_format: str
    rows: tuple
    rejected: tuple
    rows_without_isin: int

    @property
    def usable(self) -> int:
        return len(self.rows)

    def series_counts(self) -> dict:
        counts: dict = {}
        for row in self.rows:
            counts[row.series] = counts.get(row.series, 0) + 1
        return counts

    def filter_series(self, keep: Sequence[str]) -> BhavcopyFile:
        """Narrow to the given series/groups, keeping the rejection record intact."""
        allowed = {str(item).strip().upper() for item in keep}
        kept = tuple(row for row in self.rows if row.series in allowed)
        return BhavcopyFile(
            exchange=self.exchange,
            trading_day=self.trading_day,
            source_format=self.source_format,
            rows=kept,
            rejected=self.rejected,
            rows_without_isin=sum(1 for row in kept if not row.isin),
        )


def _decimal(value: str, field: str, symbol: str) -> Decimal:
    text = (value or "").strip()
    if not text or text in {"-", "NA", "N/A"}:
        raise ValueError(f"{symbol}: {field} is blank")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"{symbol}: {field} is not a number: {text!r}") from error


def _parse_day(text: str) -> date:
    """Parse the several date spellings the exchanges have used, without relying on locale.

    ``strptime`` with ``%b`` reads month names through the process locale, so a container
    set to anything but English silently fails on ``01-JUL-2019``. The month table here is
    explicit for that reason.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("blank trading date")
    head = raw.split(" ")[0]
    # ISO first, and detected by shape rather than by splitting on "T" — the month
    # abbreviation OCT contains one, so a naive time-strip silently turns 05-OCT-2019 into
    # 05-OC and loses every October file in the archive.
    if len(head) >= 10 and head[4:5] == "-" and head[:4].isdigit():
        return date.fromisoformat(head[:10])
    for separator in ("-", "/"):
        if separator not in head:
            continue
        parts = head.split(separator)
        if len(parts) != 3:
            continue
        day_text, month_text, year_text = parts
        month_text = month_text.strip().upper()
        month = _MONTHS.get(month_text[:3]) if month_text[:1].isalpha() else int(month_text)
        year = int(year_text)
        if year < 100:
            # Two-digit years appear in older BSE files. The exchanges' electronic record
            # starts in the 1990s, so 90-99 is the 1900s and everything else the 2000s.
            year += 1900 if year >= 90 else 2000
        if month is None:
            raise ValueError(f"unrecognised month in trading date: {raw!r}")
        return date(year, month, int(day_text))
    raise ValueError(f"unrecognised trading date: {raw!r}")


def _detect(header: Sequence[str]) -> str:
    names = {str(item).strip().upper().lstrip("﻿") for item in header}
    if _UDIFF_MARKERS <= names:
        return "udiff"
    if _NSE_LEGACY_MARKERS <= names:
        return "nse-legacy"
    if _BSE_LEGACY_MARKERS <= names:
        return "bse-legacy"
    raise BhavcopyFormatError(
        "unrecognised bhavcopy header; expected NSE legacy, BSE legacy or UDiFF columns, got: "
        + ", ".join(sorted(names)[:12])
    )


def _cell(record: dict, *names: str) -> str:
    for name in names:
        if name in record and record[name] is not None:
            return str(record[name]).strip()
    return ""


def _row_nse_legacy(record: dict, symbol: str) -> BhavRow:
    return BhavRow(
        exchange="NSE",
        trading_day=_parse_day(_cell(record, "TIMESTAMP")),
        symbol=symbol,
        isin=_cell(record, "ISIN"),
        series=_cell(record, "SERIES").upper(),
        open=_decimal(_cell(record, "OPEN"), "open", symbol),
        high=_decimal(_cell(record, "HIGH"), "high", symbol),
        low=_decimal(_cell(record, "LOW"), "low", symbol),
        close=_decimal(_cell(record, "CLOSE"), "close", symbol),
        previous_close=_decimal(_cell(record, "PREVCLOSE"), "previous close", symbol),
        volume=_decimal(_cell(record, "TOTTRDQTY"), "volume", symbol),
        turnover=_decimal(_cell(record, "TOTTRDVAL"), "turnover", symbol),
        trades=int(_cell(record, "TOTALTRADES") or 0) or None,
    )


def _row_bse_legacy(record: dict, symbol: str, fallback_day: date | None) -> BhavRow:
    stated = _cell(record, "TRADING_DATE", "TRADINGDATE")
    if stated:
        trading_day = _parse_day(stated)
    elif fallback_day is not None:
        trading_day = fallback_day
    else:
        raise ValueError(
            f"{symbol}: this BSE vintage carries no TRADING_DATE column, so the trading day "
            "must be supplied from the filename via trading_day="
        )
    return BhavRow(
        exchange="BSE",
        trading_day=trading_day,
        symbol=symbol,
        isin=_cell(record, "ISIN_CODE", "ISIN"),
        series=_cell(record, "SC_GROUP").upper(),
        open=_decimal(_cell(record, "OPEN"), "open", symbol),
        high=_decimal(_cell(record, "HIGH"), "high", symbol),
        low=_decimal(_cell(record, "LOW"), "low", symbol),
        close=_decimal(_cell(record, "CLOSE"), "close", symbol),
        previous_close=_decimal(_cell(record, "PREVCLOSE"), "previous close", symbol),
        volume=_decimal(_cell(record, "NO_OF_SHRS"), "volume", symbol),
        turnover=_decimal(_cell(record, "NET_TURNOV"), "turnover", symbol),
        trades=int(_cell(record, "NO_TRADES") or 0) or None,
        security_name=_cell(record, "SC_NAME"),
    )


def _row_udiff(record: dict, symbol: str, exchange: str) -> BhavRow:
    return BhavRow(
        exchange=exchange,
        trading_day=_parse_day(_cell(record, "TRADDT")),
        symbol=symbol,
        isin=_cell(record, "ISIN"),
        series=_cell(record, "SCTYSRS").upper(),
        open=_decimal(_cell(record, "OPNPRIC"), "open", symbol),
        high=_decimal(_cell(record, "HGHPRIC"), "high", symbol),
        low=_decimal(_cell(record, "LWPRIC"), "low", symbol),
        close=_decimal(_cell(record, "CLSPRIC"), "close", symbol),
        previous_close=_decimal(_cell(record, "PRVSCLSGPRIC"), "previous close", symbol),
        volume=_decimal(_cell(record, "TTLTRADGVOL"), "volume", symbol),
        turnover=_decimal(_cell(record, "TTLTRFVAL"), "turnover", symbol),
        trades=int(_cell(record, "TTLNBOFTXSEXCTD") or 0) or None,
        security_name=_cell(record, "FININSTRMNM"),
    )


def parse_bhavcopy(
    text: str,
    *,
    trading_day: date | None = None,
    exchange: str | None = None,
) -> BhavcopyFile:
    """Parse one bhavcopy's text into typed rows.

    ``trading_day`` supplies the date for older BSE vintages whose files omit it. When the
    file *does* state a date it is still checked against this argument, because a filename
    and its contents disagreeing means the archive is misfiled and every bar in it would
    land on the wrong day — a silent, and in a backtest a profitable, error.

    ``exchange`` names the venue for UDiFF files, which are identical in shape for NSE and
    BSE. It defaults to ``"NSE"`` and is ignored by the legacy parsers, whose formats are
    venue-specific already.
    """
    handle = io.StringIO(text)
    reader = csv.reader(handle)
    try:
        header = next(reader)
    except StopIteration:
        raise BhavcopyFormatError("bhavcopy file is empty") from None
    source_format = _detect(header)
    columns = [str(item).strip().upper().lstrip("﻿") for item in header]
    venue = (exchange or "NSE").strip().upper()

    rows: list = []
    rejected: list = []
    for line, values in enumerate(reader, start=2):
        if not any(str(item).strip() for item in values):
            continue
        record = dict(zip(columns, values))
        if source_format == "nse-legacy":
            symbol = _cell(record, "SYMBOL")
        elif source_format == "bse-legacy":
            symbol = _cell(record, "SC_CODE")
        else:
            symbol = _cell(record, "TCKRSYMB")
        if not symbol:
            rejected.append(RejectedRow(line=line, symbol="", reason="row carries no symbol"))
            continue

        if source_format == "udiff":
            instrument_type = _cell(record, "FININSTRMTP").upper()
            if instrument_type and instrument_type != "STK":
                continue  # A derivative sharing the file. Not an equity, not an error.
            if _cell(record, "XPRYDT"):
                continue  # Dated contract, likewise.
        elif source_format == "bse-legacy":
            security_type = _cell(record, "SC_TYPE").upper()
            if security_type and security_type != BSE_EQUITY_TYPE:
                continue  # Bond, debenture or preference share sharing the file.

        try:
            if source_format == "nse-legacy":
                row = _row_nse_legacy(record, symbol)
            elif source_format == "bse-legacy":
                row = _row_bse_legacy(record, symbol, trading_day)
            else:
                row = _row_udiff(record, symbol, venue)
        except (ValueError, ArithmeticError) as error:
            # A suspended scrip quoted at 0.00, a malformed number, an inverted range.
            # Recorded with its reason; coercing it to a bar would invent a trade.
            rejected.append(RejectedRow(line=line, symbol=symbol, reason=str(error)))
            continue
        rows.append(row)

    if not rows:
        raise BhavcopyFormatError(
            f"{source_format} bhavcopy yielded no usable rows "
            f"({len(rejected)} rejected); the file is empty, truncated or not equity cash"
        )

    days = {row.trading_day for row in rows}
    if len(days) > 1:
        raise BhavcopyFormatError(
            "a bhavcopy covers one trading day but this file spans "
            + ", ".join(day.isoformat() for day in sorted(days))
        )
    day = rows[0].trading_day
    if trading_day is not None and day != trading_day:
        raise BhavcopyFormatError(
            f"file states trading day {day} but {trading_day} was expected; a misfiled "
            "archive would place every bar in it on the wrong day"
        )

    return BhavcopyFile(
        exchange=rows[0].exchange,
        trading_day=day,
        source_format=source_format,
        rows=tuple(rows),
        rejected=tuple(rejected),
        rows_without_isin=sum(1 for row in rows if not row.isin),
    )


def read_bhavcopy(
    path,
    *,
    trading_day: date | None = None,
    exchange: str | None = None,
) -> BhavcopyFile:
    """Read a bhavcopy from a ``.csv`` or from the ``.zip`` the exchanges publish."""
    target = Path(path)
    if target.suffix.lower() == ".zip":
        with zipfile.ZipFile(target) as archive:
            members = [
                name for name in archive.namelist()
                if name.lower().endswith(".csv") and not name.startswith("__MACOSX")
            ]
            if len(members) != 1:
                raise BhavcopyFormatError(
                    f"{target.name} contains {len(members)} CSV members; expected exactly one"
                )
            payload = archive.read(members[0]).decode("utf-8-sig", errors="strict")
    else:
        payload = target.read_text(encoding="utf-8-sig")
    return parse_bhavcopy(payload, trading_day=trading_day, exchange=exchange)
