from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import AssetClass, PortfolioSnapshot, Side


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


class AdversarialStressAgent:
    DEFAULT_SCENARIOS = (
        StressScenario("YIELDS_PLUS_50BPS", Decimal("-0.035"), 50),
        StressScenario("EQUITY_GAP_DOWN_5PCT", Decimal("-0.05")),
        StressScenario("CRISIS_GAP_DOWN_8PCT", Decimal("-0.08")),
    )

    def __init__(self, tolerance_fraction: Decimal = Decimal("0.01")) -> None:
        if not Decimal(0) < tolerance_fraction <= Decimal(1):
            raise ValueError("stress tolerance must be in (0, 1]")
        self.tolerance_fraction = tolerance_fraction

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
        losses: list[tuple[Decimal, str]] = []
        for scenario in scenario_set:
            shock = scenario.equity_shock
            if proposal.asset_class in {AssetClass.BOND} and scenario.rates_shock_bps:
                shock = -Decimal(scenario.rates_shock_bps) / Decimal(10000) * Decimal(5)
            signed = shock if proposal.side == Side.BUY else -shock
            pnl = notional * signed
            losses.append((max(Decimal(0), -pnl), scenario.name))
        projected_loss, worst = max(losses, key=lambda item: item[0], default=(Decimal(0), "NONE"))
        fraction = projected_loss / portfolio.equity if portfolio.equity > 0 else Decimal(1)
        passed = fraction <= self.tolerance_fraction
        flags = () if passed else ("STRESS_VETO",)
        return StressVerdict(passed, worst, projected_loss, fraction, flags)
