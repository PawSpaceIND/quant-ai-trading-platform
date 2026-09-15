import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.ledger_integrity import PaperLedgerDataError
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ProtectiveExitEngine, market_feed_mark_resolver
from quant_ai.governance.directives import FounderDirectives
from quant_ai.marketdata.ticker_stream import LiveTick

INSTRUMENTS = tuple(Instrument(s, Market.INDIA, AssetClass.EQUITY, "INR", "NSE") for s in ["INFY", "TCS"])


def order(symbol="INFY", side=Side.BUY):
    return OrderIntent(symbol, Market.INDIA, side, 2, D(100), "synthetic", tenant_id="pilot", stop_price=D(95))


def seed(broker):
    for symbol in ["INFY", "TCS"]:
        broker.buy(order(symbol))


@pytest.mark.parametrize("column,value,code", [
    *(('quantity', value, 'invalid_position_quantity') for value in [1.5, 0, -1, 'broken', 'NaN', 2**53+1]),
    *(('average_price', value, 'invalid_position_average') for value in ['broken', 'NaN', 'sNaN', 'Infinity', '0', '-1', '1e999']),
    ('symbol', '', 'invalid_position_symbol'),
    ('market', 'unknown', 'invalid_position_identity'),
    ('asset_class', 'unknown', 'invalid_position_identity'),
])
def test_invalid_position_cannot_be_coerced_or_hide_valid_other_exit(tmp_path, column, value, code):
    broker = PaperBrokerService(tmp_path / "ledger.db", slippage_bps=D(0))
    seed(broker)
    with broker._connection:
        broker._connection.execute(f"UPDATE paper_positions SET {column}=? WHERE symbol='INFY'", (value,))
    before = tuple(broker._connection.iterdump())
    report = broker.protection_coverage("pilot")
    assert report["status"] == "invalid" and report["invalidPositionCount"] == 1
    assert code in [i["code"] for i in report["issues"]]
    with pytest.raises(PaperLedgerDataError):
        broker.get_positions("pilot")
    with pytest.raises(PaperLedgerDataError):
        broker.get_margin("pilot")
    with pytest.raises(ValueError):
        broker.sell(order(side=Side.SELL))
    assert tuple(broker._connection.iterdump()) == before
    exits = ProtectiveExitEngine(broker, lambda _: D(90), tenant_id="pilot").evaluate()
    assert [(e.symbol, e.quantity, e.filled) for e in exits] == [("TCS", 2, True)]
    remaining = broker._connection.execute("SELECT * FROM paper_positions").fetchall()
    assert len(remaining) == 1 and remaining[0][column] == value
    assert len(broker.ledger_entries("pilot")) == 3


@pytest.mark.parametrize("column,value", [
    *(('cash_balance', v) for v in ['broken', 'NaN', 'sNaN', 'Infinity', '1e999']),
    *(('starting_capital', v) for v in ['broken', 'NaN', 'Infinity', '0', '-1']),
])
def test_invalid_account_cannot_receive_fills_or_consume_a_retry_key(tmp_path, column, value):
    broker = PaperBrokerService(tmp_path / "ledger.db", slippage_bps=D(0))
    seed(broker)
    with broker._connection:
        broker._connection.execute(f"UPDATE paper_accounts SET {column}=?", (value,))
    before = tuple(broker._connection.iterdump())
    proof = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill"}
    for side in [Side.BUY, Side.SELL]:
        with pytest.raises(PaperLedgerDataError):
            broker.submit_with_evidence(order(side=side), proof, "retryable")
        assert tuple(broker._connection.iterdump()) == before
    exits = ProtectiveExitEngine(broker, lambda _: D(90), tenant_id="pilot").evaluate()
    assert len(exits) == 2 and not any(e.filled for e in exits)
    assert tuple(broker._connection.iterdump()) == before


def runner(tmp_path):
    return build_ghost_runner(
        zerodha_api_key="synthetic", zerodha_access_token="synthetic", zerodha_instrument_tokens=(1, 2),
        zerodha_symbol_by_token={1: "INFY", 2: "TCS"}, ib_client=SimpleNamespace(), ib_contracts=(),
        include_ibkr=False, database=tmp_path / "ledger.db", tenant_id="pilot",
        log_path=tmp_path / "events.jsonl", xai_directory=tmp_path / "proofs", halt_file=tmp_path / "HALT",
        directives=FounderDirectives(watchlist=INSTRUMENTS), pilot_mode=True,
    )


def ticks(run, now, price="100"):
    for symbol in ["INFY", "TCS"]:
        run.daemon.tracker.market_feed.buffer.put(LiveTick(symbol, D(price), D(100), None, None, now, "synthetic"))


def runtime(broker):
    return json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'").fetchone()[0])


@pytest.mark.parametrize("column,value", [("quantity", "broken"), ("quantity", 1.5), ("average_price", "NaN"), ("average_price", "broken")])
def test_damaged_position_survives_restart_without_blocking_valid_exits_or_publishing_partial_equity(tmp_path, column, value):
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    seed(broker)
    with broker._connection:
        broker._connection.execute(f"UPDATE paper_positions SET {column}=? WHERE symbol='INFY'", (value,))
    broker.close()
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    assert run.daemon.kill_switch.engaged
    now = datetime.now(timezone.utc)
    ticks(run, now, "90")
    run.daemon.protection_tick(now)
    state = runtime(broker)
    assert state["status"] == "running" and state["halted"]
    assert state["valuation"]["status"] == "unavailable"
    assert state["protectionCoverage"]["status"] == "invalid"
    assert state["reconciliation"]["status"] == "mismatch"
    assert [(e.symbol, e.filled) for e in run.daemon.protective_exits] == [("TCS", True)]
    snapshot = json.loads(broker._connection.execute("SELECT payload FROM paper_live_valuations").fetchone()[0])
    assert snapshot["status"] == "invalid" and snapshot["totalEquity"] is None
    assert snapshot["cash"] is None and not snapshot["qualifyingSession"]
    assert not snapshot["strategyObservation"]["eligible"]
    assert broker._connection.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
    assert broker._connection.execute(f"SELECT {column} FROM paper_positions").fetchone()[0] == value


def test_nonfinite_cash_halts_failed_exits_and_reports_unavailable_after_restart(tmp_path):
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    seed(broker)
    with broker._connection:
        broker._connection.execute("UPDATE paper_accounts SET cash_balance='NaN'")
    broker.close()
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    now = datetime.now(timezone.utc)
    ticks(run, now, "90")
    run.daemon.protection_tick(now)
    assert runtime(broker)["valuation"]["reason"] == "invalid_account_cash"
    assert runtime(broker)["halted"]
    assert len(run.daemon.protective_exits) == 2 and not any(e.filled for e in run.daemon.protective_exits)
    assert len(broker.ledger_entries("pilot")) == 2
    assert broker._connection.execute("SELECT cash_balance FROM paper_accounts").fetchone()[0] == "NaN"


def test_unavailable_minute_is_not_erased_by_repair_and_next_minute_can_resume_valuation(tmp_path):
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    seed(broker)
    now = datetime(2026, 9, 15, 6, 0, 5, tzinfo=timezone.utc)
    run.daemon.clock = lambda: now
    ticks(run, now)
    run.daemon.protection_tick(now)
    original = broker._connection.execute("SELECT average_price FROM paper_positions WHERE symbol='INFY'").fetchone()[0]
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET average_price='broken' WHERE symbol='INFY'")
    run.daemon.protection_tick(now + timedelta(seconds=10))
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET average_price=? WHERE symbol='INFY'", (original,))
    run.daemon.protection_tick(now + timedelta(seconds=20))
    rows = broker._connection.execute("SELECT payload FROM paper_live_valuations ORDER BY timestamp").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["status"] == "invalid"
    run.daemon.protection_tick(now + timedelta(minutes=1))
    rows = [json.loads(r[0]) for r in broker._connection.execute("SELECT payload FROM paper_live_valuations ORDER BY timestamp")]
    assert [r["status"] for r in rows] == ["invalid", "ok"]
    assert rows[0]["totalEquity"] is None and rows[1]["totalEquity"] > 0
    assert runtime(broker)["valuation"]["status"] == "available"
    assert runtime(broker)["halted"]


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", "0"])
def test_invalid_live_mark_is_not_used_for_pnl_or_protective_fill(tmp_path, monkeypatch, price):
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    seed(broker)
    now = datetime.now(timezone.utc)
    ticks(run, now)
    # Exercise the final valuation boundary even if an upstream adapter is replaced
    # or returns a malformed tick instead of rejecting it in its own constructor.
    monkeypatch.setattr(run.daemon.tracker.market_feed, "latest_tick",
        lambda _: SimpleNamespace(last_price=D(price), timestamp=now))
    run.daemon.protection_tick(now)
    assert runtime(broker)["valuation"]["reason"] == "invalid_market_mark"
    assert runtime(broker)["halted"]
    assert not run.daemon.protective_exits
    assert len(broker.ledger_entries("pilot")) == 2


def test_nonfinite_buffered_price_does_not_crash_callback_or_suppress_other_symbol_exit(tmp_path):
    run = runner(tmp_path)
    broker = run.daemon.tracker.broker
    seed(broker)
    now = datetime.now(timezone.utc)
    run.daemon.tracker.market_feed.buffer.put(LiveTick("INFY", D("NaN"), D(100), None, None, now, "synthetic"))
    run.daemon.tracker.market_feed.buffer.put(LiveTick("TCS", D(90), D(100), None, None, now, "synthetic"))
    run.daemon.protection_tick(now)
    assert [(e.symbol, e.filled) for e in run.daemon.protective_exits] == [("TCS", True)]
    # The malformed tick is now rejected before it reaches the latest-price cache.
    # No INFY quote exists; its explicit fallback is unqualified, while TCS exits.
    state = runtime(broker)
    assert state["marketDataIntegrity"]["rejected"]["invalid_tick_values"] == 1
    snapshot = json.loads(broker._connection.execute("SELECT payload FROM paper_live_valuations").fetchone()[0])
    assert snapshot["status"] == "degraded" and not snapshot["allMarksFresh"] and not snapshot["qualifyingSession"]
    assert broker.get_positions("pilot")[0].symbol == "INFY"


def test_quantity_beyond_cross_runtime_exact_range_is_rejected_atomically(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    before = tuple(broker._connection.iterdump())
    with pytest.raises(PaperLedgerDataError):
        broker.buy(replace(order(), quantity=2**53))
    assert tuple(broker._connection.iterdump()) == before


@pytest.mark.parametrize("offset", [1, 121, -121])
def test_protection_rejects_future_or_old_ticks_even_when_reader_admits_them(tmp_path, offset):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    seed(broker)
    now = datetime.now(timezone.utc)
    reader = SimpleNamespace(market_data_status=lambda symbol, _: (
        LiveTick(symbol, D(90), D(100), None, None, now + timedelta(seconds=offset), "synthetic"), None))
    resolver = market_feed_mark_resolver(None, lambda _: INSTRUMENTS[0], reader, lambda: now)
    assert ProtectiveExitEngine(broker, resolver, tenant_id="pilot").evaluate() == ()
    assert len(broker.ledger_entries("pilot")) == 2
