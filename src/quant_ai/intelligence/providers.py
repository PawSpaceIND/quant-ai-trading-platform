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
