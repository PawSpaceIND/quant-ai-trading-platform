from decimal import Decimal

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    Opportunity,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.portfolio.sizing import PositionSizer


def test_position_size_respects_risk_budget_and_notional_cap() -> None:
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    opportunity = Opportunity(
        instrument, Side.BUY, Decimal("0.70"), Decimal("0.02"), Decimal("0.005"),
        Decimal("0.015"), Decimal("0.8"), Decimal("0.02"), Decimal("0.04"), (),
    )
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))
    quantity = PositionSizer().quantity(opportunity, portfolio, Decimal(100), RiskMode.BALANCED)
    assert quantity == 100
