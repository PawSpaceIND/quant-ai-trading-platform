"""The pilot trades what the operator's directives say, not a list compiled into a launcher."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "scripts" / "india_paper_runtime.py"


def load_module(monkeypatch, directives_path=None):
    if directives_path is not None:
        monkeypatch.setenv("PRAMANA_PILOT_DIRECTIVES", str(directives_path))
    else:
        monkeypatch.delenv("PRAMANA_PILOT_DIRECTIVES", raising=False)
    spec = importlib.util.spec_from_file_location("india_paper_runtime", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(tmp_path, payload):
    target = tmp_path / "directives.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def directives(symbols, **overrides):
    payload = {
        "starting_capital": 100000,
        "allowed_markets": ["INDIA"],
        "allowed_asset_classes": ["EQUITY", "ETF"],
        "max_open_positions": 5,
        "watchlist": [
            {"symbol": symbol, "market": "INDIA", "asset_class": asset,
             "currency": "INR", "exchange": "NSE"}
            for symbol, asset in symbols
        ],
    }
    payload.update(overrides)
    return payload


def test_the_shipped_directives_are_what_the_pilot_would_trade(monkeypatch):
    # The launcher used to carry three equities, three open positions and EQUITY only,
    # while the operator's directives named five instruments, five positions and EQUITY
    # plus ETF. Both were internally consistent, which is exactly why the disagreement
    # survived - and the ETF restriction quietly explained the two missing instruments.
    module = load_module(monkeypatch)

    payload, symbols = module.load_directives()

    assert symbols == ("INFY", "TCS", "RELIANCE", "GOLDBEES", "SILVERBEES")
    assert payload["max_open_positions"] == 5
    assert set(payload["allowed_asset_classes"]) == {"EQUITY", "ETF"}
    assert payload["starting_capital"] == 100000
    # The ETFs are only reachable because the asset class allows them.
    etfs = {item["symbol"] for item in payload["watchlist"] if item["asset_class"] == "ETF"}
    assert etfs == {"GOLDBEES", "SILVERBEES"}


def test_changing_the_directives_changes_the_universe(monkeypatch, tmp_path):
    path = write(tmp_path, directives([("TCS", "EQUITY"), ("ITC", "EQUITY")]))
    module = load_module(monkeypatch, path)

    _, symbols = module.load_directives()

    assert symbols == ("TCS", "ITC")


def test_a_non_nse_instrument_is_not_subscribed_through_the_nse_quote_path(monkeypatch, tmp_path):
    payload = directives([("TCS", "EQUITY")])
    payload["watchlist"].append(
        {"symbol": "GOLD", "market": "INDIA", "asset_class": "COMMODITY",
         "currency": "INR", "exchange": "MCX"}
    )
    module = load_module(monkeypatch, write(tmp_path, payload))

    _, symbols = module.load_directives()

    assert symbols == ("TCS",)


def test_an_unreadable_directives_file_stops_the_launcher(monkeypatch, tmp_path):
    # Failing closed matters more than it looks: starting on a compiled-in list is how a
    # pilot trades a universe nobody configured while every log says it is fine.
    module = load_module(monkeypatch, tmp_path / "absent.json")

    with pytest.raises(RuntimeError, match="unreadable"):
        module.load_directives()


def test_directives_naming_no_nse_instruments_stop_the_launcher(monkeypatch, tmp_path):
    module = load_module(monkeypatch, write(tmp_path, directives([])))

    with pytest.raises(RuntimeError, match="no NSE instruments"):
        module.load_directives()


def test_a_symbol_listed_twice_stops_the_launcher(monkeypatch, tmp_path):
    module = load_module(monkeypatch, write(tmp_path, directives([("TCS", "EQUITY"), ("TCS", "EQUITY")])))

    with pytest.raises(RuntimeError, match="twice"):
        module.load_directives()


def test_the_status_file_reports_the_live_subscription_not_a_constant(monkeypatch):
    module = load_module(monkeypatch)
    monkeypatch.setenv("PRAMANA_ZERODHA_SYMBOLS_JSON", json.dumps({"111": "TCS", "222": "INFY"}))

    assert module.running_watchlist() == ("INFY", "TCS")

    # Nothing subscribed reports nothing, rather than a list that was never watched.
    monkeypatch.setenv("PRAMANA_ZERODHA_SYMBOLS_JSON", "not json")
    assert module.running_watchlist() == ()
    monkeypatch.delenv("PRAMANA_ZERODHA_SYMBOLS_JSON")
    assert module.running_watchlist() == ()


def test_the_launcher_no_longer_carries_its_own_universe():
    source = RUNTIME.read_text(encoding="utf-8")

    assert "SYMBOLS = (" not in source
    assert '"max_open_positions": 3' not in source
    assert '"allowed_asset_classes": ["EQUITY"]' not in source
    assert "json.dumps(directives)" in source
