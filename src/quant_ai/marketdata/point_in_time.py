"""What the universe looked like on a past day, and what was knowable on it.

Two distinct biases live here, and conflating them is how backtests flatter themselves.

**Survivorship.** A universe assembled today contains the companies that still exist. The
ones that were delisted, merged away or went to zero are simply absent, so a backtest
cannot buy them and cannot lose on them. The effect is not small and it always points the
same way: upward. ``PointInTimeUniverse`` keeps delisted names with the dates they were
tradeable between, so ``members_on`` answers what could actually have been bought.

**Lookahead.** A price series fetched today carries today's values, including revisions and
adjustments that were not knowable at the time. ``close_known_at`` returns the value as it
stood at a given instant, and ``None`` when nothing was known — which a backtest must treat
as an absence of data, never as permission to use the modern figure.

This module holds the structure and the checks. It does not supply the data: a genuine
point-in-time source is an external dependency the capability register already tracks as
``licensed_point_in_time_source`` under requirement D02. What the module refuses to do is
let a study run on a survivor-only universe without saying so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

SCHEMA = "pramana.point_in_time.v1"

#: Equity universes lose names every year to delisting, suspension, merger and insolvency.
#: The exact rate varies by venue and era, so this is a smell test rather than a law: a
#: universe reporting a rate at or near zero across several years is not a clean universe,
#: it is a universe someone assembled from the companies that are still listed today.
IMPLAUSIBLY_LOW_ANNUAL_DELISTING_RATE = 0.005


@dataclass(frozen=True)
class Listing:
    """One instrument and the window in which it could be traded."""

    symbol: str
    market: str
    listed_on: date
    delisted_on: date | None = None
    delisting_reason: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("a listing needs a symbol")
        if self.delisted_on is not None and self.delisted_on < self.listed_on:
            raise ValueError(f"{self.symbol} cannot be delisted before it listed")

    def tradeable_on(self, day: date) -> bool:
        if day < self.listed_on:
            return False
        return self.delisted_on is None or day < self.delisted_on


@dataclass(frozen=True)
class SurvivorshipAudit:
    listings: int
    delisted: int
    coverage_years: float
    annual_delisting_rate: float
    verdict: str
    reasons: tuple[str, ...]

    @property
    def usable_for_research(self) -> bool:
        return self.verdict == "plausible"

    def as_evidence(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "listings": self.listings,
            "delisted": self.delisted,
            "coverage_years": round(self.coverage_years, 3),
            "annual_delisting_rate": round(self.annual_delisting_rate, 5),
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "limitation": (
                "A plausible delisting rate means the universe is not obviously survivor-only. "
                "It is not proof that the constituent history is correct or complete."
            ),
        }


class PointInTimeUniverse:
    """Instrument listings with the dates that bound them."""

    def __init__(
        self,
        listings: Sequence[Listing],
        *,
        source: str,
        coverage_from: date,
        coverage_to: date,
    ) -> None:
        if not listings:
            raise ValueError("a universe needs at least one listing")
        if coverage_to <= coverage_from:
            raise ValueError("coverage must end after it starts")
        if not source.strip():
            raise ValueError("a universe must name the source it came from")
        self.listings = tuple(listings)
        self.source = source.strip()
        self.coverage_from = coverage_from
        self.coverage_to = coverage_to

    def members_on(self, day: date) -> tuple[str, ...]:
        """Symbols tradeable on ``day``, including ones that no longer exist."""
        if not self.coverage_from <= day <= self.coverage_to:
            raise ValueError(
                f"{day} is outside the universe's coverage "
                f"{self.coverage_from}..{self.coverage_to}; the answer is unknown, not empty"
            )
        return tuple(sorted(item.symbol for item in self.listings if item.tradeable_on(day)))

    def was_tradeable(self, symbol: str, day: date) -> bool:
        return symbol in self.members_on(day)

    def audit(
        self,
        *,
        minimum_annual_delisting_rate: float = IMPLAUSIBLY_LOW_ANNUAL_DELISTING_RATE,
    ) -> SurvivorshipAudit:
        delisted = sum(1 for item in self.listings if item.delisted_on is not None)
        years = (self.coverage_to - self.coverage_from).days / 365.25
        rate = (delisted / len(self.listings)) / years if years > 0 else 0.0

        reasons: list[str] = []
        verdict = "plausible"
        if delisted == 0:
            verdict = "survivor_only"
            reasons.append(
                f"{len(self.listings)} listings across {years:.1f} years and not one delisting. "
                "This is the signature of a universe built from the companies that still exist; "
                "every backtest run on it is biased upward and none of them can lose on a failure."
            )
        elif rate < minimum_annual_delisting_rate and years >= 3.0:
            verdict = "implausibly_clean"
            reasons.append(
                f"delisting rate {rate:.4%} a year over {years:.1f} years is below the "
                f"{minimum_annual_delisting_rate:.3%} floor; the universe is probably missing "
                "names that failed rather than genuinely having lost almost none"
            )
        return SurvivorshipAudit(
            listings=len(self.listings),
            delisted=delisted,
            coverage_years=years,
            annual_delisting_rate=rate,
            verdict=verdict,
            reasons=tuple(reasons),
        )


@dataclass(frozen=True)
class BarRevision:
    """One value for one trading day, and the instant it became knowable."""

    symbol: str
    trading_day: date
    close: Decimal
    known_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.close <= 0:
            raise ValueError(f"{self.symbol} close must be positive")
        if self.known_at.tzinfo is None:
            raise ValueError("known_at must be timezone-aware; a naive instant cannot be ordered")


def close_known_at(
    revisions: Sequence[BarRevision],
    *,
    symbol: str,
    trading_day: date,
    as_of: datetime,
) -> Decimal | None:
    """The close for ``trading_day`` as it stood at ``as_of``.

    Returns ``None`` when nothing was known yet. That is the correct answer and a caller
    must treat it as missing data — substituting the current value is the lookahead this
    function exists to prevent, and it is invisible in results.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    known = [
        item
        for item in revisions
        if item.symbol == symbol and item.trading_day == trading_day and item.known_at <= as_of
    ]
    if not known:
        return None
    return max(known, key=lambda item: item.known_at).close
