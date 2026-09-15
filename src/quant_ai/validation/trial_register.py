"""Durable register of research trials.

``candidate_trials`` is only meaningful if nobody can quietly forget the runs that did not
work. Every study run appends one hash-chained record naming the data it consumed, the
configuration it swept and how many candidates that sweep evaluated. Re-running the same
study over a different window therefore leaves a trace, and the cumulative count is what
the promotion evidence has to answer for.

The register is evidence, not a permission: a high trial count never approves anything, it
only says how many times the holdout has been looked at.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_ai.operations.evidence_log import append_record, read_records, verify_chain

EVENT = "research_trial"
SCHEMA = "pramana.trial_register.v1"


def record_trials(
    path: str | Path,
    *,
    study: str,
    candidate_trials: int,
    configuration: Mapping[str, Any],
    data_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one study run to the register and return the stored record."""
    if not study.strip():
        raise ValueError("a registered trial needs a study name")
    if not isinstance(candidate_trials, int) or isinstance(candidate_trials, bool) or candidate_trials < 1:
        raise ValueError("a registered trial must evaluate at least one candidate")
    if not re.fullmatch(r"[0-9a-f]{64}", str(data_sha256)):
        raise ValueError("a registered trial must pin the sha256 of the data it consumed")
    payload = {
        "schema": SCHEMA,
        "study": study.strip(),
        "candidate_trials": candidate_trials,
        "configuration": dict(configuration),
        "data_sha256": str(data_sha256),
    }
    return append_record(path, EVENT, payload, now=now)


def register_summary(path: str | Path, *, study: str | None = None) -> dict[str, Any]:
    """Cumulative trial counts. Refuses to report from a register whose chain is broken."""
    records = read_records(path)
    if not verify_chain(records):
        raise ValueError(f"trial register chain is broken: {path}")
    trials = [
        record["payload"]
        for record in records
        if record.get("event_type") == EVENT and isinstance(record.get("payload"), dict)
    ]
    selected = [item for item in trials if study is None or item.get("study") == study]
    studies: dict[str, int] = {}
    for item in selected:
        name = str(item.get("study") or "unknown")
        studies[name] = studies.get(name, 0) + int(item.get("candidate_trials") or 0)
    windows = {
        str(item.get("data_sha256"))
        for item in selected
        if isinstance(item.get("data_sha256"), str)
    }
    return {
        "schema": SCHEMA,
        "path": str(path),
        "runs": len(selected),
        "candidate_trials": sum(int(item.get("candidate_trials") or 0) for item in selected),
        "distinct_datasets": len(windows),
        "studies": [
            {"study": name, "candidate_trials": total}
            for name, total in sorted(studies.items())
        ],
        "limitation": (
            "Counts how many candidates were evaluated against the recorded datasets. "
            "Reported statistics are not corrected for this multiplicity."
        ),
    }
