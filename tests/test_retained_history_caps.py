"""In-memory decision history is bounded without losing the tail callers read."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from quant_ai.audit.journal import InMemoryAuditJournal
from quant_ai.daemon import DaemonRunner
from quant_ai.execution.audit import XAITrace, XAITraceLogger


def trace(index: int) -> XAITrace:
    return XAITrace(
        f"decision-{index:04d}",
        datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc),
        "INFY",
        ({"agent_id": f"agent-{index}", "domain": "TECHNICAL", "stance": "BULLISH",
          "confidence": "0.5", "expected_return": "0.01", "expected_risk": "0.01",
          "freshness_seconds": "1"},),
        {f"agent-{index}": "0.5"},
        {"side": "BUY", "quantity": "1"},
        ("declared",),
        {"passed": "true", "worst_scenario": "flat", "projected_loss": "0",
         "loss_fraction_of_equity": "0", "flags": ""},
        {"approved": "true", "reason": "within limits"},
    )


def test_trace_logger_retains_the_newest_traces_and_counts_every_one(tmp_path) -> None:
    logger = XAITraceLogger(tmp_path / "xai", retained=3)

    for index in range(10):
        logger.record(trace(index))

    traces = logger.traces()
    assert [item.decision_id for item in traces] == [
        "decision-0007",
        "decision-0008",
        "decision-0009",
    ]
    # The access the daemon and the attribution pass rely on: newest is last.
    assert traces[-1].decision_id == "decision-0009"
    assert logger.recorded_count == 10
    # Every trace is still on disk; only the in-memory window is bounded.
    assert len(list((tmp_path / "xai").glob("*.json"))) == 10


def test_proof_capture_keeps_writing_after_the_trace_window_overflows(tmp_path) -> None:
    logger = XAITraceLogger(retained=2)
    daemon = SimpleNamespace(
        scheduler=SimpleNamespace(pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))),
        tracker=SimpleNamespace(broker=SimpleNamespace(flush=lambda: None)),
        request_stop=lambda: None,
    )
    runner = DaemonRunner(daemon, (), log_path=tmp_path / "ghost.log")
    generated_at = datetime(2026, 9, 15, 9, 30, tzinfo=timezone.utc)

    logger.record(trace(0))
    asyncio.run(runner._capture_xai_proofs(generated_at))
    # Two new decisions in one tick, filling the window exactly.
    logger.record(trace(1))
    logger.record(trace(2))
    asyncio.run(runner._capture_xai_proofs(generated_at))
    # Nothing new: the cap must not make the runner rewrite the window.
    asyncio.run(runner._capture_xai_proofs(generated_at))
    # More decisions than the window holds: the retained ones are still written.
    for index in range(3, 8):
        logger.record(trace(index))
    asyncio.run(runner._capture_xai_proofs(generated_at))

    written = [
        json.loads(line)["proof"]["decision_id"]
        for line in (tmp_path / "ghost.log").read_text().splitlines()
    ]
    assert written == [
        "decision-0000",
        "decision-0001",
        "decision-0002",
        "decision-0006",
        "decision-0007",
    ]
    assert runner._proof_count == logger.recorded_count == 8


def test_audit_journal_retains_the_newest_events_with_an_unbroken_chain() -> None:
    journal = InMemoryAuditJournal(retained=3)

    for index in range(10):
        journal.append("DECISION", {"index": index})

    events = journal.events()
    assert [event.payload["index"] for event in events] == [7, 8, 9]
    # Sequence numbers keep counting past the window; dropped events are not reused.
    assert [event.sequence for event in events] == [8, 9, 10]
    assert events[-1].event_type == "DECISION"
    # Each retained event still links to the one before it.
    assert events[1].previous_hash == events[0].event_hash
    assert events[2].previous_hash == events[1].event_hash
    assert journal.verify_chain()


def test_audit_journal_still_anchors_an_intact_chain_to_genesis() -> None:
    journal = InMemoryAuditJournal(retained=10)

    first = journal.append("SIGNAL", {"symbol": "INFY"})
    second = journal.append("RISK", {"approved": True})

    assert first.previous_hash == "GENESIS"
    assert second.previous_hash == first.event_hash
    assert journal.verify_chain()


def test_audit_journal_rejects_a_tampered_retained_event() -> None:
    journal = InMemoryAuditJournal(retained=5)
    journal.append("SIGNAL", {"symbol": "INFY"})
    second = journal.append("RISK", {"approved": True})

    journal._events[-1] = type(second)(
        second.sequence,
        second.timestamp,
        second.event_type,
        {"approved": False},
        second.previous_hash,
        second.event_hash,
    )

    assert not journal.verify_chain()
