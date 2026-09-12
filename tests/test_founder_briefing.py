from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.contracts import AtlasDecision, Stance
from quant_ai.agents.health import assess_agent_health
from quant_ai.briefing.founder import build_founder_brief
from quant_ai.briefing.models import BriefPeriod, FounderGoals
from quant_ai.governance.escalation import EscalationPriority, classify_founder_contact


def atlas() -> AtlasDecision:
    return AtlasDecision(
        "cycle-1", datetime.now(timezone.utc), Stance.BUY, "AAPL", Decimal("0.72"),
        Decimal("0.03"), Decimal("0.02"), ("technical", "news"), (), ("consensus",),
        ("Japan",), (), False,
    )


def test_brief_flags_drawdown_and_requires_immediate_contact() -> None:
    health = assess_agent_health(())
    brief = build_founder_brief(
        now=datetime.now(timezone.utc), period=BriefPeriod.DAILY,
        nav=Decimal(100000), pnl=Decimal(-3000), drawdown=Decimal("0.12"),
        cash_fraction=Decimal("0.20"),
        goals=FounderGoals(Decimal("0.01"), Decimal("0.10"), Decimal("0.02"), Decimal("0.10")),
        atlas_decision=atlas(), agent_health=health,
    )
    assert "drawdown_limit_breached" in brief.critical_actions
    escalation = classify_founder_contact(brief.critical_actions, brief.founder_decisions_required)
    assert escalation.priority == EscalationPriority.CRITICAL
    assert escalation.contact_founder_now


def test_brief_serializes_as_json() -> None:
    health = assess_agent_health(())
    brief = build_founder_brief(
        now=datetime.now(timezone.utc), period=BriefPeriod.WEEKLY,
        nav=Decimal(100000), pnl=Decimal(2000), drawdown=Decimal("0.02"),
        cash_fraction=Decimal("0.25"),
        goals=FounderGoals(Decimal("0.01"), Decimal("0.10"), Decimal("0.02"), Decimal("0.10")),
        atlas_decision=atlas(), agent_health=health,
    )
    assert '"period":"WEEKLY"' in brief.to_json()
