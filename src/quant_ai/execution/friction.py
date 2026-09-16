from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side

LOGGER = logging.getLogger(__name__)

# How the inputs behind a fill were obtained. Recorded on every governed fill so a proof
# states whether the price came from observed market data or from a deliberate assumption.
OBSERVED_QUOTE_AND_BARS = "observed_quote_and_bars"
OBSERVED_QUOTE = "observed_quote_assumed_depth"
OBSERVED_BARS = "observed_bars"
OBSERVED_BARS_ASSUMED_SPREAD = "observed_bars_assumed_spread"
ASSUMED = "assumed"
INPUT_SOURCES = (
    OBSERVED_QUOTE_AND_BARS,
    OBSERVED_QUOTE,
    OBSERVED_BARS,
    OBSERVED_BARS_ASSUMED_SPREAD,
    ASSUMED,
)


@dataclass(frozen=True)
class FrictionContext:
    atr: Decimal
    average_daily_volume: Decimal
    liquidity_score: Decimal = Decimal(1)
    delivery: bool = True
    # Half of a genuine bid/ask, as a fraction of the mid. When the tick carries a real
    # two-sided quote it prices the fill, ahead of any model estimate.
    observed_half_spread_fraction: Decimal | None = None
    # Floor under the modelled half spread when no quote was observed. It only ever widens
    # the spread: a market nobody could see must not price cheaper than one that was seen.
    assumed_half_spread_floor: Decimal = Decimal(0)
    input_source: str = OBSERVED_BARS

    def __post_init__(self) -> None:
        if self.atr < 0 or self.average_daily_volume <= 0:
            raise ValueError("atr must be non-negative and ADV must be positive")
        if not Decimal(0) < self.liquidity_score <= Decimal(1):
            raise ValueError("liquidity_score must be in (0,1]")
        observed = self.observed_half_spread_fraction
        if observed is not None and observed < 0:
            raise ValueError("observed_half_spread_fraction cannot be negative")
        if self.assumed_half_spread_floor < 0:
            raise ValueError("assumed_half_spread_floor cannot be negative")
        if self.input_source not in INPUT_SOURCES:
            raise ValueError(f"unknown friction input source: {self.input_source}")

    @property
    def priced_off_observed_quote(self) -> bool:
        return self.observed_half_spread_fraction is not None

    def provenance(self) -> dict[str, str | None]:
        """JSON-ready statement of what priced a fill, carried into the fill proof."""
        observed = self.observed_half_spread_fraction
        return {
            "inputSource": self.input_source,
            "atr": str(self.atr),
            "averageDailyVolume": str(self.average_daily_volume),
            "liquidityScore": str(self.liquidity_score),
            "observedHalfSpreadFraction": None if observed is None else str(observed),
            "assumedHalfSpreadFloor": str(self.assumed_half_spread_floor),
            "delivery": "true" if self.delivery else "false",
        }


# What the schedules below can actually price, as market -> asset classes. Both of them
# are cash-market schedules for shares and the ETFs that trade alongside them: STT at the
# delivery and intraday rates, stamp duty on the buy and the depository charge on a
# delivery sell in India; the Section 31 fee and the FINRA TAF on a sell in the US.
#
# Every one of those lines is charged differently on anything else. A commodity pays CTT
# rather than STT; an equity future pays its own rate on its own side; neither pays a
# depository charge, because nothing is delivered. The US Section 31 fee is levied on
# securities sales and not on futures at all. None of those rates are in this module, and
# a rate that is not here cannot be guessed from one that is.
#
# So an order these schedules cannot price is refused rather than charged the nearest
# thing they can. The cost of getting it wrong is not a rounding error: over a real
# 19-year series, friction was the difference between a +832% gross return and a +206%
# net one. A strategy priced on the wrong schedule is profitable on paper and not in the
# account, and that is the more expensive failure of the two.
#
# A market absent from this mapping prices nothing: Market.GLOBAL used to fall past both
# branches below and come back with no charges at all, which is the same wrong number
# wearing a friendlier face.
PRICED_ASSET_CLASSES: dict[Market, frozenset[AssetClass]] = {
    Market.INDIA: frozenset({AssetClass.EQUITY, AssetClass.ETF}),
    Market.USA: frozenset({AssetClass.EQUITY, AssetClass.ETF}),
}


def priced_by_the_fee_schedules(order: OrderIntent) -> bool:
    """Whether a statutory charge exists in this module for what the order is buying."""
    return order.asset_class in PRICED_ASSET_CLASSES.get(order.market, frozenset())


@dataclass(frozen=True)
class FeeSchedule:
    """Statutory and exchange levies: what the government and the venue take."""

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


# Every broker-side rate lives here, with its source, and nowhere else.
#
# Source (India): the equity schedules the large retail discount brokers publish on their
# public pricing pages. The shape they share is "a flat cap per executed order, or a
# percentage of turnover, whichever is lower", applied to both delivery and intraday, plus
# a depository-participant charge on the debit (sell) leg of a delivery trade.
#   * flat cap                 INR 20 per executed order
#   * percentage of turnover   2.5% - the widest published band. Brokers that quote 0.1%
#                              are cheaper on small orders, so the widest band is the
#                              conservative default and the narrow one is configurable.
#   * DP charge, delivery sell INR 15.34 per scrip per debit, quoted excluding GST: the
#                              depository's own fee plus the participant's markup.
#   * GST                      18% on brokerage, exchange, SEBI and DP charges - never on
#                              STT or stamp duty (see ``FeeSchedule.india_gst_rate``).
# Source (US): mainstream retail brokers charge no commission on listed equities, so the
# default is zero; set it when the venue being modelled does charge.
# Overridable per field from the environment: see ``BrokerageSchedule.from_env``.
BROKERAGE_ENV_PREFIX = "PRAMANA_BROKERAGE_"


@dataclass(frozen=True)
class BrokerageSchedule:
    """Broker-side charges: what the broker keeps, on top of the statutory fees."""

    name: str
    india_percent_of_turnover: Decimal
    india_cap_per_order: Decimal
    india_minimum_per_order: Decimal
    india_dp_sell_per_scrip: Decimal
    us_flat_per_order: Decimal

    def __post_init__(self) -> None:
        rates = (
            self.india_percent_of_turnover,
            self.india_cap_per_order,
            self.india_minimum_per_order,
            self.india_dp_sell_per_scrip,
            self.us_flat_per_order,
        )
        if any(rate < 0 for rate in rates):
            raise ValueError("brokerage rates cannot be negative")

    @classmethod
    def discount_broker_2026(cls) -> BrokerageSchedule:
        return cls(
            "discount_broker_2026",
            Decimal("0.025"),
            Decimal(20),
            Decimal(0),
            Decimal("15.34"),
            Decimal(0),
        )

    @classmethod
    def zero(cls) -> BrokerageSchedule:
        return cls("zero", *(Decimal(0) for _ in range(5)))

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> BrokerageSchedule:
        """The documented default, with per-field overrides from the environment.

        ``PRAMANA_BROKERAGE_PERCENT`` (fraction of turnover, so ``0.001`` means 0.1%),
        ``PRAMANA_BROKERAGE_CAP_INR``, ``PRAMANA_BROKERAGE_MIN_INR``,
        ``PRAMANA_BROKERAGE_DP_SELL_INR`` and ``PRAMANA_BROKERAGE_US_FLAT``. A missing,
        malformed or negative value keeps the documented default for that field: a typo
        must never quietly make a modelled fill cheaper than the real one.
        """
        source = os.environ if environ is None else environ
        default = cls.discount_broker_2026()
        fields = (
            ("PERCENT", "india_percent_of_turnover"),
            ("CAP_INR", "india_cap_per_order"),
            ("MIN_INR", "india_minimum_per_order"),
            ("DP_SELL_INR", "india_dp_sell_per_scrip"),
            ("US_FLAT", "us_flat_per_order"),
        )
        overrides: dict[str, Decimal] = {}
        for suffix, field_name in fields:
            raw = str(source.get(f"{BROKERAGE_ENV_PREFIX}{suffix}", "")).strip()
            if not raw:
                continue
            try:
                value = Decimal(raw)
            except (ArithmeticError, InvalidOperation, ValueError):
                value = None
            if value is None or not value.is_finite() or value < 0:
                LOGGER.warning(
                    "ignoring malformed brokerage override %s%s; keeping documented default",
                    BROKERAGE_ENV_PREFIX,
                    suffix,
                )
                continue
            overrides[field_name] = value
        if not overrides:
            return default
        return cls(
            "discount_broker_2026_env_override",
            overrides.get("india_percent_of_turnover", default.india_percent_of_turnover),
            overrides.get("india_cap_per_order", default.india_cap_per_order),
            overrides.get("india_minimum_per_order", default.india_minimum_per_order),
            overrides.get("india_dp_sell_per_scrip", default.india_dp_sell_per_scrip),
            overrides.get("us_flat_per_order", default.us_flat_per_order),
        )

    def india_brokerage(self, notional: Decimal) -> Decimal:
        """Percentage of turnover, capped, floored, and never more than the order itself."""
        if notional <= 0:
            return Decimal(0)
        charge = notional * self.india_percent_of_turnover
        if self.india_cap_per_order > 0:
            charge = min(charge, self.india_cap_per_order)
        return min(max(charge, self.india_minimum_per_order), notional)


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
    def cash_charges(self) -> Decimal:
        """Every charge line that debits cash: brokerage, depository and statutory."""
        return sum((item.amount for item in self.charges), Decimal(0))

    @property
    def statutory_fees(self) -> Decimal:
        """Legacy name for :attr:`cash_charges`, kept for the existing ledger callers."""
        return self.cash_charges

    @property
    def total_friction(self) -> Decimal:
        return self.spread_drag + self.slippage_drag + self.cash_charges


class MarketFrictionModel:
    """Bounded spread, square-root impact, broker charges and versioned statutory fees."""

    def __init__(
        self,
        *,
        fee_schedule: FeeSchedule | None = None,
        brokerage_schedule: BrokerageSchedule | None = None,
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
        self.brokerage_schedule = brokerage_schedule or BrokerageSchedule.discount_broker_2026()
        self.gamma = gamma
        self.spread_atr_multiplier = spread_atr_multiplier
        self.max_slippage_fraction = max_slippage_fraction
        self.max_half_spread_fraction = max_half_spread_fraction
        self.fixed_slippage_bps = fixed_slippage_bps

    @classmethod
    def compatibility(cls, slippage_bps: Decimal) -> MarketFrictionModel:
        return cls(
            fee_schedule=FeeSchedule.zero(),
            brokerage_schedule=BrokerageSchedule.zero(),
            gamma=Decimal(0),
            spread_atr_multiplier=Decimal(0),
            max_slippage_fraction=Decimal(0),
            max_half_spread_fraction=Decimal(0),
            fixed_slippage_bps=slippage_bps,
        )

    @property
    def max_adverse_fraction(self) -> Decimal:
        return (
            self.max_half_spread_fraction
            + self.max_slippage_fraction
            + self.fixed_slippage_bps / Decimal(10000)
        )

    def worst_case_execution_price(self, reference_price: Decimal, side: Side) -> Decimal:
        if reference_price <= 0:
            return Decimal(0)
        fraction = self.max_adverse_fraction
        if side == Side.BUY:
            return reference_price * (Decimal(1) + fraction)
        return reference_price * max(Decimal(0), Decimal(1) - fraction)

    def half_spread_fraction(self, order: OrderIntent, context: FrictionContext) -> Decimal:
        """An observed quote wins; otherwise the ATR estimate, never below its floor."""
        if context.observed_half_spread_fraction is not None:
            modelled = context.observed_half_spread_fraction
        else:
            sigma = context.atr / order.reference_price
            liquidity_penalty = Decimal(1) / context.liquidity_score
            modelled = max(
                sigma * self.spread_atr_multiplier * liquidity_penalty,
                context.assumed_half_spread_floor,
            )
        return min(self.max_half_spread_fraction, modelled)

    def evaluate(self, order: OrderIntent, context: FrictionContext) -> FrictionResult:
        if order.quantity <= 0 or order.reference_price <= 0:
            raise ValueError("positive quantity and reference_price required")
        # Before any of it is priced. The spread and impact model would happily quote a
        # gold future - it reads an ATR and a volume and knows nothing about what it is
        # pricing - and the result would carry a real-looking drag next to charges taken
        # from a schedule that does not apply. Refuse the whole fill instead.
        if not priced_by_the_fee_schedules(order):
            raise ValueError(
                f"friction_unpriced_instrument:{order.symbol}:{order.market.value}:"
                f"{order.asset_class.value}:cash_equity_and_etf_only"
            )
        sigma = context.atr / order.reference_price
        half_spread_fraction = self.half_spread_fraction(order, context)
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
        charges = self._charges(order, execution_price, context)
        return FrictionResult(
            order.reference_price,
            execution_price,
            spread_drag,
            slippage_drag,
            charges,
        )

    def _charges(
        self, order: OrderIntent, execution_price: Decimal, context: FrictionContext
    ) -> tuple[FrictionCharge, ...]:
        notional = execution_price * order.quantity
        schedule = self.fee_schedule
        broker = self.brokerage_schedule
        charges: list[FrictionCharge] = []
        if order.market == Market.INDIA:
            brokerage = broker.india_brokerage(notional)
            stt = Decimal(0)
            if context.delivery:
                stt = notional * schedule.india_stt_delivery_rate
            elif order.side == Side.SELL:
                stt = notional * schedule.india_stt_intraday_sell_rate
            exchange = notional * schedule.india_exchange_rate
            sebi = notional * schedule.india_sebi_rate
            # A delivery sell debits the demat account, so the depository participant
            # charge lands on that leg only - never on a buy, never on intraday.
            depository = (
                broker.india_dp_sell_per_scrip
                if context.delivery and order.side == Side.SELL
                else Decimal(0)
            )
            # GST is levied on the service charges - brokerage, exchange, SEBI and DP -
            # and never on STT or stamp duty. Brokerage belongs in this base; it was only
            # absent from it while the modelled brokerage was zero.
            gst = (brokerage + exchange + sebi + depository) * schedule.india_gst_rate
            stamp_rate = (
                schedule.india_stamp_buy_rate
                if context.delivery
                else schedule.india_stamp_intraday_buy_rate
            )
            stamp = notional * stamp_rate if order.side == Side.BUY else Decimal(0)
            for code, amount in (
                ("BROKERAGE", brokerage),
                ("STT", stt),
                ("EXCHANGE", exchange),
                ("SEBI", sebi),
                ("DP", depository),
                ("GST", gst),
                ("STAMP", stamp),
            ):
                if amount > 0:
                    charges.append(FrictionCharge(code, amount))
        elif order.market == Market.USA:
            brokerage = min(broker.us_flat_per_order, notional)
            if brokerage > 0:
                charges.append(FrictionCharge("BROKERAGE", brokerage))
            if order.side == Side.SELL:
                sec = notional * schedule.us_sec_sell_rate
                taf = min(
                    schedule.us_finra_max, Decimal(order.quantity) * schedule.us_finra_per_share
                )
                if sec > 0:
                    charges.append(FrictionCharge("SEC", sec))
                if taf > 0:
                    charges.append(FrictionCharge("FINRA_TAF", taf))
        return tuple(charges)
