from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import ClassVar

from quant_ai.domain.models import RiskMode


@dataclass(frozen=True)
class GoalBand:
    floor: Decimal
    target: Decimal
    stretch: Decimal
    mandatory: bool = False


@dataclass(frozen=True)
class CapitalPlanRequest:
    starting_capital: Decimal
    confidence: Decimal
    annualized_volatility: Decimal
    expected_edge: Decimal = Decimal(0)
    current_drawdown: Decimal = Decimal(0)
    liquidity_score: Decimal = Decimal(1)
    requested_mode: RiskMode | None = None

    def __post_init__(self) -> None:
        if self.starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        for name, value in (
            ("confidence", self.confidence),
            ("annualized_volatility", self.annualized_volatility),
            ("current_drawdown", self.current_drawdown),
            ("liquidity_score", self.liquidity_score),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.confidence > 1 or self.liquidity_score > 1:
            raise ValueError("confidence and liquidity_score must be <= 1")


@dataclass(frozen=True)
class CapitalPlan:
    starting_capital: Decimal
    recommended_mode: RiskMode
    per_trade_risk_fraction: Decimal
    per_trade_risk_amount: Decimal
    max_daily_loss_fraction: Decimal
    max_daily_loss_amount: Decimal
    max_drawdown_fraction: Decimal
    max_drawdown_amount: Decimal
    max_position_fraction: Decimal
    max_position_amount: Decimal
    max_country_allocation_fraction: Decimal
    max_gross_exposure_fraction: Decimal
    cash_reserve_fraction: Decimal
    stop_loss_fraction: Decimal
    take_profit_fraction: Decimal
    reward_risk_ratio: Decimal
    daily_goal: GoalBand
    weekly_goal: GoalBand
    monthly_goal: GoalBand
    yearly_goal: GoalBand
    trading_allowed: bool
    rationale: tuple[str, ...]

    def quantity_for_price(self, price: Decimal) -> int:
        if price <= 0 or self.stop_loss_fraction <= 0:
            return 0
        risk_per_unit = price * self.stop_loss_fraction
        by_risk = (self.per_trade_risk_amount / risk_per_unit).to_integral_value(rounding=ROUND_DOWN)
        by_notional = (self.max_position_amount / price).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_notional)))


class CapitalGoalEngine:
    _RISK: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.0025"),
        RiskMode.BALANCED: Decimal("0.005"),
        RiskMode.AGGRESSIVE: Decimal("0.01"),
    }
    _DAILY_LOSS: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.0075"),
        RiskMode.BALANCED: Decimal("0.0125"),
        RiskMode.AGGRESSIVE: Decimal("0.02"),
    }
    _MAX_DRAWDOWN: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.06"),
        RiskMode.BALANCED: Decimal("0.10"),
        RiskMode.AGGRESSIVE: Decimal("0.15"),
    }
    _MAX_POSITION: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.05"),
        RiskMode.BALANCED: Decimal("0.10"),
        RiskMode.AGGRESSIVE: Decimal("0.12"),
    }
    _COUNTRY: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.35"),
        RiskMode.BALANCED: Decimal("0.50"),
        RiskMode.AGGRESSIVE: Decimal("0.60"),
    }
    _GROSS: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.35"),
        RiskMode.BALANCED: Decimal("0.60"),
        RiskMode.AGGRESSIVE: Decimal("0.75"),
    }
    _CASH: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("0.30"),
        RiskMode.BALANCED: Decimal("0.20"),
        RiskMode.AGGRESSIVE: Decimal("0.10"),
    }
    _REWARD_RISK: ClassVar[dict[RiskMode, Decimal]] = {
        RiskMode.CONSERVATIVE: Decimal("1.75"),
        RiskMode.BALANCED: Decimal("2.0"),
        RiskMode.AGGRESSIVE: Decimal("2.25"),
    }

    def recommend(self, request: CapitalPlanRequest) -> CapitalPlan:
        mode = request.requested_mode or self._recommend_mode(request)
        risk_fraction = self._RISK[mode]
        daily_loss = self._DAILY_LOSS[mode]
        drawdown_limit = self._MAX_DRAWDOWN[mode]
        max_position = self._MAX_POSITION[mode]
        country = self._COUNTRY[mode]
        gross = self._GROSS[mode]
        cash = self._CASH[mode]
        reward_risk = self._REWARD_RISK[mode]
        stop_fraction = self._adaptive_stop_fraction(request, mode)
        take_profit = stop_fraction * reward_risk

        trading_allowed = (
            request.confidence >= Decimal("0.55")
            and request.liquidity_score >= Decimal("0.50")
            and request.current_drawdown < drawdown_limit
        )
        annual_target = self._annual_target(request, mode) if trading_allowed else Decimal(0)
        goals = self._goal_bands(annual_target)
        rationale = self._rationale(request, mode, trading_allowed, stop_fraction)
        capital = request.starting_capital
        return CapitalPlan(
            capital,
            mode,
            risk_fraction,
            capital * risk_fraction,
            daily_loss,
            capital * daily_loss,
            drawdown_limit,
            capital * drawdown_limit,
            max_position,
            capital * max_position,
            country,
            gross,
            cash,
            stop_fraction,
            take_profit,
            reward_risk,
            goals[0],
            goals[1],
            goals[2],
            goals[3],
            trading_allowed,
            rationale,
        )

    def _recommend_mode(self, request: CapitalPlanRequest) -> RiskMode:
        if (
            request.confidence < Decimal("0.60")
            or request.annualized_volatility >= Decimal("0.40")
            or request.current_drawdown >= Decimal("0.06")
            or request.liquidity_score < Decimal("0.70")
        ):
            return RiskMode.CONSERVATIVE
        if (
            request.confidence >= Decimal("0.80")
            and request.annualized_volatility <= Decimal("0.22")
            and request.current_drawdown < Decimal("0.02")
            and request.liquidity_score >= Decimal("0.90")
            and request.expected_edge >= Decimal("0.01")
        ):
            return RiskMode.AGGRESSIVE
        return RiskMode.BALANCED

    def _adaptive_stop_fraction(self, request: CapitalPlanRequest, mode: RiskMode) -> Decimal:
        daily_vol = request.annualized_volatility / Decimal(252).sqrt()
        multiplier = {
            RiskMode.CONSERVATIVE: Decimal("1.25"),
            RiskMode.BALANCED: Decimal("1.50"),
            RiskMode.AGGRESSIVE: Decimal("1.75"),
        }[mode]
        raw = daily_vol * multiplier
        return min(Decimal("0.05"), max(Decimal("0.006"), raw))

    def _annual_target(self, request: CapitalPlanRequest, mode: RiskMode) -> Decimal:
        base = {
            RiskMode.CONSERVATIVE: Decimal("0.08"),
            RiskMode.BALANCED: Decimal("0.14"),
            RiskMode.AGGRESSIVE: Decimal("0.22"),
        }[mode]
        confidence_adjustment = (request.confidence - Decimal("0.55")) * Decimal("0.20")
        edge_adjustment = min(Decimal("0.08"), max(Decimal("-0.04"), request.expected_edge * Decimal(2)))
        volatility_cap = min(Decimal("0.35"), request.annualized_volatility * Decimal("0.80"))
        return max(Decimal(0), min(base + confidence_adjustment + edge_adjustment, volatility_cap))

    def _goal_bands(self, annual_target: Decimal) -> tuple[GoalBand, GoalBand, GoalBand, GoalBand]:
        if annual_target <= 0:
            zero = GoalBand(Decimal(0), Decimal(0), Decimal(0))
            return zero, zero, zero, zero
        daily = annual_target / Decimal(252)
        weekly = annual_target / Decimal(52)
        monthly = annual_target / Decimal(12)
        return (
            self._band(daily),
            self._band(weekly),
            self._band(monthly),
            self._band(annual_target),
        )

    @staticmethod
    def _band(target: Decimal) -> GoalBand:
        return GoalBand(target * Decimal("0.60"), target, target * Decimal("1.40"), False)

    @staticmethod
    def _rationale(
        request: CapitalPlanRequest,
        mode: RiskMode,
        trading_allowed: bool,
        stop_fraction: Decimal,
    ) -> tuple[str, ...]:
        reasons = [
            f"recommended_mode={mode.value}",
            f"confidence={request.confidence}",
            f"annualized_volatility={request.annualized_volatility}",
            f"liquidity_score={request.liquidity_score}",
            f"adaptive_stop_fraction={stop_fraction}",
        ]
        if not trading_allowed:
            reasons.append("capital_preservation_mode:no_trade_until_quality_recovers")
        return tuple(reasons)
