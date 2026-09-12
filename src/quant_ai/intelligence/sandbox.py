from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.intelligence.providers import (
    FundamentalSnapshot,
    MacroSnapshot,
    NewsSignal,
)


@dataclass(frozen=True)
class SandboxNewsSentimentProvider:
    provider_id: str = "sandbox-news"
    age_seconds: int = 0

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]:
        profiles = {
            "GEOPOLITICAL": ("trade tensions ease", Decimal("0.35")),
            "GOLD": ("safe haven demand steady", Decimal("0.20")),
            "CRUDE": ("supply discipline supports crude", Decimal("0.25")),
            "RELIANCE": ("refining margins improve", Decimal("0.40")),
            "TCS": ("deal pipeline remains healthy", Decimal("0.35")),
            "AAPL": ("services demand resilient", Decimal("0.30")),
            "NVDA": ("AI infrastructure demand strong", Decimal("0.55")),
        }
        headline, sentiment = profiles.get(subject.upper(), ("market conditions mixed", Decimal(0)))
        return (NewsSignal(subject, headline, sentiment, self.provider_id, now - timedelta(seconds=self.age_seconds)),)


@dataclass(frozen=True)
class SandboxFundamentalDataProvider:
    provider_id: str = "sandbox-fundamentals"
    age_seconds: int = 0

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot:
        metrics = {
            "RELIANCE": {"pe": Decimal(24), "debt_equity": Decimal("0.42"), "operating_margin": Decimal("0.17"), "fcf_yield": Decimal("0.035")},
            "TCS": {"pe": Decimal(29), "debt_equity": Decimal("0.08"), "operating_margin": Decimal("0.25"), "fcf_yield": Decimal("0.038")},
            "AAPL": {"pe": Decimal(31), "debt_equity": Decimal("1.45"), "operating_margin": Decimal("0.30"), "fcf_yield": Decimal("0.032")},
            "NVDA": {"pe": Decimal(42), "debt_equity": Decimal("0.22"), "operating_margin": Decimal("0.58"), "fcf_yield": Decimal("0.028")},
        }
        return FundamentalSnapshot(subject, metrics.get(subject.upper(), {}), now - timedelta(seconds=self.age_seconds))


@dataclass(frozen=True)
class SandboxMacroIndicatorProvider:
    provider_id: str = "sandbox-macro"
    age_seconds: int = 0

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        values = {
            "US10Y": Decimal("4.10"),
            "INDIA10Y": Decimal("6.85"),
            "BRENT": Decimal(78),
            "GOLD": Decimal(2450),
            "DXY": Decimal(103),
        }
        return MacroSnapshot({key: values[key] for key in indicators if key in values}, now - timedelta(seconds=self.age_seconds))
