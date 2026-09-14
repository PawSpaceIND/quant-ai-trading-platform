from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import AssetClass, OrderIntent, PortfolioSnapshot, Side
from quant_ai.notifications.trading import TradingAlertCode, TradingNotificationDispatcher
from quant_ai.planning.capital import CapitalPlan
from quant_ai.risk.policy import RiskFirewall, RiskPolicy


@dataclass(frozen=True)
class WardenDecision:
    approved: bool
    reason: str
    order: OrderIntent | None = None


class RiskWarden:
    """Mandatory proposal interceptor. Atlas cannot bypass this component."""

    def __init__(
        self,
        dispatcher: TradingNotificationDispatcher | None = None,
        *,
        blocked_asset_classes: tuple[AssetClass, ...] = (),
    ) -> None:
        self.dispatcher = dispatcher or TradingNotificationDispatcher()
        self.blocked_asset_classes = tuple(blocked_asset_classes)

    def evaluate(
        self,
        proposal: TradeProposal,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        country_exposure: dict[str, Decimal] | None = None,
        tenant_id: str = "default",
    ) -> WardenDecision:
        if proposal.side is None:
            return self._reject("atlas_non_actionable_proposal", proposal, tenant_id)
        if proposal.quantity <= 0 or proposal.reference_price <= 0:
            return self._reject("invalid_trade_proposal", proposal, tenant_id)

        held = portfolio.symbol_quantity.get(proposal.symbol, 0)
        pure_de_risking_sell = (
            proposal.side == Side.SELL and held > 0 and proposal.quantity <= held
        )
        if not plan.trading_allowed and not pure_de_risking_sell:
            return self._reject("capital_plan_halted", proposal, tenant_id)

        notional = proposal.reference_price * proposal.quantity
        exposure = portfolio.country_exposure if country_exposure is None else country_exposure
        current_country = exposure.get(proposal.country, Decimal(0))
        country_limit = portfolio.equity * plan.max_country_allocation_fraction
        projected_country = self._project_country_exposure(
            proposal, portfolio, current_country, notional
        )
        if not pure_de_risking_sell and projected_country > country_limit:
            return self._reject("country_allocation_limit", proposal, tenant_id)

        order = OrderIntent(
            proposal.symbol,
            proposal.market,
            proposal.side,
            proposal.quantity,
            proposal.reference_price,
            "atlas-cio",
            proposal.asset_class,
            tenant_id,
            proposal.stop_price,
            proposal.take_profit_price,
        )
        baseline = RiskPolicy()
        policy = RiskPolicy(
            max_daily_loss=min(plan.max_daily_loss_fraction, baseline.max_daily_loss),
            max_drawdown=min(plan.max_drawdown_fraction, baseline.max_drawdown),
            max_single_trade_notional=min(
                plan.max_position_fraction, baseline.max_single_trade_notional
            ),
            max_symbol_exposure=min(
                plan.max_position_fraction, baseline.max_symbol_exposure
            ),
            max_asset_class_exposure=min(
                baseline.max_asset_class_exposure, plan.max_gross_exposure_fraction
            ),
            max_gross_exposure=min(
                plan.max_gross_exposure_fraction, baseline.max_gross_exposure
            ),
            require_protective_stop=True,
            blocked_asset_classes=self.blocked_asset_classes,
        )
        decision = RiskFirewall(policy).evaluate(order, portfolio)
        if not decision.approved:
            return self._reject(decision.reason, proposal, tenant_id)
        return WardenDecision(True, "approved", order)

    @staticmethod
    def _project_country_exposure(
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        current_country: Decimal,
        notional: Decimal,
    ) -> Decimal:
        if proposal.side == Side.BUY:
            return current_country + notional
        held = portfolio.symbol_quantity.get(proposal.symbol, 0)
        if held <= 0:
            return current_country + notional
        current_symbol = max(
            Decimal(0), portfolio.symbol_exposure.get(proposal.symbol, Decimal(0))
        )
        covered = min(proposal.quantity, held)
        reducing = current_symbol * Decimal(covered) / Decimal(held)
        adding_units = max(0, proposal.quantity - covered)
        adding = proposal.reference_price * Decimal(adding_units)
        return max(Decimal(0), current_country - reducing) + adding

    def reject(
        self, reason: str, proposal: TradeProposal, tenant_id: str = "default"
    ) -> WardenDecision:
        return self._reject(reason, proposal, tenant_id)

    def _reject(
        self, reason: str, proposal: TradeProposal, tenant_id: str
    ) -> WardenDecision:
        self.dispatcher.dispatch(
            TradingAlertCode.RISK_PROPOSAL_REJECTED,
            f"Atlas trade proposal rejected: {reason}",
            tenant_id=tenant_id,
            metadata={
                "reason": reason,
                "decision_id": proposal.decision_id,
                "symbol": proposal.symbol,
                "country": proposal.country,
            },
        )
        return WardenDecision(False, reason, None)
