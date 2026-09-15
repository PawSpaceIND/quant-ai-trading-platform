from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import AssetClass, PortfolioSnapshot, Side
from quant_ai.risk.policy import exposure_delta


@dataclass(frozen=True)
class StressScenario:
    name: str
    equity_shock: Decimal
    rates_shock_bps: int = 0


@dataclass(frozen=True)
class StressVerdict:
    passed: bool
    worst_scenario: str
    projected_loss: Decimal
    loss_fraction_of_equity: Decimal
    flags: tuple[str, ...]
    # Book figures. The fields above stay the single-trade ones they have always
    # been so that recorded evidence keeps its meaning; these are the whole-book
    # equivalents, which is what the veto now actually turns on.
    book_worst_scenario: str = "NONE"
    book_loss: Decimal = Decimal(0)
    book_loss_fraction_of_equity: Decimal = Decimal(0)


class AdversarialStressAgent:
    """Gap-scenario veto over the book the proposal would create.

    What was wrong. The agent shocked the proposed order and nothing else, and
    vetoed above 1% of equity. A -8% crisis gap on a single trade only reaches
    1% of equity once that trade is 12.5% of equity, and both
    ``RiskPolicy.max_single_trade_notional`` and the position sizer cap a trade
    at 5%. The veto was therefore unreachable on the governed path: dead code
    that made the engine look stress-tested when nothing was being tested. It
    was also the wrong question - a book of five separate 5% positions gaps
    together, and no single-trade measure can see that.

    What it does now. Every scenario is applied to the book the fill would
    leave: existing positions at their marked exposure (per asset class where
    the snapshot carries that breakdown, the remaining gross at the equity
    shock), the unwound slice of a SELL removed, and the added slice of the new
    order included. A -8% gap across three 4.5% positions is the 1.08% book loss
    it really is, instead of the 0.36% single-trade loss the old measure saw.

    The thresholds.

    ``tolerance_fraction`` (1%, unchanged)
        The original single-trade rule is preserved exactly as it was. It is
        unreachable through the warden, but the agent is also called directly
        (``pramana`` stress-tests open positions with it), and relaxing a
        control while widening another is how a safety change quietly becomes a
        loosening. Nothing that was vetoed before passes now.

    ``book_tolerance_fraction`` (2%)
        The worst modelled gap across the whole post-trade book may not cost
        more than 2% of equity. That is the engine's own intraday loss breaker
        (``RiskPolicy.max_daily_loss``), and a crisis gap is precisely the event
        that breaker exists for - but the breaker is reactive, firing after the
        loss, while this is preventive. Tying them together means the engine
        will not add exposure that one modelled crisis day would, on its own,
        turn into a halt. Against the worst default scenario (-8%) it binds at
        25% gross exposure, which is exactly five positions at the 5% single-
        position cap, the largest book the default ``max_open_positions`` of
        five can hold. So the veto is reachable well inside the 5% cap while the
        intended book shape is still permitted, and it binds long before the 60%
        notional gross cap. An operator who wants the three-4.5%-position book
        refused as well can set it to 1%; every verdict reports the book loss
        fraction, so the number to set is visible before it is set.

    Both rules must hold. A verdict flags ``STRESS_VETO`` for the single-trade
    breach and ``BOOK_STRESS_VETO`` for the book breach; either one fails the
    verdict, and ``swarm_runtime`` refuses on a failed verdict. Risk-reducing
    SELLs bypass the veto there exactly as before.
    """

    DEFAULT_SCENARIOS = (
        StressScenario("YIELDS_PLUS_50BPS", Decimal("-0.035"), 50),
        StressScenario("EQUITY_GAP_DOWN_5PCT", Decimal("-0.05")),
        StressScenario("CRISIS_GAP_DOWN_8PCT", Decimal("-0.08")),
    )

    def __init__(
        self,
        tolerance_fraction: Decimal = Decimal("0.01"),
        book_tolerance_fraction: Decimal = Decimal("0.02"),
    ) -> None:
        if not Decimal(0) < tolerance_fraction <= Decimal(1):
            raise ValueError("stress tolerance must be in (0, 1]")
        if not Decimal(0) < book_tolerance_fraction <= Decimal(1):
            raise ValueError("book stress tolerance must be in (0, 1]")
        self.tolerance_fraction = tolerance_fraction
        self.book_tolerance_fraction = book_tolerance_fraction

    def evaluate(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        scenarios: tuple[StressScenario, ...] | None = None,
    ) -> StressVerdict:
        if proposal.side is None or proposal.quantity <= 0 or proposal.reference_price <= 0:
            return StressVerdict(True, "NO_ACTION", Decimal(0), Decimal(0), ())
        scenario_set = scenarios or self.DEFAULT_SCENARIOS
        notional = proposal.reference_price * proposal.quantity
        current = portfolio.symbol_exposure.get(proposal.symbol, Decimal(0))
        reducing, adding = exposure_delta(proposal, portfolio, notional, current)
        losses: list[tuple[Decimal, str]] = []
        book_losses: list[tuple[Decimal, str]] = []
        for scenario in scenario_set:
            signed = self._shock_for(proposal.asset_class, scenario)
            if proposal.side != Side.BUY:
                signed = -signed
            losses.append((max(Decimal(0), -(notional * signed)), scenario.name))
            book_losses.append(
                (self._book_loss(proposal, portfolio, scenario, reducing, adding), scenario.name)
            )
        projected_loss, worst = max(losses, key=lambda item: item[0], default=(Decimal(0), "NONE"))
        book_loss, book_worst = max(
            book_losses, key=lambda item: item[0], default=(Decimal(0), "NONE")
        )
        equity = portfolio.equity
        fraction = projected_loss / equity if equity > 0 else Decimal(1)
        book_fraction = book_loss / equity if equity > 0 else Decimal(1)
        flags: list[str] = []
        if fraction > self.tolerance_fraction:
            flags.append("STRESS_VETO")
        if book_fraction > self.book_tolerance_fraction:
            flags.append("BOOK_STRESS_VETO")
        return StressVerdict(
            not flags,
            worst,
            projected_loss,
            fraction,
            tuple(flags),
            book_worst,
            book_loss,
            book_fraction,
        )

    @staticmethod
    def _shock_for(asset_class: AssetClass, scenario: StressScenario) -> Decimal:
        """A bond's shock comes from the rates leg at a five-year duration."""
        if asset_class == AssetClass.BOND and scenario.rates_shock_bps:
            return -Decimal(scenario.rates_shock_bps) / Decimal(10000) * Decimal(5)
        return scenario.equity_shock

    def _book_loss(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        scenario: StressScenario,
        reducing: Decimal,
        adding: Decimal,
    ) -> Decimal:
        """Loss of the post-fill book under one scenario, floored at zero.

        Existing exposure is shocked per asset class where the snapshot carries
        that breakdown; whatever gross the breakdown does not account for is
        shocked at the equity leg, so a snapshot without an asset map still has
        its whole book counted rather than silently none of it.
        """
        pnl = Decimal(0)
        covered = Decimal(0)
        for asset_class, exposure in portfolio.asset_exposure.items():
            covered += exposure
            pnl += exposure * self._shock_for(asset_class, scenario)
        residual = portfolio.gross_exposure - covered
        if residual > 0:
            pnl += residual * scenario.equity_shock
        shock = self._shock_for(proposal.asset_class, scenario)
        # The unwound slice leaves the book; the added slice joins it, short-side
        # if the order sells beyond the holding.
        pnl -= reducing * shock
        pnl += (adding if proposal.side == Side.BUY else -adding) * shock
        return max(Decimal(0), -pnl)
