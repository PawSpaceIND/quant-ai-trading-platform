"""Generate a private, input-bound benchmark attribution calculation report."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.analytics.benchmark_attribution import benchmark_attribution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("input_exceeds_bound")
        result = benchmark_attribution(json.loads(raw))
        result["inputSha256"] = hashlib.sha256(raw).hexdigest()
        result["input"] = json.loads(raw)
        payload = json.dumps(result, indent=2, allow_nan=False) + "\n"
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
    except (OSError, ValueError, ArithmeticError):
        print("Attribution publication failed: check input validity and choose a new writable output path.", file=sys.stderr)
        return 2
    print(json.dumps({"status": "calculated", "inputSha256": result["inputSha256"],
                      "sourceQualified": False, "liveExecutionEnabled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
