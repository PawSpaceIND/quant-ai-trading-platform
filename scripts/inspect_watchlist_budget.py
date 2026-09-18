"""Read existing AI counters without renewing tokens, making calls or editing budgets.

Run inside the existing paper container, including by passing this file on stdin.
Only selected numeric limits/counters are emitted; input paths and other environment
values never appear in diagnostics. Aggregate counters are not an API invoice.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Direct checkout invocation and stdin invocation both reuse the deployed budget
# constants/parser. Do not instantiate SqliteAIBudget: its constructor creates state.
if __file__ != "<stdin>":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.llm.anthropic_client import BUDGET_SCOPE, HEADLINE_BUDGET_SCOPE
from quant_ai.llm.budget import (
    CALL_LIMIT_ENV,
    DATABASE_ENV,
    DEFAULT_DAILY_CALL_LIMIT,
    DEFAULT_DAILY_TOKEN_LIMIT,
    DEFAULT_DATABASE_NAME,
    TOKEN_LIMIT_ENV,
    _env_limit,
)

SCOPES = (BUDGET_SCOPE, HEADLINE_BUDGET_SCOPE)
MAX_DAYS = 7
SQLITE_MAX_INTEGER = 2**63 - 1


class BudgetObservationError(ValueError):
    """Fixed diagnostic codes; never embed input or database exception text."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise BudgetObservationError(code)


def _limits(environ: Mapping[str, str]) -> dict:
    result = {}
    for key, default in ((CALL_LIMIT_ENV, DEFAULT_DAILY_CALL_LIMIT),
                         (TOKEN_LIMIT_ENV, DEFAULT_DAILY_TOKEN_LIMIT)):
        try:
            value = _env_limit(environ, key, default)
        except (TypeError, ValueError, AttributeError):
            raise BudgetObservationError("budget_limit_invalid") from None
        _require(0 < value <= SQLITE_MAX_INTEGER, "budget_limit_disabled_or_unusable")
        result[key] = {"value": value, "source": "environment" if environ.get(key, "").strip()
                       else "runtime_default"}
    return result


def observe_budget(
    database: Path, *, now: datetime, days: int, environ: Mapping[str, str],
) -> dict:
    """Observe a single consistent read-only snapshot of the two existing budget scopes.

    These are reserved logical calls and recorded input/output tokens. Missing rows
    remain unknown, not fabricated zeros. In-flight requests, SDK retries and cache-
    billing categories cannot be reconstructed from this aggregate table.
    """
    _require(environ.get("TRADING_LIVE_MONEY_ACTIVE", "").strip().lower() == "false",
             "budget_paper_mode_required")
    _require(isinstance(now, datetime) and now.utcoffset() is not None,
             "budget_aware_clock_required")
    _require(type(days) is int and 1 <= days <= MAX_DAYS, "budget_window_invalid")
    limits = _limits(environ)
    call_limit = limits[CALL_LIMIT_ENV]["value"]
    token_limit = limits[TOKEN_LIMIT_ENV]["value"]
    current = now.astimezone(timezone.utc)
    dates = [(current - timedelta(days=offset)).date().isoformat()
             for offset in range(days - 1, -1, -1)]

    try:
        info = database.lstat()
        _require(stat.S_ISREG(info.st_mode), "budget_regular_database_required")
        uri = database.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            # Bound pathological scans as well as the number of returned rows.
            steps = [0]

            def progress():
                steps[0] += 1
                return int(steps[0] > 100)

            db.set_progress_handler(progress, 1000)
            db.execute("BEGIN")
            table = db.execute(
                "SELECT type FROM sqlite_master WHERE name='ai_budget'"
            ).fetchall()
            _require(table == [("table",)], "budget_table_unavailable")
            rows = db.execute(
                "SELECT day, scope, calls, input_tokens, output_tokens FROM ai_budget "
                "WHERE day BETWEEN ? AND ? AND scope IN (?, ?) "
                "ORDER BY day, scope LIMIT ?",
                (dates[0], dates[-1], *SCOPES, 2 * days + 1),
            ).fetchall()
            _require(len(rows) <= 2 * days, "budget_row_limit")
            db.rollback()
    except BudgetObservationError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise BudgetObservationError("budget_database_unavailable") from None

    observed = {}
    for day, scope, calls, inputs, outputs in rows:
        _require(day in dates and scope in SCOPES, "budget_row_identity_invalid")
        _require((day, scope) not in observed, "budget_duplicate_or_excess_rows")
        _require(all(type(value) is int and 0 <= value <= SQLITE_MAX_INTEGER
                     for value in (calls, inputs, outputs)), "budget_counters_invalid")
        tokens = inputs + outputs
        observed[day, scope] = {
            "dayUTC": day, "scope": scope, "rowPresent": True,
            "reservedLogicalCalls": calls,
            "recordedInputTokens": inputs, "recordedOutputTokens": outputs,
            "recordedTokens": tokens,
            "remainingCallsAtSnapshot": max(0, call_limit - calls),
            "remainingRecordedTokensAtSnapshot": max(0, token_limit - tokens),
            "belowBothCountersAtSnapshot": calls < call_limit and tokens < token_limit,
        }
    records = []
    for day in dates:
        for scope in SCOPES:
            records.append(observed.get((day, scope), {
                "dayUTC": day, "scope": scope, "rowPresent": False,
                "reservedLogicalCalls": None,
                "recordedInputTokens": None, "recordedOutputTokens": None,
                "recordedTokens": None, "remainingCallsAtSnapshot": None,
                "remainingRecordedTokensAtSnapshot": None,
                "belowBothCountersAtSnapshot": None,
            }))
    return {
        "schema": "pramana.watchlist_budget_observation.v1",
        "status": "observed", "checkedAt": current.isoformat(),
        "budgetDayBasis": "UTC", "windowDays": days,
        "limitsPerScopePerUTCDate": limits, "usage": records,
        "fiftyNameConsensus": {
            "logicalCallsPerCompleteSweep": 50,
            "completeSweepsWithinDailyCallLimit": call_limit // 50,
            "note": "Call-count capacity only; token exhaustion and latency may reduce coverage",
        },
        "currencyCost": None, "forecastTokenDemand": None, "measuredLatency": None,
        "actualPaidRequestCount": None, "deploymentApproved": False,
        "limitations": [
            "Counters are per scope, not a single shared daily allowance",
            "Reserved logical calls include requests that may fail; not completed analyses or fills",
            "SDK retries and in-flight usage are not fully represented by these counters",
            "Recorded tokens omit separate cache-billing categories; do not infer an invoice",
            "A missing row is unknown; a present all-zero row is a distinct observation",
            "A UTC counter day is not an IST trading session; no automatic timezone relabelling",
            "No current fifty-name token, latency or monetary forecast is inferred",
        ],
        "providerRequestsMade": 0, "budgetChanged": False,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BudgetObservationError("budget_arguments_invalid")


def main(argv=None) -> int:
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--database", type=Path)
        parser.add_argument("--days", type=int, default=3)
        args = parser.parse_args(argv)
        database = args.database
        if database is None:
            configured = os.getenv(DATABASE_ENV, "").strip()
            database = (Path(configured).expanduser() if configured else
                        Path(os.getenv("PRAMANA_LEDGER_PATH") or "/data/pramana.db").parent
                        / DEFAULT_DATABASE_NAME)
        report = observe_budget(database, now=datetime.now(timezone.utc),
                                days=args.days, environ=os.environ)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except BudgetObservationError as error:
        print(json.dumps({"status": "observation_refused", "reason": str(error)}))
        return 2
    except Exception:  # noqa: BLE001 - diagnostic boundary must not echo private input
        print(json.dumps({"status": "observation_refused", "reason": "budget_observation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
