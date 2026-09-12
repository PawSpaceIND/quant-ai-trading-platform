from datetime import timedelta

from fastapi.testclient import TestClient

from quant_ai.api.app import ApiState, create_app
from quant_ai.security.api_keys import ApiKeyRegistry
from quant_ai.security.rate_limit import SlidingWindowRateLimiter
from quant_ai.service.portfolio_service import TenantPortfolioStore
from quant_ai.service.trading_service import TradingService


def client_with_key() -> tuple[TestClient, str]:
    keys = ApiKeyRegistry()
    raw, _ = keys.issue("tenant-a")
    state = ApiState(
        keys,
        SlidingWindowRateLimiter(100, timedelta(minutes=1)),
        TradingService(),
        TenantPortfolioStore(),
        {"primary_market_data", "paper_broker"},
    )
    return TestClient(create_app(state)), raw


def test_health_is_public_and_live_is_unavailable() -> None:
    client, _ = client_with_key()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["live_execution_available"] is False


def test_private_endpoints_require_api_key() -> None:
    client, _ = client_with_key()
    assert client.get("/v1/usage").status_code == 401


def test_readiness_and_paper_trade() -> None:
    client, key = client_with_key()
    headers = {"X-API-Key": key}
    readiness = client.get("/v1/readiness", headers=headers)
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    payload = {
        "symbol": "AAPL",
        "market": "USA",
        "asset_class": "EQUITY",
        "side": "BUY",
        "quantity": 10,
        "entry": "100",
        "stop": "95",
        "take_profit": "110",
        "probability": "0.70",
        "expected_value": "25",
        "risk_amount": "500",
        "strategy_id": "momentum",
        "nonce": "api-1",
        "portfolio_equity": "100000",
        "daily_realized_pnl": "0",
        "gross_exposure": "0"
    }
    response = client.post("/v1/paper/trades", headers=headers, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["approved"] is True
    assert body["order_id"].startswith("PAPER-")
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["usage"][0]["count"] == 1


def test_no_live_trade_route_exists() -> None:
    client, key = client_with_key()
    response = client.post("/v1/live/trades", headers={"X-API-Key": key}, json={})
    assert response.status_code == 404


def atlas_cycle_payload() -> dict[str, object]:
    domains = [
        "TECHNICAL", "NEWS", "MACRO", "COUNTRY", "DERIVATIVES", "LIQUIDITY", "RISK", "PORTFOLIO"
    ]
    return {
        "subject": "AAPL",
        "evidence": [
            {
                "agent_id": domain.lower(),
                "domain": domain,
                "subject": "AAPL",
                "stance": "BUY",
                "confidence": "0.75",
                "expected_return": "0.03",
                "expected_risk": "0.02",
                "rationale": ["signal"],
                "source_freshness_seconds": 60,
            }
            for domain in domains
        ],
        "country_opportunities": [],
        "incumbent_country": "India",
    }


def test_atlas_control_plane_and_founder_brief() -> None:
    client, key = client_with_key()
    headers = {"X-API-Key": key}
    before = client.get("/v1/atlas/status", headers=headers).json()
    assert before["ready"] is False
    cycle = client.post("/v1/atlas/cycle", headers=headers, json=atlas_cycle_payload())
    assert cycle.status_code == 200
    assert cycle.json()["subject"] == "AAPL"
    after = client.get("/v1/atlas/status", headers=headers).json()
    assert after["ready"] is True
    assert after["cycle_count"] == 1
    repeated = client.post("/v1/atlas/cycle", headers=headers, json=atlas_cycle_payload())
    assert repeated.status_code == 409
    history = client.get("/v1/atlas/history", headers=headers).json()
    assert len(history["decisions"]) == 1
    brief_payload = {
        "period": "DAILY",
        "nav": "100000",
        "pnl": "1500",
        "drawdown": "0.02",
        "cash_fraction": "0.20",
        "goals": {
            "target_return": "0.01",
            "max_drawdown": "0.10",
            "max_daily_loss": "0.02",
            "minimum_cash_reserve": "0.10",
        },
    }
    brief = client.post("/v1/founder/brief", headers=headers, json=brief_payload)
    assert brief.status_code == 200
    assert brief.json()["period"] == "DAILY"


def test_dynamic_capital_recommendation_endpoint() -> None:
    client, key = client_with_key()
    headers = {"X-API-Key": key}
    payload = {
        "starting_capital": "100000",
        "confidence": "0.70",
        "annualized_volatility": "0.20",
        "expected_edge": "0.01",
        "current_drawdown": "0.01",
        "liquidity_score": "0.90",
        "reference_price": "1000",
    }
    response = client.post("/v1/capital/recommendation", headers=headers, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["recommended_mode"] == "BALANCED"
    assert body["recommended_quantity"] > 0
    assert body["goals"]["daily"]["mandatory"] is False
    assert float(body["take_profit_fraction"]) > float(body["stop_loss_fraction"])


def test_capital_recommendation_can_refuse_trading() -> None:
    client, key = client_with_key()
    headers = {"X-API-Key": key}
    response = client.post("/v1/capital/recommendation", headers=headers, json={
        "starting_capital": "50000",
        "confidence": "0.30",
        "annualized_volatility": "0.60",
        "current_drawdown": "0.08",
        "liquidity_score": "0.40"
    })
    assert response.status_code == 200
    body = response.json()
    assert body["trading_allowed"] is False
    assert body["goals"]["yearly"]["target"] == "0"
