"""The operator side of a capital contribution: when it may run, and the pre-market line
that catches a plan left describing the smaller book."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.operations.premarket import load_ledger_capital, premarket_checks

spec = importlib.util.spec_from_file_location("pilot_ops", Path(__file__).parents[1] / "scripts/pilot_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

IST = timezone(timedelta(hours=5, minutes=30))
AFTER_CLOSE = datetime(2026, 9, 23, 16, 0, tzinfo=IST)
DURING_SESSION = datetime(2026, 9, 23, 11, 0, tzinfo=IST)
PAPER = {"TRADING_LIVE_MONEY_ACTIVE": "false"}


def ledger(tmp_path) -> Path:
    path = tmp_path / "pramana.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    broker.buy(OrderIntent("AAPL", Market.USA, Side.BUY, 10, Decimal(100), "test",
                           AssetClass.EQUITY, "ghost", Decimal(90), Decimal(130)))
    broker.close()
    return path


def directives(tmp_path, monkeypatch, capital) -> Path:
    path = tmp_path / "directives.json"
    path.write_text(json.dumps({"starting_capital": capital}))
    monkeypatch.delenv("PRAMANA_FOUNDER_DIRECTIVES_JSON", raising=False)
    monkeypatch.setenv("PRAMANA_FOUNDER_DIRECTIVES_FILE", str(path))
    return path


def run(path, *, env=PAPER, now=AFTER_CLOSE, reference="topup-2026-09-23"):
    return ops.capital_contribution(path, "ghost", "900000", reference, "scale paper book",
                                    env=env, now=now)


def test_after_the_close_it_records_and_reports_the_plan_that_matches(tmp_path, monkeypatch):
    path = ledger(tmp_path)
    directives(tmp_path, monkeypatch, 1_000_000)

    result = run(path)

    assert result["recorded"] is True
    assert result["capital"] == ["100000", "1000000"]
    assert result["planCapital"] == "1000000" and result["planMatchesLedger"] is True


def test_a_plan_still_at_the_old_capital_is_reported_as_a_mismatch(tmp_path, monkeypatch):
    path = ledger(tmp_path)
    directives(tmp_path, monkeypatch, 100000)

    result = run(path)

    assert result["recorded"] is True and result["planMatchesLedger"] is False


def test_nse_regular_hours_are_refused_and_nothing_is_written(tmp_path, monkeypatch):
    path = ledger(tmp_path)
    directives(tmp_path, monkeypatch, 1_000_000)

    with pytest.raises(ValueError, match="nse_regular_hours"):
        run(path, now=DURING_SESSION)

    assert load_ledger_capital(path, "ghost") == Decimal(100000)


@pytest.mark.parametrize("env", [{}, {"TRADING_LIVE_MONEY_ACTIVE": "true"},
                                 {"TRADING_LIVE_MONEY_ACTIVE": "yes"}])
def test_anything_but_paper_only_is_refused(tmp_path, monkeypatch, env):
    path = ledger(tmp_path)
    directives(tmp_path, monkeypatch, 1_000_000)

    with pytest.raises(ValueError, match="requires_paper_only"):
        run(path, env=env)

    assert load_ledger_capital(path, "ghost") == Decimal(100000)


def test_a_missing_ledger_is_refused_rather_than_created(tmp_path):
    missing = tmp_path / "absent.db"
    with pytest.raises(ValueError, match="ledger_missing"):
        run(missing)
    assert not missing.exists()


def test_premarket_capital_line_fails_until_the_plan_matches_the_book(tmp_path):
    path = ledger(tmp_path)
    plan = tmp_path / "directives.json"
    env = {"PRAMANA_FOUNDER_DIRECTIVES_FILE": str(plan)}
    at = datetime(2026, 9, 24, 8, 30, tzinfo=IST)

    def line(capital):
        plan.write_text(json.dumps({"starting_capital": capital}))
        checks = premarket_checks({}, None, env, at, ledger_capital=load_ledger_capital(path, "ghost"))
        return next(check for check in checks if check.id == "capital_plan")

    assert line(100000).state == "OK"
    ops.capital_contribution(path, "ghost", "900000", "topup-1", "scale", env=PAPER, now=AFTER_CLOSE)
    stale = line(100000)
    assert stale.state == "FAIL" and "1000000" in stale.detail
    assert line(1_000_000).state == "OK"


def test_premarket_capital_line_fails_on_unreadable_directives(tmp_path):
    path = ledger(tmp_path)
    env = {"PRAMANA_FOUNDER_DIRECTIVES_FILE": str(tmp_path / "absent.json")}
    checks = premarket_checks({}, None, env, AFTER_CLOSE,
                              ledger_capital=load_ledger_capital(path, "ghost"))
    assert next(c for c in checks if c.id == "capital_plan").state == "FAIL"


def test_the_ledger_capital_reader_is_read_only_and_tolerates_absence(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert load_ledger_capital(empty, "ghost") is None
    path = ledger(tmp_path)
    assert load_ledger_capital(path, "nobody") is None
    assert load_ledger_capital(path, "ghost") == Decimal(100000)

