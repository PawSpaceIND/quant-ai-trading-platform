"""Explicit option exercise/assignment economics and broker-sourced spread margin."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from quant_ai.derivatives.options import OptionContract, OptionType


class SettlementStyle(str, Enum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


@dataclass(frozen=True)
class ExpiryObligation:
    symbol: str
    signed_contracts: int
    cash_delta: Decimal
    underlying_delta: Decimal
    exercised: bool
    settlement_style: SettlementStyle


def expiry_obligation(
    contract: OptionContract,
    *,
    signed_contracts: int,
    settlement_spot: Decimal,
    settlement_style: SettlementStyle,
    exercise_or_assign: bool,
) -> ExpiryObligation:
    """Economic result *if* exercise/assignment is instructed.

    The module deliberately does not auto-exercise. Broker/exchange thresholds and exercise
    policy must be supplied by the caller. Positive contracts are long, negative are short.
    """
    if type(signed_contracts) is not int or signed_contracts == 0:
        raise ValueError("option_signed_contracts_must_be_nonzero_integer")
    if not settlement_spot.is_finite() or settlement_spot <= 0:
        raise ValueError("option_settlement_spot_must_be_positive")
    intrinsic = contract.intrinsic_value(settlement_spot)
    if not exercise_or_assign or intrinsic == 0:
        return ExpiryObligation(
            contract.symbol, signed_contracts, Decimal(0), Decimal(0), False, settlement_style
        )
    units = Decimal(signed_contracts) * contract.multiplier
    if settlement_style is SettlementStyle.CASH:
        return ExpiryObligation(
            contract.symbol, signed_contracts, intrinsic * units, Decimal(0), True,
            settlement_style,
        )
    if contract.option_type is OptionType.CALL:
        underlying = units
        cash = -contract.strike * units
    else:
        underlying = -units
        cash = contract.strike * units
    return ExpiryObligation(
        contract.symbol, signed_contracts, cash, underlying, True, settlement_style
    )


@dataclass(frozen=True)
class SpreadMarginEvidence:
    strategy_reference: str
    required_margin: Decimal
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.strategy_reference.strip() or not self.source.strip():
            raise ValueError("option_spread_margin_identity_source_required")
        if not self.required_margin.is_finite() or self.required_margin <= 0:
            raise ValueError("option_spread_margin_must_be_positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("option_spread_margin_time_must_be_timezone_aware")
