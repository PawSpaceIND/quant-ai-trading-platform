import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from portfolio_attribution_fixture import scenarios

from quant_ai.research.portfolio_attribution import contribution


def test_shared_fixture_reproduces_the_full_published_accounting(tmp_path):
    expected = json.loads((Path(__file__).parent / "fixtures" / "portfolio-attribution.json").read_text())
    assert scenarios(tmp_path) == expected


def test_partial_fills_average_cost_closed_positions_and_cost_bridge(tmp_path):
    book = scenarios(tmp_path)["fresh"]["books"]["active"]
    a = book["attribution"]
    rows = {r["symbol"]: r for r in a["rows"]}
    tcs = rows["NSE:TCS"]
    # Six shares bought for 609.52 + .60952 fees, three sold for 329.175 - .329175.
    assert Decimal(tcs["realized_pnl_inr"]) == Decimal("23.781065")
    assert Decimal(tcs["unrealized_pnl_inr"]) == Decimal("24.93524")
    assert Decimal(tcs["net_pnl_inr"]) == Decimal("48.716305")
    assert Decimal(tcs["reference_pnl_inr"]) == Decimal(61)
    assert Decimal(tcs["spread_cost_inr"]) == 9
    assert Decimal(tcs["slippage_cost_inr"]) == Decimal("2.345")
    assert Decimal(tcs["fees_inr"]) == Decimal(".938695")
    assert rows["NSE:INFY"]["quantity"] == 0  # Closed losses cannot disappear from attribution.
    assert Decimal(rows["NSE:INFY"]["net_pnl_inr"]) == Decimal("-14.679035")
    assert Decimal(rows["NSE:BANK"]["net_pnl_inr"]) == Decimal("-2.287205")
    t = a["totals"]
    assert Decimal(t["net_pnl_inr"]) == Decimal("31.750065")
    assert Decimal(t["reference_pnl_inr"]) == 50
    assert Decimal(t["reference_pnl_inr"]) - sum(Decimal(t[k]) for k in ["spread_cost_inr", "slippage_cost_inr", "fees_inr"]) == Decimal(t["net_pnl_inr"])
    assert sum(Decimal(r["contribution_fraction"]) for r in a["rows"]) == Decimal(book["net_return_fraction"])
    assert a["status"] == "complete" and Decimal(a["reconciliation_difference_inr"]) == 0


def test_missing_marks_withhold_totals_but_retain_known_costs_and_closed_results(tmp_path):
    reports = scenarios(tmp_path)
    for name in ["empty", "stale", "partial"]:
        a = reports[name]["books"]["active"]["attribution"]
        assert a["status"] == "incomplete" and a["reconciliation_difference_inr"] is None
        assert a["totals"]["net_pnl_inr"] is None and a["totals"]["reference_pnl_inr"] is None
    rows = {r["symbol"]: r for r in reports["partial"]["books"]["active"]["attribution"]["rows"]}
    assert rows["NSE:TCS"]["net_pnl_inr"] is not None and rows["NSE:BANK"]["net_pnl_inr"] is None
    assert Decimal(rows["NSE:INFY"]["net_pnl_inr"]) == Decimal("-14.679035")
    assert reports["stale"]["books"]["cash"]["attribution"]["totals"]["net_pnl_inr"] == "0"
    assert reports["restored"]["books"]["active"]["attribution"]["status"] == "complete"


@pytest.mark.parametrize("field", ["cash_inr", "realized_pnl_inr", "unrealized_pnl_inr", "fees_inr", "net_return_fraction", "current_equity_inr"])
def test_inconsistent_summary_cannot_be_published(tmp_path, field):
    book = copy.deepcopy(scenarios(tmp_path)["fresh"]["books"]["active"])
    book[field] = str(Decimal(book[field]) + 1)
    with pytest.raises(ValueError, match="accounting_mismatch"):
        contribution(book, Decimal(10000))
