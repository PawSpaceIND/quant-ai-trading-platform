"""Offline recovery checks for explicitly selected research state.

Logical database hashes include every stored row, including raw feed BLOBs. They
prove preservation against the retained manifest, not market/model authenticity.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from quant_ai.execution.broker_journal import BrokerJournal
from quant_ai.research.lab import ResearchLab, canonical
from quant_ai.research.portfolio_sim import PortfolioJournal

TABLES = {
    "broker_journal": {"broker_journal_meta", "broker_captures"},
    "experiment_journal": {"experiments", "cases", "decisions", "outcomes"},
    "portfolio_journal": {"simulation_config", "simulation_events"},
    "company_events": {"event_revisions", "feed_captures", "symbol_mappings"},
}
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
        if tables != TABLES[kind]:
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
    if kind == "broker_journal":
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
