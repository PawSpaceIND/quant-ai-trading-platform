from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from quant_ai.config.runtime import RuntimeMode
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.integrations.readiness import blockers, readiness_for_mode
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


@dataclass
class ApiState:
    keys: ApiKeyRegistry
    rate_limiter: SlidingWindowRateLimiter
    trading: TradingService
    portfolios: TenantPortfolioStore
    configured_integrations: set[str]


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
