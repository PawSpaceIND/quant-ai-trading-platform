"""Independent exits must reach accounting without granting it execution authority."""

import json
import sqlite3
from dataclasses import replace

import pytest
from test_institutional_paper_coordinator import NOW, D, Harness, make_request

from quant_ai.execution.protective_exits import ProtectiveExitEngine


def enter_and_protect(h, mark):
    request = make_request(h.broker)
    prepared = h.coordinator.prepare(request)
    assert prepared.approved
    assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
    exits = ProtectiveExitEngine(h.broker, lambda _: D(mark), tenant_id="tenant").evaluate(NOW)
    assert len(exits) == 1 and exits[0].filled
    assert h.broker.get_positions("tenant") == ()
    return request, prepared, exits[0]


@pytest.mark.parametrize("mark,pnl", [("94", "-60"), ("112", "120")])
def test_independent_exit_reaches_double_entry_through_coordinator_recovery(tmp_path, mark, pnl):
    h = Harness(tmp_path)
    try:
        _request, prepared, _exit = enter_and_protect(h, mark)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        before = tuple(h.broker._connection.iterdump())
        result = h.coordinator.reconcile_accounting(prepared.program.program_id)
        assert result.stage.value == "COMPLETE"
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert h.journal.native_balance("tenant", "REALIZED_PNL", "INR") == D(pnl)
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        assert h.journal.verify("tenant")["verified"]
        assert tuple(h.broker._connection.iterdump()) == before
    finally:
        h.close()


def test_missing_only_exit_evidence_is_not_reported_as_no_work(tmp_path):
    h = Harness(tmp_path)
    try:
        _request, prepared, exit_ = enter_and_protect(h, "94")
        with h.broker._connection:
            h.broker._connection.execute("DELETE FROM paper_protection_evidence WHERE order_id=?", (exit_.order_id,))
        before = tuple(h.journal.db.iterdump())
        result = h.coordinator.reconcile_accounting(prepared.program.program_id)
        assert result.stage.value == "RECOVERY_REQUIRED"
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_restart_reconciles_once_and_never_resubmits_exit(tmp_path):
    h = Harness(tmp_path)
    try:
        _request, _prepared, exit_ = enter_and_protect(h, "94")
        exit_id = exit_.order_id
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        source = tuple(h.broker._connection.iterdump())
        first = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert first.status == "matched" and first.posted_order_ids == (exit_id,)
        posted = tuple(h.journal.db.iterdump())
        again = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert again.status == "matched" and again.posted_order_ids == ()
        assert again.source_sha256 == first.source_sha256
        assert tuple(h.journal.db.iterdump()) == posted
        assert tuple(h.broker._connection.iterdump()) == source
    finally:
        h.close()


@pytest.mark.parametrize("verb", ["UPDATE paper_protective_fill_outbox SET receipt_sha256='bad'",
                                  "DELETE FROM paper_protective_fill_outbox"])
def test_exit_outbox_is_append_only(tmp_path, verb):
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            h.broker._connection.execute(verb)
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [("priorAverage", "50"), ("priorQuantity", 1),
                                         ("schema", "invalid"), ("orderIntent", "{}")])
def test_changed_receipt_refuses_without_accounting_mutation(tmp_path, field, value):
    h = Harness(tmp_path)
    try:
        _request, _prepared, exit_ = enter_and_protect(h, "94")
        row = h.broker._connection.execute("SELECT payload FROM paper_protection_evidence WHERE order_id=?", (exit_.order_id,)).fetchone()
        payload = json.loads(row[0])
        payload["paper_submission_receipt"][field] = value
        with h.broker._connection:
            h.broker._connection.execute("UPDATE paper_protection_evidence SET payload=? WHERE order_id=?", (json.dumps(payload), exit_.order_id))
        before = tuple(h.journal.db.iterdump())
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable"
        assert "outbox_hash_mismatch" in report.reason
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_exit_is_not_blocked_by_unavailable_accounting_database(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        h.journal.close()
        exits = ProtectiveExitEngine(h.broker, lambda _: D(94), tenant_id="tenant").evaluate(NOW)
        assert len(exits) == 1 and exits[0].filled
        assert h.broker.get_positions("tenant") == ()
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "unavailable"
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99940)
    finally:
        h.close()


def test_unaccounted_entry_is_not_invented_to_support_exit(tmp_path):
    from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
    h = Harness(tmp_path)
    try:
        h.broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 10, D(100), "outside-coordinator",
                                AssetClass.EQUITY, "tenant", D(95), D(110)))
        exits = ProtectiveExitEngine(h.broker, lambda _: D(94), tenant_id="tenant").evaluate(NOW)
        assert exits[0].filled
        before = tuple(h.journal.db.iterdump())
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable" and "preceding_fill_unaccounted" in report.reason
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_exit_fees_post_atomically_and_retry_without_duplicate_debits(tmp_path, monkeypatch):
    from quant_ai.execution.friction import FrictionCharge
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        original = h.broker.friction_model.evaluate
        def with_synthetic_fee(order, context):
            return replace(original(order, context), charges=(FrictionCharge("BROKERAGE", D(2)), FrictionCharge("GST", D(1))))
        monkeypatch.setattr(h.broker.friction_model, "evaluate", with_synthetic_fee)
        exits = ProtectiveExitEngine(h.broker, lambda _: D(94), tenant_id="tenant").evaluate(NOW)
        assert exits[0].filled
        before = tuple(h.journal.db.iterdump())
        original_post = h.journal.post
        def failing_post(**kwargs):
            result = original_post(**kwargs)
            if kwargs["transaction_id"].endswith(":GST"):
                raise RuntimeError("synthetic failure after nested fee post")
            return result
        with monkeypatch.context() as patch:
            patch.setattr(h.journal, "post", failing_post)
            report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable"
        assert tuple(h.journal.db.iterdump()) == before
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99937)
        assert h.journal.native_balance("tenant", "FEE_EXPENSE", "INR") == D(2)
        assert h.journal.native_balance("tenant", "TAX_EXPENSE", "INR") == D(1)
        assert h.journal.native_balance("tenant", "REALIZED_PNL", "INR") == D(-60)
        final = tuple(h.journal.db.iterdump())
        assert h.coordinator.reconcile_protective_accounting(currency="INR").posted_order_ids == ()
        assert tuple(h.journal.db.iterdump()) == final
    finally:
        h.close()


def test_failed_mirror_blocks_next_coordinator_entry_without_claiming_slice(tmp_path, monkeypatch):
    from test_institutional_paper_coordinator import proposal
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        request = make_request(h.broker, p=replace(proposal(), decision_id="next-decision"))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        def unavailable(**_kwargs):
            raise sqlite3.OperationalError("synthetic accounting unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(h.journal, "post", unavailable)
            result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "RECOVERY_REQUIRED"
        assert result.program.slices[0].state.value == "PENDING"
        assert len(h.broker.ledger_entries("tenant")) == 2
        resumed = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert resumed.stage.value == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 3
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
    finally:
        h.close()


def test_known_futures_exit_posts_only_margin_release_and_realized_pnl(tmp_path):
    from test_bound_margin_integration import NOW as MARGIN_NOW
    from test_bound_margin_integration import opened

    from quant_ai.accounting.journal import TradingJournal
    from quant_ai.accounting.protective import ProtectiveExitAccounting
    from quant_ai.accounting.trading import TradingAccounting
    broker = opened(tmp_path / "future.sqlite")
    with TradingJournal(tmp_path / "accounts.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        try:
            accounting.seed_capital("capital", currency="INR", amount=D(100000), base_amount=D(100000), at=MARGIN_NOW)
            entry = broker.ledger_entries("tenant")[0]
            accounting.reserve_margin(f"margin:{entry.order_id}", currency="INR", amount=D(10000), base_amount=D(10000),
                reference=f"paper fill {entry.order_id} {entry.symbol}", at=entry.created_at)
            broker.margin_source = None
            exits = ProtectiveExitEngine(broker, lambda _: D(4900), tenant_id="tenant").evaluate(MARGIN_NOW)
            assert exits[0].filled
            report = ProtectiveExitAccounting(broker, accounting, currency="INR").reconcile()
            assert report.status == "matched", report.reason
            assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
            assert journal.native_balance("tenant", "CASH_RESERVED_MARGIN", "INR") == 0
            assert journal.native_balance("tenant", "REALIZED_PNL", "INR") == D(-1000)
            assert journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
            assert broker.reconcile("tenant")["status"] == "matched"
        finally:
            broker.close()


def test_other_tenant_protective_history_cannot_be_posted(tmp_path):
    from quant_ai.accounting.protective import ProtectiveExitAccounting
    from quant_ai.accounting.trading import TradingAccounting
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        other = TradingAccounting(h.journal, "another-tenant")
        before = tuple(h.journal.db.iterdump())
        report = ProtectiveExitAccounting(h.broker, other, currency="INR").reconcile()
        assert report.status == "not_required"
        assert tuple(h.journal.db.iterdump()) == before
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
    finally:
        h.close()


def test_abrupt_process_exit_during_mirror_rolls_back_then_recovers(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness
from test_protective_exit_accounting import enter_and_protect
h = Harness(Path(sys.argv[1]))
enter_and_protect(h, "94")
post = h.journal.post
def terminate(**kwargs):
    post(**kwargs)
    os._exit(79)
h.journal.post = terminate
h.coordinator.reconcile_protective_accounting(currency="INR")
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))), "TRADING_LIVE_MONEY_ACTIVE": "false"}
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 79, result.stdout + result.stderr
    h = Harness(tmp_path)
    try:
        assert len(h.broker.ledger_entries("tenant")) == 2
        assert h.broker.get_positions("tenant") == ()
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "matched", report.reason
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99940)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert len(h.broker.ledger_entries("tenant")) == 2
    finally:
        h.close()


@pytest.mark.parametrize("currency", ["USD", "EUR", "", "inr"])
def test_unqualified_currency_translation_never_posts(tmp_path, currency):
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        before = tuple(h.journal.db.iterdump())
        report = h.coordinator.reconcile_protective_accounting(currency=currency)
        assert report.status == "unavailable"
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_legacy_exit_without_durable_outbox_requires_review(tmp_path):
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
    finally:
        h.close()
    # Simulate an old database schema, not an in-place rewrite of approved history.
    db = sqlite3.connect(tmp_path / "paper.sqlite")
    with db:
        db.execute("DROP TABLE paper_protective_fill_outbox")
    db.close()
    h = Harness(tmp_path)
    try:
        before = tuple(h.journal.db.iterdump())
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable" and "outbox_evidence_mismatch" in report.reason
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_existing_wrong_exit_posting_is_not_treated_as_idempotent_success(tmp_path):
    h = Harness(tmp_path)
    try:
        _request, _prepared, exit_ = enter_and_protect(h, "94")
        entry = h.broker.ledger_entries("tenant")[-1]
        h.accounting.sell_security(f"trade:{exit_.order_id}", currency="INR",
            proceeds=D(950), released_cost=D(1000), base_proceeds=D(950), base_released_cost=D(1000),
            reference=f"paper fill {exit_.order_id} INFY", at=entry.created_at)
        before = tuple(h.journal.db.iterdump())
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable" and "recorded_posting_mismatch" in report.reason
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_mirror_serializes_competing_accounting_writers(tmp_path, monkeypatch):
    from quant_ai.accounting.journal import TradingJournal
    from quant_ai.accounting.protective import ProtectiveExitAccounting
    from quant_ai.accounting.trading import TradingAccounting
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        with TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR") as other:
            other.db.execute("PRAGMA busy_timeout=0")
            original = h.journal.post
            calls = []
            def competing(**kwargs):
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.db.execute("BEGIN IMMEDIATE")
                calls.append(kwargs["transaction_id"])
                return original(**kwargs)
            monkeypatch.setattr(h.journal, "post", competing)
            assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
            assert len(calls) == 1
            second = ProtectiveExitAccounting(h.broker, TradingAccounting(other, "tenant"), currency="INR").reconcile()
            assert second.status == "matched" and second.posted_order_ids == ()
    finally:
        h.close()


def test_broker_advance_during_posting_is_pending_not_current_match(tmp_path, monkeypatch):
    from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
    h = Harness(tmp_path)
    try:
        enter_and_protect(h, "94")
        original = h.journal.post
        def broker_advanced(**kwargs):
            result = original(**kwargs)
            h.broker.buy(OrderIntent("TCS", Market.INDIA, Side.BUY, 1, D(100), "concurrent-fixture",
                                    AssetClass.EQUITY, "tenant", D(95), D(110)))
            return result
        monkeypatch.setattr(h.journal, "post", broker_advanced)
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "pending" and report.reason == "paper_ledger_advanced"
    finally:
        h.close()
