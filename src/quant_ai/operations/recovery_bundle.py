"""Hash-verified paper state bundles for stopped writers; never overwrite a restore.

Secrets are provisioned separately. This does not stop processes or attest that an
operator stopped them. Pre/post source fingerprints detect changes during capture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.execution.reconciliation import reconcile_paper

KINDS = {"ledger": "sqlite", "console": "sqlite", "proofs": "directory", "reviews": "directory",
         "directives": "file", "halt": "optional", "research": "optional"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Not a regular file: {path.name}")


def sources(spec: dict) -> dict[str, Path]:
    if set(spec.get("sources", {})) != set(KINDS):
        raise ValueError("Specify ledger, console, proofs, reviews, directives, halt and research paths")
    if not re.fullmatch(r"[0-9a-f]{40}", spec.get("revision", "")) or not spec.get("tenant"):
        raise ValueError("Exact release SHA and tenant required")
    return {name: Path(value).absolute() for name, value in spec["sources"].items()}


def inventory(paths: dict[str, Path]) -> dict[str, str]:
    result = {}
    for role, path in paths.items():
        if path.is_symlink():
            raise ValueError("Symlink sources are unsupported")
        if not path.exists() and KINDS[role] == "optional":
            result[role] = "absent"
            continue
        if KINDS[role] == "directory":
            if not path.is_dir():
                raise ValueError(f"Directory missing: {role}")
            result[role + "/"] = "directory"
            for item in sorted(path.rglob("*")):
                if item.is_symlink():
                    raise ValueError("Symlink sources are unsupported")
                if item.is_dir():
                    result[role + "/" + item.relative_to(path).as_posix() + "/"] = "directory"
                else:
                    regular(item)
                    result[role + "/" + item.relative_to(path).as_posix()] = digest(item)
        else:
            regular(path)
            result[role] = digest(path)
            if KINDS[role] == "sqlite":
                wal = Path(str(path) + "-wal")
                if wal.exists():
                    regular(wal)
                    result[role + "-wal"] = digest(wal)
    return result


def sqlite_backup(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dest:
        src.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    target.chmod(0o600)


def create(spec: dict, destination: Path, *, writers_stopped: bool) -> dict:
    if not writers_stopped:
        raise ValueError("Stop all writers before creating a cross-file bundle")
    paths = sources(spec)
    destination = destination.absolute()
    for source in paths.values():
        if destination.resolve() == source.resolve() or destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("Bundle must be outside source paths")
    before = inventory(paths)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        for role, source in paths.items():
            if before.get(role) == "absent":
                continue
            kind = KINDS[role]
            target = destination / role
            if kind == "sqlite":
                sqlite_backup(source, target)
            elif kind == "directory":
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copyfile(source, target, follow_symlinks=False)
        if before != inventory(paths):
            raise ValueError("Source changed during capture; stop writers and retry")
        files = {}
        directories = []
        for item in sorted(destination.rglob("*")):
            relative = item.relative_to(destination).as_posix()
            if item.is_dir():
                item.chmod(0o700)
                directories.append(relative)
            else:
                regular(item)
                item.chmod(0o600)
                files[relative] = {"sha256": digest(item), "size": item.stat().st_size}
        manifest = {"schema": 1, "tenant": spec["tenant"], "revision": spec["revision"],
                    "createdAt": datetime.now(timezone.utc).isoformat(), "files": files,
                    "directories": directories,
                    "absent": [role for role in KINDS if before.get(role) == "absent"],
                    "sourceFingerprint": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    "consistency": "operator-declared stopped writers; unchanged capture fingerprints",
                    "secrets": "No secret-store discovery; selected sources must be reviewed to exclude credentials"}
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (destination / "manifest.json").chmod(0o600)
        return {**manifest, "manifestSha256": digest(destination / "manifest.json")}
    except Exception:
        shutil.rmtree(destination)
        raise


def safe_relative(value: str) -> Path:
    part = Path(value)
    if not value or part.is_absolute() or ".." in part.parts or part.as_posix() != value:
        raise ValueError("Unsafe manifest path")
    return part


def restore(bundle: Path, destination: Path, *, manifest_sha256: str) -> dict:
    start = time.monotonic()
    regular(bundle / "manifest.json")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or "") or digest(bundle / "manifest.json") != manifest_sha256:
        raise ValueError("Trusted manifest checksum mismatch")
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported bundle schema")
    files, directories = manifest["files"], manifest.get("directories", [])
    expected = set(files) | set(directories) | {"manifest.json"}
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*")}
    if actual != expected:
        raise ValueError("Bundle inventory mismatch")
    for name in expected:
        item = bundle / safe_relative(name)
        if item.is_symlink():
            raise ValueError("Symlinks are unsupported")
        if name in files:
            regular(item)
            if item.stat().st_size != files[name]["size"] or digest(item) != files[name]["sha256"]:
                raise ValueError("Bundle checksum mismatch")
        elif name in directories and not item.is_dir():
            raise ValueError("Bundle directory mismatch")
    if not {"ledger", "console", "directives"} <= files.keys() or not {"proofs", "reviews"} <= set(directories):
        raise ValueError("Required recovery state missing")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        for name in sorted(directories, key=lambda n: len(Path(n).parts)):
            (destination / safe_relative(name)).mkdir(mode=0o700)
        for name, info in files.items():
            target = destination / safe_relative(name)
            shutil.copyfile(bundle / name, target)
            target.chmod(0o600)
            if digest(target) != info["sha256"]:
                raise ValueError("Restored checksum mismatch")
        with closing(sqlite3.connect(f"{(destination / 'ledger').resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored ledger integrity failed")
            reconciliation = reconcile_paper(db, manifest["tenant"])
            filled_ids = {r[0] for r in db.execute("SELECT order_id FROM paper_ledger WHERE tenant_id=? AND status='FILLED'", (manifest["tenant"],))}
        proof_ids = set()
        invalid_proofs = 0
        for proof in (destination / "proofs").rglob("*.json"):
            try:
                payload = json.loads(proof.read_text())
                if not isinstance(payload, dict):
                    raise TypeError("Invalid proof object")
                if isinstance(payload.get("order_id"), str):
                    proof_ids.add(payload["order_id"])
            except (ValueError, TypeError, UnicodeError):
                invalid_proofs += 1
        missing_proofs = filled_ids - proof_ids
        with closing(sqlite3.connect(f"{(destination / 'console').resolve().as_uri()}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored console integrity failed")
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("preferences", "conversations", "audit")}
        result = {"status": "restored" if reconciliation["status"] == "matched" and not missing_proofs and not invalid_proofs else "discrepancy",
                  "tenant": manifest["tenant"], "revision": manifest["revision"],
                  "manifestSha256": manifest_sha256,
                  "durationSeconds": round(time.monotonic() - start, 3), "fileCount": len(files),
                  "consoleCounts": counts, "reconciliation": reconciliation,
                  "proofCoverage": {"filledOrders": len(filled_ids), "missingCount": len(missing_proofs),
                      "missingOrderIds": sorted(missing_proofs)[:50], "invalidJsonFiles": invalid_proofs,
                      "scope": "Order-ID presence only; not proof authenticity or decision validation"},
                  "haltPresent": (destination / "halt").exists(),
                  "activation": "none; do not start against restored state without review and secrets"}
        (destination / "restore-report.json").write_text(json.dumps(result, indent=2))
        (destination / "restore-report.json").chmod(0o600)
        return result
    except Exception:
        shutil.rmtree(destination)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "restore"))
    parser.add_argument("--source", type=Path, required=True, help="Create: spec JSON; restore: bundle directory")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--manifest-sha256", help="Restore: independently retained creation digest")
    args = parser.parse_args()
    result = (create(json.loads(args.source.read_text()), args.destination, writers_stopped=args.writers_stopped)
              if args.action == "create" else restore(args.source, args.destination, manifest_sha256=args.manifest_sha256))
    print(json.dumps(result, indent=2))
    return 2 if result.get("status") == "discrepancy" else 0


if __name__ == "__main__":
    raise SystemExit(main())
