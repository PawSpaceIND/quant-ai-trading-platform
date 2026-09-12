from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, Side


@dataclass(frozen=True)
class FrictionContext:
    atr: Decimal
    average_daily_volume: Decimal
    liquidity_score: Decimal = Decimal(1)
    delivery: bool = True

    def __post_init__(self) -> None:
        if self.atr < 0 or self.average_daily_volume <= 0:
            raise ValueError("atr must be non-negative and ADV must be positive")
        if not Decimal(0) < self.liquidity_score <= Decimal(1):
            raise ValueError("liquidity_score must be in (0,1]")


@dataclass(frozen=True)
class FeeSchedule:
    name: str
    india_exchange_rate: Decimal
    india_sebi_rate: Decimal
    india_gst_rate: Decimal
    india_stamp_buy_rate: Decimal
    india_stamp_intraday_buy_rate: Decimal
    india_stt_delivery_rate: Decimal
    india_stt_intraday_sell_rate: Decimal
    us_sec_sell_rate: Decimal
    us_finra_per_share: Decimal
    us_finra_max: Decimal

    @classmethod
    def current_2026(cls) -> FeeSchedule:
        return cls(
            "current_2026",
            Decimal("0.000030699"),
            Decimal("0.000001"),
            Decimal("0.18"),
            Decimal("0.00015"),
            Decimal("0.00003"),
            Decimal("0.001"),
            Decimal("0.00025"),
            Decimal("0.0000206"),
            Decimal("0.000195"),
            Decimal("9.79"),
        )

    @classmethod
    def legacy_prompt_rates(cls) -> FeeSchedule:
        return cls(
            "legacy_prompt_rates",
            Decimal("0.0000297"),
            Decimal("0.000001"),
            Decimal("0.18"),
            Decimal("0.00015"),
            Decimal("0.00003"),
            Decimal("0.001"),
            Decimal("0.00025"),
            Decimal("0.0000278"),
            Decimal("0.000166"),
            Decimal("8.30"),
        )

    @classmethod
    def zero(cls) -> FeeSchedule:
        return cls("zero", *(Decimal(0) for _ in range(10)))


@dataclass(frozen=True)
class FrictionCharge:
    code: str
    amount: Decimal


@dataclass(frozen=True)
class FrictionResult:
    reference_price: Decimal
    execution_price: Decimal
    spread_drag: Decimal
    slippage_drag: Decimal
    charges: tuple[FrictionCharge, ...]

    @property
    def statutory_fees(self) -> Decimal:
        return sum((item.amount for item in self.charges), Decimal(0))

    @property
    def total_friction(self) -> Decimal:
        return self.spread_drag + self.slippage_drag + self.statutory_fees


class MarketFrictionModel:
    """Bounded spread, square-root impact, and versioned statutory fee model."""

    def __init__(
        self,
        *,
        fee_schedule: FeeSchedule | None = None,
        gamma: Decimal = Decimal("0.50"),
        spread_atr_multiplier: Decimal = Decimal("0.05"),
        max_slippage_fraction: Decimal = Decimal("0.02"),
        max_half_spread_fraction: Decimal = Decimal("0.01"),
        fixed_slippage_bps: Decimal = Decimal(0),
    ) -> None:
        if gamma < 0 or spread_atr_multiplier < 0 or fixed_slippage_bps < 0:
            raise ValueError("friction coefficients cannot be negative")
        if not Decimal(0) <= max_slippage_fraction <= Decimal("0.10"):
            raise ValueError("max_slippage_fraction must be within 10%")
        if not Decimal(0) <= max_half_spread_fraction <= Decimal("0.05"):
            raise ValueError("max_half_spread_fraction must be within 5%")
        self.fee_schedule = fee_schedule or FeeSchedule.current_2026()
        self.gamma = gamma
        self.spread_atr_multiplier = spread_atr_multiplier
        self.max_slippage_fraction = max_slippage_fraction
        self.max_half_spread_fraction = max_half_spread_fraction
        self.fixed_slippage_bps = fixed_slippage_bps

    @classmethod
    def compatibility(cls, slippage_bps: Decimal) -> MarketFrictionModel:
        return cls(
            fee_schedule=FeeSchedule.zero(),
            gamma=Decimal(0),
            spread_atr_multiplier=Decimal(0),
            max_slippage_fraction=Decimal(0),
            max_half_spread_fraction=Decimal(0),
            fixed_slippage_bps=slippage_bps,
        )

    def evaluate(self, order: OrderIntent, context: FrictionContext) -> FrictionResult:
        if order.quantity <= 0 or order.reference_price <= 0:
            raise ValueError("positive quantity and reference_price required")
        sigma = context.atr / order.reference_price
        liquidity_penalty = Decimal(1) / context.liquidity_score
        half_spread_fraction = min(
            self.max_half_spread_fraction,
            sigma * self.spread_atr_multiplier * liquidity_penalty,
        )
        participation = Decimal(order.quantity) / context.average_daily_volume
        root_participation = participation.sqrt()
        slippage_fraction = min(
            self.max_slippage_fraction,
            self.gamma * sigma * root_participation,
        ) + self.fixed_slippage_bps / Decimal(10000)
        direction = Decimal(1) if order.side == Side.BUY else Decimal(-1)
        spread_per_unit = order.reference_price * half_spread_fraction
        slippage_per_unit = order.reference_price * slippage_fraction
        execution_price = order.reference_price + direction * (spread_per_unit + slippage_per_unit)
        spread_drag = spread_per_unit * order.quantity
        slippage_drag = slippage_per_unit * order.quantity
        charges = self._statutory_charges(order, execution_price, context)
        return FrictionResult(
            order.reference_price,
            execution_price,
            spread_drag,
            slippage_drag,
            charges,
        )

    def _statutory_charges(
        self, order: OrderIntent, execution_price: Decimal, context: FrictionContext
    ) -> tuple[FrictionCharge, ...]:
        notional = execution_price * order.quantity
        schedule = self.fee_schedule
        charges: list[FrictionCharge] = []
        if order.market == Market.INDIA:
            stt = Decimal(0)
            if context.delivery:
                stt = notional * schedule.india_stt_delivery_rate
            elif order.side == Side.SELL:
                stt = notional * schedule.india_stt_intraday_sell_rate
            exchange = notional * schedule.india_exchange_rate
            sebi = notional * schedule.india_sebi_rate
            gst = (exchange + sebi) * schedule.india_gst_rate
            stamp_rate = (
                schedule.india_stamp_buy_rate
                if context.delivery
                else schedule.india_stamp_intraday_buy_rate
            )
            stamp = notional * stamp_rate if order.side == Side.BUY else Decimal(0)
            for code, amount in (
                ("STT", stt),
                ("EXCHANGE", exchange),
                ("SEBI", sebi),
                ("GST", gst),
                ("STAMP", stamp),
            ):
                if amount > 0:
                    charges.append(FrictionCharge(code, amount))
        elif order.market == Market.USA and order.side == Side.SELL:
            sec = notional * schedule.us_sec_sell_rate
            taf = min(schedule.us_finra_max, Decimal(order.quantity) * schedule.us_finra_per_share)
            if sec > 0:
                charges.append(FrictionCharge("SEC", sec))
            if taf > 0:
                charges.append(FrictionCharge("FINRA_TAF", taf))
        return tuple(charges)
