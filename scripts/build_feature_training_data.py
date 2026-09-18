#!/usr/bin/env python3
"""Build a source-backed training package, or fit that package without trading authority."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT / "scripts"))
from fit_shadow_candidate import load_grants, publish_new_bundle, read_private_bytes

from quant_ai.learning.feature_dataset import (
    MAX_PACKAGE_BYTES,
    MAX_PLAN_BYTES,
    build_training_package,
    canonical,
    fit_training_package,
)
from quant_ai.learning.shadow import MAX_PAYLOAD_BYTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--plan", type=Path, required=True)
    fit = actions.add_parser("fit")
    fit.add_argument("--package", type=Path, required=True)
    fit.add_argument("--run-id", required=True)
    fit.add_argument("--candidate-id", required=True)
    for action in (build, fit):
        action.add_argument("--grants", type=Path, required=True)
        action.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false":
            raise ValueError("paper-only command")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("existing output requires review")
        grants = load_grants(read_private_bytes(args.grants, MAX_PAYLOAD_BYTES))
        if args.action == "build":
            raw = build_training_package(args.source, read_private_bytes(args.plan, MAX_PLAN_BYTES),
                                         source_grants=grants, now=datetime.now(timezone.utc))
            summary = {"mode": "TRAINING_DATA_ONLY", "rows": len(json.loads(raw)["data"]["rows"])}
        else:
            result = fit_training_package(read_private_bytes(args.package, MAX_PACKAGE_BYTES),
                source_grants=grants, run_id=args.run_id, candidate_id=args.candidate_id)
            raw = canonical(result.bundle.payload())
            summary = {"mode": "TRAINING_ONLY", "rows": result.diagnostics["rows"]}
        publish_new_bundle(args.output, raw)
        print(json.dumps({**summary, "trading_authorized": False, "source_authenticity_verified": False,
                          "out_of_sample_evaluated": False}, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - do not print private paths, observations or diagnostics
        print("Training-data operation not confirmed. Inspect inputs and existing output before retrying.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
