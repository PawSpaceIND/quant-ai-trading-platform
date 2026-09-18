"""Authenticated tenant identity on browser-facing recovery observations."""
from test_institutional_bridge_recovery import interrupted_fill
from test_institutional_operator_recovery import body, route, setup_api
from test_institutional_swarm_bridge import BridgeHarness


def test_preview_outcome_and_status_identify_the_authenticated_recovery_tenant(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, _, _ = setup_api(tmp_path, h)
        preview = client.get(route(pid), headers=headers)
        assert preview.status_code == 200
        assert preview.json()["tenant_id"] == "tenant"
        result = client.post(route(pid), headers=headers, json=body(preview.json()["context_sha256"]))
        assert result.status_code == 200
        assert result.json()["tenant_id"] == result.json()["result"]["tenant_id"] == "tenant"
        saved = client.get("/v1/institutional/recovery-requests/synthetic-request-1", headers=headers)
        assert saved.status_code == 200 and saved.json()["tenant_id"] == "tenant"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert result.json()["execution_authorized"] is False
    finally:
        if operations:
            operations.close()
        h.close()
