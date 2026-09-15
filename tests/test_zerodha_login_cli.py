from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

from quant_ai.cli import main as cli_main
from quant_ai.operations import zerodha_login
from quant_ai.operations.zerodha_login import LoginError, parse_request_token

API_SECRET = "s3cret-value-never-shown"
ACCESS_TOKEN = "access-token-never-shown"
REDIRECT = (
    "https://127.0.0.1/callback?type=login&request_token=req-from-url"
    "&action=login&status=success"
)


class FakeKiteConnect:
    instances: ClassVar[list[FakeKiteConnect]] = []
    profile_user_id = "AB1234"

    def __init__(self, api_key: str, access_token: str | None = None, timeout: int | None = None):
        self.api_key = api_key
        self.access_token = access_token
        self.calls: list[tuple] = []
        type(self).instances.append(self)

    def login_url(self) -> str:
        return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"

    def generate_session(self, request_token: str, api_secret: str) -> dict:
        self.calls.append(("generate_session", request_token, api_secret))
        return {
            "user_id": "AB1234", "access_token": ACCESS_TOKEN, "api_key": self.api_key,
            "public_token": "public-token", "refresh_token": None,
            "login_time": datetime(2026, 9, 15, 3, 40, tzinfo=timezone.utc),
        }

    def set_access_token(self, token: str) -> None:
        self.calls.append(("set_access_token", token))
        self.access_token = token

    def profile(self) -> dict:
        self.calls.append(("profile",))
        return {"user_id": type(self).profile_user_id, "exchanges": ["NSE"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TRADING_LIVE_MONEY_ACTIVE", raising=False)
    config = tmp_path / ".config" / "pramana"
    config.mkdir(parents=True)
    (config / "zerodha.json").write_text(json.dumps({"api_key": "key123", "api_secret": API_SECRET}))
    return config


@pytest.fixture
def fake_kite(monkeypatch):
    monkeypatch.setattr(FakeKiteConnect, "instances", [])
    monkeypatch.setattr(FakeKiteConnect, "profile_user_id", "AB1234")
    # The helper imports the SDK lazily via importlib; the fake module satisfies it.
    monkeypatch.setitem(sys.modules, "kiteconnect", SimpleNamespace(KiteConnect=FakeKiteConnect))
    return FakeKiteConnect


def test_happy_path_writes_session_file_and_never_prints_secrets(home, fake_kite, capsys) -> None:
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 0
    client = fake_kite.instances[0]
    assert client.calls == [
        ("generate_session", "req123", API_SECRET), ("set_access_token", ACCESS_TOKEN), ("profile",),
    ]
    session_file = home / "zerodha-session.json"
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
    text = session_file.read_text()
    payload = json.loads(text)
    assert set(payload) == {"user_id", "access_token", "issued_at", "login_time"}
    assert payload["user_id"] == "AB1234" and payload["access_token"] == ACCESS_TOKEN
    assert datetime.fromisoformat(payload["issued_at"]).tzinfo is not None
    assert API_SECRET not in text and "public-token" not in text
    out = capsys.readouterr().out
    assert "https://kite.zerodha.com/connect/login?api_key=key123" in out
    assert "user_id=AB1234" in out
    assert "06:00:00+05:30" in out  # next 06:00 IST expiry
    assert ACCESS_TOKEN not in out and API_SECRET not in out


def test_wrong_profile_user_id_refuses_to_save(home, fake_kite, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fake_kite, "profile_user_id", "ZZ9999")
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 1
    assert not (home / "zerodha-session.json").exists()
    err = capsys.readouterr().err
    assert "user_id does not match" in err and ACCESS_TOKEN not in err


def test_request_token_is_parsed_from_the_pasted_redirect_url(home, fake_kite, monkeypatch) -> None:
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return REDIRECT + "\n"

    monkeypatch.setattr("builtins.input", fake_input)
    assert cli_main(["zerodha-login"]) == 0
    assert prompts and "redirect URL" in prompts[0]
    assert fake_kite.instances[0].calls[0] == ("generate_session", "req-from-url", API_SECRET)
    assert cli_main(["zerodha-login", "--request-token", REDIRECT]) == 0
    assert fake_kite.instances[1].calls[0] == ("generate_session", "req-from-url", API_SECRET)


def test_parse_request_token_variants() -> None:
    assert parse_request_token("  abc123  ") == "abc123"
    assert parse_request_token(REDIRECT) == "req-from-url"
    with pytest.raises(LoginError, match="no request_token"):
        parse_request_token("https://127.0.0.1/callback?action=login")
    with pytest.raises(LoginError, match="status=failure"):
        parse_request_token("https://127.0.0.1/?request_token=x&status=failure")
    with pytest.raises(LoginError, match="empty"):
        parse_request_token("   ")
    with pytest.raises(LoginError, match="unexpected characters"):
        parse_request_token("abc def")


def test_live_money_flag_refuses_before_touching_kite(home, fake_kite, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 1
    assert fake_kite.instances == []
    assert not (home / "zerodha-session.json").exists()
    assert "paper trading only" in capsys.readouterr().err


def test_missing_credentials_are_reported_actionably(home, fake_kite, capsys) -> None:
    (home / "zerodha.json").write_text(json.dumps({"api_key": "key123"}))
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 1
    assert "api_secret" in capsys.readouterr().err
    (home / "zerodha.json").unlink()
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 1
    assert "zerodha.json not found" in capsys.readouterr().err
    assert fake_kite.instances == []


def test_sdk_rejection_is_scrubbed_and_non_zero(home, fake_kite, monkeypatch, capsys) -> None:
    def reject(self, request_token, api_secret):
        raise RuntimeError(f"Token is invalid ({api_secret})")

    monkeypatch.setattr(fake_kite, "generate_session", reject)
    assert cli_main(["zerodha-login", "--request-token", "req123"]) == 1
    err = capsys.readouterr().err
    assert "Kite rejected the login" in err and "single-use" in err
    assert API_SECRET not in err


def test_missing_sdk_is_actionable(home, monkeypatch, capsys) -> None:
    monkeypatch.delitem(sys.modules, "kiteconnect", raising=False)

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(zerodha_login, "import_module", missing)
    assert zerodha_login.main(["--request-token", "req123"]) == 1
    assert "pip install -e '.[pilot]'" in capsys.readouterr().err
