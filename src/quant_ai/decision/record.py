from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.domain.models import Side


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    timestamp: datetime
    symbol: str
    side: Side
    strategy_id: str
    model_versions: tuple[str, ...]
    probability: Decimal
    expected_value: Decimal
    risk_amount: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    approved: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("decision timestamp must be timezone-aware")
        if not Decimal(0) <= self.probability <= Decimal(1):
            raise ValueError("probability must be between zero and one")
        if self.risk_amount < 0:
            raise ValueError("risk amount cannot be negative")
