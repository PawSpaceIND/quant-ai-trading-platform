"""Offline, long-only research experiments. No broker or provider network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def number(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("invalid_number")
    return result


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identifier_required")
    return value


def integer(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("invalid_integer")
    return value


class ResearchLab:
    """Append-only through this API; local SQLite is not tamper-proof storage.

    Each case is an independent, flat-to-flat NSE equity/ETF experiment with a
    common starting cash allocation. It is NOT a continuous portfolio simulator.
    """

    def __init__(self, path, *, readonly=False):
        if readonly:
            uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
        else:
            self.db = sqlite3.connect(path)
        tables = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected = {"experiments", "cases", "decisions", "outcomes"}
        if (tables and tables != expected) or (readonly and tables != expected):
            self.db.close()
            raise ValueError("not_a_research_database")
        if readonly:
            self.db.execute("PRAGMA query_only=ON")
            return
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cases (
            experiment TEXT NOT NULL REFERENCES experiments(id),
            id TEXT NOT NULL, packet TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY(experiment,id));
        CREATE TABLE IF NOT EXISTS decisions (
            experiment TEXT NOT NULL, case_id TEXT NOT NULL,
            candidate TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(experiment,case_id,candidate),
            FOREIGN KEY(experiment,case_id) REFERENCES cases(experiment,id));
        CREATE TABLE IF NOT EXISTS outcomes (
            experiment TEXT NOT NULL, case_id TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(experiment,case_id),
            FOREIGN KEY(experiment,case_id) REFERENCES cases(experiment,id));
        """)

    def close(self):
        self.db.close()

    def create(self, experiment, config):
        identity(experiment)
        candidates = config["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("candidate_list_required")
        if len(candidates) < 2 or len(candidates) != len(set(candidates)):
            raise ValueError("distinct_candidates_required")
        if any(not isinstance(c, str) or not c.strip() for c in candidates):
            raise ValueError("candidate_name_required")
        if config["baseline"] not in candidates:
            raise ValueError("baseline_required")
        if config["mode"] not in ("historical", "forward_paper"):
            raise ValueError("paper_only")
        for key in ("expected_cases", "max_quantity", "max_quote_age_seconds"):
            integer(config[key], positive=True)
        number(config["capital_per_case"], positive=True)
        for key in ("fee_bps", "slippage_bps"):
            if number(config[key]) >= 10000:
                raise ValueError("invalid_cost_assumption")
        if not config.get("protocol_version"):
            raise ValueError("protocol_version_required")
        encoded = canonical(config)
        with self.db:
            self.db.execute(
                "INSERT INTO experiments VALUES (?,?,?)",
                (experiment, encoded, hashlib.sha256(encoded.encode()).hexdigest()),
            )

    def config(self, experiment):
        row = self.db.execute(
            "SELECT config,digest FROM experiments WHERE id=?", (experiment,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown_experiment")
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("experiment_integrity_failure")
        return json.loads(row[0])

    def add_case(self, experiment, case_id, packet):
        identity(case_id)
        config = self.config(experiment)
        decision_at = instant(packet["decision_at"])
        quote_at = instant(packet["quote_at"])
        if (
            not timedelta(0)
            <= decision_at - quote_at
            <= timedelta(seconds=config["max_quote_age_seconds"])
        ):
            raise ValueError("stale_or_future_quote")
        if packet["exchange"] != "NSE" or packet["asset_class"] not in ("EQUITY", "ETF"):
            raise ValueError("unsupported_instrument")
        if packet["currency"] != "INR" or not packet["symbol"]:
            raise ValueError("invalid_instrument")
        number(packet["reference_price"], positive=True)
        if packet["adjustment_status"] not in ("verified", "unverified"):
            raise ValueError("adjustment_status_required")
        if not packet["sources"]:
            raise ValueError("source_evidence_required")
        ids = set()
        for source in packet["sources"]:
            if source["id"] in ids or not source["id"]:
                raise ValueError("duplicate_source")
            ids.add(source["id"])
            if instant(source["available_at"]) > decision_at:
                raise ValueError("future_source")
            if not source["provenance"]:
                raise ValueError("source_provenance_required")
        encoded = canonical(packet)
        with self.db:
            self.db.execute("UPDATE experiments SET id=id WHERE id=?", (experiment,))
            count = self.db.execute(
                "SELECT count(*) FROM cases WHERE experiment=?", (experiment,)
            ).fetchone()[0]
            if count >= config["expected_cases"]:
                raise ValueError("case_budget_exceeded")
            self.db.execute(
                "INSERT INTO cases VALUES (?,?,?,?)",
                (experiment, case_id, encoded, hashlib.sha256(encoded.encode()).hexdigest()),
            )

    def record_decision(self, experiment, case_id, candidate, body):
        config = self.config(experiment)
        if candidate not in config["candidates"]:
            raise ValueError("unknown_candidate")
        row = self.db.execute(
            "SELECT packet,digest FROM cases WHERE experiment=? AND id=?", (experiment, case_id)
        ).fetchone()
        if row is None:
            raise ValueError("unknown_case")
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError("packet_integrity_failure")
        packet = json.loads(row[0])
        if body["input_digest"] != row[1]:
            raise ValueError("input_mismatch")
        if not body["model_version"] or not body["prompt_version"]:
            raise ValueError("version_required")
        at = instant(body["decided_at"])
        if at < instant(packet["decision_at"]):
            raise ValueError("decision_before_input")
        if body["status"] not in ("ok", "provider_error", "invalid"):
            raise ValueError("invalid_status")
        number(body["latency_ms"])
        number(body["api_cost_usd"])
        for key in ("input_tokens", "output_tokens"):
            integer(body[key])
        if body["status"] == "ok":
            if body["action"] not in ("BUY", "HOLD"):
                raise ValueError("unsupported_action")
            qty = integer(body["quantity"])
            if (body["action"] == "HOLD" and qty != 0) or (
                body["action"] == "BUY" and not 0 < qty <= config["max_quantity"]
            ):
                raise ValueError("quantity_limit")
            if not body["rationale"] or not body["evidence_ids"]:
                raise ValueError("decision_evidence_required")
            if not set(body["evidence_ids"]) <= {s["id"] for s in packet["sources"]}:
                raise ValueError("unknown_evidence")
        with self.db:
            # Acquire writer lock before checking to prevent outcome/decision races.
            self.db.execute("UPDATE experiments SET id=id WHERE id=?", (experiment,))
            if self.db.execute(
                "SELECT 1 FROM outcomes WHERE experiment=? AND case_id=?", (experiment, case_id)
            ).fetchone():
                raise ValueError("outcome_already_known")
            previous = self.db.execute(
                "SELECT body FROM decisions WHERE experiment=? AND candidate=? LIMIT 1",
                (experiment, candidate),
            ).fetchone()
            if previous:
                previous = json.loads(previous[0])
                if any(previous[key] != body[key] for key in ("model_version", "prompt_version")):
                    raise ValueError("candidate_version_changed")
            self.db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?)",
                (experiment, case_id, candidate, canonical(body)),
            )

    def record_outcome(self, experiment, case_id, body):
        config = self.config(experiment)
        entry_at, exit_at = instant(body["entry_at"]), instant(body["exit_at"])
        if entry_at >= exit_at:
            raise ValueError("invalid_execution_times")
        number(body["exit_bid"], positive=True)
        number(body["entry_ask"], positive=True)
        integer(body["entry_available_quantity"])
        integer(body["exit_available_quantity"])
        if not body["provenance"]:
            raise ValueError("execution_provenance_required")
        with self.db:
            self.db.execute("UPDATE experiments SET id=id WHERE id=?", (experiment,))
            decisions = self.db.execute(
                "SELECT candidate,body FROM decisions WHERE experiment=? AND case_id=?",
                (experiment, case_id),
            ).fetchall()
            if {r[0] for r in decisions} != set(config["candidates"]):
                raise ValueError("all_candidates_must_decide_first")
            if any(instant(json.loads(r[1])["decided_at"]) >= entry_at for r in decisions):
                raise ValueError("execution_must_follow_decisions")
            self.db.execute(
                "INSERT INTO outcomes VALUES (?,?,?)", (experiment, case_id, canonical(body))
            )

    def export_evidence(self, experiment):
        """Consistent private evidence snapshot; retain its hash independently."""
        self.db.execute("SAVEPOINT research_export")
        try:
            config = self.config(experiment)
            cases = []
            for case_id, packet, digest in self.db.execute(
                "SELECT id,packet,digest FROM cases WHERE experiment=? ORDER BY id", (experiment,)
            ).fetchall():
                if hashlib.sha256(packet.encode()).hexdigest() != digest:
                    raise ValueError("packet_integrity_failure")
                decisions = {
                    name: json.loads(body)
                    for name, body in self.db.execute(
                        "SELECT candidate,body FROM decisions WHERE experiment=? AND case_id=?",
                        (experiment, case_id),
                    ).fetchall()
                }
                if any(d["input_digest"] != digest for d in decisions.values()):
                    raise ValueError("decision_integrity_failure")
                outcome = self.db.execute(
                    "SELECT body FROM outcomes WHERE experiment=? AND case_id=?",
                    (experiment, case_id),
                ).fetchone()
                cases.append(
                    {
                        "case_id": case_id,
                        "packet": json.loads(packet),
                        "input_digest": digest,
                        "decisions": decisions,
                        "outcome": json.loads(outcome[0]) if outcome else None,
                    }
                )
            body = {"schema_version": 1, "experiment": experiment, "config": config, "cases": cases}
            return {"sha256": hashlib.sha256(canonical(body).encode()).hexdigest(), "body": body}
        finally:
            self.db.execute("RELEASE research_export")

    def report(self, experiment):
        self.db.execute("SAVEPOINT research_report")
        try:
            self.export_evidence(experiment)
            return self._report(experiment)
        finally:
            self.db.execute("RELEASE research_report")

    def _report(self, experiment):
        config = self.config(experiment)
        rows = self.db.execute(
            "SELECT c.id,c.packet,o.body FROM cases c LEFT JOIN outcomes o "
            "ON c.experiment=o.experiment AND c.id=o.case_id WHERE c.experiment=? ORDER BY c.id",
            (experiment,),
        ).fetchall()
        reports = {}
        for candidate in config["candidates"]:
            totals = {
                "decisions": 0,
                "errors": 0,
                "holds": 0,
                "completed_episodes": 0,
                "unfilled": 0,
                "partial_entries": 0,
                "unresolved_exits": 0,
                "missing_decisions": 0,
                "pending_buy_outcomes": 0,
            }
            pnl, cost, latency = Decimal(0), Decimal(0), Decimal(0)
            outcomes = []
            for case_id, _, outcome in rows:
                record = self.db.execute(
                    "SELECT body FROM decisions WHERE experiment=? AND case_id=? AND candidate=?",
                    (experiment, case_id, candidate),
                ).fetchone()
                if record is None:
                    totals["missing_decisions"] += 1
                    continue
                decision = json.loads(record[0])
                totals["decisions"] += 1
                cost += number(decision["api_cost_usd"])
                latency += number(decision["latency_ms"])
                if decision["status"] != "ok":
                    totals["errors"] += 1
                    continue
                if decision["action"] == "HOLD":
                    totals["holds"] += 1
                    continue
                if outcome is None:
                    totals["pending_buy_outcomes"] += 1
                    continue
                execution = simulate(config, decision, json.loads(outcome))
                outcomes.append({"case_id": case_id, **execution})
                if execution["filled_quantity"] < decision["quantity"]:
                    totals["partial_entries"] += int(execution["filled_quantity"] > 0)
                if execution["status"] == "completed":
                    totals["completed_episodes"] += 1
                    pnl += Decimal(execution["net_pnl_inr"])
                elif execution["status"] == "unfilled":
                    totals["unfilled"] += 1
                else:
                    totals["unresolved_exits"] += 1
            reports[candidate] = {
                **totals,
                "completed_case_pnl_inr": str(pnl),
                "api_cost_usd": str(cost),
                "total_latency_ms": str(latency),
                "outcomes": outcomes,
            }
        return {
            "experiment": experiment,
            "config": config,
            "registered_cases": len(rows),
            "resolved_cases": sum(r[2] is not None for r in rows),
            "status": "insufficient_evidence",
            "automatic_promotion": False,
            "limitations": [
                "Independent cases; not continuous portfolio returns or drawdown",
                "Historical model knowledge leakage cannot be ruled out",
                "Source timestamps and costs are supplied, not independently certified",
                "No live provider calls, broker orders or automatic strategy changes",
                "API costs in USD are not deducted from INR trading P&L",
            ],
            "unverified_adjustments": sum(
                json.loads(r[1])["adjustment_status"] != "verified" for r in rows
            ),
            "candidates": reports,
        }


def simulate(config, decision, outcome):
    """One hypothetical round trip. Explicit liquidity and cash caps, no shorting."""
    fee = number(config["fee_bps"]) / 10000
    slip = number(config["slippage_bps"]) / 10000
    entry = number(outcome["entry_ask"], positive=True) * (1 + slip)
    exit_price = number(outcome["exit_bid"], positive=True) * (1 - slip)
    cash = number(config["capital_per_case"], positive=True)
    qty = min(
        decision["quantity"], outcome["entry_available_quantity"], int(cash // (entry * (1 + fee)))
    )
    if qty == 0:
        return {"status": "unfilled", "filled_quantity": 0, "net_pnl_inr": None}
    if outcome["exit_available_quantity"] < qty:
        return {"status": "unresolved_exit", "filled_quantity": qty, "net_pnl_inr": None}
    pnl = qty * (exit_price - entry - fee * (entry + exit_price))
    return {
        "status": "completed",
        "filled_quantity": qty,
        "net_pnl_inr": str(pnl),
        "entry_price": str(entry),
        "exit_price": str(exit_price),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "operation", choices=("create", "case", "decision", "outcome", "report", "html", "export")
    )
    parser.add_argument("experiment")
    parser.add_argument("--file", type=Path, help="JSON body; never credentials")
    parser.add_argument("--case-id")
    parser.add_argument("--candidate")
    args = parser.parse_args()
    lab = ResearchLab(args.database, readonly=args.operation in ("report", "html", "export"))
    try:
        if args.operation == "export":
            print(json.dumps(lab.export_evidence(args.experiment), indent=2))
            return
        if args.operation == "html":
            from quant_ai.research.reporting import render_html

            print(render_html(lab.report(args.experiment)))
            return
        if args.operation == "report":
            print(json.dumps(lab.report(args.experiment), indent=2))
            return
        if args.operation in ("case", "decision", "outcome") and not args.case_id:
            parser.error("--case-id is required")
        if args.operation == "decision" and not args.candidate:
            parser.error("--candidate is required")
        if args.file is None:
            parser.error("--file is required")
        body = json.loads(args.file.read_text())
        if args.operation == "create":
            lab.create(args.experiment, body)
        elif args.operation == "case":
            lab.add_case(args.experiment, args.case_id, body)
        elif args.operation == "decision":
            lab.record_decision(args.experiment, args.case_id, args.candidate, body)
        else:
            lab.record_outcome(args.experiment, args.case_id, body)
        print("Recorded research evidence; no orders submitted.")
    finally:
        lab.close()


if __name__ == "__main__":
    main()
