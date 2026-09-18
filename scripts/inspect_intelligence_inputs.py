#!/usr/bin/env python3
"""Inspect production intelligence selection without fetching data or starting services."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.operations.intelligence_inputs import inspect_intelligence_configuration


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"]):
        print(__doc__ + " No arguments. Output is configuration-only, never acceptance.")
        return 0
    if args:
        print("Intelligence configuration inspection refused: unsupported arguments.", file=sys.stderr)
        return 2
    try:
        print(json.dumps(inspect_intelligence_configuration(), sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - do not expose private environment or exception details
        print("Intelligence configuration inspection refused; inspect the private configuration locally.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
