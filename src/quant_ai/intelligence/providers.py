from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class NewsSignal:
    """One headline and the instrument it was attributed to.

    ``matched_alias`` records how that attribution was made. ``None`` means the text named
    ``subject`` itself: the link was observed. A string means the link came from an
    operator-configured alias - somebody asserted that this name refers to this instrument,
    and nothing in the platform verified it. The two are not the same quality of evidence,
    so they are not collapsed into one; the distinction travels to the proof, where a
    reader can see which headlines reached a decision on an assertion.
    """

    subject: str
    headline: str
    sentiment: Decimal
    source: str
    published_at: datetime
    matched_alias: str | None = None


@dataclass(frozen=True)
class FundamentalSnapshot:
    subject: str
    metrics: dict[str, Decimal]
    observed_at: datetime


# The indicators every macro request asks for and the freshness gate requires in full.
# One retired upstream series answers HTTP 400 and, because the provider fails closed, blanks
# the whole snapshot; every specialist that reads macro then reports zero. So the set lives
# here, once, and a series that FRED retires is replaced here rather than left to fail the
# fetch. INDIA10Y left this set in September 2026: FRED retired the series behind it, no
# metric ever read it, and as a monthly series it aged the whole snapshot.
MACRO_CORE_INDICATORS: tuple[str, ...] = ("US10Y", "BRENT", "GOLD", "USD_BROAD")

# India-specific context the macro snapshot carries on top of the core set. None of these is
# required for macro freshness: the core set decides whether macro is present at all, and a
# missing India series simply leaves its metric out, so a specialist can tell "not observed"
# from a value. INDIA_VIX is NSE's 30-day implied volatility index and its previous close,
# both from Yahoo's ^INDIAVIX daily bars. The flow figures are NSE's provisional daily net
# purchases by foreign (FII/FPI) and domestic (DII) institutions in the cash market, in
# rupees crore, published after the close for the same day.
INDIA_MACRO_INDICATORS: tuple[str, ...] = (
    "INDIA_VIX",
    "INDIA_VIX_PREV_CLOSE",
    "FII_NET_CRORE",
    "DII_NET_CRORE",
)
# What the pipeline asks the macro provider for on every cycle.
MACRO_INDICATORS: tuple[str, ...] = MACRO_CORE_INDICATORS + INDIA_MACRO_INDICATORS


@dataclass(frozen=True)
class MacroSnapshot:
    indicators: dict[str, Decimal]
    # Preserve the latest-observation version/change clock.
    observed_at: datetime
    oldest_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.oldest_observed_at is None:
            return  # Preserve legacy two-field snapshots.
        for stamp in (self.observed_at, self.oldest_observed_at):
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("macro_snapshot_clock_invalid")
        if self.oldest_observed_at > self.observed_at:
            raise ValueError("macro_snapshot_oldest_after_latest")
    @property
    def freshness_observed_at(self) -> datetime:
        return self.observed_at if self.oldest_observed_at is None else self.oldest_observed_at


class NewsSentimentProvider(Protocol):
    provider_id: str

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]: ...


class FundamentalDataProvider(Protocol):
    provider_id: str

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot: ...


class MacroIndicatorProvider(Protocol):
    provider_id: str

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot: ...
