from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class CorporateAction:
    split_ratio: Decimal = Decimal(1)
    cash_dividend: Decimal = Decimal(0)


def adjust_candle(candle: Candle, action: CorporateAction) -> Candle:
    if action.split_ratio <= 0 or action.cash_dividend < 0:
        raise ValueError("invalid corporate action")
    ratio = action.split_ratio
    dividend = action.cash_dividend
    return Candle(
        candle.instrument,
        candle.timestamp,
        (candle.open - dividend) / ratio,
        (candle.high - dividend) / ratio,
        (candle.low - dividend) / ratio,
        (candle.close - dividend) / ratio,
        candle.volume * ratio,
    )
