from decimal import Decimal as D

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.validation.trade_evidence import build_trade_evidence


def account(tmp_path):
    broker = PaperBrokerService(tmp_path / "trades.db", slippage_bps=D(0))
    broker.configure_pilot((Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),), "pilot")
    broker.get_margin("pilot")
    return broker


def fill(broker, side, quantity, price):
    return broker.submit(OrderIntent("INFY", Market.INDIA, side, quantity, D(price), "test", tenant_id="pilot",
                                     stop_price=D(95) if side == Side.BUY else None))


def report(broker):
    return build_trade_evidence(broker._connection, "pilot")


def test_scaling_and_partial_exits_count_one_completed_episode(tmp_path):
    b = account(tmp_path)
    fill(b, Side.BUY, 2, 100)
    fill(b, Side.BUY, 1, 110)
    fill(b, Side.SELL, 1, 120)
    open_report = report(b)
    assert open_report["summary"]["completedTrades"] == 0
    assert open_report["summary"]["openEpisodes"] == 1
    assert open_report["summary"]["expectancy"] is None
    fill(b, Side.SELL, 2, 105)
    r = report(b)
    assert r["fillCount"] == 4
    assert r["summary"]["completedTrades"] == 1
    assert D(r["summary"]["netPnl"]) == 20
    assert len(r["episodes"][0]["orderIds"]) == 4


def test_fees_can_turn_a_gross_winner_into_a_net_loser(tmp_path):
    b = account(tmp_path)
    f = fill(b, Side.BUY, 1, 100)
    # Isolated accounting fixture: add an explicit cash-debit charge and its cash effect.
    b._connection.execute("INSERT INTO paper_cost_ledger(order_id,tenant_id,code,amount,cash_debit,created_at) VALUES(?,?,?,?,?,?)", (f.order_id,"pilot","TEST_FEE","2",1,"2026-09-14T10:00:00+00:00"))
    cash = b.get_margin("pilot").cash_balance
    b._connection.execute("UPDATE paper_accounts SET cash_balance=? WHERE tenant_id='pilot'", (str(cash-D(2)),))
    b._connection.commit()
    fill(b, Side.SELL, 1, 101)
    r=report(b)
    assert D(r["episodes"][0]["grossPnl"]) == 1
    assert D(r["summary"]["netPnl"]) == -1
    assert r["summary"]["losses"] == 1
    assert D(r["summary"]["closedCashFees"]) == 2


def test_reentry_is_a_new_trade_and_no_loss_is_not_infinite_certainty(tmp_path):
    b=account(tmp_path)
    for _ in range(2):
        fill(b,Side.BUY,1,100);fill(b,Side.SELL,1,110)
    r=report(b)
    assert r["summary"]["completedTrades"] == 2
    assert r["summary"]["profitFactor"] is None
    assert r["summary"]["profitFactorState"] == "no_observed_losses"
    fill(b,Side.BUY,1,100);fill(b,Side.SELL,1,90)
    r=report(b)
    assert D(r["summary"]["profitFactor"]) == 2
    assert D(r["summary"]["expectancy"]) == D(10)/D(3)


def test_unreconciled_or_unscoped_accounts_do_not_publish_metrics(tmp_path):
    b=account(tmp_path)
    b._connection.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='pilot'")
    b._connection.commit()
    assert report(b)["reason"] == "paper_account_not_reconciled"
    b.get_margin("other")
    assert build_trade_evidence(b._connection,"other")["reason"] == "verified_INR_cash_scope_required"


def test_reentry_time_cannot_precede_prior_episode(tmp_path):
    b=account(tmp_path)
    fill(b,Side.BUY,1,100);fill(b,Side.SELL,1,110);fill(b,Side.BUY,1,100)
    b._connection.execute("UPDATE paper_ledger SET created_at='2000-01-01T00:00:00+00:00' WHERE id=(SELECT MAX(id) FROM paper_ledger)")
    b._connection.commit()
    assert report(b)["reason"] == "invalid_fill_timestamps"
