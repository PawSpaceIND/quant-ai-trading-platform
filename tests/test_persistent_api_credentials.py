"""Disposable possession keys and local stores; no deployed credential actions."""
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 9, 18, 7, tzinfo=timezone.utc)
READ = "institutional.recovery.read"
APPLY = "institutional.recovery.apply"


def registry(path, *, create=True, clock=lambda: NOW):
    from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry
    return PersistentApiKeyRegistry(path, create=create, clock=clock)


def test_key_and_explicit_permissions_survive_reopen_without_storing_secret(tmp_path):
    path = tmp_path / "operator-credentials.sqlite"
    store = registry(path)
    raw, principal = store.issue("tenant", scopes=(READ, APPLY), expires_at=NOW + timedelta(hours=1))
    assert store.authenticate(raw) == principal
    store.close()
    with registry(path, create=False) as reopened:
        assert reopened.authenticate(raw) == principal
        assert reopened.is_active(principal)
        assert raw not in "\n".join(reopened.db.iterdump())
    assert raw.encode() not in path.read_bytes()


def test_revocation_is_visible_to_other_connection_and_survives_restart(tmp_path):
    path = tmp_path / "operator-credentials.sqlite"
    with registry(path) as first, registry(path, create=False) as second:
        raw, principal = first.issue("tenant", scopes=(READ,), expires_at=NOW + timedelta(hours=1))
        assert second.authenticate(raw) == principal
        assert first.revoke(principal.key_id)
        assert second.authenticate(raw) is None
        assert not second.is_active(principal)
    with registry(path, create=False) as third:
        assert third.authenticate(raw) is None
        assert not third.revoke(principal.key_id)


def test_expiry_and_atomic_rotation_preserve_selected_permissions(tmp_path):
    now = [NOW]
    with registry(tmp_path / "credentials.sqlite", clock=lambda: now[0]) as store:
        old, before = store.issue("tenant", scopes=(READ,), expires_at=NOW + timedelta(minutes=10))
        new, after = store.rotate(before.key_id, expires_at=NOW + timedelta(hours=1))
        assert before.key_id != after.key_id
        assert after.tenant_id == before.tenant_id and after.scopes == before.scopes
        assert store.authenticate(old) is None and not store.is_active(before)
        assert store.authenticate(new) == after
        now[0] = NOW + timedelta(hours=1)
        assert store.authenticate(new) is None and not store.is_active(after)


@pytest.mark.parametrize("expiry", [None, NOW, NOW-timedelta(seconds=1), NOW+timedelta(days=366),
                                  NOW.replace(tzinfo=None), "tomorrow", True])
def test_invalid_expiry_never_creates_a_grant(tmp_path, expiry):
    with registry(tmp_path / "keys.sqlite") as store:
        before = tuple(store.db.iterdump())
        with pytest.raises(ValueError):
            store.issue("tenant", expires_at=expiry)
        assert tuple(store.db.iterdump()) == before


@pytest.mark.parametrize("scopes", [("admin",), ("*",), ("institutional.recovery",), READ, [True], None])
def test_unknown_permission_is_not_inferred(tmp_path, scopes):
    with registry(tmp_path / "keys.sqlite") as store:
        with pytest.raises(ValueError, match="invalid_api_key_scopes"):
            store.issue("tenant", scopes=scopes, expires_at=NOW+timedelta(hours=1))
        assert store.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0] == 0


@pytest.mark.parametrize("tenant", ["", " ", " tenant", "tenant/other", None, True])
def test_invalid_tenant_is_never_stored(tmp_path, tenant):
    with registry(tmp_path / "keys.sqlite") as store, pytest.raises(ValueError, match="credential_tenant_invalid"):
        store.issue(tenant, expires_at=NOW+timedelta(hours=1))


def test_no_implicit_permissions_and_expiry_at_exact_boundary(tmp_path):
    now = [NOW]
    with registry(tmp_path / "keys.sqlite", clock=lambda:now[0]) as store:
        raw, principal = store.issue("tenant", expires_at=NOW+timedelta(seconds=1))
        assert principal.scopes == frozenset()
        now[0] += timedelta(microseconds=999999)
        assert store.authenticate(raw) == principal
        now[0] += timedelta(microseconds=1)
        assert store.authenticate(raw) is None


def test_clock_rollback_cannot_revive_an_expired_key_across_restart(tmp_path):
    path=tmp_path / "keys.sqlite";now=[NOW]
    with registry(path, clock=lambda:now[0]) as store:
        raw, principal=store.issue("tenant", expires_at=NOW+timedelta(minutes=1))
        now[0]+=timedelta(minutes=2)
        assert store.authenticate(raw) is None
        now[0]=NOW
        assert store.authenticate(raw) is None and not store.is_active(principal)
        with pytest.raises(ValueError, match="credential_clock_regressed"):
            store.issue("tenant", expires_at=NOW+timedelta(hours=1))
    with pytest.raises(ValueError, match="credential_store_unavailable"):
        registry(path, create=False, clock=lambda:NOW)
    with registry(path, create=False, clock=lambda:NOW+timedelta(minutes=3)) as restored:
        assert restored.authenticate(raw) is None


@pytest.mark.parametrize("value", [None, True, 123, "", "x"*10000, b"not-a-string"])
def test_malformed_token_is_anonymous(tmp_path, value):
    with registry(tmp_path / "keys.sqlite") as store:
        assert store.authenticate(value) is None
        assert not store.is_active(value)


def test_create_is_exclusive_and_missing_reopen_does_not_create_a_file(tmp_path):
    path=tmp_path / "keys.sqlite"
    with pytest.raises(ValueError, match="credential_store_unavailable"):
        registry(path,create=False)
    assert not path.exists()
    with registry(path) as store:
        raw,_=store.issue("tenant",expires_at=NOW+timedelta(hours=1))
        before=path.read_bytes()
        with pytest.raises(ValueError, match="credential_store_unavailable"):
            registry(path)
        assert path.read_bytes()==before
        assert store.authenticate(raw) is not None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public_permissions", "unrelated_database"])
def test_unsafe_or_unrelated_store_is_not_adopted(tmp_path,kind):
    import os
    import sqlite3
    path=tmp_path / "keys.sqlite";alias=tmp_path / "alias.sqlite"
    if kind == "unrelated_database":
        with sqlite3.connect(alias) as db:db.execute("CREATE TABLE unrelated(value TEXT)")
        alias.chmod(0o600)
    else:
        registry(path).close()
        if kind=="symlink":alias.symlink_to(path)
        elif kind=="hardlink":os.link(path,alias)
        else:
            path.rename(alias);alias.chmod(0o644)
    before=alias.read_bytes()
    with pytest.raises(ValueError, match="credential_store_unavailable"):
        registry(alias,create=False)
    assert alias.read_bytes()==before


@pytest.mark.parametrize("change", ["replace", "remove", "close", "public_permissions"])
def test_store_loss_or_change_never_uses_cached_credentials(tmp_path,change):
    path=tmp_path / "keys.sqlite"
    store=registry(path)
    try:
        raw,principal=store.issue("tenant",expires_at=NOW+timedelta(hours=1))
        if change=="replace":
            path.rename(tmp_path / "old.sqlite")
            registry(path).close()
        elif change=="remove":path.unlink()
        elif change=="close":store.close()
        else:path.chmod(0o644)
        assert store.authenticate(raw) is None and not store.is_active(principal)
        with pytest.raises(ValueError):store.issue("tenant",expires_at=NOW+timedelta(hours=1))
    finally:store.close()


def test_immutable_grant_and_irreversible_revocation(tmp_path):
    import sqlite3
    with registry(tmp_path / "keys.sqlite") as store:
        _,principal=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE operator_credential_grants SET payload='{}'")
        store.db.rollback()
        assert store.revoke(principal.key_id)
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE operator_credential_grants SET revoked_at=NULL")
        store.db.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("DELETE FROM operator_credential_grants")
        store.db.rollback()


@pytest.mark.parametrize("field,value", [("scopes", ["admin"]), ("expiresAt", "2999-01-01T00:00:00+00:00"),
    ("tenant", ""), ("keyHash", "f"*64), ("issuedAt", "2026-09-18T07:00:00"), ("extra", True)])
def test_rehashed_malformed_grant_cannot_authenticate(tmp_path,field,value):
    import json
    from hashlib import sha256
    with registry(tmp_path / "keys.sqlite") as store:
        raw,principal=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        row=store.db.execute("SELECT payload FROM operator_credential_grants").fetchone()[0]
        payload=json.loads(row);payload[field]=value
        encoded=json.dumps(payload,sort_keys=True,separators=(",", ":"))
        store.db.execute("DROP TRIGGER credential_grant_immutable")
        store.db.execute("UPDATE operator_credential_grants SET payload=?,payload_sha256=?",
                         (encoded,sha256(encoded.encode()).hexdigest()))
        store.db.commit()
        assert store.authenticate(raw) is None and not store.is_active(principal)


def test_rotation_failure_rolls_back_new_grant_and_retains_original(tmp_path):
    with registry(tmp_path / "keys.sqlite") as store:
        raw,principal=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        store.db.execute("""CREATE TRIGGER synthetic_revoke_failure BEFORE UPDATE OF revoked_at
            ON operator_credential_grants BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        before=tuple(store.db.iterdump())
        with pytest.raises(ValueError,match="credential_store_unavailable"):
            store.rotate(principal.key_id,expires_at=NOW+timedelta(hours=2))
        assert tuple(store.db.iterdump())==before
        assert store.authenticate(raw)==principal


def test_racing_rotations_of_one_key_create_only_one_successor(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path=tmp_path / "keys.sqlite"
    with registry(path) as a,registry(path,create=False) as b:
        old,principal=a.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        barrier=Barrier(2)
        def rotate(store):
            barrier.wait()
            try:return store.rotate(principal.key_id,expires_at=NOW+timedelta(hours=2))
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(rotate,(a,b)))
        winners=[r for r in results if r is not None]
        assert len(winners)==1
        assert a.authenticate(old) is None
        assert b.authenticate(winners[0][0])==winners[0][1]
        assert a.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0]==2


def test_capacity_exhaustion_cannot_revoke_rotation_source(tmp_path,monkeypatch):
    from quant_ai.security import persistent_api_keys as module
    with registry(tmp_path / "keys.sqlite") as store:
        monkeypatch.setattr(module,"MAX_GRANTS",1)
        raw,principal=store.issue("tenant",expires_at=NOW+timedelta(hours=1))
        with pytest.raises(ValueError,match="credential_capacity_exhausted"):
            store.rotate(principal.key_id,expires_at=NOW+timedelta(hours=2))
        assert store.authenticate(raw)==principal



def test_real_recovery_api_uses_reopened_registry_and_preserves_audit_actor(tmp_path, monkeypatch):
    from test_institutional_bridge_recovery import interrupted_fill
    from test_institutional_operator_recovery import body, route, setup_api
    from test_institutional_swarm_bridge import BridgeHarness
    h=BridgeHarness(tmp_path);operations=None;active=None
    try:
        pid=interrupted_fill(h,monkeypatch)
        client,_headers,operations,state,_old=setup_api(tmp_path,h)
        path=tmp_path / "keys.sqlite"
        with registry(path) as first:
            secret,principal=first.issue("tenant",scopes=(READ,APPLY),expires_at=NOW+timedelta(hours=1))
        active=registry(path,create=False);state.keys=active
        headers={"X-API-Key":secret}
        preview=client.get(route(pid),headers=headers)
        assert preview.status_code==200
        reply=client.post(route(pid),headers=headers,json=body(preview.json()["context_sha256"]))
        assert reply.status_code==200 and reply.json()["status"]=="RETURNED"
        assert reply.json()["actor_key_id"]==principal.key_id
        assert reply.json()["result"]["execution_authorized"] is False
        assert len(h.broker.ledger_entries("tenant"))==1
        audit="\n".join(operations.db.iterdump())
        assert secret not in audit and secret not in reply.text and str(path) not in reply.text
        successor,_=active.rotate(principal.key_id,expires_at=NOW+timedelta(hours=2))
        assert client.get(route(pid),headers=headers).status_code==401
        assert client.get(route(pid),headers={"X-API-Key":successor}).status_code==200
    finally:
        if active:active.close()
        if operations:operations.close()
        h.close()


@pytest.mark.parametrize("scopes,tenant,status", [((),"tenant",403),((READ,),"tenant",403),((READ,APPLY),"other",503)])
def test_durable_registry_does_not_weaken_recovery_permission_or_tenant_boundaries(tmp_path,monkeypatch,scopes,tenant,status):
    from test_institutional_bridge_recovery import interrupted_fill
    from test_institutional_operator_recovery import body, route, setup_api, snapshots
    from test_institutional_swarm_bridge import BridgeHarness
    h=BridgeHarness(tmp_path);operations=None
    try:
        pid=interrupted_fill(h,monkeypatch)
        client,_headers,operations,state,_old=setup_api(tmp_path,h)
        with registry(tmp_path / "keys.sqlite") as store:
            state.keys=store
            secret,_=store.issue(tenant,scopes=scopes,expires_at=NOW+timedelta(hours=1))
            before=snapshots(h)
            reply=client.post(route(pid),headers={"X-API-Key":secret},json=body(h.programs.get(pid).context_sha256))
            assert reply.status_code==status
            assert snapshots(h)==before and operations._records()==[]
    finally:
        if operations:operations.close()
        h.close()


@pytest.mark.parametrize("change", ["revoke", "expire", "rotate"])
def test_queued_http_request_rechecks_durable_authority_after_runtime_lock(tmp_path,monkeypatch,change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_institutional_bridge_recovery import interrupted_fill
    from test_institutional_operator_recovery import body, route, setup_api, snapshots
    from test_institutional_swarm_bridge import BridgeHarness
    h=BridgeHarness(tmp_path);operations=None;now=[NOW]
    try:
        pid=interrupted_fill(h,monkeypatch)
        client,_headers,operations,state,_old=setup_api(tmp_path,h)
        path=tmp_path / "keys.sqlite"
        with registry(path,clock=lambda:now[0]) as a,registry(path,create=False,clock=lambda:now[0]) as b:
            state.keys=a
            secret,principal=a.issue("tenant",scopes=(READ,APPLY),expires_at=NOW+timedelta(minutes=1))
            authenticated=Event();original=a.authenticate
            def observed(raw):
                result=original(raw)
                authenticated.set()
                return result
            monkeypatch.setattr(a,"authenticate",observed)
            before=snapshots(h)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with h.runtime._route_lock:
                    pending=pool.submit(client.post,route(pid),headers={"X-API-Key":secret},
                        json=body(h.programs.get(pid).context_sha256))
                    assert authenticated.wait(5)
                    if change=="revoke":assert b.revoke(principal.key_id)
                    elif change=="rotate":b.rotate(principal.key_id,expires_at=NOW+timedelta(hours=1))
                    else:now[0]+=timedelta(minutes=2)
                reply=pending.result(timeout=10)
            assert reply.status_code==401
            assert reply.headers["Cache-Control"]=="no-store"
            assert snapshots(h)==before and operations._records()==[]
    finally:
        if operations:operations.close()
        h.close()


def test_copied_principal_cannot_add_permissions_or_change_tenant(tmp_path):
    from dataclasses import replace
    with registry(tmp_path / "keys.sqlite") as store:
        raw,principal=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        assert not store.is_active(replace(principal,scopes=frozenset({READ,APPLY})))
        assert not store.is_active(replace(principal,tenant_id="other"))
        assert store.authenticate(raw)==principal


@pytest.mark.parametrize("boundary", ["before_rotation_commit", "after_rotation_commit", "after_revocation_commit"])
def test_process_death_never_restores_a_committed_revocation(tmp_path,boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path
    path=tmp_path / "keys.sqlite"
    with registry(path) as store:
        secret,principal=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
    code=r"""
import os, sys
from datetime import datetime, timedelta, timezone
from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry
now=datetime(2026,9,18,7,tzinfo=timezone.utc)
path,key_id,boundary=sys.argv[1:]
store=PersistentApiKeyRegistry(path,clock=lambda:now)
if boundary=='before_rotation_commit':
    insert=store._insert
    def interrupted(*args,**kwargs):
        insert(*args,**kwargs)
        os._exit(73)
    store._insert=interrupted
if boundary=='after_revocation_commit':store.revoke(key_id)
else:store.rotate(key_id,expires_at=now+timedelta(hours=2))
os._exit(73)
"""
    root=Path(__file__).resolve().parents[1]
    run=subprocess.run([sys.executable,"-c",code,str(path),principal.key_id,boundary],
        env={**os.environ,"PYTHONPATH":str(root / "src"),"TRADING_LIVE_MONEY_ACTIVE":"false"},
        text=True,capture_output=True,timeout=30,check=False)
    assert run.returncode==73, "synthetic crash point was not reached"
    with registry(path,create=False) as store:
        assert (store.authenticate(secret)==principal) is (boundary=="before_rotation_commit")
        count=store.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0]
        assert count==(2 if boundary=="after_rotation_commit" else 1)
        assert secret not in "\n".join(store.db.iterdump())


def test_unavailable_store_is_denied_by_http_without_exposing_storage_details(tmp_path,monkeypatch):
    from test_institutional_operator_recovery import setup_api
    from test_institutional_swarm_bridge import BridgeHarness
    h=BridgeHarness(tmp_path);operations=None
    try:
        client,_headers,operations,state,_old=setup_api(tmp_path,h)
        store=registry(tmp_path / "sensitive-location.sqlite")
        secret,_=store.issue("tenant",scopes=(READ,),expires_at=NOW+timedelta(hours=1))
        state.keys=store;store.close()
        reply=client.get("/v1/institutional/recovery-requests/missing",headers={"X-API-Key":secret})
        assert reply.status_code==401 and reply.json()=={"detail":"invalid_api_key"}
        assert secret not in reply.text and str(tmp_path) not in reply.text
        assert reply.headers["Cache-Control"]=="no-store"
    finally:
        if operations:operations.close()
        h.close()
