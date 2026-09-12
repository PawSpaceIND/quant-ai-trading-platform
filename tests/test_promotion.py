from decimal import Decimal

from quant_ai.validation.promotion import StrategyEvidence, evaluate_promotion


def test_strong_strategy_can_promote() -> None:
    evidence = StrategyEvidence(200, Decimal(10), Decimal("0.05"), Decimal("1.6"), 3, 60)
    assert evaluate_promotion(evidence).approved


def test_weak_strategy_is_blocked() -> None:
    evidence = StrategyEvidence(10, Decimal(-1), Decimal("0.20"), Decimal("0.8"), 1, 3)
    decision = evaluate_promotion(evidence)
    assert not decision.approved
    assert "insufficient_trade_sample" in decision.reasons
