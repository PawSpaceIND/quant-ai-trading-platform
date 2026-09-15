"""Pair recorded paper observations with a retained historical harness run.

This diagnostic neither declares strategy equivalence nor promotes a strategy.
Every source is read in its own SQLite snapshot; selected clock gaps stay gaps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.domain.models import Market
from quant_ai.execution.reconciliation import reconcile_paper
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.governance.runtime_manifest import digest, encoded, stable

ZERO = Decimal(0)
MAX_POINTS = 10000
MAX_BYTES = 16_000_000


def require(ok, code):
    if not ok:
        raise ValueError(code)


def instant(value):
    require(isinstance(value, str) and len(value) <= 40, "invalid_time")
    result = datetime.fromisoformat(value)
    require(result.utcoffset() is not None, "time_offset_required")
    return result.astimezone(timezone.utc)


def numeric(value, *, positive=False):
    require(not isinstance(value, bool), "invalid_number")
    result = Decimal(str(value))
    require(result.is_finite() and abs(result) <= Decimal("1e12"), "invalid_number")
    require(not positive or result > 0, "positive_number_required")
    return result


def close(a, b):
    return abs(numeric(a) - numeric(b)) <= Decimal("0.01")


def key(row):
    return f"{row['market']}:{row.get('assetClass', row.get('asset_class'))}:{row['symbol']}"


def comparison_window(start, end, now):
    start, end = instant(start), instant(end)
    require(
        start == start.replace(second=0, microsecond=0)
        and end == end.replace(second=0, microsecond=0)
        and start < end <= now,
        "explicit_completed_minute_boundaries_required",
    )
    require(
        (end - start).total_seconds() <= 86400 * 45 and start.year == end.year == 2026,
        "comparison_window_exceeds_calendar_bounds",
    )
    calendar = MarketCalendar(holidays=default_holidays())
    grid, excluded, at = [], 0, start
    while at < end:
        if calendar.state(Market.INDIA, at) == MarketState.REGULAR_HOURS:
            grid.append(at)
        else:
            excluded += 1
        at += timedelta(minutes=1)
    require(0 < len(grid) <= MAX_POINTS, "no_eligible_or_excessive_comparison_minutes")
    return start, end, grid, excluded, digest(stable(calendar))


def capture(database, tenant, mode, run_id=None, *, start=None, end=None, now=None):
    require(mode in ("paper", "replay"), "invalid_source_mode")
    require(
        isinstance(tenant, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", tenant), "invalid_tenant"
    )
    database = Path(database)
    require(not database.is_symlink(), "source_symlink_unsupported")
    selection = None
    predicate = "tenant_id=?"
    if start is not None or end is not None:
        require(mode == "paper", "window_selection_requires_paper_source")
        start, end, grid, _, calendar_hash = comparison_window(
            start, end, now or datetime.now(timezone.utc)
        )
        grid_set = set(grid)

        def selected_bucket(value):
            # Compare timezone-aware instants, not lexical strings or SQLite's
            # millisecond-rounded date conversion. Unlocatable rows fail closed.
            try:
                at = instant(value)
                require(at == at.replace(second=0, microsecond=0), "invalid_minute_bucket")
                return int(at in grid_set)
            except (ValueError, TypeError, OverflowError):
                return None

        predicate += " AND pramana_selected_bucket(timestamp)=1"
        selection = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendarSha256": calendar_hash,
            "expectedMinutes": len(grid),
        }
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        account = None
        if mode == "paper":
            for table, limit in [("paper_ledger", 20000), ("paper_cost_ledger", 200000)]:
                count = db.execute(
                    f"SELECT count(*) FROM {table} WHERE tenant_id=?", (tenant,)
                ).fetchone()[0]
                require(count <= limit, "account_exceeds_comparison_bounds")
            require(
                reconcile_paper(db, tenant)["status"] == "matched", "source_account_not_reconciled"
            )
            account = dict(
                db.execute("SELECT * FROM paper_accounts WHERE tenant_id=?", (tenant,)).fetchone()
            )
        manifests = []
        if mode == "paper":
            if selection is not None:
                db.create_function(
                    "pramana_selected_bucket", 1, selected_bucket, deterministic=True
                )
                total, unlocatable = db.execute(
                    "SELECT count(*),coalesce(sum(pramana_selected_bucket(timestamp) IS NULL),0) FROM paper_live_valuations WHERE tenant_id=?",
                    (tenant,),
                ).fetchone()
                require(unlocatable == 0, "unlocatable_paper_observation")
            count, size = db.execute(
                f"SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM paper_live_valuations WHERE {predicate}",
                (tenant,),
            ).fetchone()
            require(
                count <= MAX_POINTS and size <= MAX_BYTES,
                "paper_observations_exceed_capture_bounds",
            )
            rows = [
                dict(r)
                for r in db.execute(
                    f"SELECT timestamp,ledger_id,payload FROM paper_live_valuations WHERE {predicate} ORDER BY timestamp",
                    (tenant,),
                )
            ]
            if selection is not None:
                selection.update(
                    selectedObservations=count,
                    totalAccountObservations=total,
                    excludedObservations=total - count,
                )
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_strategy_manifests'"
            ).fetchone()
            if table:
                manifest_predicate = "tenant_id=?"
                manifest_args = (tenant,)
                if selection is not None:
                    references = set()
                    for row in rows:
                        observation = json.loads(row["payload"]).get("strategyObservation") or {}
                        reference = observation.get("manifestSha256")
                        if reference is not None:
                            require(
                                isinstance(reference, str)
                                and re.fullmatch("[a-f0-9]{64}", reference),
                                "invalid_strategy_reference",
                            )
                            references.add(reference)
                    require(len(references) <= 100, "strategy_manifests_exceed_bounds")
                    manifest_predicate += (
                        " AND sha256 IN (" + ",".join("?" for _ in references) + ")"
                    )
                    manifest_args += tuple(sorted(references))
                count, size = db.execute(
                    f"SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM pilot_strategy_manifests WHERE {manifest_predicate}",
                    manifest_args,
                ).fetchone()
                require(count <= 100 and size <= MAX_BYTES, "strategy_manifests_exceed_bounds")
                manifests = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT sha256,payload FROM pilot_strategy_manifests WHERE {manifest_predicate} ORDER BY sha256",
                        manifest_args,
                    )
                ]
            metadata = None
        else:
            require(
                isinstance(run_id, str) and re.fullmatch("[a-f0-9]{32}", run_id),
                "explicit_replay_run_required",
            )
            run = db.execute(
                "SELECT * FROM paper_replay_runs WHERE run_id=? AND tenant_id=?", (run_id, tenant)
            ).fetchone()
            require(
                run is not None and run["status"] == "complete", "completed_replay_run_required"
            )
            count, size = db.execute(
                "SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM paper_replay_run_points WHERE run_id=?",
                (run_id,),
            ).fetchone()
            require(
                count <= MAX_POINTS and size <= MAX_BYTES and len(run["metadata"]) <= 2_000_000,
                "replay_exceeds_capture_bounds",
            )
            points = [
                {"sequence": r[0], "ledgerId": r[1], "payload": json.loads(r[2])}
                for r in db.execute(
                    "SELECT sequence,ledger_id,payload FROM paper_replay_run_points WHERE run_id=? ORDER BY sequence",
                    (run_id,),
                )
            ]
            metadata = json.loads(run["metadata"])
            require(
                metadata["schema"] == "pramana.replay_run.v1"
                and metadata["runId"] == run_id
                and metadata["tenantId"] == tenant,
                "replay_identity_mismatch",
            )
            require(
                [p["sequence"] for p in points] == list(range(1, len(points) + 1))
                and len(points) == metadata["barCount"],
                "replay_point_coverage_changed",
            )
            require(
                digest({"metadata": metadata, "points": points, "status": "complete"})
                == run["sha256"],
                "replay_evidence_changed",
            )
            require(
                digest(metadata["source"]["files"]) == metadata["source"]["sha256"],
                "replay_source_inventory_changed",
            )
            instrument = metadata["instrument"]
            require(
                instrument["market"] == "INDIA"
                and instrument["currency"] == "INR"
                and instrument["exchange"] == "NSE"
                and instrument["asset_class"] in ("EQUITY", "ETF"),
                "replay_requires_INR_NSE_cash_scope",
            )
            require(
                points
                and points[0]["ledgerId"] == metadata["startingLedgerId"]
                and points[-1]["ledgerId"] == metadata["endingLedgerId"],
                "replay_ledger_boundary_changed",
            )
            require(
                points[0]["payload"]["updatedAt"] == metadata["start"]
                and points[-1]["payload"]["updatedAt"] == metadata["end"],
                "replay_clock_boundary_changed",
            )
            rows = [
                {
                    "timestamp": p["payload"]["updatedAt"],
                    "ledger_id": p["ledgerId"],
                    "payload": encoded(p["payload"]),
                }
                for p in points
            ]
        if mode == "paper":
            fills = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id", (tenant,)
                )
            ]
            costs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id", (tenant,)
                )
            ]
        else:
            head = metadata["endingLedgerId"]
            require(type(head) is int and head >= 0, "invalid_replay_ending_head")
            fills = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM paper_ledger WHERE tenant_id=? AND id<=? ORDER BY id LIMIT 20001",
                    (tenant, head),
                )
            ]
            costs = [
                dict(r)
                for r in db.execute(
                    "SELECT c.* FROM paper_cost_ledger c JOIN paper_ledger f ON f.order_id=c.order_id AND f.tenant_id=c.tenant_id WHERE c.tenant_id=? AND f.id<=? ORDER BY c.id LIMIT 200001",
                    (tenant, head),
                )
            ]
            require(len(fills) <= 20000 and len(costs) <= 200000, "replay_ledger_exceeds_bounds")
            require(
                digest({"fills": fills, "costs": costs}) == metadata["ledgerSourceSha256"],
                "retained_replay_fills_or_fees_changed",
            )
            account = metadata["account"]
    source = {
        "mode": mode,
        "tenant": tenant,
        "account": account,
        "fills": fills,
        "costs": costs,
        "rows": rows,
        "manifests": manifests,
        "replay": metadata,
    }
    if selection is not None:
        source["observationSelection"] = selection
    require(len(encoded(source).encode()) <= MAX_BYTES, "source_capture_exceeds_bounds")
    return source


def normalize(source, now):
    initial = numeric(source["account"]["starting_capital"], positive=True)
    costs, ids = {}, set()
    for c in source["costs"]:
        if c["cash_debit"]:
            costs[c["order_id"]] = costs.get(c["order_id"], ZERO) + numeric(c["amount"])
    fills = []
    last_time = None
    for f in source["fills"]:
        at = instant(f["created_at"])
        require(
            f["order_id"] not in ids and (last_time is None or at >= last_time),
            "duplicate_or_unordered_fill",
        )
        require(
            f["market"] == "INDIA" and f["asset_class"] in ("EQUITY", "ETF"),
            "recorded_INR_cash_scope_required",
        )
        require(
            type(f["quantity"]) is int and 0 < f["quantity"] <= 1_000_000_000,
            "invalid_fill_quantity",
        )
        require(
            numeric(f["fill_price"], positive=True) * f["quantity"] == numeric(f["notional"]),
            "invalid_fill_notional",
        )
        ids.add(f["order_id"])
        last_time = at
        fills.append(
            {
                "ledgerId": f["id"],
                "orderId": f["order_id"],
                "key": key(f),
                "symbol": f["symbol"],
                "side": f["side"],
                "quantity": f["quantity"],
                "price": str(numeric(f["fill_price"])),
                "cashFees": str(costs.get(f["order_id"], ZERO)),
                "at": at.isoformat(),
            }
        )
    valid_heads = {0} | {f["ledgerId"] for f in fills}
    cash, holdings, cursor, last_head, last_at = initial, {}, 0, 0, None
    points = []
    for row in sorted(source["rows"], key=lambda row: instant(row["timestamp"])):
        require(len(row["payload"]) <= 1_000_000, "oversized_valuation")
        p = json.loads(row["payload"])
        at = instant(p["updatedAt"])
        stored = instant(row["timestamp"])
        require(last_at is None or at > last_at, "unordered_valuation_clock")
        require(
            at <= now
            and (
                at.replace(second=0, microsecond=0) == stored
                if source["mode"] == "paper"
                else at == stored
            ),
            "invalid_valuation_clock",
        )
        head = row["ledger_id"]
        require(
            type(head) is int and head in valid_heads and head >= last_head,
            "invalid_valuation_ledger_head",
        )
        require(
            p["tenantId"] == source["tenant"]
            and p["markMode"]
            == ("engine_live" if source["mode"] == "paper" else "historical_replay"),
            "valuation_source_identity_changed",
        )
        require(
            source["mode"] != "paper" or p.get("currency") == "INR", "paper_currency_unverified"
        )
        errors = []
        while cursor < len(fills) and fills[cursor]["ledgerId"] <= head:
            f = fills[cursor]
            cursor += 1
            require(instant(f["at"]) <= at, "valuation_contains_future_fill")
            h = holdings.setdefault(f["key"], {"quantity": 0, "average": ZERO})
            q, price = f["quantity"], numeric(f["price"])
            cash -= numeric(f["cashFees"])
            if f["side"] == "BUY":
                h["average"] = (h["quantity"] * h["average"] + q * price) / (h["quantity"] + q)
                h["quantity"] += q
                cash -= q * price
            else:
                require(f["side"] == "SELL" and h["quantity"] >= q, "uncovered_sale")
                h["quantity"] -= q
                cash += q * price
            if not h["quantity"]:
                del holdings[f["key"]]
        if cursor < len(fills) and instant(fills[cursor]["at"]) < at:
            errors.append("valuation_precedes_known_fill")
        require(cash >= 0, "negative_cash_outside_qualified_scope")
        values = []
        if p.get("status") == "invalid":
            errors.append("invalid_recorded_valuation")
        else:
            try:
                require(p.get("status") in ("ok", "degraded"), "unsupported_valuation_status")
                require(
                    close(p["startingCapital"], initial) and close(p["cash"], cash),
                    "valuation_cash_mismatch",
                )
                require(
                    isinstance(p["holdings"], list) and len(p["holdings"]) <= 50, "invalid_holdings"
                )
                observed = {key(h): h for h in p["holdings"]}
                require(
                    len(observed) == len(p["holdings"]) and observed.keys() == holdings.keys(),
                    "valuation_holdings_mismatch",
                )
                equity = cash
                for k, expected in holdings.items():
                    h = observed[k]
                    mark = numeric(h["markPrice"], positive=True)
                    require(
                        type(h["quantity"]) is int
                        and h["quantity"] == expected["quantity"]
                        and close(h["averageEntry"], expected["average"]),
                        "valuation_position_mismatch",
                    )
                    require(
                        close(h["marketValue"], mark * h["quantity"]),
                        "valuation_market_value_mismatch",
                    )
                    if source["mode"] == "paper":
                        require(
                            h.get("fresh") is True
                            and h.get("markSource") == "live_tick"
                            and 0 <= (at - instant(h["markTimestamp"])).total_seconds() <= 120,
                            "stale_or_unknown_mark",
                        )
                    equity += mark * h["quantity"]
                    values.append(
                        {
                            "key": k,
                            "quantity": h["quantity"],
                            "average": str(expected["average"]),
                            "mark": str(mark),
                        }
                    )
                require(close(p["totalEquity"], equity) and equity > 0, "valuation_equity_mismatch")
                require(
                    source["mode"] != "paper" or p.get("allMarksFresh") is True,
                    "stale_or_unknown_mark",
                )
            except (ValueError, TypeError, KeyError, ArithmeticError):
                errors.append("valuation_accounting_or_mark_invalid")
        strategy = p.get("strategyObservation") or {}
        require(
            isinstance(strategy, dict)
            and (
                strategy.get("manifestSha256") is None
                or (
                    isinstance(strategy.get("manifestSha256"), str)
                    and re.fullmatch("[a-f0-9]{64}", strategy["manifestSha256"])
                )
            ),
            "invalid_strategy_reference",
        )
        points.append(
            {
                "at": at.isoformat(),
                "ledgerId": head,
                "equity": str(equity) if not errors else None,
                "cash": str(cash),
                "holdings": values if not errors else [],
                "issues": errors,
                "strategySha256": strategy.get("manifestSha256"),
                "strategyEligible": strategy.get("eligible") is True,
            }
        )
        last_head, last_at = head, at
    return {
        "mode": source["mode"],
        "tenant": source["tenant"],
        "startingCapital": str(initial),
        "sourceSha256": digest(source),
        "points": points,
        "fills": fills,
    }


def configuration(paper, replay, points):
    selected = sorted({p["strategySha256"] for p in points if isinstance(p["strategySha256"], str)})
    manifests = {}
    for row in paper["manifests"]:
        value = json.loads(row["payload"])
        require(
            digest(value) == row["sha256"] and value.get("tenant_id") == paper["tenant"],
            "observed_manifest_changed",
        )
        require(
            digest(value["source"]["files"]) == value["source"]["sha256"],
            "observed_source_inventory_changed",
        )
        manifests[row["sha256"]] = value
    result = {
        "observedManifestHashes": selected,
        "observedUnqualifiedPoints": sum(not p["strategyEligible"] for p in points),
        "replayRunId": replay["runId"],
        "replayDatasetSha256": replay["datasetSha256"],
        "sourceComparison": "unavailable",
        "differentComponents": [],
        "strategyEquivalence": "unverified",
        "replayProtectionModel": replay["protectionModel"],
    }
    if len(selected) == 1 and selected[0] in manifests:
        live = manifests[selected[0]]
        result["sourceComparison"] = (
            "same_inventory"
            if live["source"]["sha256"] == replay["source"]["sha256"]
            else "different_inventory"
        )
        differences = [
            name
            for name, value in replay["configuration"]["components"].items()
            if value != live["components"].get(name)
        ]
        params = live["components"].get("daemon", {}).get("parameters", {})
        differences += [
            name
            for name in ("plan", "quantity", "country")
            if replay["configuration"][name] != params.get(name)
        ]
        if replay["configuration"]["newsWindowSeconds"] != live.get("news_window_seconds"):
            differences.append("news_window")
        result["differentComponents"] = sorted(differences)
    return result


def build(paper_source, replay_source, start, end, *, max_skew_seconds=0, now=None):
    now = now or datetime.now(timezone.utc)
    start, end, grid, excluded, calendar_hash = comparison_window(start, end, now)
    require(type(max_skew_seconds) is int and 0 <= max_skew_seconds <= 59, "invalid_pairing_skew")
    require(
        paper_source["mode"] == "paper" and replay_source["mode"] == "replay",
        "source_modes_required",
    )
    paper, replay = normalize(paper_source, now), normalize(replay_source, now)
    selection = paper_source.get("observationSelection")
    if selection is not None:
        require(
            selection["start"] == start.isoformat()
            and selection["end"] == end.isoformat()
            and selection["calendarSha256"] == calendar_hash
            and selection["expectedMinutes"] == len(grid)
            and selection["selectedObservations"] == len(paper["points"])
            and type(selection["totalAccountObservations"]) is int
            and selection["totalAccountObservations"] >= len(paper["points"])
            and selection["excludedObservations"]
            == selection["totalAccountObservations"] - len(paper["points"]),
            "paper_capture_window_mismatch",
        )
    grid_set = set(grid)
    maps = []
    for source in (paper, replay):
        selected = {}
        for p in source["points"]:
            t = instant(p["at"])
            minute = t.replace(second=0, microsecond=0)
            if start <= t < end and minute in grid_set:
                require(minute not in selected, "multiple_source_observations_in_minute")
                selected[minute] = p
        maps.append(selected)
    if selection is not None:
        require(len(maps[0]) == len(paper["points"]), "paper_capture_window_mismatch")
    curve = []
    for t in grid:
        p, r = maps[0].get(t), maps[1].get(t)
        issues = []
        if not p:
            issues.append("paper_observation_missing")
        elif p["equity"] is None:
            issues.append("paper_valuation_invalid")
        if not r:
            issues.append("replay_observation_missing")
        elif r["equity"] is None:
            issues.append("replay_valuation_invalid")
        skew = abs((instant(p["at"]) - instant(r["at"])).total_seconds()) if p and r else None
        if skew is not None and skew > max_skew_seconds:
            issues.append("observation_time_mismatch")
        curve.append(
            {
                "minute": t.isoformat(),
                "paper": p,
                "replay": r,
                "skewSeconds": skew,
                "issues": issues,
                "equityDifference": str(numeric(p["equity"]) - numeric(r["equity"]))
                if not issues
                else None,
            }
        )
    first = curve[0]
    initial_state = "unavailable"
    if first["paper"] and first["replay"] and not first["issues"]:
        p, r = first["paper"], first["replay"]
        same = (
            paper["startingCapital"] == replay["startingCapital"]
            and close(p["cash"], r["cash"])
            and sorted((h["key"], h["quantity"], h["average"]) for h in p["holdings"])
            == sorted((h["key"], h["quantity"], h["average"]) for h in r["holdings"])
        )
        initial_state = "same_recorded_book" if same else "different_recorded_book"
    complete = not any(p["issues"] for p in curve)
    metrics = None
    if complete and len(curve) >= 2:
        pp, rr = [numeric(curve[0][mode]["equity"]) for mode in ("paper", "replay")]
        pe, re_ = [numeric(curve[-1][mode]["equity"]) for mode in ("paper", "replay")]
        differences = [numeric(p["equityDifference"]) for p in curve]
        metrics = {
            "paperReturn": str(pe / pp - 1),
            "replayReturn": str(re_ / rr - 1),
            "returnDifference": str(pe / pp - re_ / rr),
            "endingEquityDifference": str(differences[-1]),
            "maxAbsoluteEquityDifference": str(max(abs(d) for d in differences)),
        }
    selected_fills = {
        s["mode"]: [f for f in s["fills"] if start <= instant(f["at"]) < end]
        for s in (paper, replay)
    }
    groups = {}
    for mode, fills in selected_fills.items():
        for f in fills:
            at = instant(f["at"]).replace(second=0, microsecond=0).isoformat()
            k = (at, f["key"], f["side"])
            group = groups.setdefault(
                k,
                {
                    "minute": at,
                    "key": f["key"],
                    "symbol": f["symbol"],
                    "side": f["side"],
                    "paper": None,
                    "replay": None,
                },
            )
            totals = group[mode] or {
                "quantity": 0,
                "notional": ZERO,
                "cashFees": ZERO,
                "fillCount": 0,
            }
            totals["quantity"] += f["quantity"]
            totals["notional"] += f["quantity"] * numeric(f["price"])
            totals["cashFees"] += numeric(f["cashFees"])
            totals["fillCount"] += 1
            group[mode] = totals
    for g in groups.values():
        for mode in ("paper", "replay"):
            if g[mode]:
                g[mode]["averagePrice"] = str(g[mode]["notional"] / g[mode]["quantity"])
                g[mode]["notional"] = str(g[mode]["notional"])
                g[mode]["cashFees"] = str(g[mode]["cashFees"])
        g["quantityDifference"] = (g["paper"] or {}).get("quantity", 0) - (g["replay"] or {}).get(
            "quantity", 0
        )
        g["priceDifference"] = (
            str(numeric(g["paper"]["averagePrice"]) - numeric(g["replay"]["averagePrice"]))
            if g["paper"] and g["replay"]
            else None
        )
    comparison = configuration(paper_source, replay_source["replay"], list(maps[0].values()))
    return {
        "schema": "pramana.run_comparison.v1",
        "tenantId": paper["tenant"],
        "generatedAt": now.isoformat(),
        "qualification": "unqualified_paper_replay_diagnostic",
        "automaticPromotion": False,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "maxSkewSeconds": max_skew_seconds,
            "calendarSha256": calendar_hash,
            "excludedClosedMinutes": excluded,
        },
        "sources": {
            s["mode"]: {
                **{k: s[k] for k in ("tenant", "startingCapital", "sourceSha256")},
                **(
                    {"observationSelection": selection}
                    if s["mode"] == "paper" and selection
                    else {}
                ),
            }
            for s in (paper, replay)
        },
        "configuration": comparison,
        "initialState": initial_state,
        "status": "complete_observations" if complete else "incomplete_observations",
        "metrics": metrics,
        "curve": curve,
        "fills": selected_fills,
        "fillGroups": [groups[k] for k in sorted(groups)],
        "limitations": [
            "Recorded paper and historical harness runs are paired for diagnosis, not certified as the same strategy or an untouched out-of-sample test.",
            "Each source uses its own SQLite snapshot. Their capture is not a transaction across accounts; source hashes do not authenticate market observations.",
            "All expected regular-session minute buckets in the requested 2026 NSE calendar remain visible. No forward fill or interpolation. Original observation times and pairing tolerance are explicit.",
            "Returns use each run's first selected equity. Starting cash/holdings and source/configuration differences require review; a small divergence is not proof of equivalence.",
            "Fill comparisons aggregate by minute, instrument and side. They are not one-to-one order matches, execution acknowledgements or causal attribution.",
            "Marks must reconcile to recorded fill quantities, average basis and cash within 0.01 INR. Stored invalid or stale observations remain gaps.",
            "Actual input availability, complete providers, execution-clock/scheduler/protection parity, adjustments, corporate actions, cash flows and AI inference replay remain unqualified.",
            "Recorded cash fees are included; spread/slippage already affect fill prices. Unrecorded model, data, infrastructure and other costs are excluded.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("paper-database", "replay-database", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("paper-tenant", "replay-tenant", "replay-run", "start", "end"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--max-skew-seconds", type=int, default=0)
    args = parser.parse_args()
    require(not args.output.exists(), "select_new_output_path")
    require(
        args.paper_database.resolve() != args.replay_database.resolve()
        or args.paper_tenant != args.replay_tenant,
        "distinct_source_accounts_required",
    )
    paper = capture(args.paper_database, args.paper_tenant, "paper", start=args.start, end=args.end)
    replay = capture(args.replay_database, args.replay_tenant, "replay", args.replay_run)
    report = build(paper, replay, args.start, args.end, max_skew_seconds=args.max_skew_seconds)
    payload = encoded(report)
    envelope = {"payload": payload, "sha256": digest(report)}
    require(len(encoded(envelope).encode()) <= MAX_BYTES, "comparison_report_exceeds_bounds")
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(envelope, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    print(
        json.dumps(
            {
                "status": report["status"],
                "sha256": envelope["sha256"],
                "points": len(report["curve"]),
                "automaticPromotion": False,
            }
        )
    )


if __name__ == "__main__":
    main()
