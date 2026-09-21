from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from quant_ai.agents.contracts import AgentEvidence
from quant_ai.agents.participation import participation
from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.brokers.base import ExecutionResult
from quant_ai.intelligence.adversarial import StressVerdict
from quant_ai.risk.warden import WardenDecision

PRAMANA_PROOF_DIRECTORY = Path("pramana-proofs")

# A daemon that runs for months would otherwise hold every decision of every session in
# memory. Only the tail is ever read (``traces()[-1]`` for attribution, the unwritten
# tail for proof capture); the durable copy of every trace is the proof directory.
RETAINED_TRACES = 2_000


@dataclass(frozen=True)
class XAITrace:
    decision_id: str
    generated_at: datetime
    subject: str
    input_matrix: tuple[dict[str, str], ...]
    confidence_distribution: dict[str, str]
    proposal: dict[str, str]
    declared_rationales: tuple[str, ...]
    stress_verdict: dict[str, str]
    risk_verdict: dict[str, str]
    # Broker order id of the fill this rationale produced. None on every rejected path, so
    # a proof can only ever be joined to the exact fill it caused - never by timestamp or
    # symbol guesswork.
    order_id: str | None = None
    provenance: dict | None = None
    # The deterministic market regime the decision was made in (``trending_up``,
    # ``ranging``, ``insufficient_history``, ...), lifted from the proposal provenance so
    # decision quality can be broken down by regime without parsing the provenance.
    regime: str | None = None


class XAITraceLogger:
    """Decision-evidence logger. Records declared inputs/rationales, not hidden chain-of-thought."""

    def __init__(
        self, directory: str | Path | None = None, *, retained: int = RETAINED_TRACES
    ) -> None:
        if retained < 1:
            raise ValueError("retained traces must be positive")
        self.directory = Path(directory) if directory is not None else None
        self._traces: deque[XAITrace] = deque(maxlen=retained)
        self._recorded = 0
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        proposal: TradeProposal,
        stress: StressVerdict,
        risk: WardenDecision,
        fill: ExecutionResult | None = None,
    ) -> XAITrace:
        trace = self.build(request, evidence, proposal, stress, risk, fill)
        self.record(trace)
        return trace

    def build(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        proposal: TradeProposal,
        stress: StressVerdict,
        risk: WardenDecision,
        fill: ExecutionResult | None = None,
    ) -> XAITrace:
        """Prepare evidence without a file write or a claimed fill."""
        matrix = tuple(
            {
                "agent_id": item.agent_id,
                "domain": item.domain.value,
                "stance": item.stance.value,
                "confidence": str(item.confidence),
                "expected_return": str(item.expected_return),
                "expected_risk": str(item.expected_risk),
                "freshness_seconds": str(item.source_freshness_seconds),
                **participation(item),
                "rationale_json": json.dumps(item.rationale, ensure_ascii=False),
            }
            for item in evidence
        )
        trace = XAITrace(
            proposal.decision_id,
            request.observed_at,
            request.subject,
            matrix,
            {item.agent_id: str(item.confidence) for item in evidence},
            {
                "side": proposal.side.value if proposal.side else "NONE",
                "quantity": str(proposal.quantity),
                "reference_price": str(proposal.reference_price),
                "confidence": str(proposal.confidence),
                "expected_return": str(proposal.expected_return),
                "expected_risk": str(proposal.expected_risk),
            },
            proposal.rationale,
            {
                "passed": str(stress.passed).lower(),
                "worst_scenario": stress.worst_scenario,
                "projected_loss": str(stress.projected_loss),
                "loss_fraction_of_equity": str(stress.loss_fraction_of_equity),
                # The book figures the veto now turns on, alongside the
                # single-trade ones the proof has always carried.
                "book_worst_scenario": stress.book_worst_scenario,
                "book_loss": str(stress.book_loss),
                "book_loss_fraction_of_equity": str(stress.book_loss_fraction_of_equity),
                "flags": ",".join(stress.flags),
            },
            {
                "approved": str(risk.approved).lower(),
                "reason": risk.reason,
            },
            fill.order_id if fill is not None else None,
            proposal.provenance,
            self._regime(proposal.provenance),
        )
        return trace

    @staticmethod
    def _regime(provenance: dict | None) -> str | None:
        label = provenance.get("regime") if isinstance(provenance, dict) else None
        return label if isinstance(label, str) else None

    def record(self, trace: XAITrace) -> None:
        """Publish the in-memory/file projection of a prepared trace."""
        self._traces.append(trace)
        self._recorded += 1
        if self.directory is not None:
            stem = self.directory / trace.decision_id
            stem.with_suffix(".json").write_text(self.to_json(trace))
            stem.with_suffix(".md").write_text(self.to_markdown(trace))

    def traces(self) -> tuple[XAITrace, ...]:
        """The retained tail, oldest first, so ``traces()[-1]`` is still the newest."""
        return tuple(self._traces)

    @property
    def recorded_count(self) -> int:
        """Every trace ever recorded, including those the tail cap has since dropped.

        Callers that track how far they have consumed must count against this rather
        than ``len(traces())``, which stops growing once the cap is reached.
        """
        return self._recorded

    @staticmethod
    def _normalize(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, tuple):
            return [XAITraceLogger._normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: XAITraceLogger._normalize(item) for key, item in value.items()}
        return value

    def to_json(self, trace: XAITrace) -> str:
        return json.dumps(self._normalize(asdict(trace)), sort_keys=True, separators=(",", ":"))

    def to_markdown(self, trace: XAITrace) -> str:
        lines = [
            f"# PRAMANA XAI Decision Report {trace.decision_id}",
            "",
            f"- Subject: {trace.subject}",
            f"- Generated: {trace.generated_at.isoformat()}",
            f"- Proposal: {trace.proposal['side']} x {trace.proposal['quantity']}",
            f"- Stress: {trace.stress_verdict['passed']} ({trace.stress_verdict['worst_scenario']})",
            f"- Risk: {trace.risk_verdict['approved']} ({trace.risk_verdict['reason']})",
            f"- Order: {trace.order_id or 'none (not filled)'}",
            f"- Regime: {trace.regime or 'unrecorded'}",
            "",
            "## Specialist evidence",
        ]
        for row in trace.input_matrix:
            lines.append(
                f"- {row['agent_id']}: {row['stance']} confidence={row['confidence']} "
                f"return={row['expected_return']} risk={row['expected_risk']}"
            )
        lines.extend(("", "## Declared Atlas rationales"))
        lines.extend(f"- {item}" for item in trace.declared_rationales)
        return "\n".join(lines) + "\n"
