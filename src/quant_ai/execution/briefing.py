from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.execution.session import MarketState

FOUNDER_EXECUTION_BRIEF_HEADER = "PRAMANA CORE: Execution & Risk Digest"


@dataclass(frozen=True)
class FounderExecutionBrief:
    generated_at: datetime
    market_state: MarketState
    subject: str
    mode: str
    swarm_consensus: tuple[str, ...]
    risk_decision: str
    paper_order_ids: tuple[str, ...]
    provider_status: tuple[str, ...]
    market_regime: str = "UNKNOWN"
    stress_verdict: str = "NOT_RUN"
    # None when the return series was too short to support an annualised ratio. The
    # digest says so rather than printing a number that no sample supports.
    sharpe_ratio: Decimal | None = None
    sortino_ratio: Decimal | None = None
    xai_rationales: tuple[str, ...] = ()
    analytics_source: str = "instrument_price_returns_not_strategy"
    # Periods per year the ratios above were annualised with; 0 when none was available.
    analytics_periods_per_year: int = 0

    def to_json(self) -> str:
        payload = asdict(self)
        payload["header"] = FOUNDER_EXECUTION_BRIEF_HEADER
        payload["generated_at"] = self.generated_at.isoformat()
        payload["market_state"] = self.market_state.value
        payload["sharpe_ratio"] = None if self.sharpe_ratio is None else str(self.sharpe_ratio)
        payload["sortino_ratio"] = None if self.sortino_ratio is None else str(self.sortino_ratio)
        payload["analytics_periods_per_year"] = self.analytics_periods_per_year
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
