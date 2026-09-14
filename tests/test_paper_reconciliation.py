from decimal import Decimal

import pytest

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService


def order(side, quantity=3, price=100, tenant="test", stop=None):
    return OrderIntent("INFY", Market.INDIA, side, quantity, Decimal(price), "test",
                       tenant_id=tenant, stop_price=Decimal(stop) if stop else None)


def test_reconstruction_matches_scaling_partial_exits_fees_and_reentry(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    for item in [order(Side.BUY, stop=95), order(Side.BUY, 2, 110),
                 order(Side.SELL, 1, 120), order(Side.SELL, 4, 95),
                 order(Side.BUY, 2, 150, stop=140)]:
        broker.submit(item)
        report = broker.reconcile("test")
        assert report["status"] == "matched", report
        assert report["issueCount"] == 0
        assert abs(Decimal(report["expectedCash"]) - broker.get_margin("test").cash_balance) < Decimal("0.00000001")
    assert report["fills"] == 5
    assert report["costRows"] > 5


@pytest.mark.parametrize("statement,expected", [
    ("UPDATE paper_accounts SET cash_balance='123' WHERE tenant_id='test'", "cash_mismatch"),
    ("UPDATE paper_positions SET quantity=9 WHERE tenant_id='test'", "position_quantity_mismatch"),
    ("UPDATE paper_positions SET average_price='99' WHERE tenant_id='test'", "position_average_mismatch"),
    ("UPDATE paper_positions SET stop_price='50' WHERE tenant_id='test'", "position_protection_mismatch"),
    ("DELETE FROM paper_positions WHERE tenant_id='test'", "position_presence_mismatch"),
    ("UPDATE paper_ledger SET notional='1' WHERE tenant_id='test'", "invalid_fill_geometry"),
    ("UPDATE paper_ledger SET status='CANCELLED' WHERE tenant_id='test'", "unsupported_order_status"),
    ("UPDATE paper_cost_ledger SET order_id='absent' WHERE tenant_id='test'", "orphan_cost"),
    ("UPDATE paper_accounts SET cash_balance='NaN' WHERE tenant_id='test'", "invalid_numeric_record"),
])
def test_corruption_is_reported_without_repair(tmp_path, statement, expected):
    broker = PaperBrokerService(tmp_path / "corrupt.db")
    broker.buy(order(Side.BUY, stop=95))
    broker._connection.execute(statement)
    broker._connection.commit()
    before = tuple(broker._connection.iterdump())
    report = broker.reconcile("test")
    assert report["status"] == "mismatch"
    assert expected in [issue["code"] for issue in report["issues"]]
    assert tuple(broker._connection.iterdump()) == before


def test_reconciliation_is_tenant_scoped_and_fails_missing_account(tmp_path):
    broker = PaperBrokerService(tmp_path / "tenants.db")
    broker.buy(order(Side.BUY, tenant="other"))
    broker._connection.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='other'")
    broker._connection.commit()
    broker.get_margin("default")
    assert broker.reconcile("default")["status"] == "matched"
    assert broker.reconcile("other")["status"] == "mismatch"
    assert broker.reconcile("missing")["issues"][0]["code"] == "account_missing"
