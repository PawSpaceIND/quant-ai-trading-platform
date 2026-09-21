"""A proof file must name the account that wrote it, and the directory must not grow forever.

The proof directory is shared and nothing pruned it. Two containers running as the same
tenant is a deployment fact, not a property of a file, so ownership has to be written down.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from quant_ai.execution.audit import (
    DEFAULT_PROOF_RETENTION_DAYS,
    PROOF_RETENTION_ENV,
    XAITrace,
    XAITraceLogger,
    prune_proofs,
    retention_days,
)

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def trace(decision_id: str, *, order_id: str | None = None) -> XAITrace:
    return XAITrace(
        decision_id=decision_id, generated_at=NOW, subject="NSE:INFY",
        input_matrix=({"agent_id": "indian-equities", "stance": "BUY", "confidence": "0.62",
                       "expected_return": "0.012", "expected_risk": "0.008"},),
        confidence_distribution={"BUY": "0.62"},
        proposal={"side": "BUY", "quantity": "5"}, declared_rationales=("synthetic",),
        stress_verdict={"passed": "true", "worst_scenario": "synthetic"},
        risk_verdict={"approved": "true", "reason": "synthetic"},
        order_id=order_id, provenance=None, regime="ranging",
    )


def test_a_written_proof_names_its_account_without_changing_the_trace(tmp_path):
    logger = XAITraceLogger(tmp_path, tenant_id="ghost")
    logger.record(trace("d-1"))
    written = json.loads((tmp_path / "d-1.json").read_text())
    assert written["tenant_id"] == "ghost"
    assert "Account: ghost" in (tmp_path / "d-1.md").read_text()

    # The trace itself must keep exactly the dataclass's keys: the institutional recovery
    # path compares a stored source trace against `fields(XAITrace)` for equality, so an
    # extra key there would refuse every programme recorded before this change.
    payload = json.loads(logger.to_json(trace("d-1")))
    assert set(payload) == {field.name for field in fields(XAITrace)}
    assert "tenant_id" not in payload


def test_a_logger_without_an_account_writes_no_ownership_claim(tmp_path):
    XAITraceLogger(tmp_path).record(trace("d-2"))
    assert "tenant_id" not in json.loads((tmp_path / "d-2.json").read_text())


def test_pruning_keeps_recent_files_and_every_fill_proof(tmp_path):
    logger = XAITraceLogger(tmp_path, tenant_id="ghost")
    for decision_id in ("old-abstained", "old-filled", "fresh-abstained"):
        logger.record(trace(decision_id, order_id="PAPER-1" if "filled" in decision_id else None))
    old = (NOW - timedelta(days=200)).timestamp()
    for stem in ("old-abstained", "old-filled"):
        for suffix in (".json", ".md"):
            os.utime(tmp_path / f"{stem}{suffix}", (old, old))
    (tmp_path / "notes.txt").write_text("not a proof")

    result = prune_proofs(tmp_path, protected={"old-filled"},
                          older_than=NOW - timedelta(days=90))
    assert result["removed"] == 2
    # A decision that produced an order is audit evidence and is kept whatever its age.
    assert (tmp_path / "old-filled.json").exists() and (tmp_path / "old-filled.md").exists()
    assert (tmp_path / "fresh-abstained.json").exists()
    assert not (tmp_path / "old-abstained.json").exists()
    assert not (tmp_path / "old-abstained.md").exists()
    # Anything that is not a proof is never touched.
    assert (tmp_path / "notes.txt").exists()


def test_pruning_a_missing_directory_is_a_no_op(tmp_path):
    assert prune_proofs(tmp_path / "absent", protected=set(),
                        older_than=NOW)["scanned"] == 0


def test_retention_is_configurable_and_refuses_a_value_it_cannot_read():
    assert retention_days({}) == DEFAULT_PROOF_RETENTION_DAYS
    assert retention_days({PROOF_RETENTION_ENV: "30"}) == 30
    # Zero disables pruning outright: keeping evidence is the safe direction.
    assert retention_days({PROOF_RETENTION_ENV: "0"}) == 0
    with pytest.raises(ValueError):
        retention_days({PROOF_RETENTION_ENV: "ninety"})
    with pytest.raises(ValueError):
        retention_days({PROOF_RETENTION_ENV: "-1"})


def test_pruning_never_removes_a_proof_it_cannot_attribute_to_this_account(tmp_path):
    """The directory is shared, so one account's protected set must not delete another's."""
    ours = XAITraceLogger(tmp_path, tenant_id="ghost")
    theirs = XAITraceLogger(tmp_path, tenant_id="other")
    legacy = XAITraceLogger(tmp_path)
    ours.record(trace("ours-old"))
    theirs.record(trace("theirs-old", order_id="PAPER-2"))
    legacy.record(trace("legacy-old"))
    old = (NOW - timedelta(days=200)).timestamp()
    for stem in ("ours-old", "theirs-old", "legacy-old"):
        for suffix in (".json", ".md"):
            os.utime(tmp_path / f"{stem}{suffix}", (old, old))

    result = prune_proofs(tmp_path, protected=set(),
                          older_than=NOW - timedelta(days=90), tenant_id="ghost")
    assert result["removed"] == 2
    assert not (tmp_path / "ours-old.json").exists()
    # Another account's fill evidence is not this account's to delete.
    assert (tmp_path / "theirs-old.json").exists() and (tmp_path / "theirs-old.md").exists()
    # A proof written before proofs named their account cannot be attributed, so it stays.
    assert (tmp_path / "legacy-old.json").exists() and (tmp_path / "legacy-old.md").exists()


def test_pruning_without_an_account_still_removes_only_aged_unprotected_files(tmp_path):
    XAITraceLogger(tmp_path, tenant_id="ghost").record(trace("aged"))
    old = (NOW - timedelta(days=200)).timestamp()
    for suffix in (".json", ".md"):
        os.utime(tmp_path / f"aged{suffix}", (old, old))
    assert prune_proofs(tmp_path, protected=set(),
                        older_than=NOW - timedelta(days=90))["removed"] == 2
