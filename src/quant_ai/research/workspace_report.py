"""Publish a private, review-only snapshot without exposing research input packets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.research.lab import ResearchLab, canonical, identity
from quant_ai.research.reporting import cost_stress, diagnostics

MAX_BYTES = 2_000_000
COUNTS = (
    "decisions",
    "errors",
    "holds",
    "completed_episodes",
    "unfilled",
    "partial_entries",
    "unresolved_exits",
    "missing_decisions",
    "pending_buy_outcomes",
    "unknown_cost_decisions",
)
LIMITATIONS = [
    "Published offline snapshot; not a live feed or independently verified capture.",
    "Independent cases reset capital; completed-case P&L is not portfolio return or drawdown.",
    "Missing decisions, provider failures and unresolved exits can conceal losses.",
    "Historical model knowledge leakage and selection bias have not been ruled out.",
    "Source timestamps, adjustments, liquidity and costs are supplied, not independently certified.",
    "API cost is reported in USD and is not deducted from INR case P&L.",
    "Cost stress changes declared fees and slippage only; it is not a market-impact model.",
    "No winner, promotion, broker order or pilot acceptance is produced by this report.",
]


def workspace_snapshot(lab: ResearchLab, experiment: str, tenant: str) -> dict:
    """Read all report inputs in one SQLite snapshot; allowlist every exported field."""
    identity(tenant)
    lab.db.execute("SAVEPOINT workspace_snapshot")
    try:
        evidence = lab.export_evidence(experiment)
        report = lab.report(experiment)
    finally:
        lab.db.execute("RELEASE workspace_snapshot")
    config = report["config"]
    if len(report["candidates"]) > 16 or report["registered_cases"] > 5000:
        raise ValueError("workspace_report_too_large")
    candidates = {}
    for name, candidate in report["candidates"].items():
        decisions = [
            c["decisions"][name] for c in evidence["body"]["cases"] if name in c["decisions"]
        ]
        versions = decisions[0] if decisions else {}
        stresses = []
        for factor in (1, 2, 3):
            pnl, completed, unresolved, unfilled = Decimal(0), 0, 0, 0
            try:
                for case in evidence["body"]["cases"]:
                    decision = case["decisions"].get(name)
                    if (
                        not decision
                        or decision["status"] != "ok"
                        or decision["action"] != "BUY"
                        or case["outcome"] is None
                    ):
                        continue
                    result = cost_stress(config, decision, case["outcome"], [factor])[0]
                    if result["status"] == "completed":
                        completed += 1
                        pnl += Decimal(result["net_pnl_inr"])
                    elif result["status"] == "unresolved_exit":
                        unresolved += 1
                    else:
                        unfilled += 1
                stresses.append(
                    {
                        "multiplier": factor,
                        "status": "computed",
                        "completed_cases": completed,
                        "unresolved_exits": unresolved,
                        "unfilled": unfilled,
                        "completed_case_pnl_inr": str(pnl),
                    }
                )
            except ValueError:
                stresses.append(
                    {
                        "multiplier": factor,
                        "status": "unavailable",
                        "completed_cases": None,
                        "unresolved_exits": None,
                        "unfilled": None,
                        "completed_case_pnl_inr": None,
                    }
                )
        candidates[name] = {
            **{key: candidate[key] for key in COUNTS},
            "completed_case_pnl_inr": candidate["completed_case_pnl_inr"],
            "api_cost_usd": candidate["api_cost_usd"],
            "cost_total_complete": candidate["cost_total_complete"],
            "total_latency_ms": candidate["total_latency_ms"],
            "model_version": versions.get("model_version"),
            "returned_models": sorted(
                {
                    d["returned_model"]
                    for d in decisions
                    if isinstance(d.get("returned_model"), str) and d["returned_model"].strip()
                }
            ),
            "prompt_version": versions.get("prompt_version"),
            "outcomes": [
                {key: row[key] for key in ("case_id", "status", "filled_quantity", "net_pnl_inr")}
                for row in candidate["outcomes"]
            ],
            "cost_stress": stresses,
        }
    body = {
        "schema": "pramana.research_workspace.v1",
        "tenant_id": tenant,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_sha256": evidence["sha256"],
        "experiment": experiment,
        "mode": config["mode"],
        "protocol_version": config["protocol_version"],
        "baseline": config["baseline"],
        "expected_cases": config["expected_cases"],
        "registered_cases": report["registered_cases"],
        "resolved_cases": report["resolved_cases"],
        "unverified_adjustments": report["unverified_adjustments"],
        "costs": {key: str(config[key]) for key in ("capital_per_case", "fee_bps", "slippage_bps")},
        "candidates": candidates,
        "comparison_blockers": diagnostics(report)["comparison_blockers"],
        "limitations": LIMITATIONS,
        "status": "insufficient_evidence",
        "automatic_promotion": False,
    }
    payload = canonical(body)
    envelope = {"payload": payload, "sha256": hashlib.sha256(payload.encode()).hexdigest()}
    if len(canonical(envelope).encode()) > MAX_BYTES:
        raise ValueError("workspace_report_too_large")
    return envelope


def publish(database: Path, experiment: str, tenant: str, output: Path) -> dict:
    lab = ResearchLab(database, readonly=True)
    try:
        envelope = workspace_snapshot(lab, experiment, tenant)
    finally:
        lab.close()
    # No overwrite, including symlinks and the source database. Caller chooses a new file.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(envelope, stream, ensure_ascii=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("experiment")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    publish(args.database, args.experiment, args.tenant, args.output)


if __name__ == "__main__":
    main()
