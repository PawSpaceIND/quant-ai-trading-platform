from dataclasses import dataclass

from quant_ai.domain.models import RiskMode


@dataclass(frozen=True)
class TenantAccount:
    tenant_id: str
    display_name: str
    base_currency: str
    risk_mode: RiskMode = RiskMode.BALANCED
    live_trading_enabled: bool = False
