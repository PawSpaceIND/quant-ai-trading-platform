from decimal import Decimal

import pytest

from quant_ai.brokers.router import BrokerRouter
from quant_ai.compliance.gates import ComplianceContext, ExecutionMode, execution_allowed
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.paper_broker import PaperBroker


def order() -> OrderIntent:
    return OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "test")


def test_paper_is_allowed() -> None:
    ctx = ComplianceContext(Market.USA, AssetClass.EQUITY, ExecutionMode.PAPER)
    assert execution_allowed(ctx)
    assert BrokerRouter(PaperBroker()).submit(order(), ctx).status == "FILLED"


def test_live_fails_closed_without_approvals() -> None:
    ctx = ComplianceContext(Market.USA, AssetClass.EQUITY, ExecutionMode.LIVE)
    with pytest.raises(PermissionError):
        BrokerRouter(PaperBroker()).submit(order(), ctx)
