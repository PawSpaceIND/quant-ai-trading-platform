"""Explicit paper-daemon contract rollout, with a persistent account/configuration pin.

This does not expand pilot admission, migrate legacy approvals, or enable a broker transport.
A path digest pins local storage selection; it is not database or provider authentication.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from quant_ai.domain.models import Side
from quant_ai.instruments.identity import canonical_instrument_identity, instrument_from_identity

SCHEMA = "pramana.runtime_order_identity.v1"
MODES = frozenset({"legacy_cash", "bound_v1"})
TABLE = "paper_runtime_order_identity"


def parse_identity_mode(value: object) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("order_identity_mode_must_be_legacy_cash_or_bound_v1")
    return value


def path_digest(path: str | Path) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def validate_identity_storage(mode, *, pilot_mode, database, oms_database) -> str:
    mode = parse_identity_mode(mode)
    if mode == "legacy_cash":
        if oms_database is not None:
            raise ValueError("runtime_identity_oms_path_requires_bound_mode")
        return mode
    if pilot_mode is not True:
        raise ValueError("runtime_identity_bound_mode_requires_pilot")
    if any(value is None or not str(value).strip() or str(value) == ":memory:"
           for value in (database, oms_database)):
        raise ValueError("runtime_identity_durable_separate_databases_required")
    paper, oms = Path(database), Path(oms_database)
    if any(p.is_symlink() or p.is_dir() for p in (paper, oms)):
        raise ValueError("runtime_identity_storage_path_invalid")
    if paper.resolve() == oms.resolve() or paper.exists() and oms.exists() and paper.samefile(oms):
        raise ValueError("runtime_identity_databases_must_be_distinct")
    return mode


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _scope(instruments) -> dict[str, str]:
    result = {}
    for instrument in instruments:
        if instrument.symbol in result:
            raise ValueError("runtime_identity_duplicate_symbol")
        result[instrument.symbol] = canonical_instrument_identity(instrument)
    if not result:
        raise ValueError("runtime_identity_empty_scope")
    return result


def runtime_identity_configuration(broker, tenant_id: str) -> dict | None:
    with broker._lock:
        db = broker._connection
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = db.execute("SELECT payload,sha256 FROM paper_runtime_order_identity WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
            raise ValueError("runtime_identity_configuration_hash_mismatch")
        payload = json.loads(row["payload"])
        if (not isinstance(payload, dict) or set(payload) != {"schema", "mode", "instruments", "oms_path_sha256"}
                or payload["schema"] != SCHEMA or payload["mode"] != "bound_v1"
                or not isinstance(payload["oms_path_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload["oms_path_sha256"])
                or not isinstance(payload["instruments"], dict) or not payload["instruments"]):
            raise ValueError("runtime_identity_configuration_invalid")
        for symbol, raw in payload["instruments"].items():
            instrument = instrument_from_identity(raw)
            if instrument.symbol != symbol or canonical_instrument_identity(instrument) != raw:
                raise ValueError("runtime_identity_configuration_invalid")
        if _canonical(payload) != row["payload"]:
            raise ValueError("runtime_identity_configuration_not_canonical")
        return payload


def configure_runtime_identity(broker, instruments, tenant_id, mode, oms_database=None, *, oms=None) -> None:
    mode = parse_identity_mode(mode)
    expected = None if mode == "legacy_cash" else {
        "schema": SCHEMA, "mode": mode, "instruments": _scope(instruments),
        "oms_path_sha256": path_digest(oms_database),
    }
    with broker._lock:
        db = broker._connection
        if db.in_transaction:
            raise ValueError("runtime_identity_configuration_transaction_active")
        db.execute("BEGIN IMMEDIATE")
        try:
            previous = runtime_identity_configuration(broker, tenant_id)
            if previous is not None:
                if previous != expected:
                    raise ValueError("runtime_identity_configuration_change_requires_review")
            elif expected is not None:
                if oms is None or oms.all_orders(tenant_id):
                    raise ValueError("runtime_identity_existing_oms_requires_review")
                # Even a flat old book may have approvals or fills in another OMS. Do not
                # make a clean-start claim merely because there are no current positions.
                if (db.execute("SELECT 1 FROM paper_ledger WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone()
                        or db.execute("SELECT 1 FROM paper_positions WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone()):
                    raise ValueError("runtime_identity_legacy_history_requires_review")
                db.execute("CREATE TABLE IF NOT EXISTS paper_runtime_order_identity (tenant_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)")
                for verb in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS runtime_order_identity_{verb.lower()}_blocked BEFORE {verb} ON paper_runtime_order_identity BEGIN SELECT RAISE(ABORT,'Runtime identity configuration is immutable'); END")
                raw = _canonical(expected)
                db.execute("INSERT INTO paper_runtime_order_identity VALUES (?,?,?)", (tenant_id, raw, hashlib.sha256(raw.encode()).hexdigest()))
            db.commit()
        except BaseException:
            db.rollback()
            raise


def assert_runtime_order_identity(broker, order) -> None:
    # Covered exits are checked against the position's own immutable snapshot in the
    # broker, not this rollout metadata. A configuration failure must not trap an exit.
    if order.side is Side.SELL:
        return
    configuration = runtime_identity_configuration(broker, order.tenant_id)
    if configuration is None:
        return
    instrument = getattr(order, "instrument", None)
    if instrument is None:
        raise ValueError("runtime_identity_bound_order_required")
    if canonical_instrument_identity(instrument) != configuration["instruments"].get(order.symbol):
        raise ValueError("runtime_identity_order_out_of_scope")


def runtime_identity_entry_issue(daemon) -> str | None:
    """Bound-mode entry fence; no order submission and no restriction on protective exits."""
    from quant_ai.orders.oms import DurableOms
    from quant_ai.orders.state import OrderState
    runtime = daemon.scheduler.pipeline.runtime
    try:
        configuration = runtime_identity_configuration(runtime.broker, daemon.tenant_id)
        if configuration is None:
            return "runtime_identity_configuration_missing" if daemon.scheduler.pipeline.bind_order_instruments else None
        if daemon.strategy_manifest is None or daemon.strategy_manifest.summary.get("status") != "matched":
            return "pilot_strategy_manifest_unverified"
        oms = runtime.oms
        if not isinstance(oms, DurableOms) or path_digest(oms.path) != configuration["oms_path_sha256"]:
            return "runtime_identity_oms_unavailable"
        orders = oms.all_orders(daemon.tenant_id)
        for order in orders:
            oms.verify(order.client_order_id)
        if oms.open_orders(daemon.tenant_id):
            return "runtime_identity_oms_recovery_required"
        by_fill = {o.broker_order_id: o for o in orders if o.state is OrderState.FILLED}
        # Detect loss of an OMS file even when a newly empty database uses the same path.
        protected = {row[0] for row in runtime.broker._connection.execute(
            "SELECT order_id FROM paper_protective_fill_outbox WHERE tenant_id=?", (daemon.tenant_id,))}
        for entry in runtime.broker.ledger_entries(daemon.tenant_id):
            if entry.order_id in protected:
                continue
            observed = by_fill.get(entry.order_id)
            if (observed is None or observed.filled_quantity != entry.quantity
                    or observed.average_fill_price != entry.fill_price
                    or observed.instrument_identity != entry.instrument_identity
                    or (observed.symbol, observed.market, observed.asset_class, observed.side)
                    != (entry.symbol, entry.market.value, entry.asset_class.value, entry.side.value)):
                return "runtime_identity_oms_recovery_required"
        return None
    except (ValueError, TypeError, AttributeError, KeyError, OSError, sqlite3.Error):
        return "runtime_identity_oms_or_configuration_unavailable"
