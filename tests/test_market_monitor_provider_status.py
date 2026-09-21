"""Provider labels must describe selection, without making a network health claim."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def monitor(monkeypatch):
    sdk = ModuleType("kiteconnect")
    sdk.KiteConnect = lambda *args, **kwargs: pytest.fail("status must not connect to broker")
    monkeypatch.setitem(sys.modules, "kiteconnect", sdk)
    spec = importlib.util.spec_from_file_location(
        "provider_status_test", Path(__file__).parents[1] / "scripts/market_monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("selection,label", [
    (None, "Yahoo daily bars configured"), ("", "Yahoo daily bars configured"),
    ("yahoo", "Yahoo daily bars configured"), (" KITE ", "Kite daily bars configured"),
    ("none", "Disabled by configuration; intraday-only regime"),
    ("invalid", "Unsupported daily-history configuration; engine selection rejects it"),
])
def test_history_selection_is_not_misreported_as_disabled(monitor, monkeypatch, selection, label):
    if selection is None:
        monkeypatch.delenv("PRAMANA_DAILY_HISTORY_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("PRAMANA_DAILY_HISTORY_PROVIDER", selection)
    status = monitor.provider_status(False)["Regime history"]
    assert status.startswith(label)
    if "configured" in label:
        assert "availability checked per history request" in status


def test_fundamentals_label_matches_partial_ratio_support(monitor, monkeypatch):
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "yahoo")
    status = monitor.provider_status(False)["Fundamentals"]
    assert "available ratios are scored" in status
    assert "all four" not in status
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    assert "valuation agents abstain" in monitor.provider_status(False)["Fundamentals"]
