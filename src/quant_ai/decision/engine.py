from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_ai.decision.record import DecisionRecord
from quant_ai.domain.models import Side


@dataclass(frozen=True)
class DecisionInput:
    symbol: str
    side: Side
    strategy_id: str
    probability: Decimal
    expected_value: Decimal
    risk_amount: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    model_versions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionPolicy:
    min_probability: Decimal = Decimal("0.55")
    min_expected_value: Decimal = Decimal(0)
    max_risk_amount: Decimal = Decimal(10000)


class DecisionEngine:
    def __init__(self, policy: DecisionPolicy | None = None) -> None:
        self.policy = policy or DecisionPolicy()

    def evaluate(self, item: DecisionInput) -> DecisionRecord:
        reasons: list[str] = []
        if item.probability < self.policy.min_probability:
            reasons.append("probability_below_threshold")
        if item.expected_value <= self.policy.min_expected_value:
            reasons.append("expected_value_not_positive")
        if item.risk_amount <= 0 or item.risk_amount > self.policy.max_risk_amount:
            reasons.append("risk_amount_out_of_bounds")
        if item.side == Side.BUY and not item.stop_price < item.take_profit_price:
            reasons.append("invalid_buy_exit_geometry")
        if item.side == Side.SELL and not item.stop_price > item.take_profit_price:
            reasons.append("invalid_sell_exit_geometry")
        return DecisionRecord(
            str(uuid4()),
            datetime.now(timezone.utc),
            item.symbol,
            item.side,
            item.strategy_id,
            item.model_versions,
            item.probability,
            item.expected_value,
            item.risk_amount,
            item.stop_price,
            item.take_profit_price,
            not reasons,
            tuple(reasons),
        )
