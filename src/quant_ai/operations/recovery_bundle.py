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
from quant_ai.governance.runtime_identity import path_digest
from quant_ai.operations import oms_recovery, research_recovery

KINDS = {"ledger": "sqlite", "console": "sqlite", "proofs": "directory", "reviews": "directory",
         "directives": "file", "halt": "optional", "research": "optional"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Not a regular file: {path.name}")


def sources(spec: dict) -> dict[str, Path]:
    if set(spec) - {"tenant", "revision", "sources", "research_state", "oms"}:
        raise ValueError("Unknown recovery specification fields")
    if set(spec.get("sources", {})) != set(KINDS):
        raise ValueError("Specify ledger, console, proofs, reviews, directives, halt and research paths")
    if not re.fullmatch(r"[0-9a-f]{40}", spec.get("revision", "")) or not spec.get("tenant"):
        raise ValueError("Exact release SHA and tenant required")
    return {name: Path(value).absolute() for name, value in spec["sources"].items()}


def source_plan(spec: dict) -> tuple[dict[str, Path], dict[str, str], dict]:
    paths, kinds, research = sources(spec), dict(KINDS), {}
    if "oms" in spec:
        raw = spec["oms"]
        if not isinstance(raw, str) or not raw.strip() or raw == ":memory:":
            raise ValueError("Recovery OMS requires an explicit durable path")
        paths["oms"] = Path(raw).absolute()
        kinds["oms"] = "sqlite"
        if (paths["oms"].is_symlink() or not paths["oms"].is_file()
                or paths["oms"].stat().st_nlink != 1):
            raise ValueError("Recovery OMS requires an unaliased existing regular file")
    selected = spec.get("research_state", {})
    if not isinstance(selected, dict) or len(selected) > 64:
        raise ValueError("Select at most 64 named research sources")
    for name, entry in selected.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("Invalid research source name")
        if (not isinstance(entry, dict) or set(entry) != {"kind", "path"}
                or entry["kind"] not in research_recovery.STORAGE
                or not isinstance(entry["path"], str) or not entry["path"].strip()):
            raise ValueError("Research source requires supported kind and explicit path")
        role = f"research-state/{name}"
        paths[role] = Path(entry["path"]).absolute()
        kinds[role] = research_recovery.STORAGE[entry["kind"]]
        research[name] = {"kind": entry["kind"], "path": role}
    resolved = [p.resolve() for p in paths.values()]
    for i, path in enumerate(resolved):
        if any(path == other or path.is_relative_to(other) or other.is_relative_to(path)
               for other in resolved[:i]):
            raise ValueError("Selected recovery sources must not overlap")
    return paths, kinds, research


def inventory(paths: dict[str, Path], kinds: dict[str, str] | None = None) -> dict[str, str]:
    kinds = kinds or KINDS
    result = {}
    for role, path in paths.items():
        if path.is_symlink():
            raise ValueError("Symlink sources are unsupported")
        if not path.exists() and kinds[role] == "optional":
            result[role] = "absent"
            continue
        if kinds[role] == "directory":
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
            if kinds[role] == "sqlite":
                wal = Path(str(path) + "-wal")
                if wal.exists():
                    regular(wal)
                    # A read-only SQLite open can create a zero-byte WAL sidecar.
                    # It carries no database frames; nonempty WALs remain fingerprinted.
                    if wal.stat().st_size:
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
    if writers_stopped is not True:
        raise ValueError("Stop all writers before creating a cross-file bundle")
    paths, kinds, research = source_plan(spec)
    binding = oms_recovery.configuration(paths["ledger"], spec["tenant"])
    if binding is not None and "oms" not in paths:
        raise ValueError("Recovery OMS required for bound account")
    if "oms" in paths and (binding is None or path_digest(paths["oms"]) != binding["oms_path_sha256"]):
        raise ValueError("Recovery OMS source does not match the bound account")
    destination = destination.absolute()
    for source in paths.values():
        if destination.resolve() == source.resolve() or destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("Bundle must be outside source paths")
    before = inventory(paths, kinds)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        if research:
            (destination / "research-state").mkdir(mode=0o700)
        for role, source in paths.items():
            if before.get(role) == "absent":
                continue
            kind = kinds[role]
            target = destination / role
            if kind == "sqlite":
                sqlite_backup(source, target)
            elif kind == "directory":
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copyfile(source, target, follow_symlinks=False)
        if before != inventory(paths, kinds):
            raise ValueError("Source changed during capture; stop writers and retry")
        for entry in research.values():
            entry["verification"] = research_recovery.inspect(destination / entry["path"], entry["kind"])
        order_state = None
        if binding is not None:
            order_state = {
                "path": "oms", "sourcePathSha256": binding["oms_path_sha256"],
                "verification": oms_recovery.inspect(destination / "ledger", destination / "oms",
                                                     spec["tenant"], binding["oms_path_sha256"]),
            }
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
        manifest = {"schema": 3 if order_state is not None else 2, "tenant": spec["tenant"], "revision": spec["revision"],
                    "createdAt": datetime.now(timezone.utc).isoformat(), "files": files,
                    "directories": directories,
                    "researchState": research,
                    "absent": [role for role in KINDS if before.get(role) == "absent"],
                    "sourceFingerprint": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                    "consistency": "operator-declared stopped writers; unchanged capture fingerprints",
                    "secrets": "No secret-store discovery; selected sources must be reviewed to exclude credentials"}
        if order_state is not None:
            manifest["orderState"] = order_state
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        (destination / "manifest.json").chmod(0o600)
        return {**manifest, "manifestSha256": digest(destination / "manifest.json")}
    except Exception:
        shutil.rmtree(destination)
        raise


def safe_relative(value: str) -> Path:
    part = Path(value)
    if not value or value == "." or part.is_absolute() or ".." in part.parts or part.as_posix() != value:
        raise ValueError("Unsafe manifest path")
    return part


def restore(bundle: Path, destination: Path, *, manifest_sha256: str) -> dict:
    start = time.monotonic()
    regular(bundle / "manifest.json")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or "") or digest(bundle / "manifest.json") != manifest_sha256:
        raise ValueError("Trusted manifest checksum mismatch")
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema") not in (1, 2, 3) or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported bundle schema")
    files, directories = manifest["files"], manifest.get("directories", [])
    research = manifest.get("researchState") if manifest["schema"] in (2, 3) else {}
    if not isinstance(research, dict) or len(research) > 64:
        raise ValueError("Invalid research recovery inventory")
    for name, entry in research.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                or not isinstance(entry, dict) or set(entry) != {"kind", "path", "verification"}
                or entry["kind"] not in research_recovery.STORAGE
                or entry["path"] != f"research-state/{name}"
                or not isinstance(entry["verification"], dict)):
            raise ValueError("Invalid research recovery entry")
        expected_kind = research_recovery.STORAGE[entry["kind"]]
        if entry["path"] not in (directories if expected_kind == "directory" else files):
            raise ValueError("Required research recovery state missing")
    order_state = manifest.get("orderState")
    if manifest["schema"] == 3:
        if (not isinstance(order_state, dict)
                or set(order_state) != {"path", "sourcePathSha256", "verification"}
                or order_state["path"] != "oms" or "oms" not in files
                or not isinstance(order_state["verification"], dict)
                or not isinstance(order_state["sourcePathSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", order_state["sourcePathSha256"])):
            raise ValueError("Recovery OMS inventory invalid")
    elif order_state is not None or "oms" in files:
        raise ValueError("Recovery OMS requires bundle schema 3")
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
    bound_configuration = oms_recovery.configuration(bundle / "ledger", manifest["tenant"])
    if bound_configuration is not None and order_state is None:
        raise ValueError("Recovery OMS required for bound account; legacy bundle is incomplete")
    if order_state is not None and bound_configuration is None:
        raise ValueError("Recovery OMS bound account is missing")
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
        order_result = {"status": "not_configured", "liveExecutionAuthorized": False}
        if order_state is not None:
            order_result = oms_recovery.inspect(destination / "ledger", destination / "oms",
                manifest["tenant"], order_state["sourcePathSha256"])
            if order_result != order_state["verification"]:
                raise ValueError("Recovery OMS verification mismatch; use the captured release")
        research_results = {}
        for name, entry in research.items():
            checked = research_recovery.inspect(destination / entry["path"], entry["kind"])
            if checked != entry["verification"]:
                raise ValueError("Restored research verification mismatch; use the captured release")
            research_results[name] = {"kind": entry["kind"], "path": entry["path"], **checked}
        with closing(sqlite3.connect(f"{(destination / 'ledger').resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored ledger integrity failed")
            reconciliation = reconcile_paper(db, manifest["tenant"])
            filled_ids = {r[0] for r in db.execute("SELECT order_id FROM paper_ledger WHERE tenant_id=? AND status='FILLED'", (manifest["tenant"],))}
            protection_rows = db.execute("SELECT order_id,payload FROM paper_protection_evidence WHERE tenant_id=?", (manifest["tenant"],)).fetchall() if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_protection_evidence'").fetchone() else []
            decision_rows = db.execute("SELECT order_id,payload FROM paper_decision_evidence WHERE tenant_id=?", (manifest["tenant"],)).fetchall() if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_decision_evidence'").fetchone() else []
        proof_ids = set()
        invalid_proofs = 0
        records = [(r, "pramana.protective_exit.v1", "protective_exit") for r in protection_rows]
        records.extend((r, "pramana.swarm_fill.v1", "swarm_fill") for r in decision_rows)
        for row, schema, event_type in records:
            try:
                proof = json.loads(row["payload"])
                if (proof.get("schema") != schema or proof.get("event_type") != event_type
                        or proof.get("tenant_id") != manifest["tenant"] or proof.get("order_id") != row["order_id"]
                        or (event_type == "swarm_fill" and not isinstance(proof.get("input_matrix"), list))):
                    raise ValueError("Invalid ledger evidence")
                proof_ids.add(row["order_id"])
            except (ValueError, TypeError, AttributeError):
                invalid_proofs += 1
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
        result = {"status": "restored" if reconciliation["status"] == "matched" and not missing_proofs and not invalid_proofs and order_result["status"] != "discrepancy" else "discrepancy",
                  "tenant": manifest["tenant"], "revision": manifest["revision"],
                  "manifestSha256": manifest_sha256,
                  "durationSeconds": round(time.monotonic() - start, 3), "fileCount": len(files),
                  "consoleCounts": counts, "reconciliation": reconciliation,
                  "orderRecovery": order_result,
                  "researchRecovery": {
                      "status": "selected_state_verified" if research else "not_selected",
                      "sources": research_results,
                      "scope": "Explicitly selected state only; no source discovery, provider retry, publication or qualification"},
                  "proofCoverage": {"filledOrders": len(filled_ids), "missingCount": len(missing_proofs),
                      "missingOrderIds": sorted(missing_proofs)[:50], "invalidJsonRecords": invalid_proofs, "ledgerProtectionRecords": len(protection_rows),
                      "ledgerDecisionRecords": len(decision_rows),
                      "scope": "File/ledger order-ID presence only; not proof authenticity or decision validation"},
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
