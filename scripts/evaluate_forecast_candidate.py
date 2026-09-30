#!/usr/bin/env python3
"""Fit earlier recorded observations and evaluate later ones without trading authority."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT / "scripts"))
from fit_shadow_candidate import load_grants, publish_new_bundle, read_private_bytes

from quant_ai.learning.feature_dataset import MAX_PACKAGE_BYTES, canonical
from quant_ai.learning.forecast_evaluation import evaluate_forecasts
from quant_ai.learning.rolling_evaluation import evaluate_rolling_forecasts
from quant_ai.learning.shadow import MAX_PAYLOAD_BYTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--grants", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training-cutoff")
    parser.add_argument("--holdout-start")
    parser.add_argument("--folds", type=Path, help="Private JSON list of 2–12 explicit expanding-window folds")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false":
            raise ValueError("paper-only process required")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("existing output requires review")
        inputs = {"source_grants": load_grants(read_private_bytes(args.grants, MAX_PAYLOAD_BYTES)),
                  "run_id": args.run_id, "candidate_id": args.candidate_id}
        package = read_private_bytes(args.package, MAX_PACKAGE_BYTES)
        if args.folds is not None:
            if args.training_cutoff is not None or args.holdout_start is not None:
                raise ValueError("choose either folds or a single holdout")
            report = evaluate_rolling_forecasts(package,
                folds=json.loads(read_private_bytes(args.folds, MAX_PAYLOAD_BYTES)), **inputs)
        else:
            if args.training_cutoff is None or args.holdout_start is None:
                raise ValueError("explicit single holdout boundaries required")
            report = evaluate_forecasts(package, training_cutoff=args.training_cutoff,
                holdout_start=args.holdout_start, **inputs)
        publish_new_bundle(args.output, canonical(report))
        print(json.dumps({"mode": report["mode"], "training_rows": report["training"]["rows"] if "training" in report else None,
            "holdout_rows": report["candidate"]["samples"], "excluded_rows": len(report["split"]["excluded"]) if "split" in report else None,
            "brier_score": report["candidate"]["brier_score"], "comparisons": report["comparisons"],
            "trading_authorized": False, "promotion_authorized": False}, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - keep private paths, vectors and exceptions off stdout
        print("Forecast evaluation not confirmed. Inspect inputs and existing output before retrying.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
