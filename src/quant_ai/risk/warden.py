from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import OrderIntent, PortfolioSnapshot
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

    def __init__(self, dispatcher: TradingNotificationDispatcher | None = None) -> None:
        self.dispatcher = dispatcher or TradingNotificationDispatcher()

    def evaluate(
        self,
        proposal: TradeProposal,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        country_exposure: dict[str, Decimal] | None = None,
        tenant_id: str = "default",
    ) -> WardenDecision:
        if not plan.trading_allowed:
            return self._reject("capital_plan_halted", proposal, tenant_id)
        if proposal.side is None:
            return self._reject("atlas_non_actionable_proposal", proposal, tenant_id)
        if proposal.quantity <= 0 or proposal.reference_price <= 0:
            return self._reject("invalid_trade_proposal", proposal, tenant_id)

        notional = proposal.reference_price * proposal.quantity
        current_country = (country_exposure or {}).get(proposal.country, Decimal(0))
        country_limit = portfolio.equity * plan.max_country_allocation_fraction
        if current_country + notional > country_limit:
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
        )
        decision = RiskFirewall(policy).evaluate(order, portfolio)
        if not decision.approved:
            return self._reject(decision.reason, proposal, tenant_id)
        return WardenDecision(True, "approved", order)

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
