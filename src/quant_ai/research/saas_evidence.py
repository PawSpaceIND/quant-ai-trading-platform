"""Local operator reports. Not an HTTP endpoint; no provider/broker calls."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.research.calibration import evaluate_forecasts
from quant_ai.service.saas_controls import SaaSControls


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("calibration")
    score.add_argument("--input", type=Path, required=True,
                       help="JSON object with protocol and records; max 5 MB")
    score.add_argument("--as-of", required=True, help="Timezone-aware evaluation cutoff")
    usage = commands.add_parser("usage")
    usage.add_argument("--database", type=Path, required=True)
    usage.add_argument("--tenant", required=True, help="Operator-selected tenant, never a public query parameter")
    usage.add_argument("--as-of", required=True, help="Timezone-aware date selecting the UTC billing month")
    args = parser.parse_args(argv)
    if args.command == "calibration":
        with args.input.open("rb") as stream:
            raw = stream.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("input_too_large")
        body = json.loads(raw)
        report = evaluate_forecasts(body["protocol"], body["records"], as_of=args.as_of)
    else:
        c = SaaSControls(args.database, readonly=True)
        try:
            at = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
            report = c.usage(args.tenant, now=at)
        finally:
            c.close()
    print(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "report": report},
                     indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
