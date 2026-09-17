"""Offline synthetic multi-store capture; never restart a daemon or dispatch live orders."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_institutional_paper_coordinator import Harness, make_request, proposal

from quant_ai.agents.swarm import InstrumentBoundTradeProposal
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.governance.runtime_identity import configure_runtime_identity
from quant_ai.operations import recovery_bundle as bundle


def fixture(tmp_path, *, execute=True, accounting_cls=None, protective=False):
    root = tmp_path / "source"
    root.mkdir()
    harness = Harness(root, **({"accounting_cls": accounting_cls} if accounting_cls else {}))
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    configure_runtime_identity(harness.broker, (instrument,), "tenant", "bound_v1",
                               harness.oms.path, oms=harness.oms)
    bound = InstrumentBoundTradeProposal(**vars(proposal()), instrument=instrument)
    request = make_request(harness.broker, p=bound)
    prepared = harness.coordinator.prepare(request)
    assert prepared.approved
    if execute:
        harness.coordinator.execute_due(prepared.program.program_id, now=request.observed_at)
    if protective:
        from decimal import Decimal

        from quant_ai.execution.protective_exits import ProtectiveExitEngine
        assert ProtectiveExitEngine(harness.broker, lambda _: Decimal(90), tenant_id="tenant").evaluate()[0].filled
        assert harness.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
    harness.close()
    with sqlite3.connect(root / "console") as db:
        db.executescript("CREATE TABLE preferences(id INTEGER); CREATE TABLE conversations(id INTEGER); CREATE TABLE audit(id INTEGER);")
    db.close()
    for name in ("proofs", "reviews"):
        (root / name).mkdir()
    (root / "directives").write_text('{"synthetic":true}')
    (root / "halt").write_text("offline test; retain halt")
    spec = {"revision": "a" * 40, "tenant": "tenant", "sources": {
        name: str(root / ("paper.sqlite" if name == "ledger" else name)) for name in bundle.KINDS
    }, "oms": str(root / "oms.sqlite")}
    return spec, prepared.program.program_id


def select(spec):
    root = Path(spec["sources"]["ledger"]).parent
    spec["institutional_state"] = {"accounting": str(root / "accounting.sqlite"),
                                   "programs": str(root / "programs.sqlite")}
    return spec


def test_recorded_institutional_fill_cannot_omit_accounting_and_programs(tmp_path):
    spec, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="Institutional recovery state required"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_complete_multi_store_capture_restores_without_activation(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 4
    capture = manifest["institutionalState"]["verification"]
    assert capture["accounting"]["missingTransactions"] == 0
    assert capture["programs"]["unresolvedSlices"] == 0
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored", result
    assert result["institutionalRecovery"]["status"] == "captured_state_consistent"
    assert result["institutionalRecovery"]["activationAuthorized"] is False
    assert result["institutionalRecovery"]["runtimeContextVerified"] is False
    assert result["haltPresent"] is True
    assert (tmp_path / "restored/accounting").is_file()
    assert (tmp_path / "restored/programs").is_file()


def test_pending_execution_is_preserved_and_restore_remains_discrepant(tmp_path):
    spec, _ = fixture(tmp_path, execute=False)
    select(spec)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "discrepancy"
    assert result["institutionalRecovery"]["programs"]["unresolvedSlices"] == 1
    with sqlite3.connect(tmp_path / "restored/programs") as db:
        assert db.execute("SELECT state FROM execution_program_slices").fetchone()[0] == "PENDING"


def mutate(path, table, sql, args=()):
    with sqlite3.connect(path) as db:
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall():
            assert name.replace("_", "").isalnum()
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute(sql, args)
    db.close()


def create(tmp_path, spec):
    return bundle.create(spec, tmp_path / "backup", writers_stopped=True)


def restore(tmp_path, manifest):
    return bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])


@pytest.mark.parametrize("state", [None, {}, {"accounting": "unused"}, {"programs": "unused"}, {"accounting": "x", "programs": "y", "unknown": "z"}])
def test_partial_or_unknown_institutional_inventory_refuses_before_output(tmp_path, state):
    spec, _ = fixture(tmp_path)
    spec["institutional_state"] = state
    with pytest.raises(ValueError, match="Institutional recovery"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("role", ["accounting", "programs"])
@pytest.mark.parametrize("defect", ["missing", "symlink", "hardlink", "same_file", "memory"])
def test_component_storage_must_be_explicit_existing_and_distinct(tmp_path, role, defect):
    import os
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["institutional_state"][role])
    if defect == "missing":
        spec["institutional_state"][role] = str(tmp_path / "missing")
    elif defect in {"symlink", "hardlink"}:
        alias = tmp_path / "alias"
        if defect == "symlink":
            alias.symlink_to(path)
        else:
            os.link(path, alias)
        spec["institutional_state"][role] = str(alias)
    elif defect == "memory":
        spec["institutional_state"][role] = ":memory:"
    else:
        spec["institutional_state"][role] = spec["oms"]
    with pytest.raises(ValueError, match="Institutional recovery|must not overlap"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("defect", ["balance", "hash", "sequence_gap", "fractional_sequence", "unknown_account", "currency", "base_currency", "orphan", "chart"])
def test_invalid_accounting_cannot_receive_a_verified_capture(tmp_path, defect):
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["institutional_state"]["accounting"])
    table, sql = {
        "balance": ("trading_postings", "UPDATE trading_postings SET amount='101' WHERE id=(SELECT MAX(id) FROM trading_postings)"),
        "hash": ("trading_transactions", "UPDATE trading_transactions SET payload_sha256='wrong' WHERE kind='TRADE'"),
        "sequence_gap": ("trading_postings", "UPDATE trading_postings SET sequence=sequence+10"),
        "fractional_sequence": ("trading_postings", "UPDATE trading_postings SET sequence=sequence+0.5"),
        "unknown_account": ("trading_postings", "UPDATE trading_postings SET account='UNKNOWN'"),
        "currency": ("trading_postings", "UPDATE trading_postings SET currency='USD'"),
        "base_currency": ("trading_journal_meta", "UPDATE trading_journal_meta SET base_currency='USD'"),
        "orphan": ("trading_postings", "UPDATE trading_postings SET transaction_id='missing' WHERE id=(SELECT MAX(id) FROM trading_postings)"),
        "chart": ("trading_accounts", "UPDATE trading_accounts SET account_type='LIABILITY' WHERE code='CASH_AVAILABLE'"),
    }[defect]
    mutate(path, table, sql)
    with pytest.raises(ValueError, match="Institutional recovery"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()


def rehash_transaction(db, tid):
    import hashlib
    row = db.execute("SELECT transaction_id,tenant_id,kind,reference,at FROM trading_transactions WHERE transaction_id=?", (tid,)).fetchone()
    payload = dict(zip(("transactionId", "tenantId", "kind", "reference", "at"), row))
    payload["postings"] = [dict(zip(("sequence", "account", "side", "currency", "amount", "baseAmount"), p))
                           for p in db.execute("SELECT sequence,account,side,currency,amount,base_amount FROM trading_postings WHERE transaction_id=? ORDER BY sequence", (tid,))]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    db.execute("UPDATE trading_transactions SET payload_sha256=? WHERE transaction_id=?", (digest, tid))


def test_balanced_rehashed_trade_still_must_match_actual_ledger_economics(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["institutional_state"]["accounting"])
    mutate(path, "trading_postings", "UPDATE trading_postings SET amount='500',base_amount='500' WHERE transaction_id LIKE 'trade:%'")
    mutate(path, "trading_transactions", "UPDATE trading_transactions SET reference=reference")
    with sqlite3.connect(path) as db:
        tid = db.execute("SELECT transaction_id FROM trading_transactions WHERE kind='TRADE'").fetchone()[0]
        rehash_transaction(db, tid)
    db.close()
    with pytest.raises(ValueError, match="posting economics mismatch"):
        create(tmp_path, spec)


@pytest.mark.parametrize("defect", ["quantity", "fractional_quantity", "sequence", "digest", "parent", "state", "client", "orphan_slice", "deleted_program"])
def test_invalid_execution_schedule_cannot_be_packaged_as_consistent(tmp_path, defect):
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["institutional_state"]["programs"])
    table, sql = {
        "quantity": ("execution_program_slices", "UPDATE execution_program_slices SET quantity=quantity+1"),
        "fractional_quantity": ("execution_program_slices", "UPDATE execution_program_slices SET quantity=quantity+0.5"),
        "sequence": ("execution_program_slices", "UPDATE execution_program_slices SET sequence=sequence+1"),
        "digest": ("execution_programs", "UPDATE execution_programs SET runtime_context_sha256='bad'"),
        "parent": ("execution_programs", "UPDATE execution_programs SET parent_order_payload=NULL"),
        "state": ("execution_programs", "UPDATE execution_programs SET state='PLANNED'"),
        "client": ("execution_program_slices", "UPDATE execution_program_slices SET client_order_id='OMS-wrong'"),
        "orphan_slice": ("execution_program_slices", "UPDATE execution_program_slices SET program_id='PROGRAM-missing'"),
        "deleted_program": ("execution_programs", "DELETE FROM execution_programs"),
    }[defect]
    mutate(path, table, sql)
    with pytest.raises(ValueError, match="Institutional recovery|order_snapshot"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()


def test_rewritten_parent_stop_cannot_change_saved_child_order(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["institutional_state"]["programs"])
    with sqlite3.connect(path) as db:
        raw = json.loads(db.execute("SELECT parent_order_payload FROM execution_programs").fetchone()[0])
    db.close()
    raw["stopPrice"] = "80"
    mutate(path, "execution_programs", "UPDATE execution_programs SET parent_order_payload=?",
           (json.dumps(raw, sort_keys=True, separators=(",", ":")),))
    with pytest.raises(ValueError, match="receipt parent or decision|slice OMS intent"):
        create(tmp_path, spec)


@pytest.mark.parametrize("defect", ["program", "slice", "intent", "prior", "idempotency", "cash_fees"])
def test_receipt_attribution_and_historical_cost_are_rechecked(tmp_path, defect):
    spec, _ = fixture(tmp_path)
    select(spec)
    ledger = Path(spec["sources"]["ledger"])
    with sqlite3.connect(ledger) as db:
        raw = json.loads(db.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
    db.close()
    if defect == "program":
        raw["institutional_program"] = "PROGRAM-wrong"
    elif defect == "slice":
        raw["institutional_slice"] = 2
    elif defect == "intent":
        raw["order_intent_sha256"] = "f" * 64
    elif defect == "prior":
        raw["paper_submission_receipt"]["priorQuantity"] = 10
    elif defect == "idempotency":
        raw["idempotency_key"] = "incorrect"
    else:
        raw["fill"]["cash_fees"] = "17"
    mutate(ledger, "paper_decision_evidence", "UPDATE paper_decision_evidence SET payload=?", (json.dumps(raw),))
    with pytest.raises(ValueError, match="Institutional recovery"):
        create(tmp_path, spec)


def test_unaccounted_fill_is_captured_without_posting_or_resubmitting(tmp_path):
    from test_institutional_paper_coordinator import FailOnceAccounting
    spec, _ = fixture(tmp_path, accounting_cls=FailOnceAccounting)
    select(spec)
    before = {role: Path(path).read_bytes() for role, path in spec["institutional_state"].items()}
    manifest = create(tmp_path, spec)
    result = restore(tmp_path, manifest)
    report = result["institutionalRecovery"]
    assert result["status"] == "discrepancy"
    assert report["accounting"]["missingTransactions"] == 1
    assert report["programs"]["unresolvedSlices"] == 1
    with sqlite3.connect(tmp_path / "restored/programs") as db:
        assert db.execute("SELECT state FROM execution_program_slices").fetchone()[0] == "FILLED_UNACCOUNTED"
    assert before == {role: Path(path).read_bytes() for role, path in spec["institutional_state"].items()}


def test_protective_exit_and_realized_loss_survive_without_reopening_position(tmp_path):
    spec, _ = fixture(tmp_path, protective=True)
    select(spec)
    manifest = create(tmp_path, spec)
    result = restore(tmp_path, manifest)
    assert result["status"] == "restored", result
    assert result["institutionalRecovery"]["accounting"]["missingTransactions"] == 0
    with sqlite3.connect(tmp_path / "restored/ledger") as db:
        assert db.execute("SELECT COUNT(*) FROM paper_positions WHERE tenant_id='tenant'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM paper_ledger WHERE tenant_id='tenant'").fetchone()[0] == 2


def test_other_tenant_journal_rows_do_not_change_selected_tenant_report(tmp_path):
    from decimal import Decimal

    from quant_ai.accounting.journal import TradingJournal
    from quant_ai.accounting.trading import TradingAccounting
    spec, _ = fixture(tmp_path)
    select(spec)
    with TradingJournal(spec["institutional_state"]["accounting"], base_currency="INR") as journal:
        TradingAccounting(journal, "other-tenant").seed_capital("other-capital", currency="INR", amount=Decimal(1234567), base_amount=Decimal(1234567))
    result = restore(tmp_path, create(tmp_path, spec))
    assert result["status"] == "restored"
    assert result["institutionalRecovery"]["accounting"]["transactions"] == 2
    assert "other-tenant" not in json.dumps(result["institutionalRecovery"])


def test_capture_uses_no_writable_engine_or_journal_constructor(tmp_path, monkeypatch):
    from quant_ai.accounting.journal import TradingJournal
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.orders.oms import DurableOms
    spec, _ = fixture(tmp_path)
    select(spec)
    def forbidden(*a, **kw):
        pytest.fail("backup verification must not initialize writable trading components")
    for component in (TradingJournal, PaperBrokerService, ExecutionProgramJournal):
        monkeypatch.setattr(component, "__init__", forbidden)
    monkeypatch.setattr(DurableOms, "_schema", forbidden)
    monkeypatch.setattr(DurableOms, "fill", forbidden)
    result = restore(tmp_path, create(tmp_path, spec))
    assert result["status"] == "restored"


@pytest.mark.parametrize("role", ["accounting", "programs"])
def test_component_changed_during_capture_refuses_and_preserves_source(tmp_path, monkeypatch, role):
    spec, _ = fixture(tmp_path)
    select(spec)
    original = bundle.sqlite_backup
    def changed(source, target):
        original(source, target)
        if target.name == role:
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE synthetic_changed_state(id INTEGER)")
            db.close()
    monkeypatch.setattr(bundle, "sqlite_backup", changed)
    with pytest.raises(ValueError, match="Source changed"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()
    assert Path(spec["institutional_state"][role]).exists()


@pytest.mark.parametrize("defect", ["missing_manifest_role", "unknown_role", "changed_report", "downgrade"])
def test_rehashed_manifest_cannot_hide_components_or_override_verification(tmp_path, defect):
    spec, _ = fixture(tmp_path)
    select(spec)
    manifest = create(tmp_path, spec)
    path = tmp_path / "backup/manifest.json"
    data = json.loads(path.read_text())
    if defect == "missing_manifest_role":
        del data["institutionalState"]["accountingPath"]
    elif defect == "unknown_role":
        data["institutionalState"]["programsPath"] = "other"
    elif defect == "changed_report":
        data["institutionalState"]["verification"]["accounting"]["transactions"] = 999
    else:
        data["schema"] = 3
        del data["institutionalState"]
        for role in ("accounting", "programs"):
            del data["files"][role]
            (tmp_path / "backup" / role).unlink()
    path.write_text(json.dumps(data))
    manifest["manifestSha256"] = bundle.digest(path)
    with pytest.raises(ValueError, match="Institutional recovery"):
        restore(tmp_path, manifest)
    assert not (tmp_path / "restored").exists()


def test_restore_never_overwrites_existing_output_or_changes_source(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    manifest = create(tmp_path, spec)
    before = {role: bundle.digest(Path(path)) for role, path in spec["institutional_state"].items()}
    target = tmp_path / "restored"
    target.mkdir()
    sentinel = target / "user-state"
    sentinel.write_text("preserve")
    with pytest.raises(FileExistsError):
        restore(tmp_path, manifest)
    assert sentinel.read_text() == "preserve"
    assert before == {role: bundle.digest(Path(path)) for role, path in spec["institutional_state"].items()}


@pytest.mark.parametrize("column,value", [("broker_order_id", "made-up-fill"), ("pre_fill_average_price", "100"), ("failure_reason", "contradiction")])
def test_dispatching_slice_cannot_claim_unrecorded_completion_details(tmp_path, column, value):
    from dataclasses import replace

    from quant_ai.orders.intent import order_from_snapshot
    from quant_ai.orders.oms import DurableOms
    spec, _ = fixture(tmp_path, execute=False)
    select(spec)
    path = Path(spec["institutional_state"]["programs"])
    with sqlite3.connect(path) as db:
        parent, decision = db.execute("SELECT parent_order_payload,decision_id FROM execution_programs").fetchone()
        cid = DurableOms.client_order_id(replace(order_from_snapshot(parent), quantity=10), decision + ":slice:1")
        db.execute("UPDATE execution_programs SET state='ACTIVE'")
        db.execute(f"UPDATE execution_program_slices SET state='DISPATCHING',client_order_id=?,{column}=?", (cid, value))
    db.close()
    with pytest.raises(ValueError, match="Institutional recovery dispatch metadata"):
        create(tmp_path, spec)


def test_accounting_dump_is_not_accepted_with_duplicate_receipt_json_keys(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    ledger = Path(spec["sources"]["ledger"])
    with sqlite3.connect(ledger) as db:
        raw = db.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0]
    db.close()
    mutate(ledger, "paper_decision_evidence", "UPDATE paper_decision_evidence SET payload=?", (raw[:-1] + ',"institutional_program":"other"}',))
    with pytest.raises(ValueError, match="duplicate JSON key"):
        create(tmp_path, spec)


def test_cash_and_securities_arithmetic_is_preserved_exactly(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    manifest = create(tmp_path, spec)
    assert restore(tmp_path, manifest)["status"] == "restored"
    with sqlite3.connect(tmp_path / "restored/accounting") as db:
        rows = db.execute("SELECT account,side,amount FROM trading_postings ORDER BY id").fetchall()
    from decimal import Decimal
    balances = {}
    for account, side, amount in rows:
        balances[account] = balances.get(account, Decimal(0)) + (Decimal(amount) if side == "DEBIT" else -Decimal(amount))
    assert balances == {"CASH_AVAILABLE": Decimal(99000), "CAPITAL": Decimal(-100000), "SECURITIES_COST": Decimal(1000)}


def test_missing_recorded_fee_posting_is_preserved_as_discrepancy(tmp_path):
    from decimal import Decimal
    spec, _ = fixture(tmp_path)
    select(spec)
    ledger = Path(spec["sources"]["ledger"])
    with sqlite3.connect(ledger) as db:
        oid, at = db.execute("SELECT order_id,created_at FROM paper_ledger").fetchone()
        proof = json.loads(db.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        db.execute("INSERT INTO paper_cost_ledger(order_id,tenant_id,code,amount,cash_debit,created_at) VALUES (?,?,?,'2',1,?)", (oid, 'tenant', 'BROKERAGE', at))
        cash = Decimal(db.execute("SELECT cash_balance FROM paper_accounts WHERE tenant_id='tenant'").fetchone()[0])
        db.execute("UPDATE paper_accounts SET cash_balance=? WHERE tenant_id='tenant'", (str(cash - 2),))
    db.close()
    proof["fill"]["cash_fees"] = "2"  # Explicit synthetic charge, not a statutory-rate claim.
    mutate(ledger, "paper_decision_evidence", "UPDATE paper_decision_evidence SET payload=?", (json.dumps(proof),))
    result = restore(tmp_path, create(tmp_path, spec))
    assert result["status"] == "discrepancy"
    assert result["institutionalRecovery"]["accounting"]["missingTransactions"] == 1
    assert result["institutionalRecovery"]["activationAuthorized"] is False


def test_unresolved_dispatch_without_a_receipt_is_never_retried(tmp_path, monkeypatch):
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.orders.intent import order_from_snapshot
    from quant_ai.orders.oms import DurableOms
    spec, _ = fixture(tmp_path, execute=False)
    select(spec)
    with sqlite3.connect(spec["institutional_state"]["programs"]) as db:
        parent, decision = db.execute("SELECT parent_order_payload,decision_id FROM execution_programs").fetchone()
        cid = DurableOms.client_order_id(order_from_snapshot(parent), decision + ":slice:1")
        db.execute("UPDATE execution_programs SET state='ACTIVE'")
        db.execute("UPDATE execution_program_slices SET state='DISPATCHING',client_order_id=?", (cid,))
    db.close()
    monkeypatch.setattr(PaperBrokerService, "submit_with_evidence", lambda *a, **k: pytest.fail("must never resubmit"))
    result = restore(tmp_path, create(tmp_path, spec))
    assert result["status"] == "discrepancy"
    assert result["institutionalRecovery"]["programs"]["receipts"] == 0
    with sqlite3.connect(tmp_path / "restored/programs") as db:
        assert db.execute("SELECT state FROM execution_program_slices").fetchone()[0] == "DISPATCHING"


def test_failed_capture_cleans_only_its_new_directory(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path)
    select(spec)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unrelated user file")
    original = bundle.sqlite_backup
    def fail(source, target):
        if target.name == "programs":
            raise RuntimeError("synthetic copy interruption")
        return original(source, target)
    monkeypatch.setattr(bundle, "sqlite_backup", fail)
    with pytest.raises(RuntimeError, match="synthetic copy"):
        create(tmp_path, spec)
    assert not (tmp_path / "backup").exists()
    assert sentinel.read_text() == "unrelated user file"
    assert all(Path(path).is_file() for path in spec["institutional_state"].values())


def test_uncommitted_program_state_is_not_included_in_sqlite_backup(tmp_path):
    spec, _ = fixture(tmp_path)
    select(spec)
    db = sqlite3.connect(spec["institutional_state"]["programs"])
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE execution_programs SET state='FAILED'")
        # The read-only capture sees committed data, not another connection's pending write.
        manifest = create(tmp_path, spec)
        assert restore(tmp_path, manifest)["status"] == "restored"
        with sqlite3.connect(tmp_path / "restored/programs") as restored:
            assert restored.execute("SELECT state FROM execution_programs").fetchone()[0] == "COMPLETE"
    finally:
        db.rollback()
        db.close()
