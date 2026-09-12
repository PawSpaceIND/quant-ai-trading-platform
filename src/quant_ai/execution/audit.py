from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from quant_ai.agents.contracts import AgentEvidence
from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.intelligence.adversarial import StressVerdict
from quant_ai.risk.warden import WardenDecision

PRAMANA_PROOF_DIRECTORY = Path("pramana-proofs")


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


class XAITraceLogger:
    """Decision-evidence logger. Records declared inputs/rationales, not hidden chain-of-thought."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else None
        self._traces: list[XAITrace] = []
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        proposal: TradeProposal,
        stress: StressVerdict,
        risk: WardenDecision,
    ) -> XAITrace:
        matrix = tuple(
            {
                "agent_id": item.agent_id,
                "domain": item.domain.value,
                "stance": item.stance.value,
                "confidence": str(item.confidence),
                "expected_return": str(item.expected_return),
                "expected_risk": str(item.expected_risk),
                "freshness_seconds": str(item.source_freshness_seconds),
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
                "flags": ",".join(stress.flags),
            },
            {
                "approved": str(risk.approved).lower(),
                "reason": risk.reason,
            },
        )
        self._traces.append(trace)
        if self.directory is not None:
            stem = self.directory / proposal.decision_id
            stem.with_suffix(".json").write_text(self.to_json(trace))
            stem.with_suffix(".md").write_text(self.to_markdown(trace))
        return trace

    def traces(self) -> tuple[XAITrace, ...]:
        return tuple(self._traces)

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
