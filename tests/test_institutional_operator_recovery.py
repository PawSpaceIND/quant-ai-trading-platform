"""Authenticated local paper bookkeeping; all accounts, credentials and fills are synthetic."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_institutional_bridge_recovery import interrupted_fill
from test_institutional_swarm_bridge import BridgeHarness

from quant_ai.api.app import ApiState, create_app
from quant_ai.security.api_keys import ApiKeyRegistry
from quant_ai.security.rate_limit import SlidingWindowRateLimiter
from quant_ai.service.portfolio_service import TenantPortfolioStore
from quant_ai.service.trading_service import TradingService

READ = "institutional.recovery.read"
APPLY = "institutional.recovery.apply"
CONFIRM = "reconcile_recorded_paper_bookkeeping_only"


def setup_api(root, bridge, scopes=(READ, APPLY), *, tenant="tenant"):
    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    keys = ApiKeyRegistry()
    raw, credential = keys.issue(tenant, scopes=scopes)
    operations = InstitutionalRecoveryOperations(bridge.runtime, root / "operator-audit.sqlite")
    state = ApiState(keys, SlidingWindowRateLimiter(100, timedelta(minutes=1)),
                     TradingService(), TenantPortfolioStore(), {"paper_broker"},
                     institutional_recovery={"tenant": operations})
    return TestClient(create_app(state)), {"X-API-Key": raw}, operations, state, credential


def route(pid):
    return f"/v1/institutional/programs/{pid}/recovery"


def body(context, request_id="synthetic-request-1"):
    return {"request_id": request_id, "expected_context_sha256": context,
            "confirmation": CONFIRM}


def test_default_credentials_do_not_gain_recovery_permission():
    keys = ApiKeyRegistry()
    _, credential = keys.issue("tenant")
    assert credential.scopes == frozenset()


def test_authorized_api_recovers_original_fill_and_audits_before_posting(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, credential = setup_api(tmp_path, h)
        before = tuple(h.journal.db.iterdump())
        preview = client.get(route(pid), headers=headers)
        assert preview.status_code == 200, preview.text
        assert tuple(h.journal.db.iterdump()) == before
        assert preview.json()["execution_authorized"] is False
        original = h.runtime.reconcile_program
        def observed(*args, **kwargs):
            assert operations.request_status("synthetic-request-1", credential)["status"] == "REQUESTED"
            return original(*args, **kwargs)
        monkeypatch.setattr(h.runtime, "reconcile_program", observed)
        monkeypatch.setattr(h.broker, "submit_with_evidence", lambda *a, **k: pytest.fail("No order submission"))
        response = client.post(route(pid), headers=headers, json=body(preview.json()["context_sha256"]))
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "RETURNED" and data["result"]["program_state"] == "COMPLETE"
        assert data["result"]["execution_authorized"] is False and data["replayed"] is False
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
        assert len(h.broker.ledger_entries("tenant")) == 1
        again = client.post(route(pid), headers=headers, json=body(preview.json()["context_sha256"]))
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
    finally:
        if operations: operations.close()
        h.close()



def snapshots(h):
    return tuple(tuple(db.iterdump()) for db in (
        h.broker._connection, h.oms.db, h.programs.db, h.journal.db))


@pytest.mark.parametrize("scopes", [(), (READ,)])
def test_ordinary_or_read_only_key_cannot_reconcile(tmp_path, monkeypatch, scopes):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h, scopes)
        before = snapshots(h)
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("Authorization before recovery"))
        response = client.post(route(pid), headers=headers,
                               json=body(h.programs.get(pid).context_sha256))
        assert response.status_code == 403
        assert snapshots(h) == before
        assert operations._records() == []
        read = client.get(route(pid), headers=headers)
        assert read.status_code == (200 if READ in scopes else 403)
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("auth", ["missing", "invalid", "revoked"])
def test_missing_invalid_and_revoked_keys_cannot_reach_recovery(tmp_path, monkeypatch, auth):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, credential = setup_api(tmp_path, h)
        if auth == "missing": headers = {}
        elif auth == "invalid": headers = {"X-API-Key": "synthetic-invalid-key"}
        else: assert state.keys.revoke(credential.key_id)
        before = snapshots(h)
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("No authenticated principal"))
        assert client.post(route(pid), headers=headers, json=body(h.programs.get(pid).context_sha256)).status_code == 401
        assert client.get(route(pid), headers=headers).status_code == 401
        assert client.get("/v1/institutional/recovery-requests/anything", headers=headers).status_code == 401
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("extra", ["tenant_id", "database", "path", "action", "clear_halt", "live", "provider"])
def test_request_cannot_select_another_tenant_file_or_operation(tmp_path, monkeypatch, extra):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        before = snapshots(h)
        payload = {**body(h.programs.get(pid).context_sha256), extra: "forbidden-override"}
        assert client.post(route(pid), headers=headers, json=payload).status_code == 422
        assert client.get(route(pid), headers=headers, params={extra: "override"}).status_code == 400
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("field,value,code", [("confirmation", "yes", 400), ("confirmation", True, 422),
    ("request_id", "", 422), ("request_id", 12, 422), ("request_id", "../../outside", 400),
    ("expected_context_sha256", "a" * 64, 409), ("expected_context_sha256", "g" * 64, 400)])
def test_explicit_confirmation_and_exact_saved_context_are_required(tmp_path, monkeypatch, field, value, code):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        payload = body(h.programs.get(pid).context_sha256)
        payload[field] = value
        before = snapshots(h)
        assert client.post(route(pid), headers=headers, json=payload).status_code == code
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


def test_cross_tenant_key_and_misconfigured_map_do_not_expose_saved_programme(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, _headers, operations, state, _cred = setup_api(tmp_path, h)
        key, _ = state.keys.issue("other", scopes=(READ, APPLY))
        other_headers = {"X-API-Key": key}
        before = snapshots(h)
        assert client.get(route(pid), headers=other_headers).status_code == 503
        state.institutional_recovery["other"] = operations
        response = client.post(route(pid), headers=other_headers,
                               json=body(h.programs.get(pid).context_sha256))
        assert response.status_code == 503
        assert pid not in response.text and str(tmp_path) not in response.text
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


def test_unknown_programme_and_request_return_no_foreign_metadata(tmp_path):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        before = snapshots(h)
        for url in (route("foreign-programme"), "/v1/institutional/recovery-requests/foreign-request"):
            response = client.get(url, headers=headers)
            assert response.status_code == 404
            assert "foreign-" not in response.text
        assert snapshots(h) == before
    finally:
        if operations: operations.close()
        h.close()


def test_api_missing_receipt_remains_unresolved_and_replay_does_not_repeat_recovery(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        count = []
        def preflight(_):
            count.append(1)
            return None if len(count) == 1 else "synthetic final hold"
        h.runtime.pre_submit_check = preflight
        assert h.execute().fill is None
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        payload = body(h.programs.get(pid).context_sha256)
        response = client.post(route(pid), headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json()["result"]["recovery_stage"] == "RECOVERY_REQUIRED"
        assert response.json()["result"]["reason_code"] == "review_required"
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("No repeat on HTTP retry"))
        again = client.post(route(pid), headers=headers, json=payload)
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert h.programs.recovery_required("tenant") and h.broker.ledger_entries("tenant") == ()
    finally:
        if operations: operations.close()
        h.close()


def test_same_request_id_cannot_be_reused_by_another_credential_or_context(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, _cred = setup_api(tmp_path, h)
        payload = body(h.programs.get(pid).context_sha256)
        assert client.post(route(pid), headers=headers, json=payload).status_code == 200
        other, _ = state.keys.issue("tenant", scopes=(READ, APPLY))
        assert client.post(route(pid), headers={"X-API-Key": other}, json=payload).status_code == 409
        altered = {**payload, "expected_context_sha256": "a" * 64}
        assert client.post(route(pid), headers=headers, json=altered).status_code == 409
        assert len(operations._records()) == 2 and len(h.broker.ledger_entries("tenant")) == 1
    finally:
        if operations: operations.close()
        h.close()


def test_raw_keys_file_paths_and_provider_errors_do_not_enter_responses_or_audit(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        secret = headers["X-API-Key"]
        def fail(*args, **kwargs):
            raise OSError(f"private-path={tmp_path} secret={secret} SELECT * FROM hidden")
        monkeypatch.setattr(h.runtime, "reconcile_program", fail)
        payload = body(h.programs.get(pid).context_sha256)
        response = client.post(route(pid), headers=headers, json=payload)
        assert response.status_code == 409
        recorded = client.get("/v1/institutional/recovery-requests/synthetic-request-1", headers=headers)
        assert recorded.status_code == 200 and recorded.json()["status"] == "FAILED"
        raw = response.text + recorded.text + str(operations._records())
        assert secret not in raw and str(tmp_path) not in raw and "SELECT" not in raw
        assert client.post(route(pid), headers=headers, json=payload).status_code == 409
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(100000)
    finally:
        if operations: operations.close()
        h.close()


def test_request_fails_before_source_changes_when_audit_is_read_only(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        operations.db.execute("PRAGMA query_only=ON")
        before = snapshots(h)
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("Audit before side effect"))
        response = client.post(route(pid), headers=headers, json=body(h.programs.get(pid).context_sha256))
        assert response.status_code == 503
        assert snapshots(h) == before
    finally:
        if operations: operations.close()
        h.close()


def test_outcome_write_failure_keeps_request_unknown_and_requires_a_new_review(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, credential = setup_api(tmp_path, h)
        finish = operations._finish
        def fail(*args, **kwargs):
            raise OSError("synthetic audit completion failure")
        monkeypatch.setattr(operations, "_finish", fail)
        payload = body(h.programs.get(pid).context_sha256)
        assert client.post(route(pid), headers=headers, json=payload).status_code == 503
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
        assert operations.request_status(payload["request_id"], credential)["status"] == "REQUESTED"
        monkeypatch.setattr(operations, "_finish", finish)
        assert client.post(route(pid), headers=headers, json=payload).status_code == 409
        reviewed = client.post(route(pid), headers=headers,
                               json=body(payload["expected_context_sha256"], "new-reviewed-request"))
        assert reviewed.status_code == 200
        assert reviewed.json()["result"]["recovered_sequences"] == []
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        if operations: operations.close()
        h.close()



@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_recorded_recovery_audit_is_append_only(tmp_path, monkeypatch, verb):
    import sqlite3
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        assert client.post(route(pid), headers=headers,
                           json=body(h.programs.get(pid).context_sha256)).status_code == 200
        sql = ("UPDATE institutional_operator_events SET sha256='changed'" if verb == "UPDATE"
               else "DELETE FROM institutional_operator_events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            operations.db.execute(sql)
        operations.db.rollback()
        assert len(operations._records()) == 2
    finally:
        if operations: operations.close()
        h.close()


def test_corrupt_audit_blocks_subsequent_recovery_without_exposing_raw_payload(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        payload = body(h.programs.get(pid).context_sha256)
        assert client.post(route(pid), headers=headers, json=payload).status_code == 200
        operations.db.execute("DROP TRIGGER institutional_operator_events_update_blocked")
        operations.db.execute("UPDATE institutional_operator_events SET sha256='corrupt' WHERE sequence=2")
        operations.db.commit()
        before = snapshots(h)
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("Corrupt audit cannot authorize"))
        response = client.post(route(pid), headers=headers,
                               json=body(payload["expected_context_sha256"], "next"))
        assert response.status_code == 503 and str(tmp_path) not in response.text
        assert snapshots(h) == before
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("source", ["ledger", "oms", "programs", "accounting", "symlink", "memory"])
def test_audit_cannot_alias_selected_trading_stores(tmp_path, source):
    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    h = BridgeHarness(tmp_path)
    try:
        before = snapshots(h)
        ledger = next(row[2] for row in h.broker._connection.execute("PRAGMA database_list") if row[1] == "main")
        alias = tmp_path / "alias"
        alias.symlink_to(h.journal.path)
        path = {"ledger": ledger, "oms": h.oms.path, "programs": h.programs.path,
                "accounting": h.journal.path, "symlink": alias, "memory": ":memory:"}[source]
        with pytest.raises(ValueError, match="institutional_recovery_"):
            InstitutionalRecoveryOperations(h.runtime, path)
        assert snapshots(h) == before
    finally:
        h.close()


def test_concurrent_http_retries_produce_one_reconciliation_and_one_audit_pair(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        original = h.runtime.reconcile_program
        calls = []
        def count(*a, **k):
            calls.append(1)
            return original(*a, **k)
        monkeypatch.setattr(h.runtime, "reconcile_program", count)
        payload = body(h.programs.get(pid).context_sha256)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(route(pid), headers=headers, json=payload), range(2)))
        assert [r.status_code for r in responses] == [200, 200]
        assert sorted(r.json()["replayed"] for r in responses) == [False, True]
        assert calls == [1] and len(operations._records()) == 2
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        if operations: operations.close()
        h.close()


def test_reopen_preserves_idempotency_and_does_not_claim_a_new_recovery(tmp_path, monkeypatch):
    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, _cred = setup_api(tmp_path, h)
        payload = body(h.programs.get(pid).context_sha256)
        first = client.post(route(pid), headers=headers, json=payload).json()
        operations.close()
        operations = InstitutionalRecoveryOperations(h.runtime, tmp_path / "operator-audit.sqlite")
        state.institutional_recovery["tenant"] = operations
        monkeypatch.setattr(h.runtime, "reconcile_program", lambda *a, **k: pytest.fail("Already returned"))
        replay = client.post(route(pid), headers=headers, json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["recorded_at"] == first["recorded_at"]
        assert replay.json()["result"] == first["result"]
    finally:
        if operations: operations.close()
        h.close()


def test_independent_exit_stays_closed_and_halt_and_risk_reservations_survive_api_recovery(tmp_path, monkeypatch):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    from quant_ai.execution.shared_risk import verify_shared_risk
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        risk = verify_shared_risk(h.programs.db, "tenant")
        h.runtime.kill_switch.engage("operator entry hold")
        exits = ProtectiveExitEngine(h.broker, lambda _: Decimal(90), tenant_id="tenant").evaluate()
        assert len(exits) == 1 and exits[0].filled
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        response = client.post(route(pid), headers=headers,
                               json=body(h.programs.get(pid).context_sha256))
        assert response.status_code == 200
        assert h.broker.get_positions("tenant") == () and len(h.broker.ledger_entries("tenant")) == 2
        assert h.runtime.kill_switch.engaged and h.runtime.kill_switch.reason == "operator entry hold"
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert verify_shared_risk(h.programs.db, "tenant") == risk
    finally:
        if operations: operations.close()
        h.close()


def test_rate_limit_and_no_store_headers_apply_to_recovery_routes(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, _cred = setup_api(tmp_path, h)
        assert client.get(route(pid)).headers["Cache-Control"] == "no-store"
        state.rate_limiter = SlidingWindowRateLimiter(1, timedelta(minutes=1))
        assert client.get(route(pid), headers=headers).status_code == 200
        response = client.post(route(pid), headers=headers,
                               json=body(h.programs.get(pid).context_sha256))
        assert response.status_code == 429 and response.headers["Cache-Control"] == "no-store"
        assert operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("scopes", ["institutional.recovery.apply", ("admin",), ("*",), (True,), None])
def test_credential_issuance_does_not_infer_or_expand_unknown_scopes(scopes):
    with pytest.raises(ValueError, match="invalid_api_key_scopes"):
        ApiKeyRegistry().issue("tenant", scopes=scopes)


def test_audit_capacity_exhaustion_blocks_before_bookkeeping(tmp_path, monkeypatch):
    from quant_ai.operations import institutional_operator
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        monkeypatch.setattr(institutional_operator, "MAX_EVENTS", 1)
        before = snapshots(h)
        assert client.post(route(pid), headers=headers,
                           json=body(h.programs.get(pid).context_sha256)).status_code == 503
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


@pytest.mark.parametrize("boundary", ["after_intent", "inside_posting", "before_outcome"])
def test_process_death_preserves_audit_intent_without_http_retry_of_unknown_outcome(tmp_path, monkeypatch, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    h = BridgeHarness(tmp_path)
    pid = interrupted_fill(h, monkeypatch)
    client, headers, operations, state, credential = setup_api(tmp_path, h)
    context = h.programs.get(pid).context_sha256
    operations.close()
    h.close()
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_swarm_bridge import BridgeHarness
from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations, READ, APPLY, CONFIRM
from quant_ai.security.api_keys import ApiCredential
root, pid, context, actor, boundary = Path(sys.argv[1]), *sys.argv[2:]
h = BridgeHarness(root)
ops = InstitutionalRecoveryOperations(h.runtime, root / 'operator-audit.sqlite')
credential = ApiCredential('tenant', actor, 'synthetic-internal-principal', frozenset({READ,APPLY}))
if boundary == 'after_intent':
    h.runtime.reconcile_program = lambda *a, **k: os._exit(73)
elif boundary == 'inside_posting':
    original = h.accounting.buy_security
    def interrupted(*a, **k):
        original(*a, **k)
        os._exit(73)
    h.accounting.buy_security = interrupted
else:
    ops._finish = lambda *a, **k: os._exit(73)
ops.apply(pid, request_id='synthetic-request-1', context_sha256=context, confirmation=CONFIRM, credential=credential)
raise AssertionError('Did not reach the intended crash boundary')
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), pid, context, credential.key_id, boundary],
                           capture_output=True, text=True, cwd=root, env=env, timeout=30, check=False)
    assert child.returncode == 73, child.stdout + child.stderr
    fresh = BridgeHarness(tmp_path)
    operations = InstitutionalRecoveryOperations(fresh.runtime, tmp_path / "operator-audit.sqlite")
    state.institutional_recovery["tenant"] = operations
    try:
        assert operations.request_status("synthetic-request-1", credential)["status"] == "REQUESTED"
        before = snapshots(fresh)
        assert client.post(route(pid), headers=headers, json=body(context)).status_code == 409
        assert snapshots(fresh) == before
        response = client.post(route(pid), headers=headers, json=body(context, "new-review"))
        assert response.status_code == 200 and response.json()["result"]["program_state"] == "COMPLETE"
        assert len(fresh.broker.ledger_entries("tenant")) == 1
        assert fresh.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
    finally:
        operations.close()
        fresh.close()



def test_revoke_while_request_waits_for_runtime_lock_prevents_audit_and_reconciliation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, credential = setup_api(tmp_path, h)
        seen = Event()
        original = state.keys.authenticate
        def authenticated(raw):
            result = original(raw)
            seen.set()
            return result
        monkeypatch.setattr(state.keys, "authenticate", authenticated)
        before = snapshots(h)
        payload = body(h.programs.get(pid).context_sha256)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with h.runtime._route_lock:
                future = pool.submit(client.post, route(pid), headers=headers, json=payload)
                assert seen.wait(5), "HTTP request never authenticated"
                assert state.keys.revoke(credential.key_id)
            response = future.result(timeout=10)
        assert response.status_code == 401
        assert snapshots(h) == before and operations._records() == []
    finally:
        if operations: operations.close()
        h.close()


def test_recovery_validation_errors_do_not_echo_a_supplied_secret(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _state, _cred = setup_api(tmp_path, h)
        secret = headers["X-API-Key"]
        response = client.post(route(pid), headers=headers,
            json={**body(h.programs.get(pid).context_sha256), "database": secret})
        assert response.status_code == 422
        assert secret not in response.text
        assert operations._records() == []
    finally:
        if operations: operations.close()
        h.close()
