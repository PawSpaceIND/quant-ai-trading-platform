"""Rebuild a point-in-time universe from an archive of daily bhavcopy files.

The scarce input for honest backtesting is the delisting record, and this is where it
comes from without buying it. Each bhavcopy is a snapshot of what traded on one day, so
across an archive of them a company's tradeable window falls out of the data directly:
the first file it appears in is when it became tradeable, the last file it appears in is
when it stopped. No vendor has to have kept the dead names, because no file was ever
rewritten to remove them.

**What this can and cannot tell you.** It observes that a security *stopped appearing*.
It cannot observe *why* — delisting, merger, insolvency, or a long suspension that was
later lifted all look identical in a bhavcopy. So :attr:`Listing.delisting_reason` says
"ceased trading" and names the file as its evidence, rather than asserting a cause the
data does not contain.

That makes the reconstructed delisting rate an **upper bound** on true delistings: a name
suspended for a year and then restored is counted as having ceased. For the purpose the
universe serves this is the right event anyway — a backtest cannot trade a suspended
security either — but the bound points in a specific direction, and since a *higher* rate
is what :meth:`PointInTimeUniverse.audit` wants to see, it flatters us. It is named here
and in :attr:`ReconstructionReport.caveats` so nobody reads the audit's ``plausible`` as
stronger evidence than it is.

**Identity.** Series are grouped by ISIN where the file carries one, because a company
rename moves the ticker and would otherwise register as one company dying and another
being born on the same day — a spurious delisting *and* a spurious listing. Files with no
ISIN column fall back to the symbol and are counted, so the exposure is visible.

The output is the pair :func:`quant_ai.research.study_runner.run_universe_study` consumes:
a ``pramana.universe_manifest.v1`` manifest and one replay dataset per instrument.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from quant_ai.domain.models import AssetClass, Market
from quant_ai.marketdata.bhavcopy import BhavcopyFile, BhavRow
from quant_ai.marketdata.point_in_time import Listing, PointInTimeUniverse
from quant_ai.marketdata.timeframes import session_close_at, venue_for
from quant_ai.marketdata.universe_manifest import SCHEMA as MANIFEST_SCHEMA

SCHEMA = "pramana.listing_reconstruction.v1"

#: A security seen trading in any of the final N observed sessions is treated as still
#: listed. Counting sessions rather than calendar days makes the rule self-calibrating:
#: it does not assume a holiday calendar, and it behaves the same on a daily archive as on
#: a sparse one. Twenty sessions is about a trading month — long enough that an illiquid
#: name going quiet near the end of coverage is not mistaken for a death.
DEFAULT_ACTIVE_TAIL_SESSIONS = 20

CEASED_REASON = "ceased trading; the bhavcopy records the absence, not its cause"


@dataclass(frozen=True)
class InstrumentHistory:
    """Every session one security traded, and the names it traded under."""

    key: str
    isin: str
    exchange: str
    symbols: tuple
    bars: tuple
    keyed_by_isin: bool

    @property
    def symbol(self) -> str:
        """The name it last traded under, which is the one a reader will recognise."""
        return self.symbols[-1]

    @property
    def first_day(self) -> date:
        return self.bars[0].trading_day

    @property
    def last_day(self) -> date:
        return self.bars[-1].trading_day

    @property
    def sessions(self) -> int:
        return len(self.bars)

    @property
    def renamed(self) -> bool:
        return len(self.symbols) > 1


@dataclass(frozen=True)
class ReconstructionReport:
    """What the archive contained and what had to be assumed to read it."""

    sessions_observed: int
    coverage_from: date
    coverage_to: date
    instruments: int
    ceased: int
    still_listed: int
    renamed: int
    left_censored: int
    rows_without_isin: int
    rejected_rows: int
    caveats: tuple

    def as_evidence(self) -> dict:
        return {
            "schema": SCHEMA,
            "sessions_observed": self.sessions_observed,
            "coverage_from": self.coverage_from.isoformat(),
            "coverage_to": self.coverage_to.isoformat(),
            "instruments": self.instruments,
            "ceased": self.ceased,
            "still_listed": self.still_listed,
            "renamed_while_listed": self.renamed,
            "left_censored": self.left_censored,
            "rows_without_isin": self.rows_without_isin,
            "rejected_rows": self.rejected_rows,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True)
class ReconstructedUniverse:
    universe: PointInTimeUniverse
    histories: tuple
    report: ReconstructionReport

    def manifest(self) -> dict:
        return {
            "schema": MANIFEST_SCHEMA,
            "source": self.universe.source,
            "coverage_from": self.universe.coverage_from.isoformat(),
            "coverage_to": self.universe.coverage_to.isoformat(),
            "listings": [
                {
                    "symbol": item.symbol,
                    "market": item.market,
                    "listed_on": item.listed_on.isoformat(),
                    **(
                        {}
                        if item.delisted_on is None
                        else {
                            "delisted_on": item.delisted_on.isoformat(),
                            "delisting_reason": item.delisting_reason,
                        }
                    ),
                }
                for item in self.universe.listings
            ],
            "reconstruction": self.report.as_evidence(),
        }


def _rows(files: Iterable) -> list:
    collected: list = []
    for item in files:
        if isinstance(item, BhavcopyFile):
            collected.extend(item.rows)
        elif isinstance(item, BhavRow):
            collected.append(item)
        else:
            raise TypeError("reconstruction takes BhavcopyFile or BhavRow values")
    return collected


def reconstruct_universe(
    files: Iterable,
    *,
    source: str,
    market: str = Market.INDIA.value,
    active_tail_sessions: int = DEFAULT_ACTIVE_TAIL_SESSIONS,
    minimum_sessions: int = 1,
) -> ReconstructedUniverse:
    """Derive listings and per-instrument bar history from parsed bhavcopy files.

    ``source`` must say where the archive came from; the manifest schema requires it and
    a universe with no stated provenance cannot be told from one assembled from memory.

    ``minimum_sessions`` drops securities with fewer sessions than that. It defaults to
    keeping everything, because dropping the short-lived names is itself a survivorship
    filter — the ones that traded for three weeks and died are precisely the observations
    a survivor-only universe is missing.
    """
    rows = _rows(files)
    if not rows:
        raise ValueError("cannot reconstruct a universe from an empty archive")
    if not source.strip():
        raise ValueError("reconstruction must name the archive it read")
    if active_tail_sessions < 1:
        raise ValueError("active_tail_sessions must be at least 1")

    sessions = sorted({row.trading_day for row in rows})
    coverage_from, coverage_to = sessions[0], sessions[-1]
    # The cutoff is a session boundary, not a date arithmetic guess: anything last seen on
    # or after it is still trading as far as this archive can tell.
    tail_index = max(0, len(sessions) - active_tail_sessions)
    active_from = sessions[tail_index]

    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)

    histories: list = []
    listings: list = []
    rejected_duplicates = 0
    for key, bars in sorted(grouped.items()):
        bars.sort(key=lambda item: item.trading_day)
        deduped: list = []
        for bar in bars:
            if deduped and deduped[-1].trading_day == bar.trading_day:
                # The same security twice on one day means two archives were merged, or a
                # file was counted twice. Keep the first and count the rest; silently
                # averaging them would invent a bar that never printed.
                rejected_duplicates += 1
                continue
            deduped.append(bar)
        if len(deduped) < minimum_sessions:
            continue
        symbols: list = []
        for bar in deduped:
            if not symbols or symbols[-1] != bar.symbol:
                symbols.append(bar.symbol)
        history = InstrumentHistory(
            key=key,
            isin=deduped[-1].isin,
            exchange=deduped[-1].exchange,
            symbols=tuple(symbols),
            bars=tuple(deduped),
            keyed_by_isin=bool(deduped[-1].isin),
        )
        histories.append(history)

        ceased = history.last_day < active_from
        listings.append(
            Listing(
                symbol=history.symbol,
                market=market,
                listed_on=history.first_day,
                # +1 day so the final session it traded on is still tradeable:
                # ``tradeable_on`` treats ``delisted_on`` as exclusive.
                delisted_on=history.last_day + timedelta(days=1) if ceased else None,
                delisting_reason=CEASED_REASON if ceased else "",
            )
        )

    if not listings:
        raise ValueError(
            f"no security traded for at least {minimum_sessions} sessions in this archive"
        )

    left_censored = sum(1 for item in histories if item.first_day == coverage_from)
    upper_bound_caveat = (
        "Listing dates are first and last appearance in the archive, not exchange listing "
        "and delisting circulars. A security is 'ceased' when it stopped trading, whatever "
        "the legal cause; a long suspension counts as ceased, so the delisting rate is an "
        "upper bound and errs towards making the survivorship audit pass."
    )
    caveats = [upper_bound_caveat]
    if left_censored:
        caveats.append(
            f"{left_censored} securities were already trading on the first session in the "
            "archive, so their real listing dates are earlier and unknown here. Studies must "
            "not read those listed_on values as IPO dates."
        )
    without_isin = sum(1 for item in histories if not item.keyed_by_isin)
    if without_isin:
        caveats.append(
            f"{without_isin} securities had no ISIN and were keyed by symbol. A rename in "
            "that group registers as one security ceasing and another listing."
        )
    if rejected_duplicates:
        caveats.append(
            f"{rejected_duplicates} duplicate security-days were dropped, keeping the first. "
            "Two archives covering the same session were probably read together."
        )

    universe = PointInTimeUniverse(
        listings,
        source=source.strip(),
        coverage_from=coverage_from,
        # PointInTimeUniverse requires coverage to end after it starts, and members_on
        # must answer for the final session, so coverage runs to the day after it.
        coverage_to=coverage_to + timedelta(days=1),
    )
    report = ReconstructionReport(
        sessions_observed=len(sessions),
        coverage_from=coverage_from,
        coverage_to=coverage_to,
        instruments=len(histories),
        ceased=sum(1 for item in listings if item.delisted_on is not None),
        still_listed=sum(1 for item in listings if item.delisted_on is None),
        renamed=sum(1 for item in histories if item.renamed),
        left_censored=left_censored,
        rows_without_isin=without_isin,
        rejected_rows=rejected_duplicates,
        caveats=tuple(caveats),
    )
    return ReconstructedUniverse(universe=universe, histories=tuple(histories), report=report)


def _dataset_payload(history: InstrumentHistory, market: str) -> dict:
    venue = venue_for(Market(market))
    bars = []
    for bar in history.bars:
        # Midday UTC lands inside the Indian session on every date, so the session this
        # bar belongs to is unambiguous either side of a DST boundary elsewhere.
        midday = datetime.combine(bar.trading_day, time(12), tzinfo=timezone.utc)
        close_at = session_close_at(midday, venue)
        bars.append(
            {
                "timestamp": close_at.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
            }
        )
    return {
        "provenance": {
            "instrument": {
                "symbol": history.symbol,
                "market": market,
                "asset_class": AssetClass.EQUITY.value,
                "currency": "INR",
                "exchange": history.exchange,
            },
            "source": f"{history.exchange} bhavcopy archive",
            "isin": history.isin,
            "symbols_traded_under": list(history.symbols),
            "sessions": history.sessions,
            "adjusted": False,
            "adjustment_note": (
                "Raw traded prices as the exchange printed them. Corporate actions are NOT "
                "applied: a split will read as an overnight collapse. Reconcile with "
                "quant_ai.marketdata.action_reconciliation before running a study."
            ),
        },
        "bars": bars,
    }


def write_study_inputs(
    reconstructed: ReconstructedUniverse,
    destination,
    *,
    market: str = Market.INDIA.value,
) -> dict:
    """Write the manifest and one replay dataset per instrument into ``destination``.

    The result is exactly what ``run_universe_study(datasets, manifest, register=...)``
    reads. Returns a summary naming the files written.
    """
    target = Path(destination)
    datasets = target / "datasets"
    datasets.mkdir(parents=True, exist_ok=True)

    manifest_path = target / "universe.json"
    manifest_path.write_text(
        json.dumps(reconstructed.manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    written: list = []
    for history in reconstructed.histories:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in f"{history.exchange}_{history.symbol}"
        )
        path = datasets / f"{safe}.json"
        path.write_text(
            json.dumps(_dataset_payload(history, market), indent=2) + "\n", encoding="utf-8"
        )
        written.append(path.name)

    return {
        "schema": SCHEMA,
        "manifest": str(manifest_path),
        "datasets": str(datasets),
        "files_written": len(written),
        "report": reconstructed.report.as_evidence(),
    }
