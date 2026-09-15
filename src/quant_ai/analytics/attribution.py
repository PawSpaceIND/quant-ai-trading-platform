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
    """Scores each specialist by realised outcome and weights its future conviction.

    This is the one part of the engine that adapts from results. An agent that keeps
    being right is heard louder on later decisions; one that keeps being wrong is
    quieted. The weight band is deliberately narrow, so a run of luck cannot hand any
    single agent the book.

    Records are kept per agent and per regime. An agent that reads trends well can be
    useless in a range, and one blended number hides exactly that. Regime weights apply
    only after ``MIN_REGIME_OBSERVATIONS``; until then the blended record governs.

    The engine holds no database of its own. It is rebuilt at boot from the decision
    journal by :func:`restore_from_journal`, so what it learned survives the daily
    restart instead of resetting every morning.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[Decimal]] = {}

    def record(
        self, agent_ids: tuple[str, ...], realized_pnl: Decimal, regime: str | None = None
    ) -> None:
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
    engine: AgentAttributionEngine, broker, *, tenant_id: str, since=None
) -> int:
    """Rebuild an engine's scores from closed trades in the decision journal.

    The journal is the durable record of what each specialist said and what the trade
    it produced actually earned, so attribution is rebuilt from it at boot rather than
    kept in a store of its own. Without this the engine forgets every night, and the
    daily token restart means it would never learn anything at all.

    The journal also attributes more accurately than the live path: it scores the agents
    that argued for the *entry*, in the regime that entry was made in, rather than
    whichever agents happened to speak on the tick the position closed.

    Returns the number of closed trades restored. Any failure logs and restores nothing;
    an unreadable history is a cold start, never a broken boot.
    """
    import json

    from quant_ai.analytics.decision_journal import load_rows

    restored = 0
    try:
        rows = load_rows(broker, tenant_id=tenant_id, since=since)
    except Exception:  # a cold start beats a daemon that will not boot
        LOGGER.exception("attribution_restore_failed tenant=%s", tenant_id)
        return 0
    for row in rows:
        raw = row.get("realized_net_pnl")
        if raw in (None, ""):
            continue  # still open, or never filled: nothing realised to attribute
        try:
            agents = json.loads(row.get("agents") or "{}")
            agent_ids = tuple(str(name) for name in agents)
            if not agent_ids:
                continue
            engine.record(agent_ids, Decimal(str(raw)), row.get("regime"))
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            LOGGER.warning("attribution_restore_skipped_row id=%s", row.get("decision_id"))
            continue
        restored += 1
    if restored:
        LOGGER.info("attribution_restored trades=%d tenant=%s", restored, tenant_id)
    return restored
