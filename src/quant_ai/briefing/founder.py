from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant_ai.agents.contracts import AtlasDecision
from quant_ai.agents.health import AgentHealth
from quant_ai.briefing.models import BriefPeriod, FounderBrief, FounderGoals


def build_founder_brief(
    *,
    now: datetime,
    period: BriefPeriod,
    nav: Decimal,
    pnl: Decimal,
    drawdown: Decimal,
    cash_fraction: Decimal,
    goals: FounderGoals,
    atlas_decision: AtlasDecision,
    agent_health: AgentHealth,
) -> FounderBrief:
    critical: list[str] = []
    decisions = [item.required_decision for item in atlas_decision.founder_escalations]

    if drawdown >= goals.max_drawdown:
        critical.append("drawdown_limit_breached")
    if pnl < -(nav * goals.max_daily_loss) and period in {BriefPeriod.TEN_MINUTE, BriefPeriod.DAILY}:
        critical.append("daily_loss_limit_breached")
    if cash_fraction < goals.minimum_cash_reserve:
        critical.append("cash_reserve_below_policy")
    if agent_health.health_score < Decimal("0.75"):
        critical.append("agent_health_degraded")

    if critical:
        goal_status = "AT_RISK"
    elif pnl >= nav * goals.target_return:
        goal_status = "AHEAD"
    else:
        goal_status = "ON_TRACK"

    summary = (
        f"action={atlas_decision.action.value}",
        f"confidence={atlas_decision.confidence}",
        f"expected_return={atlas_decision.expected_return}",
        f"expected_risk={atlas_decision.expected_risk}",
    )
    return FounderBrief(
        now,
        period,
        nav,
        pnl,
        drawdown,
        cash_fraction,
        goal_status,
        tuple(critical),
        summary,
        atlas_decision.country_recommendations,
        agent_health.health_score,
        tuple(decisions),
    )
