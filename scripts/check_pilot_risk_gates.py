#!/usr/bin/env python3
"""Read-only public-history preflight. No broker, ledger, LLM or account credentials.

Use --online to authorize the existing Yahoo daily-history HTTP provider. This checks
source availability and sample coverage; it neither starts nor certifies the pilot host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


# Operator-input resource budget, not a trading or exchange limit.
MAX_INPUT_BYTES = 1024 * 1024


def _input_bytes(path: Path) -> bytes:
    """Read a bounded regular file without hanging on a FIFO or special device."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("preflight_input_must_be_regular")
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("preflight_input_too_large")
    return raw


def _invalid_constant(_value: str):
    raise ValueError("preflight_non_json_number")


def _input_object(raw: bytes) -> dict:
    value = json.loads(raw, object_pairs_hook=_unique_pairs,
                       parse_constant=_invalid_constant, parse_float=Decimal)
    if not isinstance(value, dict):
        raise TypeError("preflight_input_must_be_object")
    return value


def _unchanged(paths, originals) -> None:
    if any(_input_bytes(path) != raw for path, raw in zip(paths, originals, strict=True)):
        raise ValueError("preflight_input_changed")


@contextmanager
def _prepared_report(output: Path | None):
    """Check the destination and create a private staging file before provider work.

    The final filename stays absent until complete publication. A same-directory
    hard link publishes without ever replacing another writer's destination.
    """
    if output is None:
        yield None
        return
    if os.path.lexists(output):
        raise ValueError("preflight_output_already_exists")
    descriptor, name = tempfile.mkstemp(prefix=".risk-preflight-", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            yield output, temporary, handle
    finally:
        temporary.unlink(missing_ok=True)


def _publish_report(prepared, rendered: str, paths, originals) -> None:
    output, temporary, handle = prepared
    encoded = rendered.encode("utf-8")
    if handle.write(encoded) != len(encoded):
        raise OSError("preflight_report_short_write")
    handle.flush()
    os.fsync(handle.fileno())
    handle.seek(0)
    if handle.read() != encoded:
        raise OSError("preflight_report_readback_failed")
    _unchanged(paths, originals)
    os.link(temporary, output)
    # A failure here leaves only a COMPLETE report, but exits nonzero because
    # power-loss durability was not confirmed. Never remove a raced replacement.
    directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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
        paths = tuple(path.expanduser().absolute() for path in (args.directives, args.sector_map))
        originals = tuple(_input_bytes(path) for path in paths)
        directives = FounderDirectives.from_json(_input_object(originals[0]))
        sectors = normalize_sector_map(_input_object(originals[1]))
        output = args.output.expanduser().absolute() if args.output else None
        with _prepared_report(output) as prepared:
            provider = DailyHistoryProvider(ResilientHttpClient(UrllibTransport()))
            report = check(directives, sectors, provider, datetime.now(timezone.utc))
            _unchanged(paths, originals)
            report["inputs"] = {
                "directivesSha256": hashlib.sha256(originals[0]).hexdigest(),
                "sectorMapSha256": hashlib.sha256(originals[1]).hexdigest(),
                "unchangedAtCompletion": True,
            }
            rendered = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
            if prepared is not None:
                _publish_report(prepared, rendered, paths, originals)
        print(rendered, end="")
        return 0 if report["dataReady"] else 1
    except (ValueError, TypeError, OSError, ArithmeticError, RecursionError, KeyError, AttributeError):
        print("Risk preflight refused: invalid configuration or unavailable data.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
