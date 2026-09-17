"""Stored protection coverage, separate from accounting and market/feed health.

Read only: missing/corrupt levels are reported, never invented or repaired. A
valid stored stop does not promise an executable price or a maximum loss.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from quant_ai.execution.ledger_integrity import position_geometry_issues


def positive_level(value) -> Decimal | None:
    """Only usable positive finite levels; safe for comparison and JSON display."""
    if value is None or isinstance(value, bool):
        return None
    try:
        level = Decimal(str(value))
        if level.is_finite() and level > 0 and math.isfinite(float(level)) and float(level) > 0:
            return level
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        pass
    return None


def protection_coverage(db: sqlite3.Connection, tenant: str, now: datetime | None = None) -> dict:
    db.execute("SAVEPOINT protection_coverage")
    try:
        ledger_id = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM paper_ledger WHERE tenant_id=?", (tenant,)
        ).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM paper_positions WHERE tenant_id=? ORDER BY symbol,market,asset_class", (tenant,)
        ).fetchall()
    finally:
        db.execute("RELEASE SAVEPOINT protection_coverage")
    issues = []
    issue_count = covered = missing = invalid = 0
    for row in rows:
        codes = position_geometry_issues(row)
        stop, target = positive_level(row["stop_price"]), positive_level(row["take_profit_price"])
        if row["stop_price"] is None:
            codes.append("missing_stop")
            missing += 1
        elif stop is None:
            codes.append("invalid_stop")
        if row["take_profit_price"] is not None and target is None:
            codes.append("invalid_target")
        if stop is not None and target is not None and target <= stop:
            codes.append("target_not_above_stop")
        if codes:
            invalid += int(any(code != "missing_stop" for code in codes))
            issue_count += len(codes)
            for code in codes:
                if len(issues) < 50:
                    issues.append({"key": f"{row['market']}:{row['asset_class']}:{row['symbol']}"[:200], "code": code})
        else:
            covered += 1
    return {
        "schema": "pramana.protection_coverage.v1", "tenantId": tenant,
        "status": "invalid" if invalid else "incomplete" if missing else "complete",
        "checkedAt": (now or datetime.now(timezone.utc)).isoformat(), "ledgerId": ledger_id,
        "positionCount": len(rows), "coveredCount": covered, "missingStopCount": missing,
        "invalidPositionCount": invalid, "issueCount": issue_count, "issues": issues,
        "scope": "stored_paper_levels_only; feed_freshness_and_execution_are_separate",
    }
