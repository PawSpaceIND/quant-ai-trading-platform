from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.paper_broker import PaperBroker


def test_paper_buy_adds_slippage() -> None:
    order = OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "demo")
    fill = PaperBroker(Decimal(10)).submit(order)
    assert fill.status == "FILLED"
    assert fill.average_price == Decimal("100.100")
    assert fill.order_id.startswith("PAPER-")
