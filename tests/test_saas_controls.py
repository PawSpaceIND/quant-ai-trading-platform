import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from quant_ai.service.saas_controls import SaaSControls

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def account(control, tenant="a", budget=100):
    control.configure_account(tenant, active=True, monthly_budget_micro_usd=budget,
                              reviewer="operator", now=NOW)


def grant(control, tenant="a", source="licensed-feed", purpose="model_input"):
    control.grant_data(tenant, source, purpose, valid_from=NOW, expires=NOW + timedelta(days=1),
                       evidence_sha256="a" * 64, reviewer="licence-reviewer", now=NOW)


def reserve(control, request="r1", tenant="a", maximum=60, now=NOW):
    return control.reserve(tenant, request, input_sha256="b" * 64, model="frozen-model-v1",
                           sources=["licensed-feed"], maximum_cost_micro_usd=maximum, now=now)


@pytest.fixture
def control(tmp_path):
    value = SaaSControls(tmp_path / "saas.sqlite")
    yield value
    value.close()


def test_default_denial_and_separate_purpose_tenant_and_source(control):
    assert not reserve(control)["execute"]
    account(control)
    grant(control, purpose="private_research")
    assert not reserve(control)["execute"]
    grant(control)
    assert reserve(control)["execute"]
    account(control, "b")
    assert not reserve(control, tenant="b")["execute"]
    assert not control.admission("a", sources=["different-feed"],
                                 purpose="model_input", now=NOW)["allowed"]
    assert not control.admission("a", sources=["licensed-feed"],
                                 purpose="customer_display", now=NOW)["allowed"]


def test_expiry_boundary_microseconds_and_revocation(control):
    account(control)
    grant(control)
    assert reserve(control, now=NOW + timedelta(microseconds=1))["execute"]
    assert not reserve(control, "r2", now=NOW + timedelta(days=1))["execute"]
    control.revoke_data("a", "licensed-feed", "model_input", reviewer="operator", now=NOW)
    assert not reserve(control, "r3")["execute"]


def test_future_grant_and_paused_account_denied(control):
    account(control)
    control.grant_data("a", "licensed-feed", "model_input", valid_from=NOW + timedelta(hours=1),
                       expires=NOW + timedelta(days=1), evidence_sha256="a" * 64,
                       reviewer="operator", now=NOW)
    assert not reserve(control)["execute"]
    grant(control)
    control.configure_account("a", active=False, monthly_budget_micro_usd=100,
                              reviewer="operator", now=NOW)
    assert not reserve(control)["execute"]


def test_reserve_retry_unknown_cost_restart_and_settlement(tmp_path):
    path = tmp_path / "saas.sqlite"
    c = SaaSControls(path)
    account(c)
    grant(c)
    assert reserve(c)["execute"]
    c.settle("a", "r1", actual_cost_micro_usd=None, now=NOW)
    c.close()
    c = SaaSControls(path)
    try:
        assert not reserve(c)["execute"]
        assert not reserve(c, "r2")["execute"]
        assert c.usage("a", now=NOW)["held"] == 60
        c.settle("a", "r1", actual_cost_micro_usd=25, now=NOW)
        c.settle("a", "r1", actual_cost_micro_usd=25, now=NOW)
        assert reserve(c, "r2")["execute"]
        report = c.usage("a", now=NOW)
        assert (report["known_cost"], report["held"], report["unresolved"]) == (25, 60, 1)
    finally:
        c.close()


def test_tenant_cannot_settle_another_request_or_see_its_usage(control):
    account(control)
    grant(control)
    reserve(control)
    account(control, "b")
    with pytest.raises(ValueError, match="unknown_request"):
        control.settle("b", "r1", actual_cost_micro_usd=0, now=NOW)
    assert control.usage("b", now=NOW)["requests"] == 0
    grant(control, "b")
    assert reserve(control, tenant="b")["execute"]


def test_conflicting_retry_and_settlement_fail(control):
    account(control)
    grant(control)
    reserve(control)
    with pytest.raises(ValueError, match="idempotency_conflict"):
        reserve(control, maximum=50)
    control.settle("a", "r1", actual_cost_micro_usd=30, now=NOW)
    with pytest.raises(ValueError, match="settlement_conflict"):
        control.settle("a", "r1", actual_cost_micro_usd=0, now=NOW)
    with pytest.raises(ValueError, match="settlement_before_request"):
        control.settle("a", "r1", actual_cost_micro_usd=30, now=NOW-timedelta(seconds=1))


def test_cost_overrun_is_recorded_honestly(control):
    account(control)
    grant(control)
    reserve(control)
    control.settle("a", "r1", actual_cost_micro_usd=110, now=NOW)
    assert control.usage("a", now=NOW)["known_cost"] == 110
    assert not reserve(control, "r2", maximum=1)["execute"]
    body = control.db.execute("SELECT body FROM saas_audit WHERE event='cost_settled'").fetchone()[0]
    assert '"exceeded_reservation": true' in body


def test_concurrent_budget_reservations_cannot_overspend(tmp_path):
    path = tmp_path / "saas.sqlite"
    c = SaaSControls(path)
    account(c, budget=100)
    grant(c)
    c.close()

    def request(i):
        c = SaaSControls(path)
        try:
            return reserve(c, str(i), maximum=30)["execute"]
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(16))) == 3
    c = SaaSControls(path)
    assert c.usage("a", now=NOW)["held"] == 90
    c.close()


def test_same_request_concurrent_only_admitted_once(tmp_path):
    path = tmp_path / "saas.sqlite"
    c = SaaSControls(path)
    account(c)
    grant(c)
    c.close()

    def request(_):
        c = SaaSControls(path)
        try:
            return reserve(c)["execute"]
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(12))) == 1


def test_budget_period_utc_and_old_pending_hold_survives(control):
    account(control)
    grant(control)
    reserve(control)
    next_month = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert control.usage("a", now=next_month)["held"] == 0
    assert control.usage("a", now=NOW)["held"] == 60
    assert not reserve(control, now=next_month)["execute"]


@pytest.mark.parametrize("amount", [True, -1, 1.5, float("nan"), 10**13])
def test_invalid_money_rejected(control, amount):
    with pytest.raises(ValueError):
        account(control, budget=amount)


def test_no_implicit_permission_or_naive_clock(control):
    account(control)
    with pytest.raises(ValueError, match="source_list"):
        control.admission("a", sources=[], purpose="model_input", now=NOW)
    with pytest.raises(ValueError, match="purpose"):
        control.admission("a", sources=["licensed-feed"], purpose="live_trading", now=NOW)
    with pytest.raises(ValueError, match="aware_time"):
        control.admission("a", sources=["licensed-feed"], purpose="model_input", now=NOW.replace(tzinfo=None))


def test_reject_existing_trading_database_without_mutation(tmp_path):
    path = tmp_path / "ledger.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE paper_ledger(id INTEGER)")
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="not_a_saas"):
        SaaSControls(path)
    assert path.read_bytes() == before


def test_readonly_report_cannot_mutate_or_create_database(tmp_path):
    path = tmp_path / "controls.sqlite"
    c = SaaSControls(path)
    account(c)
    grant(c)
    reserve(c)
    c.close()
    before = path.read_bytes()
    c = SaaSControls(path, readonly=True)
    assert c.usage("a", now=NOW)["held"] == 60
    assert c.admission("a", sources=["licensed-feed"], purpose="model_input", now=NOW)["allowed"]
    with pytest.raises(sqlite3.OperationalError):
        account(c, budget=1)
    c.close()
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        SaaSControls(tmp_path / "missing.sqlite", readonly=True)
    assert not (tmp_path / "missing.sqlite").exists()


def test_usage_cli_is_tenant_scoped_and_readonly(tmp_path, capsys):
    import json

    from quant_ai.research.saas_evidence import main

    path = tmp_path / "controls.sqlite"
    c = SaaSControls(path)
    account(c)
    grant(c)
    reserve(c)
    c.close()
    before = path.read_bytes()
    main(["usage", "--database", str(path), "--tenant", "b", "--as-of", NOW.isoformat()])
    assert json.loads(capsys.readouterr().out)["report"]["requests"] == 0
    assert path.read_bytes() == before
