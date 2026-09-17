"""Offline recovery checks for explicitly selected research state.

Logical database hashes include every stored row, including raw feed BLOBs. They
prove preservation against the retained manifest, not market/model authenticity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from quant_ai.execution.broker_journal import BrokerJournal
from quant_ai.governance.runtime_manifest import digest
from quant_ai.research.lab import ResearchLab, canonical
from quant_ai.research.portfolio_sim import PortfolioJournal

TABLES = {
    "replay_ledger": {"paper_accounts", "paper_positions", "paper_ledger", "sqlite_sequence",
                      "paper_cost_ledger", "paper_protection_evidence", "paper_decision_evidence",
                      "paper_idempotency", "paper_exit_cooldowns", "paper_replay_runs",
                      "paper_replay_run_points"},
    "broker_journal": {"broker_journal_meta", "broker_captures"},
    "experiment_journal": {"experiments", "cases", "decisions", "outcomes"},
    "portfolio_journal": {"simulation_config", "simulation_events"},
    "company_events": {"event_revisions", "feed_captures", "symbol_mappings"},
}
REPLAY_OPTIONAL = {"paper_protective_fill_outbox", "risk_daily_equity", "risk_control_state", "paper_replay_valuations", "paper_derivative_margin"}
STORAGE = {**dict.fromkeys(TABLES, "sqlite"), "file": "file", "directory": "directory"}


def sha(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def database_evidence(path: Path, kind: str) -> dict:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Research database integrity failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Research database foreign-key discrepancy")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        valid_tables = (TABLES[kind] <= tables <= TABLES[kind] | REPLAY_OPTIONAL
                        if kind == "replay_ledger" else tables == TABLES[kind])
        if not valid_tables:
            raise ValueError("Research database kind/schema mismatch")
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        counts, hashes = {}, {}
        for table in sorted(tables):
            # Table names come from the fixed allowlist, never SQL supplied by a caller.
            rows = [canonical([
                {"blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row
            ]) for row in db.execute(f'SELECT * FROM "{table}"')]
            counts[table] = len(rows)
            hashes[table] = sha(sorted(rows))
        return {"schemaSha256": sha(schema), "rowCounts": counts,
                "logicalSha256": sha({"schema": schema, "tables": hashes})}


def inspect(path: Path, kind: str) -> dict:
    """Read restored/captured files only. Never fetch, call providers or activate state."""
    if kind not in TABLES:
        return {"check": "manifest file hashes only; contents not semantically validated"}
    result = database_evidence(path, kind)
    if kind == "replay_ledger":
        from quant_ai.validation.run_comparison import capture
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            runs = db.execute("SELECT run_id,tenant_id,status,metadata,sha256 FROM paper_replay_runs ORDER BY run_id").fetchall()
            if db.execute("SELECT 1 FROM paper_replay_run_points p LEFT JOIN paper_replay_runs r ON p.run_id=r.run_id WHERE r.run_id IS NULL").fetchone():
                raise ValueError("Orphan replay point")
            checked = {}
            for run_id, tenant, status, raw, retained_sha in runs:
                metadata = json.loads(raw)
                points = [{"sequence": r[0], "ledgerId": r[1], "payload": json.loads(r[2])}
                          for r in db.execute("SELECT sequence,ledger_id,payload FROM paper_replay_run_points WHERE run_id=? ORDER BY sequence", (run_id,))]
                if (metadata.get("runId") != run_id or metadata.get("tenantId") != tenant
                        or status not in {"running", "failed", "invalid", "complete"}
                        or [p["sequence"] for p in points] != list(range(1, len(points)+1))):
                    raise ValueError("Replay run identity or sequence changed")
                expected = digest({"metadata":metadata, "points":points, "status":status})
                if retained_sha != (None if status == "running" else expected):
                    raise ValueError("Replay run hash changed")
                checked[run_id] = {"status":status, "points":len(points), "sha256":retained_sha}
        # The bundle requires stopped writers and checks source inventory around capture.
        for run_id, tenant, status, _, _ in runs:
            if status == "complete":
                checked[run_id]["sourceSha256"] = digest(capture(path, tenant, "replay", run_id))
        result["runs"] = checked
        result["check"] = "database integrity, all-row hash, retained run hashes and completed INR/NSE cash replay source capture; unfinished runs remain unqualified"
    elif kind == "broker_journal":
        with closing(BrokerJournal(path, readonly=True)) as journal:
            report = journal.report()
            result["journalId"] = report["journalId"]
            result["headHash"] = report["headHash"]
            result["reportSha256"] = sha(report)
        result["check"] = "database integrity, all-row hash, capture chain and deterministic lifecycle replay"
    elif kind == "experiment_journal":
        with closing(ResearchLab(path, readonly=True)) as lab:
            result["experiments"] = {
                name: {"evidenceSha256": lab.export_evidence(name)["sha256"],
                       "reportSha256": sha(lab.report(name))}
                for (name,) in lab.db.execute("SELECT id FROM experiments ORDER BY id").fetchall()
            }
        result["check"] = "database integrity, all-row hash and deterministic case report"
    elif kind == "portfolio_journal":
        with closing(PortfolioJournal(path, readonly=True)) as journal:
            result["evidenceSha256"] = journal.export_evidence()["sha256"]
            result["reportSha256"] = sha(journal.report())
        result["check"] = "database integrity, all-row hash and deterministic portfolio replay"
    else:
        result["check"] = "database integrity and all-row hash, including raw captures and mappings"
    return result
