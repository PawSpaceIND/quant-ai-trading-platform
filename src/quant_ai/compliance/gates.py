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
    # Type annotations do not validate values arriving at this boundary. Never
    # interpret a nonempty string (including "false") as an approval, or allow an
    # unknown execution mode to fall through to the live-approval expression.
    if type(context) is not ComplianceContext:
        return False
    if (type(context.execution_mode) is not ExecutionMode
            or type(context.market) is not Market
            or type(context.asset_class) is not AssetClass):
        return False
    if type(context.live_approved) is not bool or type(context.jurisdiction_approved) is not bool:
        return False
    if context.execution_mode in {ExecutionMode.RESEARCH, ExecutionMode.PAPER}:
        return True
    return (context.execution_mode is ExecutionMode.LIVE
            and context.live_approved is True and context.jurisdiction_approved is True)
