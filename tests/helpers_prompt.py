"""Build a consensus prompt carrying supplied lessons, for evidence-block assertions."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.atlas import _atlas_prompt
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, EvidenceContext, Stance

NOW = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)


def prompt_with_lessons(lessons: tuple[str, ...]) -> str:
    evidence = (
        AgentEvidence(
            "technical", AgentDomain.TECHNICAL, "INFY", Stance.BUY, Decimal("0.6"),
            Decimal("0.01"), Decimal("0.005"), ("momentum",), NOW, 30,
        ),
    )
    return _atlas_prompt("INFY", evidence, None, context=EvidenceContext(lessons=lessons))
