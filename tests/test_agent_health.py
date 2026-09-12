from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.health import assess_agent_health


def evidence(agent_id: str, confidence: str, freshness: int) -> AgentEvidence:
    return AgentEvidence(
        agent_id, AgentDomain.NEWS, "AAPL", Stance.BUY, Decimal(confidence), Decimal("0.02"),
        Decimal("0.01"), ("signal",), datetime.now(timezone.utc), freshness,
    )


def test_health_detects_stale_and_low_confidence_agents() -> None:
    health = assess_agent_health((evidence("a", "0.8", 100), evidence("b", "0.2", 100), evidence("c", "0.8", 7200)))
    assert health.healthy_agents == 1
    assert health.low_confidence_agents == ("b",)
    assert health.stale_agents == ("c",)
