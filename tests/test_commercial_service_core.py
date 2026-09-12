from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.security.api_keys import ApiKeyRegistry
from quant_ai.security.rate_limit import SlidingWindowRateLimiter
from quant_ai.service.portfolio_service import TenantPortfolioStore, TenantPosition
from quant_ai.service.trading_service import ServiceTradeRequest, TradingService


def test_api_key_authentication_and_tenant_isolation() -> None:
    registry = ApiKeyRegistry()
    raw, credential = registry.issue("tenant-a")
    assert registry.authenticate(raw) == credential
    assert registry.authenticate("wrong") is None
    store = TenantPortfolioStore()
    store.upsert(TenantPosition("tenant-a", "AAPL", 1, Decimal(100)))
    store.upsert(TenantPosition("tenant-b", "MSFT", 2, Decimal(200)))
    assert [p.symbol for p in store.list_for_tenant("tenant-a")] == ["AAPL"]


def test_sliding_window_rate_limit() -> None:
    limiter = SlidingWindowRateLimiter(2, timedelta(seconds=60))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert limiter.allow("tenant", now)
    assert limiter.allow("tenant", now)
    assert not limiter.allow("tenant", now)


def test_tenant_scoped_paper_service_and_usage() -> None:
    service = TradingService()
    request = ServiceTradeRequest(
        "tenant-a", "AAPL", Market.USA, AssetClass.EQUITY, Side.BUY, 10,
        Decimal(100), Decimal(95), Decimal(110), Decimal("0.70"), Decimal(25),
        Decimal(500), "momentum", "1",
    )
    result = service.submit_paper(request, PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0)))
    assert result.approved
    assert service.meter.get("tenant-a", "paper_trade_requests") == 1
