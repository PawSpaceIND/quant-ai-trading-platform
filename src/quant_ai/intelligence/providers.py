from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class NewsSignal:
    subject: str
    headline: str
    sentiment: Decimal
    source: str
    published_at: datetime


@dataclass(frozen=True)
class FundamentalSnapshot:
    subject: str
    metrics: dict[str, Decimal]
    observed_at: datetime


@dataclass(frozen=True)
class MacroSnapshot:
    indicators: dict[str, Decimal]
    observed_at: datetime


class NewsSentimentProvider(Protocol):
    provider_id: str

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]: ...


class FundamentalDataProvider(Protocol):
    provider_id: str

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot: ...


class MacroIndicatorProvider(Protocol):
    provider_id: str

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot: ...
