import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Thread
from types import SimpleNamespace

import pytest

from quant_ai.daemon import DaemonRunner, build_ghost_runner
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.pilot import validate_pilot_instruments
from quant_ai.marketdata.ticker_stream import LiveTick

INSTRUMENT = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def test_pilot_scope_survives_restart_and_blocks_mixed_money(tmp_path):
    db = tmp_path / "ledger.db"
    broker = PaperBrokerService(db)
    broker.configure_pilot((INSTRUMENT,), "pilot")
    broker = PaperBrokerService(db)
    with pytest.raises(ValueError, match="out_of_scope"):
        broker.buy(OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot"))
    with pytest.raises(ValueError, match="not_supported"):
        validate_pilot_instruments((Instrument("GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX"),))
    assert broker.get_margin("pilot").cash_balance == Decimal(100000)


def test_legacy_mixed_currency_account_cannot_be_relabeled(tmp_path):
    broker = PaperBrokerService(tmp_path / "legacy.db")
    broker.buy(OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot"))
    with pytest.raises(ValueError, match="out_of_scope|currency_ambiguous"):
        broker.configure_pilot((INSTRUMENT,), "pilot")


def runner_for(tmp_path):
    return build_ghost_runner(
        zerodha_api_key="test", zerodha_access_token="test", zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: "INFY"}, ib_client=SimpleNamespace(), ib_contracts=(),
        include_ibkr=False, database=tmp_path / "ledger.db", tenant_id="pilot",
        log_path=tmp_path / "events.jsonl", xai_directory=tmp_path / "proofs",
        halt_file=tmp_path / "HALT", directives=FounderDirectives(watchlist=(INSTRUMENT,)),
        pilot_mode=True,
    )


def publish_tick(runner, price, now):
    buffer = runner.daemon.tracker.market_feed.buffer
    buffer.put(LiveTick("INFY", Decimal(price), Decimal(100), None, None, now, "test"))


def test_protection_runs_during_blocking_analysis_and_persists_ui_values(tmp_path):
    runner = runner_for(tmp_path)
    now = datetime.now(timezone.utc)
    publish_tick(runner, "100", now)
    broker = runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(100), "test",
                           tenant_id="pilot", stop_price=Decimal(95), take_profit_price=Decimal(120)))
    publish_tick(runner, "90", now)
    # A synchronous provider can block the asyncio loop. The protection worker must
    # still liquidate, without awaiting that loop or an LLM response.
    runner.protection_interval = .01
    thread = Thread(target=runner._protect)
    thread.start()
    try:
        asyncio.run(_blocked_event_loop())
        assert not broker.get_positions("pilot")
        payload = json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime").fetchone()[0])
        assert payload["mode"] == "paper"
        snapshots = broker._connection.execute("SELECT payload FROM paper_live_valuations").fetchall()
        assert snapshots
        assert json.loads(snapshots[-1][0])["holdings"] == []
    finally:
        runner.request_stop()
        thread.join(2)
    assert not thread.is_alive()


async def _blocked_event_loop():
    time.sleep(.15)  # noqa: ASYNC251 - deliberate blocked-loop regression


def test_operator_halt_is_acknowledged_without_analysis(tmp_path):
    runner = runner_for(tmp_path)
    runner.daemon.halt_file.write_text("review")
    runner.daemon.protection_tick()
    assert runner.daemon.kill_switch.engaged
    restarted = runner_for(tmp_path)
    assert restarted.daemon.kill_switch.engaged
    runner.daemon.halt_file.unlink()
    restarted.daemon.protection_tick()
    assert not restarted.daemon.kill_switch.engaged


def test_snapshot_freshness_tracks_stale_positions(tmp_path):
    runner = runner_for(tmp_path)
    now = datetime.now(timezone.utc)
    publish_tick(runner, "100", now - timedelta(minutes=5))
    broker = runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot", stop_price=Decimal(95)))
    snapshot = runner.daemon.telemetry.publish(now)
    assert snapshot["status"] == "degraded"
    assert not snapshot["allMarksFresh"]
    assert snapshot["holdings"][0]["markSource"] == "stale_or_entry_fallback"


def test_protection_interval_must_be_positive():
    with pytest.raises(ValueError):
        DaemonRunner(SimpleNamespace(), (), protection_interval=0)


def test_pre_submit_rechecks_price_and_halt_after_analysis(tmp_path):
    runner = runner_for(tmp_path)
    now = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    runner.daemon.clock = lambda: now
    proposal = SimpleNamespace(symbol="INFY", side=Side.BUY, reference_price=Decimal(100))
    publish_tick(runner, "100", now)
    assert runner.daemon._pilot_pre_submit(proposal) is None
    publish_tick(runner, "101", now)
    assert runner.daemon._pilot_pre_submit(proposal) == "pilot_price_moved_during_analysis"
    publish_tick(runner, "100", now - timedelta(minutes=5))
    assert runner.daemon._pilot_pre_submit(proposal) == "pilot_stale_entry_price"
    runner.daemon.halt_file.write_text("review")
    assert runner.daemon._pilot_pre_submit(proposal) == "pilot_halted"
    # Exits bypass the entry halt, but cannot invent fills at old prices.
    proposal.side = Side.SELL
    assert runner.daemon._pilot_pre_submit(proposal) == "pilot_stale_entry_price"
    publish_tick(runner, "100", now)
    assert runner.daemon._pilot_pre_submit(proposal) is None


def test_reconciliation_failure_persists_halt_and_does_not_disable_covered_exit(tmp_path):
    runner = runner_for(tmp_path)
    now = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    runner.daemon.clock = lambda: now
    publish_tick(runner, "100", now)
    broker = runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot", stop_price=Decimal(95)))
    broker._connection.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='pilot'")
    broker._connection.commit()
    proposal = SimpleNamespace(symbol="INFY", side=Side.BUY, reference_price=Decimal(100))
    assert runner.daemon._pilot_pre_submit(proposal) == "pilot_reconciliation_failed"
    assert runner.daemon.kill_switch.engaged
    assert runner.daemon.reconciliation["status"] == "mismatch"
    restarted = runner_for(tmp_path)
    assert restarted.daemon.kill_switch.engaged
    proposal.side = Side.SELL
    assert runner.daemon._pilot_pre_submit(proposal) is None
    assert broker.reconcile("pilot")["status"] == "mismatch"


def test_reconciliation_telemetry_cannot_certify_later_fill(tmp_path):
    runner = runner_for(tmp_path)
    broker = runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot", stop_price=Decimal(95)))
    runner.daemon.telemetry.publish(datetime.now(timezone.utc))
    state = json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])
    assert state["reconciliation"]["status"] == "outdated"
    assert runner.daemon._reconcile_pilot()
    runner.daemon.telemetry.publish(datetime.now(timezone.utc))
    state = json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])
    assert state["reconciliation"]["status"] == "matched"


def test_direct_pilot_buy_cannot_bypass_reconciliation(tmp_path):
    broker = PaperBrokerService(tmp_path / "direct.db")
    broker.configure_pilot((INSTRUMENT,), "pilot")
    buy = OrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot", stop_price=Decimal(95))
    broker.buy(buy)
    broker._connection.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='pilot'")
    broker._connection.commit()
    before = len(broker.ledger_entries("pilot"))
    with pytest.raises(ValueError, match="pilot_reconciliation_failed"):
        broker.buy(buy)
    assert len(broker.ledger_entries("pilot")) == before
    broker.sell(OrderIntent("INFY", Market.INDIA, Side.SELL, 1, Decimal(100), "test", tenant_id="pilot"))
    assert not broker.get_positions("pilot")


def test_trade_evidence_follows_checked_ledger_and_never_counts_open_fill_as_trade(tmp_path):
    runner = runner_for(tmp_path)
    broker = runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100), "test", tenant_id="pilot", stop_price=Decimal(95)))
    runner.daemon.telemetry.publish(datetime.now(timezone.utc))
    state=json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])
    assert state["tradeEvidence"] is None
    assert runner.daemon._reconcile_pilot()
    runner.daemon.telemetry.publish(datetime.now(timezone.utc))
    state=json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])
    assert state["tradeEvidence"]["fillCount"] == 1
    assert state["tradeEvidence"]["summary"]["completedTrades"] == 0
    assert "episodes" not in state["tradeEvidence"]


def test_trade_evidence_refreshes_after_fee_correction_without_new_fill(tmp_path):
    runner=runner_for(tmp_path)
    broker=runner.daemon.tracker.broker
    broker.buy(OrderIntent("INFY",Market.INDIA,Side.BUY,1,Decimal(100),"test",tenant_id="pilot", stop_price=Decimal(95)))
    runner.daemon._reconcile_pilot()
    before=runner.daemon.trade_evidence
    row=broker._connection.execute("SELECT id,amount FROM paper_cost_ledger WHERE tenant_id='pilot' AND cash_debit=1 ORDER BY id LIMIT 1").fetchone()
    cash=broker.get_margin("pilot").cash_balance
    broker._connection.execute("UPDATE paper_cost_ledger SET amount=? WHERE id=?",(str(Decimal(row['amount'])+1),row['id']))
    broker._connection.execute("UPDATE paper_accounts SET cash_balance=? WHERE tenant_id='pilot'",(str(cash-1),))
    broker._connection.commit()
    assert runner.daemon._reconcile_pilot()
    after=runner.daemon.trade_evidence
    assert after['ledgerId']==before['ledgerId']
    assert after['sourceSha256']!=before['sourceSha256']
    assert Decimal(after['summary']['openCashFees'])==Decimal(before['summary']['openCashFees'])+1
