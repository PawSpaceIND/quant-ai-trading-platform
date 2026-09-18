#!/usr/bin/env python3
"""Explicit shadow-only inference from private artifact and feature files."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.learning.shadow import (
    MAX_PAYLOAD_BYTES,
    ShadowForecastWriter,
    ShadowModelBundle,
    decode,
)


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("private input required")
        return decode(handle.read(MAX_PAYLOAD_BYTES + 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--pair-id", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false":
            raise ValueError("paper-only process required")
        bundle = ShadowModelBundle.from_payload(read_private(args.bundle))
        features = read_private(args.features)
        with ShadowForecastWriter(args.journal, tenant_id=args.tenant) as writer:
            forecast = writer.record(bundle, features, pair_id=args.pair_id)
        print(json.dumps({"schema": "pramana.shadow_recorded.v1", "mode": "SHADOW",
                          "candidate_id": forecast.candidate_id,
                          "forecast_id": forecast.forecast_id,
                          "probability_positive_after_cost": str(forecast.probability_positive_after_cost),
                          "decision_at": forecast.decision_at.isoformat(),
                          "resolve_after": forecast.resolve_after.isoformat(),
                          "trading_authorized": False}, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - no arbitrary file/provider diagnostics on stdout
        print("Shadow forecast refused: invalid or unavailable model/input/journal evidence.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
