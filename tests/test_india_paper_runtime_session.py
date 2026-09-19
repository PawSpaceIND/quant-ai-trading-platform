"""The India paper launcher refuses an expired Zerodha session before it connects."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from quant_ai.operations.zerodha_session import SessionRecord, write_session

SCRIPT = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"


class FakeKiteConnect:
    instances: ClassVar[list[FakeKiteConnect]] = []

    def __init__(self, api_key: str, access_token: str | None = None, timeout: int | None = None):
        self.api_key = api_key
        self.access_token = access_token
        type(self).instances.append(self)

    def profile(self) -> dict:
        return {"user_id": "AB1234", "exchanges": ["NSE", "BSE"]}

    def quote(self, keys: list[str]) -> dict:
        return {key: {"instrument_token": index + 1} for index, key in enumerate(keys)}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeKiteConnect, "instances", [])
    monkeypatch.setitem(sys.modules, "kiteconnect", SimpleNamespace(KiteConnect=FakeKiteConnect))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PRAMANA_PILOT_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    for name in ("FRED_API_KEY", "PRAMANA_FUNDAMENTALS_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / ".config" / "pramana"
    config.mkdir(parents=True)
    (config / "zerodha.json").write_text(json.dumps({"api_key": "key123", "api_secret": "s3cret"}))
    (config / "anthropic.key").write_text("anthropic-key\n")
    spec = importlib.util.spec_from_file_location("india_paper_runtime_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = dict(os.environ)
    yield SimpleNamespace(module=module, config=config)
    os.environ.clear()
    os.environ.update(snapshot)


def test_expired_session_is_refused_before_any_kite_call(runtime) -> None:
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    write_session(runtime.config / "zerodha-session.json", SessionRecord("AB1234", "old-token", stale))
    with pytest.raises(RuntimeError, match=r"Zerodha session expired at 06:00 IST; run: pramana zerodha-login"):
        runtime.module.configure()
    assert FakeKiteConnect.instances == []
    assert "ZERODHA_ACCESS_TOKEN" not in os.environ


def test_legacy_session_without_issued_at_is_refused(runtime) -> None:
    (runtime.config / "zerodha-session.json").write_text(
        json.dumps({"user_id": "AB1234", "access_token": "old-token"})
    )
    with pytest.raises(RuntimeError, match="run: pramana zerodha-login"):
        runtime.module.configure()
    assert FakeKiteConnect.instances == []


def test_fresh_session_configures_and_passes_provider_env_through(runtime, monkeypatch) -> None:
    write_session(
        runtime.config / "zerodha-session.json",
        SessionRecord("AB1234", "fresh-token", datetime.now(timezone.utc)),
    )
    monkeypatch.setenv("FRED_API_KEY", "fred-key")
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "example-provider")
    runtime.module.configure()
    assert FakeKiteConnect.instances[0].access_token == "fresh-token"
    assert os.environ["ZERODHA_ACCESS_TOKEN"] == "fresh-token"
    assert os.environ["TRADING_LIVE_MONEY_ACTIVE"] == "false"
    assert os.environ["FRED_API_KEY"] == "fred-key"
    assert os.environ["PRAMANA_FUNDAMENTALS_PROVIDER"] == "example-provider"
    assert os.environ["PRAMANA_DAILY_HISTORY_PROVIDER"] == "kite"
    assert os.environ["PRAMANA_INTRADAY_WARMUP_PROVIDER"] == "kite"
    assert os.environ["PRAMANA_BOOK_RISK_HISTORY"] == "daily"
    assert os.environ["PRAMANA_REQUIRE_BOOK_RISK_GATES"] == "true"
    assert os.environ["PRAMANA_SESSION_FLATTEN_MINUTES"] == "15"
    assert os.environ["PRAMANA_OVERNIGHT_GROSS_CAP"] == "0.25"
    assert os.environ["PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES"] == "15"
    directives = json.loads(os.environ["PRAMANA_FOUNDER_DIRECTIVES_JSON"])
    assert directives["starting_capital"] == 100000
    assert directives["max_open_positions"] == 5
    assert directives["allowed_asset_classes"] == ["EQUITY", "ETF"]
    assert [item["symbol"] for item in directives["watchlist"]] == [
        "INFY", "TCS", "RELIANCE", "GOLDBEES", "SILVERBEES"
    ]
    assert runtime.module.running_watchlist() == (
        "GOLDBEES", "INFY", "RELIANCE", "SILVERBEES", "TCS"
    )
    assert runtime.module.provider_status() == {
        "News": "Economic Times RSS; rule-based sentiment",
        "Macro": "FRED configured (FRED_API_KEY present)",
        "Fundamentals": "example-provider configured (PRAMANA_FUNDAMENTALS_PROVIDER)",
    }


def test_provider_status_reports_unconfigured_providers(runtime) -> None:
    assert runtime.module.provider_status() == {
        "News": "Economic Times RSS; rule-based sentiment",
        "Macro": "Unavailable; FRED_API_KEY not configured",
        "Fundamentals": "Unavailable; affected agents abstain",
    }


def test_user_id_mismatch_still_fails_account_validation(runtime) -> None:
    write_session(
        runtime.config / "zerodha-session.json",
        SessionRecord("ZZ9999", "fresh-token", datetime.now(timezone.utc)),
    )
    with pytest.raises(RuntimeError, match="Account validation failed"):
        runtime.module.configure()
