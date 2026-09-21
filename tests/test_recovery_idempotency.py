"""Is the recorded-paper reconciliation convergent when repeated under a NEW request id?

The operator's audit already refuses a repeat of the *same* request id. The dashboard's
duplicate guard is per browser tab, so a second tab mints a fresh request id, re-reads the
preview and passes the context check. These tests therefore drive the economic operation a
second (and third) time through that reachable path and count committed state in the stores
themselves -- postings, fills and shared-risk reservations -- rather than trusting a return
value. Everything here is synthetic paper bookkeeping; no order is ever submitted.
"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from test_institutional_bridge_recovery import interrupted_fill
from test_institutional_operator_recovery import CONFIRM, body, route, setup_api
from test_institutional_swarm_bridge import BridgeHarness

from quant_ai.execution.shared_risk import verify_shared_risk

TENANT = "tenant"
# Everything the reconciliation reports that describes the *programme*, as opposed to the
# work this particular call happened to finalize (recovered_sequences).
ECONOMIC_FIELDS = ("program_id", "tenant_id", "program_state", "recovery_stage",
                   "committed_order_ids", "source_revision_sha256", "reason_code",
                   "execution_authorized")


def economics(result):
    return {field: result[field] for field in ECONOMIC_FIELDS}


def _rows(db, sql):
    return tuple(tuple(row) for row in db.execute(sql))


def _count(db, table, tables):
    if table not in tables:
        return 0
    return db.execute("SELECT count(*) FROM " + table).fetchone()[0]


def census(h):
    """Committed state read straight from every store the reconciliation can write to."""
    journal, programs, oms, broker = h.journal.db, h.programs.db, h.oms.db, h.broker._connection
    tables = {row[0] for row in programs.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        "transaction_ids": tuple(row[0] for row in journal.execute(
            "SELECT transaction_id FROM trading_transactions ORDER BY transaction_id")),
        "transactions": journal.execute("SELECT count(*) FROM trading_transactions").fetchone()[0],
        "postings": journal.execute("SELECT count(*) FROM trading_postings").fetchone()[0],
        "posting_rows": _rows(journal, "SELECT transaction_id,sequence,account,side,currency,"
                                       "amount,base_amount FROM trading_postings"
                                       " ORDER BY transaction_id,sequence"),
        "cash": h.journal.native_balance(TENANT, "CASH_AVAILABLE", "INR"),
        "securities_cost": h.journal.native_balance(TENANT, "SECURITIES_COST", "INR"),
        "fills": tuple(sorted(entry.order_id for entry in h.broker.ledger_entries(TENANT))),
        "paper_ledger": broker.execute("SELECT count(*) FROM paper_ledger").fetchone()[0],
        "paper_costs": broker.execute("SELECT count(*) FROM paper_cost_ledger").fetchone()[0],
        "oms_orders": oms.execute("SELECT count(*) FROM oms_orders").fetchone()[0],
        "oms_fills": oms.execute("SELECT count(*) FROM oms_fills").fetchone()[0],
        "reservations": _count(programs, "shared_risk_reservations", tables),
        "releases": _count(programs, "shared_risk_releases", tables),
        "reserved_risk": verify_shared_risk(programs, TENANT),
        "programs": _rows(programs, "SELECT program_id,state FROM execution_programs"
                                    " ORDER BY program_id"),
        "slices": _rows(programs, "SELECT program_id,sequence,state,client_order_id,"
                                  "broker_order_id,pre_fill_average_price"
                                  " FROM execution_program_slices ORDER BY program_id,sequence"),
    }


def assert_single_application(before, after, order_id):
    """Exactly one trade was booked, once, whatever number of applications ran."""
    new_ids = tuple(tid for tid in after["transaction_ids"]
                    if tid not in before["transaction_ids"])
    assert new_ids == (f"trade:{order_id}",), new_ids
    assert len(set(after["transaction_ids"])) == len(after["transaction_ids"])
    assert after["fills"] == before["fills"] == (order_id,)
    assert after["paper_ledger"] == before["paper_ledger"] == 1
    assert after["paper_costs"] == before["paper_costs"]
    assert after["oms_orders"] == 1 and after["oms_fills"] == 1
    assert after["cash"] == Decimal(99000) and after["securities_cost"] == Decimal(1000)
    # Reserved risk is neither duplicated nor released by a bookkeeping reconciliation.
    assert after["reservations"] == before["reservations"] == 1
    assert after["releases"] == before["releases"] == 0
    assert after["reserved_risk"] == before["reserved_risk"]
    assert after["slices"][0][2] == "EXECUTED"


def forbid_submission(h, monkeypatch):
    monkeypatch.setattr(h.broker, "submit_with_evidence",
                        lambda *a, **k: pytest.fail("Recovery must never submit an order"))


def test_repeat_apply_under_a_new_request_id_converges_without_double_bookkeeping(
        tmp_path, monkeypatch):
    """A second browser tab: new request id, fresh preview, context check passes."""
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _credential = setup_api(tmp_path, h)
        forbid_submission(h, monkeypatch)
        order_id = h.broker.ledger_entries(TENANT)[0].order_id
        before = census(h)
        assert before["cash"] == Decimal(100000) and before["slices"][0][2] != "EXECUTED"

        first_preview = client.get(route(pid), headers=headers)
        assert first_preview.status_code == 200, first_preview.text
        first = client.post(route(pid), headers=headers,
                            json=body(first_preview.json()["context_sha256"], "synthetic-tab-1"))
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "RETURNED" and first.json()["replayed"] is False
        after_first = census(h)

        second_preview = client.get(route(pid), headers=headers)
        assert second_preview.status_code == 200, second_preview.text
        # The guard that would stop a repeat is exactly the one that still passes here.
        assert second_preview.json()["context_sha256"] == first_preview.json()["context_sha256"]
        second = client.post(route(pid), headers=headers,
                             json=body(second_preview.json()["context_sha256"], "synthetic-tab-2"))
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "RETURNED"
        # Not the audit replay path: this is a genuinely new application of the operation.
        assert second.json()["replayed"] is False
        after_second = census(h)

        assert after_second == after_first
        assert_single_application(before, after_second, order_id)
        assert economics(second.json()["result"]) == economics(first.json()["result"])
        assert first.json()["result"]["recovered_sequences"] == [1]
        assert second.json()["result"]["recovered_sequences"] == []
        assert first.json()["result"]["program_state"] == "COMPLETE"
        assert h.journal.verify(TENANT)["verified"] is True
        assert len(operations._records()) == 4
    finally:
        if operations:
            operations.close()
        h.close()


def test_concurrent_applies_with_different_request_ids_do_not_double_apply(tmp_path, monkeypatch):
    """Two tabs firing at once; both really contend for runtime._route_lock."""
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        _client, _headers, operations, _state, credential = setup_api(tmp_path, h)
        forbid_submission(h, monkeypatch)
        order_id = h.broker.ledger_entries(TENANT)[0].order_id
        context = h.programs.get(pid).context_sha256
        before = census(h)

        gate = threading.Barrier(2, timeout=30)
        results, failures = {}, {}

        def apply_once(tag):
            try:
                gate.wait()
                results[tag] = operations.apply(
                    pid, request_id=f"synthetic-concurrent-{tag}", context_sha256=context,
                    confirmation=CONFIRM, credential=credential)
            except BaseException as error:  # noqa: BLE001 - reported, never swallowed
                failures[tag] = repr(error)

        threads = [threading.Thread(target=apply_once, args=(tag,)) for tag in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not any(thread.is_alive() for thread in threads), "apply deadlocked"
        assert failures == {}, failures
        assert set(results) == {1, 2}

        after = census(h)
        assert all(item["status"] == "RETURNED" for item in results.values())
        assert all(item["result"]["program_state"] == "COMPLETE" for item in results.values())
        assert all(item["replayed"] is False for item in results.values())
        # Exactly one of the two callers did the finalizing work; the other found nothing to do.
        assert sorted(tuple(item["result"]["recovered_sequences"])
                      for item in results.values()) == [(), (1,)]
        assert economics(results[1]["result"]) == economics(results[2]["result"])
        assert_single_application(before, after, order_id)
        assert h.journal.verify(TENANT)["verified"] is True

        # A further sequential application must leave that same state untouched: a fixed point.
        third = operations.apply(pid, request_id="synthetic-concurrent-3", context_sha256=context,
                                 confirmation=CONFIRM, credential=credential)
        assert third["status"] == "RETURNED"
        assert tuple(third["result"]["recovered_sequences"]) == ()
        assert census(h) == after
        assert len(operations._records()) == 6
    finally:
        if operations:
            operations.close()
        h.close()


def test_restart_then_reapply_with_a_new_request_id_changes_nothing(tmp_path, monkeypatch):
    """Rebuild runtime, operator and audit from the same on-disk state, then reconcile again."""
    first_h = BridgeHarness(tmp_path)
    first_operations = None
    try:
        pid = interrupted_fill(first_h, monkeypatch)
        client, headers, first_operations, _state, _credential = setup_api(tmp_path, first_h)
        forbid_submission(first_h, monkeypatch)
        order_id = first_h.broker.ledger_entries(TENANT)[0].order_id
        before = census(first_h)
        context = first_h.programs.get(pid).context_sha256
        first = client.post(route(pid), headers=headers,
                            json=body(context, "synthetic-before-restart"))
        assert first.status_code == 200, first.text
        assert first.json()["result"]["recovered_sequences"] == [1]
        after_first = census(first_h)
    finally:
        if first_operations:
            first_operations.close()
        first_h.close()

    h = BridgeHarness(tmp_path)
    operations = None
    try:
        client, headers, operations, _state, _credential = setup_api(tmp_path, h)
        monkeypatch.setattr(h.broker, "submit_with_evidence",
                            lambda *a, **k: pytest.fail("Recovery must never submit an order"))
        # The restarted process replays the durable audit chain written before the restart.
        assert len(operations._records()) == 2
        assert census(h) == after_first
        preview = client.get(route(pid), headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["context_sha256"] == context
        second = client.post(route(pid), headers=headers,
                             json=body(context, "synthetic-after-restart"))
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "RETURNED" and second.json()["replayed"] is False
        after_second = census(h)

        assert after_second == after_first
        assert_single_application(before, after_second, order_id)
        assert economics(second.json()["result"]) == economics(first.json()["result"])
        assert second.json()["result"]["recovered_sequences"] == []
        assert h.journal.verify(TENANT)["verified"] is True
        assert len(operations._records()) == 4
    finally:
        if operations:
            operations.close()
        h.close()


def test_repeat_apply_after_a_protective_exit_does_not_remirror_the_exit(tmp_path, monkeypatch):
    """Every application re-runs the protective-exit mirror, so repeat it with an exit present."""
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        reservation = verify_shared_risk(h.programs.db, TENANT)
        exits = ProtectiveExitEngine(h.broker, lambda _: Decimal(90), tenant_id=TENANT).evaluate()
        assert len(exits) == 1 and exits[0].filled
        client, headers, operations, _state, _credential = setup_api(tmp_path, h)
        forbid_submission(h, monkeypatch)
        context = h.programs.get(pid).context_sha256
        first = client.post(route(pid), headers=headers, json=body(context, "synthetic-exit-1"))
        assert first.status_code == 200, first.text
        after_first = census(h)
        second = client.post(route(pid), headers=headers, json=body(context, "synthetic-exit-2"))
        assert second.status_code == 200, second.text
        after_second = census(h)

        assert after_second == after_first
        assert economics(second.json()["result"]) == economics(first.json()["result"])
        assert len(set(after_second["transaction_ids"])) == len(after_second["transaction_ids"])
        assert after_second["fills"] == tuple(sorted(
            entry.order_id for entry in h.broker.ledger_entries(TENANT)))
        assert after_second["paper_ledger"] == 2
        assert after_second["securities_cost"] == 0
        assert after_second["cash"] == Decimal(99900)
        assert after_second["reservations"] == 1 and after_second["releases"] == 0
        assert verify_shared_risk(h.programs.db, TENANT) == reservation
        assert h.journal.verify(TENANT)["verified"] is True
    finally:
        if operations:
            operations.close()
        h.close()
