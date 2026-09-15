"""Retain each historical harness run and its valuation lineage in the paper DB."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.agents.traded_runtime import DECISION_MAKER_NOTES, runtime_configuration
from quant_ai.governance.runtime_manifest import describe, digest, encoded, stable


def source_inventory():
    root = Path(__file__).resolve().parents[1]
    files = {}
    for path in sorted(root.rglob("*.py")):
        if path.is_symlink():
            raise ValueError("replay_source_symlink")
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "sha256": digest(files)}


def decision_components(pipeline):
    runtime = pipeline.runtime
    issues = []
    components = {
        name: describe(obj, issues)
        for name, obj in {
            "pipeline": pipeline,
            "runtime": runtime,
            "cio": runtime.cio,
            "atlas": runtime.cio.atlas,
            "llm": runtime.cio.atlas.llm_client,
            "attribution": runtime.attribution,
            "risk": runtime.warden,
            "stress": runtime.stress_agent,
            "sizer": pipeline.sizer,
            "regime": pipeline.regime_detector,
            "freshness": pipeline.freshness,
            "friction": runtime.broker.friction_model,
        }.items()
    }
    return components, issues


class ReplayRunEvidence:
    def __init__(self, harness, dataset, pipeline):
        self.broker, self.tenant = harness.broker, harness.tenant_id
        self.run_id = uuid.uuid4().hex
        components, issues = decision_components(pipeline)
        self.broker.get_margin(self.tenant)  # Ensure the selected account exists.
        self.source = source_inventory()
        self.metadata = {
            "schema": "pramana.replay_run.v1",
            "runId": self.run_id,
            "tenantId": self.tenant,
            "mode": "historical_replay",
            "source": self.source,
            "python": sys.version.split()[0],
            "datasetSha256": digest(dataset),
            "barCount": len(dataset.bars),
            "start": dataset.bars[0].timestamp.isoformat(),
            "end": dataset.bars[-1].timestamp.isoformat(),
            "instrument": stable(dataset.bars[0].instrument),
            "configuration": {
                "plan": stable(harness.plan),
                "quantity": harness.quantity,
                "country": harness.country,
                "components": components,
                "newsWindowSeconds": pipeline.news_window.total_seconds(),
                # Which agent drew this curve, and what the curve therefore is and is not
                # evidence of. A reader who skips the code still cannot miss it.
                "decisionMaker": harness.decision_maker,
                "decisionMakerNote": DECISION_MAKER_NOTES[harness.decision_maker],
                "tradedRuntime": runtime_configuration(pipeline.runtime),
                "tradedConfigurationDifferences": [
                    dict(item) for item in harness.traded_configuration_differences
                ],
            },
            "configurationIssues": issues,
            "protectionModel": "lower_timeframe_ohlc_stop_first"
            if dataset.intrabar_windows
            else "not_simulated",
            "strategyEquivalence": "unverified",
        }
        with self.broker._lock, self.broker._connection as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS paper_replay_runs(run_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,status TEXT NOT NULL,metadata TEXT NOT NULL,sha256 TEXT,created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS paper_replay_run_points(run_id TEXT NOT NULL,sequence INTEGER NOT NULL,ledger_id INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,sequence))"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS paper_one_active_replay ON paper_replay_runs(tenant_id) WHERE status='running'"
            )
            # Keep prior runs even when the legacy latest-per-timestamp projection changes.
            self.metadata["startingLedgerId"] = db.execute(
                "SELECT coalesce(max(id),0) FROM paper_ledger WHERE tenant_id=?", (self.tenant,)
            ).fetchone()[0]
            self.metadata["startingCapital"] = str(
                self.broker.get_margin(self.tenant).starting_capital
            )
            db.execute(
                "INSERT INTO paper_replay_runs VALUES (?,?,?,?,?,?)",
                (
                    self.run_id,
                    self.tenant,
                    "running",
                    encoded(self.metadata),
                    None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def finish(self, status):
        with self.broker._lock, self.broker._connection as db:
            points = [
                {"sequence": r[0], "ledgerId": r[1], "payload": json.loads(r[2])}
                for r in db.execute(
                    "SELECT sequence,ledger_id,payload FROM paper_replay_run_points WHERE run_id=? ORDER BY sequence",
                    (self.run_id,),
                )
            ]
            if status == "complete":
                if len(points) != self.metadata["barCount"] or source_inventory() != self.source:
                    status = "invalid"
                self.metadata["endingLedgerId"] = db.execute(
                    "SELECT coalesce(max(id),0) FROM paper_ledger WHERE tenant_id=?", (self.tenant,)
                ).fetchone()[0]
                fills = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id", (self.tenant,)
                    )
                ]
                costs = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id",
                        (self.tenant,),
                    )
                ]
                self.metadata["ledgerSourceSha256"] = digest({"fills": fills, "costs": costs})
                margin = self.broker.get_margin(self.tenant)
                self.metadata["account"] = {
                    "starting_capital": str(margin.starting_capital),
                    "cash_balance": str(margin.cash_balance),
                }
            body = {"metadata": self.metadata, "points": points, "status": status}
            db.execute(
                "UPDATE paper_replay_runs SET status=?,metadata=?,sha256=? WHERE run_id=? AND status='running'",
                (status, encoded(self.metadata), digest(body), self.run_id),
            )
        if status == "invalid":
            raise ValueError("replay_source_or_valuation_coverage_changed")
