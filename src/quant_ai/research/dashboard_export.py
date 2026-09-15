"""Read-only research dashboard projection. Never opens the paper engine ledger for writing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from quant_ai.research.lab import ResearchLab, canonical, instant
from quant_ai.research.portfolio_sim import replay

MAX_ROWS = 10000


def readonly(path, tables):
    uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.execute("PRAGMA query_only=ON")
    actual = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if actual != tables:
        db.close()
        raise ValueError("wrong_database")
    return db


def bounded(db, sql):
    rows = db.execute(sql).fetchmany(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise ValueError("dashboard_input_limit")
    return rows


def metric(label, value):
    return {"label": label, "value": "Unavailable" if value is None else str(value)[:160]}


def comparison(source):
    lab = ResearchLab(source["database"], readonly=True)
    try:
        if lab.db.execute("SELECT count(*) FROM cases").fetchone()[0] > MAX_ROWS:
            raise ValueError("dashboard_input_limit")
        report = lab.report(source["experiment"])
        evidence = lab.export_evidence(source["experiment"])["body"]
    finally:
        lab.close()
    timestamps = [d["decided_at"] for c in evidence["cases"] for d in c["decisions"].values()]
    rows = []
    for name, candidate in report["candidates"].items():
        rows.append(
            {
                "name": name[:80],
                "metrics": [
                    metric("Decisions", candidate["decisions"]),
                    metric("Errors", candidate["errors"]),
                    metric("Missing decisions", candidate["missing_decisions"]),
                    metric("Completed cases", candidate["completed_episodes"]),
                    metric("Completed-case P&L (INR)", candidate["completed_case_pnl_inr"]),
                    metric("Known API cost subtotal (USD)", candidate["api_cost_usd"]),
                    metric("Decisions with unknown cost", candidate["unknown_cost_decisions"]),
                ],
            }
        )
    if len(rows) > 50:
        raise ValueError("dashboard_candidate_limit")
    return {
        "status": "incomplete",
        "observedAt": max(timestamps, key=instant) if timestamps else None,
        "reason": "Independent cases; no winner or promotion approved. API costs are separate.",
        "rows": rows,
    }


def simulation(source):
    db = readonly(source["database"], {"simulation_config", "simulation_events"})
    try:
        db.execute("BEGIN")
        config = json.loads(
            db.execute("SELECT body FROM simulation_config WHERE id=1").fetchone()[0]
        )
        events = []
        for body, digest in bounded(db, "SELECT body,digest FROM simulation_events ORDER BY seq"):
            if hashlib.sha256(body.encode()).hexdigest() != digest:
                raise ValueError("event_integrity_failure")
            events.append(json.loads(body))
        report = replay(config, events)
    finally:
        db.close()
    rows = []
    incomplete = not events
    for name, book in report["books"].items():
        point = book["curve"][-1] if book["curve"] else {}
        equity = point.get("equity_inr")
        incomplete = incomplete or equity is None or book["halted"] or bool(book["orders"])
        rows.append(
            {
                "name": name[:80],
                "metrics": [
                    metric("Cash (INR)", book["cash_inr"]),
                    metric("Equity (INR)", equity),
                    metric("Realized P&L (INR)", book["realized_pnl_inr"]),
                    metric("Fees (INR)", book["fees_inr"]),
                    metric(
                        "Drawdown (%)",
                        str(Decimal(point["drawdown_fraction"]) * 100)
                        if point.get("drawdown_fraction") is not None
                        else None,
                    ),
                    metric(
                        "Open holdings", sum(p["quantity"] > 0 for p in book["positions"].values())
                    ),
                    metric("Pending orders", len(book["orders"])),
                    metric("Entries halted", "Yes" if book["halted"] else "No"),
                ],
            }
        )
    if len(rows) > 50:
        raise ValueError("dashboard_candidate_limit")
    return {
        "status": "incomplete" if incomplete else "available",
        "observedAt": events[-1]["at"] if events else None,
        "reason": "Research simulation on supplied quotes; not a live account or performance certification.",
        "rows": rows,
    }


def company_events(source):
    db = readonly(source["database"], {"event_revisions", "feed_captures", "symbol_mappings"})
    try:
        db.execute("BEGIN")
        captures = [json.loads(row[0]) for row in bounded(db, "SELECT body FROM feed_captures")]
        latest = max(captures, key=lambda c: instant(c["observed_at"]), default=None)
        revisions = db.execute("SELECT count(*) FROM event_revisions").fetchone()[0]
        mappings = db.execute("SELECT count(DISTINCT title) FROM symbol_mappings").fetchone()[0]
    finally:
        db.close()
    available = latest and latest.get("status") == "ok" and revisions and mappings
    return {
        "status": "available" if available else "incomplete",
        "observedAt": latest["observed_at"] if latest else None,
        "reason": "NSE announcement summaries only. Publication provenance does not verify company claims.",
        "rows": [
            {
                "name": "NSE company events",
                "metrics": [
                    metric("Stored revisions", revisions),
                    metric("Mapped company titles", mappings),
                    metric(
                        "Latest capture",
                        "Failed"
                        if latest and latest.get("status") == "error"
                        else "Imported"
                        if latest and latest.get("capture_kind") == "imported"
                        else "Received"
                        if latest
                        else "No capture",
                    ),
                ],
            }
        ],
    }


def snapshot(config, *, now=None):
    modules = []
    for name, title, reader in (
        ("comparison", "Model comparison", comparison),
        ("simulation", "Continuous portfolio simulation", simulation),
        ("companyEvents", "Company events", company_events),
    ):
        result = {
            "status": "unavailable",
            "observedAt": None,
            "rows": [],
            "reason": "No research source configured.",
        }
        if config.get(name):
            try:
                result = reader(config[name])
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
                result["reason"] = (
                    "Research source is missing, invalid or exceeds the export limit."
                )
        modules.append({"id": name, "title": title, **result})
    return {
        "schemaVersion": 1,
        "paperOnly": True,
        "generatedAt": (now or datetime.now(timezone.utc)).isoformat(),
        "modules": modules,
    }


def write_snapshot(path, body):
    """Atomic private publication; an interrupted export cannot expose partial JSON."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".research-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(canonical(body))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    body = snapshot(json.loads(args.config.read_text()))
    write_snapshot(args.output, body)
    print(json.dumps({m["id"]: m["status"] for m in body["modules"]}))


if __name__ == "__main__":
    main()
