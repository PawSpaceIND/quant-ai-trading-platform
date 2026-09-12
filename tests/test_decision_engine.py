from decimal import Decimal

from quant_ai.decision.engine import DecisionEngine, DecisionInput
from quant_ai.domain.models import Side


def test_high_quality_decision_is_approved() -> None:
    item = DecisionInput("AAPL", Side.BUY, "momentum", Decimal("0.70"), Decimal(25), Decimal(1000), Decimal(95), Decimal(110), ("m1",))
    record = DecisionEngine().evaluate(item)
    assert record.approved
    assert not record.reasons


def test_bad_expected_value_is_rejected() -> None:
    item = DecisionInput("AAPL", Side.BUY, "momentum", Decimal("0.70"), Decimal(-1), Decimal(1000), Decimal(95), Decimal(110))
    record = DecisionEngine().evaluate(item)
    assert not record.approved
    assert "expected_value_not_positive" in record.reasons
