"""Synthetic subprocess-boundary checks; never call Docker or a real broker."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_FIELDS = (
    "ZERODHA_API_KEY", "ZERODHA_ACCESS_TOKEN",
    "PRAMANA_ZERODHA_TOKEN_ISSUED_AT", "PRAMANA_ZERODHA_USER_ID",
)


def helper():
    spec = importlib.util.spec_from_file_location(
        "renewal_compose_boundary", ROOT / "scripts/renew_pilot_token.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", CREDENTIAL_FIELDS)
@pytest.mark.parametrize("value", ["stale-fixture", ""])
def test_shell_credentials_cannot_shadow_published_environment(tmp_path, monkeypatch, name, value):
    module = helper()
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("RENEWAL_FIXTURE_UNRELATED", "preserved")
    parent = dict(os.environ)
    calls = []
    monkeypatch.setattr(module, "check_refresh_allowed", lambda *a, **kw: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    module.refresh_services(tmp_path, tmp_path / "fixture.env")
    assert len(calls) == 1
    child = calls[0][1].get("env", os.environ)
    assert all(field not in child for field in CREDENTIAL_FIELDS)
    assert child["RENEWAL_FIXTURE_UNRELATED"] == "preserved"
    assert child.get("PATH") == parent.get("PATH")
    assert dict(os.environ) == parent  # Never mutate the caller's credentials.


def test_relative_env_keeps_callers_location_when_docker_cwd_changes(tmp_path, monkeypatch):
    module = helper()
    caller = tmp_path / "caller"
    checkout = tmp_path / "checkout"
    caller.mkdir()
    checkout.mkdir()
    monkeypatch.chdir(caller)
    calls = []
    monkeypatch.setattr(module, "check_refresh_allowed", lambda *a, **kw: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    module.refresh_services(checkout, Path("selected.env"))
    command = calls[0][0][0]
    selected = Path(command[command.index("--env-file") + 1])
    assert selected == caller / "selected.env"
    assert selected.is_absolute()
    assert calls[0][1]["cwd"] == checkout


def test_refresh_keeps_existing_session_refusal_and_never_calls_docker(tmp_path, monkeypatch):
    from quant_ai.operations.zerodha_renewal import RenewalError

    module = helper()
    def refuse(*args, **kwargs):
        raise RenewalError("fixture_session_refused")
    monkeypatch.setattr(module, "check_refresh_allowed", refuse)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("Docker was reached"))
    with pytest.raises(RenewalError, match="fixture_session_refused"):
        module.refresh_services(tmp_path, tmp_path / "fixture.env")


def test_owner_flow_publishes_then_refreshes_the_same_session_from_another_directory(
    tmp_path, monkeypatch, capsys,
):
    from datetime import datetime, timedelta, timezone

    from quant_ai.operations import zerodha_login, zerodha_renewal
    from quant_ai.operations.zerodha_session import SessionRecord

    module = helper()
    caller, checkout = tmp_path / "caller", tmp_path / "checkout"
    caller.mkdir()
    checkout.mkdir()
    selected = caller / "fixture.env"
    selected.write_text("ZERODHA_API_KEY=fixture-key\nZERODHA_ACCESS_TOKEN=old\n")
    selected.chmod(0o600)
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    issued = SessionRecord("FIXTURE", "new-fixture", now - timedelta(minutes=1))
    for name in CREDENTIAL_FIELDS:
        monkeypatch.setenv(name, "stale-fixture")
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.chdir(caller)
    monkeypatch.setattr(module, "ROOT", checkout)
    monkeypatch.setattr(module, "check_refresh_allowed", lambda *a, **kw: None)
    monkeypatch.setattr(zerodha_login, "load_credentials", lambda _: ("fixture-key", "fixture-secret"))
    def login(**kwargs):
        assert kwargs["prompt"] is module.secret_prompt
        return issued
    monkeypatch.setattr(zerodha_login, "login", login)
    monkeypatch.setattr(zerodha_renewal, "_now", lambda: now)
    class Profile:
        def set_access_token(self, value):
            assert value == issued.access_token
        def profile(self):
            return {"user_id": issued.user_id}
    monkeypatch.setattr(zerodha_renewal, "_kite_client", lambda _: Profile())
    calls = []
    def docker(command, **kwargs):
        supplied = Path(command[command.index("--env-file") + 1])
        actual = supplied if supplied.is_absolute() else kwargs["cwd"] / supplied
        assert actual == selected
        fields = zerodha_renewal._fields(actual.read_text())
        shell = kwargs.get("env", os.environ)
        effective = {name: shell.get(name, fields[name]) for name in CREDENTIAL_FIELDS}
        assert effective == {
            "ZERODHA_API_KEY": "fixture-key", "ZERODHA_ACCESS_TOKEN": issued.access_token,
            zerodha_renewal.USER_ID: issued.user_id,
            zerodha_renewal.ISSUED_AT: issued.issued_at.isoformat(),
        }
        assert command[-3:] == ["pramana-ghost", "market-monitor", "token-watch"]
        assert kwargs["capture_output"] is True
        calls.append(True)
    monkeypatch.setattr(module.subprocess, "run", docker)
    assert module.main(["--env-file", "fixture.env", "--restart"]) == 0
    assert calls == [True]
    assert issued.access_token not in str(capsys.readouterr())
