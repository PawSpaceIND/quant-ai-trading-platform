#!/usr/bin/env python3
"""Ingest one private numeric-observation batch for shadow training; no provider requests."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fit_shadow_candidate import load_grants, read_private_bytes

from quant_ai.learning.ingestion import MAX_BATCH_BYTES, ReceivedFeatureStore, _prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--grants", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false":
            raise ValueError("paper process required")
        raw = read_private_bytes(args.input, MAX_BATCH_BYTES)
        grants = load_grants(read_private_bytes(args.grants, MAX_BATCH_BYTES))
        # Invalid observations must not even initialize the destination.
        _prepare(raw, grants, datetime.now(timezone.utc))
        with ReceivedFeatureStore(args.store, tenant_id=args.tenant) as store:
            result = store.receive(raw, source_grants=grants)
        print(json.dumps({**result, "source_authenticity_verified": False,
                          "past_availability_reconstructed": False}, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - input paths, records and provider declarations are private
        print("Observation ingestion not confirmed. Invalid evidence, conflicting identity or unavailable store. Retained data is not reset; inspect before retrying.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
