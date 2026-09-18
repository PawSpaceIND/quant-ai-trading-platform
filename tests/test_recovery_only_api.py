"""Disposable credential/account records exercise the actual recovery-only surface."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_institutional_bridge_recovery import interrupted_fill
from test_institutional_operator_recovery import body, route
from test_institutional_swarm_bridge import NOW, BridgeHarness

from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry

READ = "institutional.recovery.read"
APPLY = "institutional.recovery.apply"


class ServiceHarness:
    def __init__(self, root, monkeypatch):
        self.bridge = BridgeHarness(root)
        self.program = interrupted_fill(self.bridge, monkeypatch)
        self.now = NOW
        self.keys = PersistentApiKeyRegistry(root / "credentials.sqlite", create=True,
                                             clock=lambda: self.now)
        self.raw, self.principal = self.keys.issue("tenant", scopes=(READ, APPLY),
                                                   expires_at=NOW + timedelta(hours=1))
        self.operations = InstitutionalRecoveryOperations(self.bridge.runtime, root / "operator.sqlite")

    def app(self, **kwargs):
        from quant_ai.api.recovery_app import create_recovery_app
        return create_recovery_app(keys=self.keys, operations={"tenant": self.operations}, **kwargs)

    @property
    def headers(self):
        return {"X-API-Key": self.raw}

    def close(self):
        self.operations.close()
        self.keys.close()
        self.bridge.close()


def test_recovery_service_has_no_trade_decision_or_public_documentation_routes(tmp_path, monkeypatch):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        client = TestClient(h.app())
        for path in ("/v1/paper/trades", "/v1/live/trades", "/v1/atlas/cycle", "/v1/capital/recommendation"):
            response = client.post(path, headers=h.headers, json={})
            assert response.status_code == 404
        for path in ("/docs", "/redoc", "/openapi.json", "/v1/portfolio", "/v1/readiness"):
            assert client.get(path, headers=h.headers).status_code == 404
        assert len(h.bridge.broker.ledger_entries("tenant")) == 1
        assert h.operations._records() == []
    finally:
        h.close()


def test_selected_service_still_performs_confirmed_historical_recovery_once(tmp_path, monkeypatch):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        client = TestClient(h.app())
        preview = client.get(route(h.program), headers=h.headers)
        assert preview.status_code == 200
        payload = body(preview.json()["context_sha256"])
        original = h.bridge.runtime.reconcile_program
        calls = []
        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        monkeypatch.setattr(h.bridge.runtime, "reconcile_program", counted)
        result = client.post(route(h.program), headers=h.headers, json=payload)
        assert result.status_code == 200
        assert result.json()["result"]["program_state"] == "COMPLETE"
        assert result.json()["execution_authorized"] is False
        assert client.post(route(h.program), headers=h.headers, json=payload).json()["replayed"] is True
        assert calls == [1]
        assert len(h.bridge.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


@pytest.mark.parametrize("scope,status", [((), 403), ((READ,), 403), ((APPLY,), 200)])
def test_default_and_read_permissions_cannot_apply_on_dedicated_service(tmp_path, monkeypatch, scope, status):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        raw, _ = h.keys.issue("tenant", scopes=scope, expires_at=NOW + timedelta(hours=1))
        response = TestClient(h.app()).post(route(h.program), headers={"X-API-Key": raw},
            json=body(h.bridge.programs.get(h.program).context_sha256))
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert len(h.bridge.broker.ledger_entries("tenant")) == 1
        assert len(h.operations._records()) == (2 if status == 200 else 0)
    finally:
        h.close()


@pytest.mark.parametrize("fault,status", [("missing", 401), ("invalid", 401), ("expired", 401),
    ("revoked", 401), ("other_tenant", 403), ("missing_storage", 401), ("live_switch", 503)])
def test_changed_credentials_or_configuration_never_reach_source_recovery(tmp_path, monkeypatch, fault, status):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        app = h.app()
        headers = h.headers
        if fault == "missing": headers = {}
        elif fault == "invalid": headers = {"X-API-Key": "invalid"}
        elif fault == "expired": h.now = NOW + timedelta(hours=1)
        elif fault == "revoked": h.keys.revoke(h.principal.key_id)
        elif fault == "other_tenant":
            raw, _ = h.keys.issue("other", scopes=(READ, APPLY), expires_at=NOW + timedelta(hours=1))
            headers = {"X-API-Key": raw}
        elif fault == "missing_storage": h.keys.path.rename(tmp_path / "offline-credentials.sqlite")
        else: monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
        def forbidden(*args, **kwargs):
            pytest.fail("Unauthorized request must not invoke bookkeeping")
        monkeypatch.setattr(h.bridge.runtime, "reconcile_program", forbidden)
        response = TestClient(app).post(route(h.program), headers=headers,
            json=body(h.bridge.programs.get(h.program).context_sha256))
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert h.operations._records() == []
    finally:
        h.close()


@pytest.mark.parametrize("fault", ["memory_keys", "no_operations", "wrong_mapping_type", "wrong_tenant",
                                   "wrong_service", "boolean_rate", "zero_rate", "unbounded_rate", "live_mode"])
def test_factory_requires_explicit_durable_server_selections(tmp_path, monkeypatch, fault):
    from quant_ai.api.recovery_app import create_recovery_app
    from quant_ai.security.api_keys import ApiKeyRegistry
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        args = {"keys": h.keys, "operations": {"tenant": h.operations}}
        if fault == "memory_keys": args["keys"] = ApiKeyRegistry()
        elif fault == "no_operations": args["operations"] = {}
        elif fault == "wrong_mapping_type": args["operations"] = []
        elif fault == "wrong_tenant": args["operations"] = {"another": h.operations}
        elif fault == "wrong_service": args["operations"] = {"tenant": object()}
        elif fault == "boolean_rate": args["rate_limit_per_minute"] = True
        elif fault == "zero_rate": args["rate_limit_per_minute"] = 0
        elif fault == "unbounded_rate": args["rate_limit_per_minute"] = 10001
        else: monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
        before = tuple(h.operations.db.iterdump())
        with pytest.raises(ValueError, match="recovery_service_configuration_invalid"):
            create_recovery_app(**args)
        assert tuple(h.operations.db.iterdump()) == before
    finally:
        h.close()


def test_app_installs_only_four_routes_and_never_constructs_trading_or_model_services(tmp_path, monkeypatch):
    from quant_ai.agents.runtime import AtlasRuntimeCoordinator
    from quant_ai.service.trading_service import TradingService
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("Recovery factory must not construct a trading/model service")
        monkeypatch.setattr(TradingService, "__init__", forbidden)
        monkeypatch.setattr(AtlasRuntimeCoordinator, "__init__", forbidden)
        app = h.app()
        assert {(r.path, tuple(sorted(r.methods))) for r in app.routes} == {
            ("/health", ("GET",)),
            ("/v1/institutional/programs/{program_id}/recovery", ("GET",)),
            ("/v1/institutional/programs/{program_id}/recovery", ("POST",)),
            ("/v1/institutional/recovery-requests/{request_id}", ("GET",)),
        }
        monkeypatch.setattr(h.keys, "authenticate", forbidden)
        client = TestClient(app)
        health = client.get("/health")
        assert health.json() == {"status": "recovery_only", "live_execution_available": False,
            "trading_routes_available": False, "automatic_recovery": False, "account_health_verified": False}
        assert health.headers["cache-control"] == "no-store"
        for path in ("/v1/paper/trades", "/docs", "/v1/atlas/cycle"):
            response = client.post(path, json={}, headers=h.headers)
            assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    finally:
        h.close()


def test_app_does_not_adopt_later_mapping_changes_or_close_caller_owned_stores(tmp_path, monkeypatch):
    from dataclasses import FrozenInstanceError

    from quant_ai.api.recovery_app import create_recovery_app
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        selected = {"tenant": h.operations}
        app = create_recovery_app(keys=h.keys, operations=selected)
        selected.clear()
        with pytest.raises(TypeError): app.state.recovery.institutional_recovery["other"] = h.operations
        with pytest.raises(FrozenInstanceError): app.state.recovery.keys = None
        with TestClient(app) as client:
            assert client.get(route(h.program), headers=h.headers).status_code == 200
        assert h.keys.authenticate(h.raw) == h.principal
        assert h.bridge.programs.get(h.program).program_id == h.program
    finally:
        h.close()


def test_rate_limit_is_shared_for_keys_in_selected_tenant(tmp_path, monkeypatch):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        client = TestClient(h.app(rate_limit_per_minute=1))
        assert client.get(route(h.program), headers=h.headers).status_code == 200
        raw, _ = h.keys.issue("tenant", scopes=(READ,), expires_at=NOW + timedelta(hours=1))
        response = client.get(route(h.program), headers={"X-API-Key": raw})
        assert response.status_code == 429 and response.headers["cache-control"] == "no-store"
        assert h.operations._records() == []
    finally:
        h.close()


@pytest.mark.parametrize("payload,status", [(b"{}" + b" " * 2048, 413),
    (b"not-json", 422), (b'{"database":"sensitive-placeholder"}', 422)])
def test_rejected_request_body_is_bounded_redacted_and_never_audited(tmp_path, monkeypatch, payload, status):
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        response = TestClient(h.app()).post(route(h.program), headers={**h.headers, "Content-Type": "application/json"},
                                            content=payload)
        assert response.status_code == status
        assert "sensitive-placeholder" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert h.operations._records() == []
    finally:
        h.close()


@pytest.mark.parametrize("fault,status", [("large_length", 413), ("duplicate_length", 400),
    ("bad_length", 400), ("wrong_length", 400), ("chunked_overflow", 413), ("wrong_media", 415),
    ("duplicate_media", 415), ("timeout", 408), ("disconnect", None)])
def test_stream_envelope_refuses_before_route_parsing(monkeypatch, fault, status):
    import asyncio

    from quant_ai.api import recovery_app as module
    headers = [(b"content-type", b"application/json")]
    chunks = [{"type": "http.request", "body": b"{}", "more_body": False}]
    if fault == "large_length": headers += [(b"content-length", b"2049")]
    elif fault == "duplicate_length": headers += [(b"content-length", b"2"), (b"content-length", b"2")]
    elif fault == "bad_length": headers += [(b"content-length", b"-1")]
    elif fault == "wrong_length": headers += [(b"content-length", b"5")]
    elif fault == "chunked_overflow":
        chunks = [{"type": "http.request", "body": b"x" * 1024, "more_body": True},
                  {"type": "http.request", "body": b"x" * 1025, "more_body": False}]
    elif fault == "wrong_media": headers = [(b"content-type", b"text/plain")]
    elif fault == "duplicate_media": headers *= 2
    elif fault == "disconnect": chunks = [{"type": "http.disconnect"}]
    if fault == "timeout": monkeypatch.setattr(module, "BODY_TIMEOUT_SECONDS", 0.01)
    messages = []
    async def receive():
        if fault == "timeout": await asyncio.sleep(10)
        return chunks.pop(0)
    async def send(message): messages.append(message)
    async def forbidden(*args): pytest.fail("Rejected envelope must not reach the route")
    scope = {"type": "http", "method": "POST", "path": "/v1/institutional/programs/test/recovery", "headers": headers}
    asyncio.run(module._RecoveryEnvelope(forbidden)(scope, receive, send))
    if status is None:
        assert messages == []
    else:
        first = messages[0]
        assert first["status"] == status
        assert (b"cache-control", b"no-store") in first["headers"]


def test_queued_revoked_credential_still_refuses_on_recovery_only_service(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        client = TestClient(h.app())
        authenticated = Event()
        original = h.keys.authenticate
        def observed(raw):
            result = original(raw)
            authenticated.set()
            return result
        monkeypatch.setattr(h.keys, "authenticate", observed)
        payload = body(h.bridge.programs.get(h.program).context_sha256)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with h.bridge.runtime._route_lock:
                future = pool.submit(client.post, route(h.program), headers=h.headers, json=payload)
                assert authenticated.wait(5)
                with PersistentApiKeyRegistry(h.keys.path, clock=lambda: NOW) as other:
                    assert other.revoke(h.principal.key_id)
            response = future.result(timeout=10)
        assert response.status_code == 401
        assert h.operations._records() == []
    finally:
        h.close()


def test_halted_account_recovery_preserves_halt_reservation_and_original_order(tmp_path, monkeypatch):
    from quant_ai.execution.shared_risk import verify_shared_risk
    h = ServiceHarness(tmp_path, monkeypatch)
    try:
        h.bridge.runtime.kill_switch.engage("synthetic retained halt")
        before = verify_shared_risk(h.bridge.programs.db, "tenant")
        response = TestClient(h.app()).post(route(h.program), headers=h.headers,
            json=body(h.bridge.programs.get(h.program).context_sha256))
        assert response.status_code == 200
        assert h.bridge.runtime.kill_switch.engaged
        assert verify_shared_risk(h.bridge.programs.db, "tenant") == before
        assert len(h.bridge.broker.ledger_entries("tenant")) == 1
        outcome = TestClient(h.app()).get("/v1/institutional/recovery-requests/synthetic-request-1", headers=h.headers)
        assert outcome.status_code == 200 and outcome.json()["status"] == "RETURNED"
    finally:
        h.close()
