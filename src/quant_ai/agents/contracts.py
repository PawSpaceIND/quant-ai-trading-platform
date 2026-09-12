from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class AgentDomain(str, Enum):
    TECHNICAL = "TECHNICAL"
    NEWS = "NEWS"
    MACRO = "MACRO"
    COUNTRY = "COUNTRY"
    DERIVATIVES = "DERIVATIVES"
    LIQUIDITY = "LIQUIDITY"
    RISK = "RISK"
    PORTFOLIO = "PORTFOLIO"


class Stance(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    AVOID = "AVOID"


@dataclass(frozen=True)
class AgentEvidence:
    agent_id: str
    domain: AgentDomain
    subject: str
    stance: Stance
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    rationale: tuple[str, ...]
    observed_at: datetime
    source_freshness_seconds: int

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("confidence must be between 0 and 1")
        if self.expected_risk < 0 or self.source_freshness_seconds < 0:
            raise ValueError("risk and freshness must be non-negative")


@dataclass(frozen=True)
class FounderEscalation:
    category: str
    reason: str
    required_decision: str
    urgency: str


@dataclass(frozen=True)
class AtlasDecision:
    cycle_id: str
    generated_at: datetime
    action: Stance
    subject: str
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    supporting_agents: tuple[str, ...]
    dissenting_agents: tuple[str, ...]
    rationale: tuple[str, ...]
    country_recommendations: tuple[str, ...]
    founder_escalations: tuple[FounderEscalation, ...]
    live_execution_allowed: bool = False

    def to_json(self) -> str:
        def normalize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, tuple):
                return [normalize(item) for item in value]
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            return value

        return json.dumps(normalize(asdict(self)), sort_keys=True, separators=(",", ":"))
