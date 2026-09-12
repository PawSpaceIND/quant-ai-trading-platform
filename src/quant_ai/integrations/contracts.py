from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IntegrationKind(str, Enum):
    MARKET_DATA = "MARKET_DATA"
    BROKER = "BROKER"
    NEWS = "NEWS"
    FUNDAMENTALS = "FUNDAMENTALS"
    AI_MODEL = "AI_MODEL"


@dataclass(frozen=True)
class IntegrationRequirement:
    name: str
    kind: IntegrationKind
    required_for_paper: bool
    required_for_live: bool
    secret_names: tuple[str, ...] = ()
    jurisdiction: str | None = None


CORE_REQUIREMENTS = (
    IntegrationRequirement("primary_market_data", IntegrationKind.MARKET_DATA, True, True, ("MARKET_DATA_API_KEY",)),
    IntegrationRequirement("paper_broker", IntegrationKind.BROKER, True, False),
    IntegrationRequirement("live_broker_india", IntegrationKind.BROKER, False, True, ("INDIA_BROKER_API_KEY", "INDIA_BROKER_API_SECRET"), "IN"),
    IntegrationRequirement("live_broker_us", IntegrationKind.BROKER, False, True, ("US_BROKER_API_KEY", "US_BROKER_API_SECRET"), "US"),
    IntegrationRequirement("news_provider", IntegrationKind.NEWS, False, False, ("NEWS_API_KEY",)),
    IntegrationRequirement("fundamentals_provider", IntegrationKind.FUNDAMENTALS, False, False, ("FUNDAMENTALS_API_KEY",)),
    IntegrationRequirement("llm_provider", IntegrationKind.AI_MODEL, False, False, ("AI_API_KEY",)),
)
