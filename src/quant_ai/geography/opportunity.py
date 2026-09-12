from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CountryOpportunity:
    country: str
    expected_return: Decimal
    expected_volatility: Decimal
    liquidity_score: Decimal
    accessibility_score: Decimal
    regulatory_score: Decimal

    @property
    def risk_adjusted_score(self) -> Decimal:
        if self.expected_volatility <= 0:
            return Decimal(0)
        quality = (self.liquidity_score + self.accessibility_score + self.regulatory_score) / Decimal(3)
        return (self.expected_return / self.expected_volatility) * quality


def rank_countries(items: tuple[CountryOpportunity, ...]) -> tuple[CountryOpportunity, ...]:
    return tuple(sorted(items, key=lambda item: item.risk_adjusted_score, reverse=True))


def expansion_candidates(
    items: tuple[CountryOpportunity, ...],
    incumbent_country: str,
    improvement_threshold: Decimal = Decimal("0.20"),
) -> tuple[CountryOpportunity, ...]:
    ranked = rank_countries(items)
    incumbent = next((item for item in items if item.country == incumbent_country), None)
    if incumbent is None:
        return ranked
    hurdle = incumbent.risk_adjusted_score * (Decimal(1) + improvement_threshold)
    return tuple(item for item in ranked if item.country != incumbent_country and item.risk_adjusted_score > hurdle)
