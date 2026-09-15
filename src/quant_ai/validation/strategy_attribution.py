"""Attribute complete paper episodes to intact, exact runtime-manifest evidence.

This is configuration linkage, not a claim of AI quality or genuine market provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from quant_ai.governance.runtime_manifest import digest

TABLES = ("paper_decision_evidence", "paper_protection_evidence", "pilot_strategy_manifests")


def capture_sources(db, tenant):
    result = {}
    for table in TABLES:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        ordering = "sha256" if table == "pilot_strategy_manifests" else "order_id"
        result[table] = (
            [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {table} WHERE tenant_id=? ORDER BY {ordering}", (tenant,)
                )
            ]
            if exists
            else []
        )
    return result


def trade_summary(closed, opened):
    pnls = [Decimal(e["netPnl"]) for e in closed]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    gains, loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
    total = sum(pnls, Decimal(0))
    return {
        "completedTrades": len(closed),
        "openEpisodes": len(opened),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(pnls) - len(wins) - len(losses),
        "netPnl": str(total),
        "expectancy": str(total / len(pnls)) if pnls else None,
        "winRate": str(Decimal(len(wins)) / len(pnls)) if pnls else None,
        "profitFactor": str(gains / loss) if loss > 0 else None,
        "profitFactorState": "defined"
        if loss > 0
        else "no_observed_losses"
        if wins
        else "insufficient_data",
        "averageWin": str(gains / len(wins)) if wins else None,
        "averageLoss": str(loss / len(losses)) if losses else None,
        "closedCashFees": str(sum((Decimal(e["cashFees"]) for e in closed), Decimal(0))),
        "openCashFees": str(sum((Decimal(e["cashFees"]) for e in opened), Decimal(0))),
    }


def moment(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return stamp


def fill_binding(fill, fees, proofs, manifests, manifest_starts):
    records = proofs.get(fill["order_id"], [])
    candidate = None
    try:
        if len(records) != 1:
            raise ValueError("missing_or_ambiguous_canonical_proof")
        proof = json.loads(records[0]["payload"])
        protective = proof.get("schema") == "pramana.protective_exit.v1"
        if records[0]["evidence_table"] != (
            "paper_protection_evidence" if protective else "paper_decision_evidence"
        ):
            raise ValueError("canonical_proof_table_mismatch")
        binding = (
            proof.get("runtime_strategy")
            if protective
            else (proof.get("provenance") or {}).get("runtime_strategy")
        )
        if isinstance(binding, dict) and re.fullmatch(
            "[0-9a-f]{64}", str(binding.get("sha256", ""))
        ):
            candidate = binding["sha256"]
        if (
            proof.get("tenant_id") != fill["tenant_id"]
            or proof.get("order_id") != fill["order_id"]
            or proof.get("subject") != fill["symbol"]
            or proof.get("filled_at") != fill["created_at"]
        ):
            raise ValueError("proof_identity_mismatch")
        actual = proof["fill"]
        if (
            actual["status"] != "FILLED"
            or type(actual["quantity"]) is not int
            or actual["quantity"] != fill["quantity"]
            or Decimal(actual["price"]) != Decimal(fill["fill_price"])
            or Decimal(actual["cash_fees"]) != fees
        ):
            raise ValueError("proof_fill_or_fee_mismatch")
        if protective:
            if (
                proof.get("event_type") != "protective_exit"
                or fill["side"] != "SELL"
                or proof["proposal"]["side"] != "SELL"
            ):
                raise ValueError("invalid_protective_proof")
        else:
            if (
                proof.get("schema") != "pramana.swarm_fill.v1"
                or proof.get("event_type") != "swarm_fill"
            ):
                raise ValueError("invalid_swarm_proof")
            order = proof["approved_order"]
            if any(
                order.get(k) != fill[k]
                for k in ("symbol", "market", "asset_class", "side", "tenant_id", "quantity")
            ):
                raise ValueError("approved_order_mismatch")
        if (
            not candidate
            or binding.get("status") != "matched"
            or binding.get("bootSha256") != candidate
            or binding.get("issues") != []
        ):
            raise ValueError("runtime_binding_unverified")
        age = (moment(fill["created_at"]) - moment(binding["checkedAt"])).total_seconds()
        source_age = binding.get("sourceCheckAgeSeconds")
        if not -5 <= age <= 10 or type(source_age) not in (int, float) or not 0 <= source_age <= 65:
            raise ValueError("runtime_binding_stale")
        raw = manifests.get(candidate)
        if raw is None or hashlib.sha256(raw.encode()).hexdigest() != candidate:
            raise ValueError("manifest_missing_or_corrupt")
        if (moment(manifest_starts[candidate]) - moment(fill["created_at"])).total_seconds() > 5:
            raise ValueError("manifest_recorded_after_fill")
        manifest = json.loads(raw)
        source = manifest["source"]
        if (
            manifest.get("schema") != "pramana.runtime_strategy.v1"
            or manifest.get("tenant_id") != fill["tenant_id"]
            or manifest.get("execution_mode") != "paper"
            or manifest.get("release_revision") != binding.get("releaseRevision")
            or not re.fullmatch("[0-9a-f]{40}", str(binding.get("releaseRevision", "")))
            or source.get("sha256") != binding.get("sourceSha256")
            or digest(source["files"]) != source["sha256"]
        ):
            raise ValueError("manifest_identity_mismatch")
        return {"status": "linked", "sha256": candidate, "candidate": candidate}
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as error:
        return {
            "status": "unverified",
            "sha256": None,
            "candidate": candidate,
            "reason": str(error)
            if isinstance(error, ValueError) and str(error).replace("_", "").isalnum()
            else "invalid_evidence",
        }


def attribute_episodes(fills, fees, closed, opened, sources):
    proofs = {}
    for table in TABLES[:2]:
        for record in sources[table]:
            proofs.setdefault(record["order_id"], []).append({**record, "evidence_table": table})
    manifests = {r["sha256"]: r["payload"] for r in sources[TABLES[2]]}
    manifest_starts = {r["sha256"]: r["created_at"] for r in sources[TABLES[2]]}
    bindings = {
        f["order_id"]: fill_binding(
            f, fees.get(f["order_id"], Decimal(0)), proofs, manifests, manifest_starts
        )
        for f in fills
    }
    for episode in [*closed, *opened]:
        rows = [bindings[oid] for oid in episode["orderIds"]]
        candidates = sorted({r["candidate"] for r in rows if r["candidate"]})
        linked = all(r["status"] == "linked" for r in rows) and len(candidates) == 1
        episode["attribution"] = {
            "status": "linked" if linked else "mixed" if len(candidates) > 1 else "unverified",
            "strategySha256": candidates[0] if linked else None,
            "candidateStrategySha256s": candidates,
            "reasons": sorted({r["reason"] for r in rows if r.get("reason")}),
        }
    # Unknown episodes after this configuration was first recorded must not become
    # a convenient way to omit losses from its evaluation period.
    starts = {}
    for record in sources[TABLES[2]]:
        try:
            if hashlib.sha256(record["payload"].encode()).hexdigest() == record["sha256"]:
                starts[record["sha256"]] = moment(record["created_at"])
        except (ValueError, TypeError, KeyError):
            pass
    by_strategy = {}
    for sha, start in sorted(starts.items()):
        linked_closed = [e for e in closed if e["attribution"]["strategySha256"] == sha]
        linked_open = [e for e in opened if e["attribution"]["strategySha256"] == sha]
        unresolved = [
            e
            for e in [*closed, *opened]
            if e["attribution"]["status"] != "linked"
            and (
                sha in e["attribution"]["candidateStrategySha256s"]
                or not e.get("closedAt")
                or moment(e["closedAt"]) >= start
            )
        ]
        incompatible = sorted(
            {
                moment(f["created_at"]).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
                for f in fills
                if bindings[f["order_id"]]["sha256"] != sha
            }
        )
        by_strategy[sha] = {
            "summary": trade_summary(linked_closed, linked_open),
            "coverageStartedAt": start.isoformat(),
            "unresolvedEpisodes": len(unresolved),
            "unresolvedOrderIds": [e["orderIds"] for e in unresolved],
            "foreignOpenEpisodes": len(opened) - len(linked_open),
            "incompatibleSessionDates": incompatible,
            "completedEpisodeOrderIds": [e["orderIds"] for e in linked_closed],
            "openEpisodeOrderIds": [e["orderIds"] for e in linked_open],
        }
    return {
        "schema": "pramana.paper_strategy_attribution.v1",
        "byStrategy": by_strategy,
        "unlinkedCompletedTrades": sum(e["attribution"]["status"] != "linked" for e in closed),
        "allOpenEpisodes": len(opened),
        "recordedManifestSha256s": sorted(starts),
        "allFillSessionDates": sorted(
            {
                moment(f["created_at"]).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
                for f in fills
            }
        ),
    }


def select_strategy_evidence(report, sha):
    if (
        not report
        or report.get("status") != "ok"
        or not isinstance(sha, str)
        or not re.fullmatch("[0-9a-f]{64}", sha)
    ):
        return None
    attribution = report.get("strategyAttribution")
    if not attribution or sha not in attribution["recordedManifestSha256s"]:
        return None
    row = attribution["byStrategy"].get(sha) or {
        "summary": trade_summary([], []),
        "unresolvedEpisodes": 0,
        "unresolvedOrderIds": [],
        "foreignOpenEpisodes": attribution["allOpenEpisodes"],
        "incompatibleSessionDates": attribution["allFillSessionDates"],
        "completedEpisodeOrderIds": [],
        "openEpisodeOrderIds": [],
    }
    identity = {
        "strategySha256": sha,
        "sourceSha256": report["sourceSha256"],
        "ledgerId": report["ledgerId"],
        **row,
    }
    return {
        "schema": "pramana.strategy_episode_evidence.v1",
        "status": "ok",
        "strategySha256": sha,
        "sourceSha256": report["sourceSha256"],
        "evidenceSha256": digest(identity),
        "generatedAt": report["generatedAt"],
        "ledgerId": report["ledgerId"],
        "unlinkedAccountCompletedTrades": attribution["unlinkedCompletedTrades"],
        **row,
        "scope": "Exact configuration linkage of paper episodes; not verified market provenance, AI calibration or forward profitability",
    }
