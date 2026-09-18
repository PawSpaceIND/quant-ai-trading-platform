#!/usr/bin/env python3
"""Fit one offline shadow candidate from private, training-only numeric records."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant
from quant_ai.learning.fitting import MAX_INPUT_BYTES, FitConfig, fit_candidate
from quant_ai.learning.shadow import MAX_PAYLOAD_BYTES, _canonical, decode


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_nlink)


def read_private_bytes(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > maximum):
            raise ValueError("private bounded input required")
        raw = handle.read(maximum + 1)
        if (not raw or len(raw) > maximum or _identity(before) != _identity(os.fstat(handle.fileno()))
                or _identity(before) != _identity(path.lstat())):
            raise ValueError("input changed or exceeded its bound")
        return raw


def load_grants(raw):
    rows = decode(raw)
    if type(rows) is not list or not 1 <= len(rows) <= 64:
        raise ValueError("source grants required")
    grants = []
    for row in rows:
        if type(row) is not dict:
            raise ValueError("source grant object required")
        item = dict(row)
        item["categories"] = frozenset(KnowledgeCategory(v) for v in item["categories"])
        item["planes"] = frozenset(AccessPlane(v) for v in item["planes"])
        item["rights_status"] = RightsStatus(item["rights_status"])
        grants.append(SourceGrant(**item))
    return tuple(grants)


def publish_new_bundle(path, raw):
    # A same-directory hard-link publishes fully written bytes without replacing any
    # existing destination. Interrupted publication can leave a second private link;
    # the existing shadow reader refuses multiply-linked files until reviewed cleanup.
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("existing private output directory required")
    if path.exists() or path.is_symlink():
        raise ValueError("output already exists")
    fd, temporary = tempfile.mkstemp(prefix=".shadow-fit-", dir=parent)
    try:
        with os.fdopen(fd, "w+b") as handle:
            if handle.write(raw) != len(raw):
                raise OSError("incomplete candidate write")
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if handle.read() != raw:
                raise OSError("candidate readback mismatch")
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--grants", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false":
            raise ValueError("paper-only process required")
        # Refuse existing outputs before fitting, even for a failed/partial prior run.
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output exists; review the prior result")
        raw = read_private_bytes(args.input, MAX_INPUT_BYTES)
        grants = load_grants(read_private_bytes(args.grants, MAX_PAYLOAD_BYTES))
        result = fit_candidate(raw, source_grants=grants, run_id=args.run_id,
                               candidate_id=args.candidate_id, config=FitConfig())
        publish_new_bundle(args.output, _canonical(result.bundle.payload()).encode())
        report = result.diagnostics
        print(json.dumps({"schema": report["schema"], "mode": "TRAINING_ONLY",
            "rows": report["rows"], "positive_after_cost_rows": report["positive_after_cost_rows"],
            "iterations": report["optimization"]["iterations"],
            "converged": report["optimization"]["converged"],
            "training_objective": report["optimization"]["final_objective"],
            "trading_authorized": False, "out_of_sample_evaluated": False,
            "source_authenticity_verified": False, "calibration_verified": False}, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - suppress private input/path/exception text
        print("Offline fitting not confirmed: invalid evidence or unavailable output. Existing outputs are never overwritten; inspect before retrying.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
