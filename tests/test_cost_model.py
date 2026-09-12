from decimal import Decimal

from quant_ai.backtest.costs import CostModel


def test_cost_model_combines_components() -> None:
    model = CostModel(Decimal(1), Decimal(2), Decimal(1))
    assert model.one_way_cost(Decimal(10000)) == Decimal(4)
