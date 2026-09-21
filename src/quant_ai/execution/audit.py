from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Container, Mapping
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


PROOF_RETENTION_ENV = "PRAMANA_PROOF_RETENTION_DAYS"
# Long enough that a quarter of evidence is always on disk, short enough that a directory
# growing by two files per decision does not outlive the instance. A proof that produced
# an order is never removed by age.
DEFAULT_PROOF_RETENTION_DAYS = 90


def retention_days(environ: Mapping[str, str] | None = None) -> int:
    """Configured retention in days; 0 disables pruning entirely."""
    raw = (environ if environ is not None else os.environ).get(PROOF_RETENTION_ENV, "").strip()
    if not raw:
        return DEFAULT_PROOF_RETENTION_DAYS
    if not raw.isdigit():
        raise ValueError("proof retention days must be a whole number of days")
    return int(raw)


def prune_proofs(
    directory: str | Path, *, protected: Container[str], older_than: datetime
) -> dict[str, int]:
    """Remove proof files that are neither recent nor referenced evidence.

    Nothing else removes these, and the directory grows by two files per decision. A
    proof is kept when its decision id is in ``protected`` - the caller passes every
    decision that produced an order, which is what a fill inspection or an audit needs -
    or when the file is newer than ``older_than``. Recovery does not read this directory:
    the institutional path carries its source trace inside the programme record, so a
    pruned file cannot break a reconciliation. Files whose names are not decision ids are
    left alone, and an unreadable entry is skipped rather than guessed at.
    """
    root = Path(directory)
    if not root.is_dir():
        return {"scanned": 0, "removed": 0, "kept": 0}
    cutoff = older_than.timestamp()
    scanned = removed = kept = 0
    for entry in sorted(root.iterdir()):
        if entry.suffix not in {".json", ".md"} or not entry.is_file():
            continue
        scanned += 1
        try:
            if entry.stem in protected or entry.stat().st_mtime >= cutoff:
                kept += 1
                continue
            entry.unlink()
            removed += 1
        except OSError:
            kept += 1
    return {"scanned": scanned, "removed": removed, "kept": kept}


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
        self, directory: str | Path | None = None, *, retained: int = RETAINED_TRACES,
        tenant_id: str | None = None,
    ) -> None:
        if retained < 1:
            raise ValueError("retained traces must be positive")
        self.directory = Path(directory) if directory is not None else None
        # The account this logger writes for. Recorded on the file, never on XAITrace:
        # the institutional recovery path compares a stored source trace against the
        # dataclass's exact field set, so a new field there would refuse every programme
        # recorded before it. The proof directory is shared, so a file that does not name
        # its account cannot be treated as this account's evidence.
        self.tenant_id = tenant_id
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
            stem.with_suffix(".json").write_text(self.file_payload(trace))
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
        """The trace itself. Consumers compare this against ``fields(XAITrace)``, so it
        carries the dataclass's keys and nothing else."""
        return json.dumps(self._normalize(asdict(trace)), sort_keys=True, separators=(",", ":"))

    def file_payload(self, trace: XAITrace) -> str:
        """The on-disk proof: the trace plus the account that produced it."""
        payload = self._normalize(asdict(trace))
        if self.tenant_id is not None:
            payload["tenant_id"] = self.tenant_id
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

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
            f"- Account: {self.tenant_id or 'unrecorded'}",
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
