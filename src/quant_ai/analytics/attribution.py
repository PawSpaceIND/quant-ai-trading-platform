from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence

LOGGER = logging.getLogger(__name__)

# A regime-specific weight is only trusted once the agent has been scored this often in
# that regime. Below it the blended record is used: a handful of trades in one regime is
# a story, not evidence, and an over-eager weight would amplify noise into conviction.
MIN_REGIME_OBSERVATIONS = 10
BLENDED = ""


@dataclass(frozen=True)
class AgentAttribution:
    agent_id: str
    observations: int
    wins: int
    losses: int
    pnl: Decimal
    hit_rate: Decimal
    conviction_weight: Decimal
    regime: str = BLENDED


class AgentAttributionEngine:
    """Bounded specialist influence from resolved outcomes, not demonstrated skill.

    The live factory calls restore_from_journal, binding this engine to one tenant's
    durable decision journal. A bound engine refreshes before weighting evidence.
    Its old record(agent_ids, delta) callback now only requests that same refresh:
    a latest trace or account-wide delta cannot assign credit to an unrelated entry.

    Unbound instances retain the explicit record API for offline replay/tests. They
    never import live history unless their caller explicitly binds a journal.
    Both modes retain the original 0.75--1.25 band and regime sample threshold.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[Decimal]] = {}
        self._journal_binding = None
        self.feedback: dict = {"status": "not_refreshed", "credited_entries": 0}

    def _refresh_bound(self) -> None:
        if self._journal_binding is not None:
            from quant_ai.analytics.feedback import refresh_feedback

            broker, tenant, since = self._journal_binding
            refresh_feedback(self, broker, tenant_id=tenant, since=since)

    def record(
        self, agent_ids: tuple[str, ...], realized_pnl: Decimal, regime: str | None = None
    ) -> None:
        if self._journal_binding is not None:
            self._refresh_bound()
            return
        if not agent_ids:
            return
        share = realized_pnl / Decimal(len(agent_ids))
        label = (regime or "").strip()
        for agent_id in agent_ids:
            self._records.setdefault((agent_id, BLENDED), []).append(share)
            if label:
                self._records.setdefault((agent_id, label), []).append(share)

    def attribution(self, regime: str | None = None) -> tuple[AgentAttribution, ...]:
        """Scores for one regime, or the blended record when no regime is given."""
        wanted = (regime or "").strip()
        rows = []
        for (agent_id, label), pnls in sorted(self._records.items()):
            if label != wanted:
                continue
            rows.append(self._score(agent_id, label, pnls))
        return tuple(rows)

    @staticmethod
    def _score(agent_id: str, regime: str, pnls: list[Decimal]) -> AgentAttribution:
        wins = sum(1 for pnl in pnls if pnl > 0)
        losses = sum(1 for pnl in pnls if pnl < 0)
        observations = len(pnls)
        hit_rate = Decimal(wins) / Decimal(observations) if observations else Decimal("0.5")
        weight = min(Decimal("1.25"), max(Decimal("0.75"), Decimal("0.75") + hit_rate * Decimal("0.5")))
        return AgentAttribution(
            agent_id, observations, wins, losses, sum(pnls, Decimal(0)), hit_rate, weight, regime
        )

    def weight_for(self, agent_id: str, regime: str | None = None) -> tuple[Decimal, str]:
        """This agent's weight and the record it came from: the regime, or blended."""
        label = (regime or "").strip()
        if label:
            scoped = self._records.get((agent_id, label))
            if scoped is not None and len(scoped) >= MIN_REGIME_OBSERVATIONS:
                return self._score(agent_id, label, scoped).conviction_weight, label
        blended = self._records.get((agent_id, BLENDED))
        if blended is None:
            return Decimal(1), "unscored"
        return self._score(agent_id, BLENDED, blended).conviction_weight, "blended"

    def weight_evidence(
        self, evidence: tuple[AgentEvidence, ...], regime: str | None = None
    ) -> tuple[AgentEvidence, ...]:
        self._refresh_bound()
        adjusted = []
        for item in evidence:
            weight, source = self.weight_for(item.agent_id, regime)
            confidence = min(Decimal(1), max(Decimal(0), item.confidence * weight))
            adjusted.append(AgentEvidence(
                item.agent_id, item.domain, item.subject, item.stance, confidence,
                item.expected_return, item.expected_risk,
                item.rationale + (f"attribution_weight={weight}:{source}",),
                item.observed_at, item.source_freshness_seconds,
            ))
        return tuple(adjusted)


def restore_from_journal(
    engine: AgentAttributionEngine, broker, *, tenant_id: str, since=None, now=None
) -> int:
    """Bind and replay one tenant's audited, resolved entry outcomes.

    Repeat restoration replaces the projection instead of adding the same trades.
    Missing/inconsistent history clears adaptive weights and records refusal. The
    caller can inspect engine.feedback; failed evidence never breaks protection.
    """
    from quant_ai.analytics.feedback import refresh_feedback

    engine._journal_binding = (broker, tenant_id, since)
    restored = refresh_feedback(engine, broker, tenant_id=tenant_id, since=since, now=now)
    if engine.feedback.get("status") == "refused":
        LOGGER.warning("attribution_restore_refused tenant=%s", tenant_id)
    return restored
