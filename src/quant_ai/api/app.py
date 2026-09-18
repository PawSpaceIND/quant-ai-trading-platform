from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.health import assess_agent_health
from quant_ai.agents.runtime import AtlasRuntimeCoordinator
from quant_ai.api.institutional_recovery import install_recovery_routes
from quant_ai.briefing.founder import build_founder_brief
from quant_ai.briefing.models import BriefPeriod, FounderGoals
from quant_ai.config.runtime import RuntimeMode
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.geography.opportunity import CountryOpportunity
from quant_ai.integrations.readiness import blockers, readiness_for_mode
from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.security.api_keys import ApiCredential, ApiKeyRegistry
from quant_ai.security.rate_limit import SlidingWindowRateLimiter
from quant_ai.service.portfolio_service import TenantPortfolioStore
from quant_ai.service.trading_service import ServiceTradeRequest, TradingService


class HealthResponse(BaseModel):
    status: str = "ok"
    live_execution_available: bool = False


class PaperTradePayload(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    market: Market
    asset_class: AssetClass
    side: Side
    quantity: int = Field(gt=0)
    entry: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    take_profit: Decimal = Field(gt=0)
    probability: Decimal = Field(ge=0, le=1)
    expected_value: Decimal
    risk_amount: Decimal = Field(gt=0)
    strategy_id: str = Field(min_length=1, max_length=128)
    nonce: str = Field(min_length=1, max_length=128)
    portfolio_equity: Decimal = Field(gt=0)
    daily_realized_pnl: Decimal = Decimal(0)
    gross_exposure: Decimal = Field(default=Decimal(0), ge=0)


class PaperTradeResponse(BaseModel):
    approved: bool
    reason: str
    order_id: str | None = None
    average_price: Decimal | None = None


class AgentEvidencePayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    domain: AgentDomain
    subject: str = Field(min_length=1, max_length=128)
    stance: Stance
    confidence: Decimal = Field(ge=0, le=1)
    expected_return: Decimal
    expected_risk: Decimal = Field(ge=0)
    rationale: list[str] = Field(default_factory=list)
    source_freshness_seconds: int = Field(ge=0)


class CountryOpportunityPayload(BaseModel):
    country: str = Field(min_length=1, max_length=128)
    expected_return: Decimal
    expected_volatility: Decimal = Field(gt=0)
    liquidity_score: Decimal = Field(ge=0, le=1)
    accessibility_score: Decimal = Field(ge=0, le=1)
    regulatory_score: Decimal = Field(ge=0, le=1)


class AtlasCyclePayload(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    evidence: list[AgentEvidencePayload] = Field(min_length=1)
    country_opportunities: list[CountryOpportunityPayload] = Field(default_factory=list)
    incumbent_country: str = Field(default="India", min_length=1, max_length=128)


class FounderGoalsPayload(BaseModel):
    target_return: Decimal
    max_drawdown: Decimal = Field(gt=0)
    max_daily_loss: Decimal = Field(gt=0)
    minimum_cash_reserve: Decimal = Field(ge=0, le=1)




class CapitalRecommendationPayload(BaseModel):
    starting_capital: Decimal = Field(gt=0)
    confidence: Decimal = Field(ge=0, le=1)
    annualized_volatility: Decimal = Field(ge=0)
    expected_edge: Decimal = Decimal(0)
    current_drawdown: Decimal = Field(default=Decimal(0), ge=0)
    liquidity_score: Decimal = Field(default=Decimal(1), ge=0, le=1)
    requested_mode: str | None = None
    reference_price: Decimal | None = Field(default=None, gt=0)


class FounderBriefPayload(BaseModel):
    period: BriefPeriod
    nav: Decimal = Field(gt=0)
    pnl: Decimal
    drawdown: Decimal = Field(ge=0)
    cash_fraction: Decimal = Field(ge=0, le=1)
    goals: FounderGoalsPayload


@dataclass
class ApiState:
    keys: ApiKeyRegistry
    rate_limiter: SlidingWindowRateLimiter
    trading: TradingService
    portfolios: TenantPortfolioStore
    configured_integrations: set[str]
    atlas_runtimes: dict[str, AtlasRuntimeCoordinator] = field(default_factory=dict)
    latest_evidence: dict[str, tuple[AgentEvidence, ...]] = field(default_factory=dict)
    institutional_recovery: dict[str, InstitutionalRecoveryOperations] = field(default_factory=dict)


def create_app(state: ApiState | None = None) -> FastAPI:
    runtime = state or ApiState(
        ApiKeyRegistry(),
        SlidingWindowRateLimiter(120, timedelta(minutes=1)),
        TradingService(),
        TenantPortfolioStore(),
        {"paper_broker"},
    )
    app = FastAPI(title="Quant AI Trading Platform", version="0.1.0")
    app.state.quant = runtime

    def authenticated_tenant(x_api_key: str | None = Header(default=None)) -> ApiCredential:
        if not x_api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_api_key")
        credential = runtime.keys.authenticate(x_api_key)
        if credential is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")
        if not runtime.rate_limiter.allow(credential.tenant_id):
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limit_exceeded")
        return credential

    install_recovery_routes(app, runtime, authenticated_tenant)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/v1/readiness")
    def readiness(credential: ApiCredential = Depends(authenticated_tenant)) -> dict[str, object]:
        del credential
        statuses = readiness_for_mode(RuntimeMode.PAPER, runtime.configured_integrations)
        return {
            "mode": RuntimeMode.PAPER.value,
            "ready": not blockers(statuses),
            "blockers": blockers(statuses),
        }

    @app.get("/v1/usage")
    def usage(credential: ApiCredential = Depends(authenticated_tenant)) -> dict[str, object]:
        records = runtime.trading.meter.records(credential.tenant_id)
        return {"tenant_id": credential.tenant_id, "usage": [record.__dict__ for record in records]}

    @app.get("/v1/portfolio")
    def portfolio(credential: ApiCredential = Depends(authenticated_tenant)) -> dict[str, object]:
        rows = runtime.portfolios.list_for_tenant(credential.tenant_id)
        return {"tenant_id": credential.tenant_id, "positions": [row.__dict__ for row in rows]}

    def atlas_runtime(tenant_id: str) -> AtlasRuntimeCoordinator:
        return runtime.atlas_runtimes.setdefault(tenant_id, AtlasRuntimeCoordinator())

    @app.get("/v1/atlas/status")
    def atlas_status(credential: ApiCredential = Depends(authenticated_tenant)) -> dict[str, object]:
        coordinator = atlas_runtime(credential.tenant_id)
        status_snapshot = coordinator.status(runtime.latest_evidence.get(credential.tenant_id, ()))
        return {
            "ready": status_snapshot.ready,
            "missing_domains": [item.value for item in status_snapshot.missing_domains],
            "next_cycle_at": status_snapshot.next_cycle_at,
            "cycle_count": status_snapshot.cycle_count,
            "consensus_stability": coordinator.consensus_stability(),
        }

    @app.post("/v1/atlas/cycle")
    def atlas_cycle(
        payload: AtlasCyclePayload,
        credential: ApiCredential = Depends(authenticated_tenant),
    ) -> dict[str, object]:
        observed_at = datetime.now(timezone.utc)
        evidence = tuple(
            AgentEvidence(
                item.agent_id, item.domain, item.subject, item.stance, item.confidence,
                item.expected_return, item.expected_risk, tuple(item.rationale), observed_at,
                item.source_freshness_seconds,
            )
            for item in payload.evidence
        )
        if any(item.subject != payload.subject for item in evidence):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mixed_subject_evidence")
        countries = tuple(
            CountryOpportunity(
                item.country, item.expected_return, item.expected_volatility, item.liquidity_score,
                item.accessibility_score, item.regulatory_score,
            )
            for item in payload.country_opportunities
        )
        runtime.latest_evidence[credential.tenant_id] = evidence
        coordinator = atlas_runtime(credential.tenant_id)
        try:
            decision = coordinator.run_cycle(
                payload.subject, evidence, observed_at, country_opportunities=countries,
                incumbent_country=payload.incumbent_country,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return json.loads(decision.to_json())

    @app.get("/v1/atlas/history")
    def atlas_history(credential: ApiCredential = Depends(authenticated_tenant)) -> dict[str, object]:
        decisions = atlas_runtime(credential.tenant_id).recent_decisions()
        return {"decisions": [json.loads(item.to_json()) for item in decisions]}

    @app.post("/v1/founder/brief")
    def founder_brief(
        payload: FounderBriefPayload,
        credential: ApiCredential = Depends(authenticated_tenant),
    ) -> dict[str, object]:
        coordinator = atlas_runtime(credential.tenant_id)
        history = coordinator.recent_decisions(1)
        evidence = runtime.latest_evidence.get(credential.tenant_id, ())
        if not history:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="atlas_cycle_required")
        health_snapshot = assess_agent_health(evidence)
        brief = build_founder_brief(
            now=datetime.now(timezone.utc),
            period=payload.period,
            nav=payload.nav,
            pnl=payload.pnl,
            drawdown=payload.drawdown,
            cash_fraction=payload.cash_fraction,
            goals=FounderGoals(
                payload.goals.target_return, payload.goals.max_drawdown,
                payload.goals.max_daily_loss, payload.goals.minimum_cash_reserve,
            ),
            atlas_decision=history[-1],
            agent_health=health_snapshot,
        )
        return json.loads(brief.to_json())


    @app.post("/v1/capital/recommendation")
    def capital_recommendation(
        payload: CapitalRecommendationPayload,
        credential: ApiCredential = Depends(authenticated_tenant),
    ) -> dict[str, object]:
        del credential
        requested_mode = None
        if payload.requested_mode is not None:
            try:
                from quant_ai.domain.models import RiskMode

                requested_mode = RiskMode(payload.requested_mode)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid_requested_mode",
                ) from exc
        plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
            payload.starting_capital,
            payload.confidence,
            payload.annualized_volatility,
            payload.expected_edge,
            payload.current_drawdown,
            payload.liquidity_score,
            requested_mode,
        ))
        result: dict[str, object] = {
            "starting_capital": str(plan.starting_capital),
            "recommended_mode": plan.recommended_mode.value,
            "per_trade_risk_fraction": str(plan.per_trade_risk_fraction),
            "per_trade_risk_amount": str(plan.per_trade_risk_amount),
            "max_daily_loss_fraction": str(plan.max_daily_loss_fraction),
            "max_daily_loss_amount": str(plan.max_daily_loss_amount),
            "max_drawdown_fraction": str(plan.max_drawdown_fraction),
            "max_drawdown_amount": str(plan.max_drawdown_amount),
            "max_position_fraction": str(plan.max_position_fraction),
            "max_position_amount": str(plan.max_position_amount),
            "max_country_allocation_fraction": str(plan.max_country_allocation_fraction),
            "max_gross_exposure_fraction": str(plan.max_gross_exposure_fraction),
            "cash_reserve_fraction": str(plan.cash_reserve_fraction),
            "stop_loss_fraction": str(plan.stop_loss_fraction),
            "take_profit_fraction": str(plan.take_profit_fraction),
            "reward_risk_ratio": str(plan.reward_risk_ratio),
            "trading_allowed": plan.trading_allowed,
            "rationale": list(plan.rationale),
            "goals": {
                "daily": {"floor": str(plan.daily_goal.floor), "target": str(plan.daily_goal.target), "stretch": str(plan.daily_goal.stretch), "mandatory": False},
                "weekly": {"floor": str(plan.weekly_goal.floor), "target": str(plan.weekly_goal.target), "stretch": str(plan.weekly_goal.stretch), "mandatory": False},
                "monthly": {"floor": str(plan.monthly_goal.floor), "target": str(plan.monthly_goal.target), "stretch": str(plan.monthly_goal.stretch), "mandatory": False},
                "yearly": {"floor": str(plan.yearly_goal.floor), "target": str(plan.yearly_goal.target), "stretch": str(plan.yearly_goal.stretch), "mandatory": False},
            },
        }
        if payload.reference_price is not None:
            result["recommended_quantity"] = plan.quantity_for_price(payload.reference_price)
        return result

    @app.post("/v1/paper/trades", response_model=PaperTradeResponse)
    def submit_paper_trade(
        payload: PaperTradePayload,
        credential: ApiCredential = Depends(authenticated_tenant),
    ) -> PaperTradeResponse:
        request = ServiceTradeRequest(
            credential.tenant_id,
            payload.symbol,
            payload.market,
            payload.asset_class,
            payload.side,
            payload.quantity,
            payload.entry,
            payload.stop,
            payload.take_profit,
            payload.probability,
            payload.expected_value,
            payload.risk_amount,
            payload.strategy_id,
            payload.nonce,
        )
        snapshot = PortfolioSnapshot(
            payload.portfolio_equity,
            payload.daily_realized_pnl,
            payload.gross_exposure,
        )
        result = runtime.trading.submit_paper(request, snapshot)
        return PaperTradeResponse(
            approved=result.approved,
            reason=result.reason,
            order_id=result.fill.order_id if result.fill else None,
            average_price=result.fill.average_price if result.fill else None,
        )

    return app


app = create_app()
