#!/usr/bin/env python3
"""Prepare an offline 50-name proposal; never edit .env or start the trading engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.governance.nse_watchlist import (
    WatchlistPreparationError,
    prepare_bundle,
)


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise WatchlistPreparationError("watchlist_input_too_large")
    return value


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WatchlistPreparationError("watchlist_duplicate_json_key")
        result[key] = value
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-directives", type=Path, required=True)
    parser.add_argument("--constituents", type=Path, required=True)
    parser.add_argument("--constituents-observed-at", required=True)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--master-observed-at", required=True)
    parser.add_argument("--daily-call-limit", type=int, required=True)
    parser.add_argument("--daily-token-limit", type=int, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        base = json.loads(read_bounded(args.base_directives, 1_048_576), object_pairs_hook=unique_pairs)
        result = prepare_bundle(
            base, read_bounded(args.constituents, 200_000), read_bounded(args.master, 10_000_000),
            now=datetime.now(timezone.utc),
            constituents_at=datetime.fromisoformat(args.constituents_observed_at.replace("Z", "+00:00")),
            master_at=datetime.fromisoformat(args.master_observed_at.replace("Z", "+00:00")),
            daily_call_limit=args.daily_call_limit, daily_token_limit=args.daily_token_limit,
        )
        destination = args.output_directory
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        destination.chmod(0o700)  # Clear inherited setgid; do not alter the parent directory.
        files = {"directives.json": json.dumps(result["directives"], indent=2) + "\n",
                 "sector-map.json": json.dumps(result["sectorMap"], indent=2) + "\n",
                 "mapping.env": "".join(f"{key}='{value}'\n" for key, value in result["environment"].items()),
                 # Written last. Without this complete report the directory is incomplete.
                 "proposal.json": json.dumps(result, indent=2) + "\n"}
        for name, text in files.items():
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
        print(json.dumps({"status": "PROPOSAL_PREPARED_NOT_DEPLOYED", "symbols": 50,
                          "hostAcceptance": False, "sourceQualification": result["sourceQualification"],
                          "workload": result["workload"]}, indent=2))
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        # A wrong input might be a secret-bearing file. Never interpolate its contents.
        print("PREPARATION_REFUSED: check input files, timestamps, scope and unused output directory.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
