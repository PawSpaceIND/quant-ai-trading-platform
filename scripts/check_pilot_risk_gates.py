#!/usr/bin/env python3
"""Read-only public-history preflight. No broker, ledger, LLM or account credentials.

Use --online to authorize the existing Yahoo daily-history HTTP provider. This checks
source availability and sample coverage; it neither starts nor certifies the pilot host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_ai.governance.directives import FounderDirectives
from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
from quant_ai.marketdata.timeframes import DailyHistoryProvider
from quant_ai.risk.book_history import DailyCloseHistory, _unique_pairs, normalize_sector_map
from quant_ai.risk.policy import BookRiskFirewall


def check(directives, sectors, provider, now):
    instruments = directives.watchlist
    if not instruments:
        raise ValueError("watchlist_required")
    symbols = tuple(item.symbol for item in instruments)
    history = DailyCloseHistory(provider, instruments, clock=lambda: now, max_age=timedelta(days=7))
    effective = directives.sector_map or normalize_sector_map(sectors)
    firewall = BookRiskFirewall(history_provider=history, sector_map=effective,
                                required_symbols=symbols)
    problem = firewall.configuration_problem()
    rows = {}
    if not problem:
        try:
            rows = history(symbols)
        except (ValueError, TypeError, RuntimeError, OSError, ArithmeticError):
            problem = "history_fetch_or_validation_failed"
    state = history.readiness(symbols, now)
    return {"schema": "pramana.pilot_risk_preflight.v1", "checkedAt": now.isoformat(),
            "paperOnly": True, "hostAcceptance": False, "symbols": list(symbols),
            "allArmed": firewall.configuration_problem() is None,
            "dataReady": not problem and state["dataReady"],
            "sectorRecords": len(effective), "sectorGroups": len(set(effective.values())),
            "history": state, "reason": problem or state["reason"],
            "lastSessions": {symbol: values[-1][0] for symbol, values in rows.items() if values},
            "sourceRowsSha256": hashlib.sha256(json.dumps(rows, sort_keys=True,
                                                         default=str).encode()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directives", type=Path, default=ROOT / "deploy/founder-directives.example.json")
    parser.add_argument("--sector-map", type=Path, default=ROOT / "deploy/pilot-sector-map.example.json")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.online:
        print("Explicit --online required for public historical data requests.", file=sys.stderr)
        return 2
    try:
        directives = FounderDirectives.from_json(json.loads(args.directives.read_text()))
        sectors = json.loads(args.sector_map.read_text(), object_pairs_hook=_unique_pairs)
        provider = DailyHistoryProvider(ResilientHttpClient(UrllibTransport()))
        report = check(directives, sectors, provider, datetime.now(timezone.utc))
        rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
        print(rendered, end="")
        return 0 if report["dataReady"] else 1
    except (ValueError, TypeError, OSError):
        print("Risk preflight refused: invalid configuration or unavailable data.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
