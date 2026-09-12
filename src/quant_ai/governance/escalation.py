from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EscalationPriority(str, Enum):
    INFORMATIONAL = "INFORMATIONAL"
    REVIEW = "REVIEW"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class EscalationDecision:
    priority: EscalationPriority
    contact_founder_now: bool
    reasons: tuple[str, ...]


def classify_founder_contact(critical_actions: tuple[str, ...], decisions_required: tuple[str, ...]) -> EscalationDecision:
    if critical_actions:
        return EscalationDecision(EscalationPriority.CRITICAL, True, critical_actions)
    if decisions_required:
        return EscalationDecision(EscalationPriority.REVIEW, False, decisions_required)
    return EscalationDecision(EscalationPriority.INFORMATIONAL, False, ())
