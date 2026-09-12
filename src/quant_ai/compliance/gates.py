from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from quant_ai.domain.models import AssetClass, Market


class ExecutionMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True)
class ComplianceContext:
    market: Market
    asset_class: AssetClass
    execution_mode: ExecutionMode
    live_approved: bool = False
    jurisdiction_approved: bool = False


def execution_allowed(context: ComplianceContext) -> bool:
    if context.execution_mode in {ExecutionMode.RESEARCH, ExecutionMode.PAPER}:
        return True
    return context.live_approved and context.jurisdiction_approved
