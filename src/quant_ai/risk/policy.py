from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import AssetClass, OrderIntent, PortfolioSnapshot, Side
from quant_ai.risk.book_history import normalize_sector_map
from quant_ai.risk.portfolio_risk import (
    BookPosition,
    align_daily_closes,
    correlation_adjusted_gross,
    measure_book_risk,
)


@dataclass(frozen=True)
class RiskPolicy:
    max_daily_loss: Decimal = Decimal("0.02")
    max_drawdown: Decimal = Decimal("0.10")
    max_single_trade_notional: Decimal = Decimal("0.05")
    max_symbol_exposure: Decimal = Decimal("0.10")
    max_asset_class_exposure: Decimal = Decimal("0.40")
    max_gross_exposure: Decimal = Decimal("0.60")
    require_protective_stop: bool = True
    blocked_asset_classes: tuple[AssetClass, ...] = ()


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


class RiskFirewall:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy()

    def evaluate(self, order: OrderIntent, portfolio: PortfolioSnapshot) -> RiskDecision:
        if order.quantity <= 0 or order.reference_price <= 0:
            return RiskDecision(False, "invalid_order")
        if portfolio.equity <= 0:
            return RiskDecision(False, "invalid_portfolio_equity")
        if order.stop_price is not None and order.stop_price <= 0:
            return RiskDecision(False, "invalid_stop_price")
        if order.take_profit_price is not None and order.take_profit_price <= 0:
            return RiskDecision(False, "invalid_take_profit_price")

        notional = order.reference_price * order.quantity
        current_symbol = portfolio.symbol_exposure.get(order.symbol, Decimal(0))
        # Directionality. On this long-only ledger a SELL unwinds exposure up to the amount
        # held; only the slice beyond the holding would *add* (short) exposure and is
        # therefore the only slice the caps apply to. A BUY adds all of its notional.
        reducing, adding = exposure_delta(order, portfolio, notional, current_symbol)

        if adding == 0:
            # Pure de-risking. Founder scope, concentration caps and loss/drawdown halts
            # may never trap an existing position: a halt freezes risk-taking, not exits.
            return RiskDecision(True, "approved_risk_reducing")

        if order.asset_class in self.policy.blocked_asset_classes:
            return RiskDecision(False, "asset_class_blocked")
        if self.policy.require_protective_stop and order.stop_price is None:
            return RiskDecision(False, "protective_stop_required")

        # Portfolio drawdown is the broader hard stop, so classify it before the intraday
        # loss breaker when both are breached by the same shock.
        peak = portfolio.peak_equity or portfolio.equity
        if peak > 0 and (peak - portfolio.equity) / peak >= self.policy.max_drawdown:
            return RiskDecision(False, "max_drawdown_reached")

        # Use the more conservative loss signal. This preserves a realized loss even when
        # no daily equity baseline exists yet, while also catching unrealized MTM losses.
        intraday_pnl = min(portfolio.daily_realized_pnl, portfolio.daily_total_pnl)
        loss_limit = -(portfolio.equity * self.policy.max_daily_loss)
        if intraday_pnl <= loss_limit:
            return RiskDecision(False, "daily_loss_limit_reached")
        if adding > portfolio.equity * self.policy.max_single_trade_notional:
            return RiskDecision(False, "single_trade_notional_limit")
        projected_symbol = current_symbol - reducing + adding
        if projected_symbol > portfolio.equity * self.policy.max_symbol_exposure:
            return RiskDecision(False, "symbol_concentration_limit")
        current_asset = portfolio.asset_exposure.get(order.asset_class, Decimal(0))
        projected_asset = current_asset - reducing + adding
        if projected_asset > portfolio.equity * self.policy.max_asset_class_exposure:
            return RiskDecision(False, "asset_class_exposure_limit")
        projected_gross = portfolio.gross_exposure - reducing + adding
        if projected_gross > portfolio.equity * self.policy.max_gross_exposure:
            return RiskDecision(False, "gross_exposure_limit")
        # Exposure-opening orders must carry protection on the correct side of entry.
        if order.stop_price is not None and not _stop_on_loss_side(order):
            return RiskDecision(False, "protective_stop_wrong_side")
        if order.take_profit_price is not None and not _target_on_profit_side(order):
            return RiskDecision(False, "take_profit_wrong_side")
        return RiskDecision(True, "approved")


def exposure_delta(
    order: OrderIntent,
    portfolio: PortfolioSnapshot,
    notional: Decimal,
    current_symbol: Decimal,
) -> tuple[Decimal, Decimal]:
    """Split an order into (exposure unwound, exposure added).

    A BUY adds all of its notional. A SELL unwinds what is held and only the
    units beyond the holding add (short) exposure. When the snapshot carries
    held quantities the split is exact by units, so a full liquidation is a
    pure unwind even when the mark has drifted from the reference price; a
    snapshot without quantities falls back to the notional split.
    """
    if order.side != Side.SELL:
        return Decimal(0), notional
    held = portfolio.symbol_quantity.get(order.symbol)
    if held is None:
        reducing = min(notional, max(current_symbol, Decimal(0)))
        return reducing, notional - reducing
    if held <= 0:
        return Decimal(0), notional
    covered = min(order.quantity, held)
    reducing = max(current_symbol, Decimal(0)) * Decimal(covered) / Decimal(held)
    return reducing, order.reference_price * (order.quantity - covered)


def _stop_on_loss_side(order: OrderIntent) -> bool:
    assert order.stop_price is not None
    if order.side == Side.BUY:
        return order.stop_price < order.reference_price
    return order.stop_price > order.reference_price


def _target_on_profit_side(order: OrderIntent) -> bool:
    assert order.take_profit_price is not None
    if order.side == Side.BUY:
        return order.take_profit_price > order.reference_price
    return order.take_profit_price < order.reference_price


# ---------------------------------------------------------------------------
# Cross-position controls
#
# Every limit above is a notional bucket: one trade, one symbol, one asset
# class, the gross book, one country. None of them can see that two 4.5%
# positions in the same industry are economically one 9% bet. The three limits
# below read the book as a whole, using the covariance measure ported in
# ``risk.portfolio_risk`` from the dashboard diagnostic.
#
# They are opt-in by data, not by flag. The correlation and expected-shortfall
# limits need a return-history source and the group limit needs an operator's
# symbol-to-group mapping; with neither supplied the gate is not armed and the
# entry path behaves exactly as it did before. Once a source IS supplied, an
# unusable measure is a refusal, never a pass: insufficient history, a missing
# symbol, a broken series or an arithmetic failure all block the entry with
# ``book_risk_measure_unavailable:<cause>``. An operator who arms these controls
# is asking the engine not to trade blind, so blind means stop.
#
# Risk-reducing orders never reach these gates. ``RiskFirewall`` returns
# ``approved_risk_reducing`` before any cap is consulted and ``RiskWarden``
# honours that verdict, so an exit is never trapped by a measurement problem.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookRiskPolicy:
    """Thresholds for the cross-position controls. All fractions of equity.

    ``max_correlation_adjusted_gross`` (0.45)
        Cap on ``sqrt(wT R+ w)``, the gross book counted at its own
        correlation. A perfectly correlated book scores its plain notional sum,
        so this caps such a book at 45% of equity - tighter than the 60%
        notional gross cap, which is exactly the point. An uncorrelated book of
        n equal positions scores ``gross / sqrt(n)`` and can still reach the
        notional cap. The number is set one notch below the notional gross cap
        so that concentration, not size alone, is what it refuses.

    ``max_sector_exposure`` (0.25)
        No operator-declared group may exceed a quarter of equity. It nests
        inside the 40% asset-class cap it refines: five positions at the 5%
        single-position cap in one industry reach it exactly.

    ``max_book_expected_shortfall`` (0.03)
        The 95% one-day historical expected shortfall of the projected book -
        the average loss on the worst 5% of the sampled days - may not exceed 3%
        of equity. The engine's intraday loss breaker is 2% of equity and its
        drawdown stop 10%; requiring a typical tail day to cost no more than 1.5
        loss-breaker units and under a third of the drawdown budget leaves room
        to de-risk before a hard stop fires, instead of discovering the book was
        too large only once it had already breached.

    ``correlation_floor`` (0)
        Correlations below this are raised to it before the adjusted gross is
        computed. Zero means a negative sample correlation is counted as
        independence, never as a hedge: a short window's negative correlation is
        the least reliable number it produces, and it must not buy exposure.
    """

    max_correlation_adjusted_gross: Decimal = Decimal("0.45")
    max_sector_exposure: Decimal = Decimal("0.25")
    max_book_expected_shortfall: Decimal = Decimal("0.03")
    correlation_floor: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in (
            "max_correlation_adjusted_gross",
            "max_sector_exposure",
            "max_book_expected_shortfall",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not Decimal(-1) <= self.correlation_floor <= Decimal(1):
            raise ValueError("correlation_floor must be in [-1, 1]")


HistoryProvider = Callable[[Sequence[str]], Mapping[str, Sequence[tuple[str, Decimal]]]]


class BookRiskFirewall:
    """Correlation, group and expected-shortfall limits over the projected book.

    ``history_provider`` returns ``(session key, close)`` rows per symbol;
    ``risk.book_history.DailyCloseHistory`` adapts the engine's daily bars into
    that shape. ``sector_map`` is the operator's symbol-to-group mapping.
    Neither is invented when absent: the corresponding limits simply do not
    arm.
    """

    def __init__(
        self,
        policy: BookRiskPolicy | None = None,
        *,
        history_provider: HistoryProvider | None = None,
        sector_map: Mapping[str, str] | None = None,
    ) -> None:
        self.policy = policy or BookRiskPolicy()
        self.history_provider = history_provider
        self.sector_map = normalize_sector_map(sector_map)

    @property
    def armed(self) -> bool:
        """Whether any cross-position limit has the data it needs to apply."""
        return self.history_provider is not None or bool(self.sector_map)

    def evaluate(self, order: OrderIntent, portfolio: PortfolioSnapshot) -> RiskDecision:
        """Judge the book this exposure-adding order would create.

        Never raises: a caller on the cadence path gets a decision, and an
        unusable measurement is a refusal.
        """
        if not self.armed:
            return RiskDecision(True, "book_risk_not_armed")
        try:
            return self._evaluate(order, portfolio)
        except Exception as error:  # noqa: BLE001 - a measurement fault must not pass a trade
            return RiskDecision(
                False, f"book_risk_measure_unavailable:{type(error).__name__}"
            )

    def _evaluate(self, order: OrderIntent, portfolio: PortfolioSnapshot) -> RiskDecision:
        equity = portfolio.equity
        if equity <= 0:
            return RiskDecision(False, "book_risk_measure_unavailable:invalid_equity")
        projected = self._projected_exposure(order, portfolio)
        group = self._group_breach(projected, equity)
        if group is not None:
            return RiskDecision(False, f"sector_concentration_limit:{group}")
        if self.history_provider is None:
            return RiskDecision(True, "approved_book_risk")
        symbols = sorted(symbol for symbol, value in projected.items() if value > 0)
        if not symbols:
            return RiskDecision(True, "approved_book_risk")
        aligned, reason = align_daily_closes(dict(self.history_provider(symbols)))
        if reason:
            return RiskDecision(False, f"book_risk_measure_unavailable:{reason}")
        missing = [symbol for symbol in symbols if symbol not in aligned]
        if missing:
            return RiskDecision(
                False, f"book_risk_measure_unavailable:no_history:{','.join(missing)}"
            )
        measure = measure_book_risk(
            [BookPosition(symbol, projected[symbol], aligned[symbol]) for symbol in symbols],
            equity,
        )
        if not measure.available:
            return RiskDecision(False, f"book_risk_measure_unavailable:{measure.reason}")
        adjusted = correlation_adjusted_gross(
            measure, correlation_floor=self.policy.correlation_floor
        )
        if adjusted is None:
            return RiskDecision(
                False, "book_risk_measure_unavailable:correlation_aggregation"
            )
        if adjusted > self.policy.max_correlation_adjusted_gross:
            return RiskDecision(False, "correlation_adjusted_gross_limit")
        shortfall = measure.expected_shortfall_fraction
        if shortfall is None:
            return RiskDecision(
                False,
                f"book_risk_measure_unavailable:{measure.tail_reason or 'expected_shortfall'}",
            )
        if shortfall > self.policy.max_book_expected_shortfall:
            return RiskDecision(False, "book_expected_shortfall_limit")
        return RiskDecision(True, "approved_book_risk")

    @staticmethod
    def _projected_exposure(
        order: OrderIntent, portfolio: PortfolioSnapshot
    ) -> dict[str, Decimal]:
        """Marked exposure per symbol once this order has been applied."""
        notional = order.reference_price * order.quantity
        current = portfolio.symbol_exposure.get(order.symbol, Decimal(0))
        reducing, adding = exposure_delta(order, portfolio, notional, current)
        projected = {
            symbol: max(Decimal(0), value)
            for symbol, value in portfolio.symbol_exposure.items()
        }
        projected[order.symbol] = max(
            Decimal(0), max(Decimal(0), current) - reducing + adding
        )
        return projected

    def _group_breach(self, projected: Mapping[str, Decimal], equity: Decimal) -> str | None:
        """The first operator-declared group over its cap, if any.

        Symbols the operator did not map belong to no group and are left to the
        symbol cap; an unmapped book is therefore ungrouped, not grouped by a
        guess.
        """
        if not self.sector_map:
            return None
        limit = equity * self.policy.max_sector_exposure
        totals: dict[str, Decimal] = {}
        for symbol, value in projected.items():
            group = self.sector_map.get(symbol.strip().upper())
            if group is None or value <= 0:
                continue
            totals[group] = totals.get(group, Decimal(0)) + value
        for group in sorted(totals):
            if totals[group] > limit:
                return group
        return None
