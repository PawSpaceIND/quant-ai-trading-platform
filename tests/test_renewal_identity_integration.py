"""Combined identity/renewal startup ordering; only synthetic provider fixtures."""
from __future__ import annotations

import json

import pytest
from test_zerodha_renewal import TOKEN, FakeKite, TokenException

from quant_ai import daemon
from quant_ai.notifications.trading import JsonlFileSink, TradingNotificationDispatcher
from quant_ai.operations import zerodha_renewal as renewal


def configure(monkeypatch, tmp_path, mode):
    for name, value in {
        "TRADING_LIVE_MONEY_ACTIVE": "false",
        "PRAMANA_PILOT_MODE": "true",
        "PRAMANA_ORDER_IDENTITY_MODE": mode,
        "PRAMANA_LEDGER_PATH": str(tmp_path / "ledger.sqlite"),
        "PRAMANA_OMS_DB": str(tmp_path / "oms.sqlite") if mode == "bound_v1" else "",
        "ZERODHA_API_KEY": "synthetic-api-key",
        "ZERODHA_ACCESS_TOKEN": TOKEN,
        renewal.USER_ID: "SYNTHETIC",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(renewal.ISSUED_AT, raising=False)


def assert_no_storage(tmp_path):
    assert not (tmp_path / "ledger.sqlite").exists()
    assert not (tmp_path / "oms.sqlite").exists()


@pytest.mark.parametrize("defect", ["invalid_mode", "missing_oms", "same_storage", "legacy_oms"])
def test_identity_refusal_precedes_notifications_and_token_profile(tmp_path, monkeypatch, defect):
    configure(monkeypatch, tmp_path, "bound_v1")
    if defect == "invalid_mode":
        monkeypatch.setenv("PRAMANA_ORDER_IDENTITY_MODE", "unsupported")
    elif defect == "missing_oms":
        monkeypatch.delenv("PRAMANA_OMS_DB")
    elif defect == "same_storage":
        monkeypatch.setenv("PRAMANA_OMS_DB", str(tmp_path / "ledger.sqlite"))
    else:
        monkeypatch.setenv("PRAMANA_ORDER_IDENTITY_MODE", "legacy_cash")

    def forbidden(*_args, **_kwargs):
        pytest.fail("identity refusal must precede notification/provider construction")

    monkeypatch.setattr(daemon, "_env_notifications", forbidden)
    monkeypatch.setattr(renewal, "_kite_client", forbidden)
    monkeypatch.setattr(daemon, "import_module", forbidden)
    with pytest.raises(ValueError, match="order_identity_mode|runtime_identity_"):
        daemon.build_ghost_runner_from_env()
    assert_no_storage(tmp_path)


@pytest.mark.parametrize("mode", ["legacy_cash", "bound_v1"])
@pytest.mark.parametrize("failure", ["rejected", "wrong_account", "missing_token"])
def test_token_refusal_precedes_runner_assembly_in_each_identity_mode(
    tmp_path, monkeypatch, mode, failure
):
    configure(monkeypatch, tmp_path, mode)
    kite = FakeKite(
        user="OTHER" if failure == "wrong_account" else "SYNTHETIC",
        error=TokenException(TOKEN) if failure == "rejected" else None,
    )
    if failure == "missing_token":
        monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "")
    monkeypatch.setattr(renewal, "_kite_client", lambda _: kite)
    alert_file = tmp_path / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(alert_file),))
    monkeypatch.setattr(daemon, "_env_notifications", lambda: dispatcher)

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid token must not reach runner/provider assembly")

    monkeypatch.setattr(daemon, "import_module", forbidden)
    reason = {"rejected": "profile_rejected",
              "wrong_account": "profile_identity_mismatch",
              "missing_token": "credentials_missing"}[failure]
    with pytest.raises(renewal.RenewalError, match=reason):
        daemon.build_ghost_runner_from_env()
    assert kite.profile_calls == (0 if failure == "missing_token" else 1)
    assert kite.tokens == ([] if failure == "missing_token" else [TOKEN])
    alert = json.loads(alert_file.read_text())
    assert alert["code"] == "ZERODHA_SESSION_INVALID"
    assert alert["priority"] == "CRITICAL"
    assert alert["metadata"]["phase"] == "boot"
    assert TOKEN not in alert_file.read_text()
    assert len(dispatcher.pending()) == 1
    assert_no_storage(tmp_path)
