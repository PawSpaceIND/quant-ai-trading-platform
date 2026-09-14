"""Synthetic account using the real paper broker, tracker, reconciliation and telemetry."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.friction import FeeSchedule, MarketFrictionModel
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.reconciliation import reconcile_paper
from quant_ai.execution.session import MarketState
from quant_ai.execution.telemetry import PilotTelemetry

BASE = datetime(2000, 1, 1, 4, 1, tzinfo=timezone.utc)


def account(directory: Path, now=BASE, *, execution_times=None):
    model = MarketFrictionModel(
        fee_schedule=replace(FeeSchedule.zero(), name="synthetic_fee", india_exchange_rate=Decimal(".001")),
        gamma=Decimal(0), spread_atr_multiplier=Decimal(1), max_half_spread_fraction=Decimal(".01"),
        max_slippage_fraction=Decimal(0), fixed_slippage_bps=Decimal(10),
    )
    broker = PaperBrokerService(directory / "paper.sqlite", starting_capital=Decimal(10000), friction_model=model)
    instruments = tuple(Instrument(symbol, Market.INDIA, asset, "INR", "NSE") for symbol, asset in [
        ("TCS", AssetClass.EQUITY), ("INFY", AssetClass.EQUITY), ("NIFTY", AssetClass.ETF)])
    trades = [("TCS", Side.BUY, 4, "100"), ("TCS", Side.BUY, 2, "120"),
              ("TCS", Side.SELL, 3, "130"), ("INFY", Side.BUY, 2, "50"),
              ("INFY", Side.SELL, 2, "45"), ("NIFTY", Side.BUY, 1, "80")]
    for i, (symbol, side, quantity, price) in enumerate(trades):
        broker.set_friction_context(None, execution_time=execution_times[i] if execution_times else now - timedelta(seconds=20-i))
        with patch("quant_ai.execution.paper_ledger.uuid4", return_value=SimpleNamespace(hex=f"{i:016x}")):
            intent = OrderIntent(symbol, Market.INDIA, side, quantity, Decimal(price), "SYNTHETIC", AssetClass.ETF if symbol == "NIFTY" else AssetClass.EQUITY)
            (broker.buy if side == Side.BUY else broker.sell)(intent)
    ticks = {symbol: SimpleNamespace(last_price=Decimal(price), timestamp=now) for symbol, price in [("TCS", "125"), ("INFY", "45"), ("NIFTY", "79")]}
    feed = SimpleNamespace(latest_tick=lambda instrument: ticks[instrument.symbol])
    tracker = PortfolioTracker(broker, feed)
    scheduler = SimpleNamespace(calendar=SimpleNamespace(state=lambda *args: MarketState.REGULAR_HOURS),
        last_run_at=None, pipeline=SimpleNamespace(news=None, macro=None, fundamentals=None,
            runtime=SimpleNamespace(max_open_positions=4)))
    daemon = SimpleNamespace(tracker=tracker, tenant_id="default", instruments=instruments,
        scheduler=scheduler, strategy_manifest=None, trade_evidence=None, reconciliation=None,
        kill_switch=SimpleNamespace(engaged=False, reason=None), plan=SimpleNamespace(
            max_daily_loss_fraction=Decimal(".02"), max_gross_exposure_fraction=Decimal(".6"), max_drawdown_fraction=Decimal(".1")))
    # This accounting-only fixture supplies the same read-only protection report to
    # telemetry; daemon fault/restart behavior is exercised by the real-factory tests.
    def check_protection(now):
        daemon.protection_coverage = broker.protection_coverage("default", now)
        complete = daemon.protection_coverage["status"] == "complete"
        if not complete:
            daemon.kill_switch.engaged = True
            daemon.kill_switch.reason = "paper_position_protection_incomplete"
        return complete
    daemon.check_protection_coverage = check_protection
    return broker, PilotTelemetry(daemon), ticks


def fixture(directory: Path):
    broker, telemetry, ticks = account(directory)
    try:
        fresh = telemetry.publish(BASE)
        stale = telemetry.publish(BASE + timedelta(seconds=180))
        ticks["TCS"].timestamp = BASE + timedelta(seconds=181)
        partial = telemetry.publish(BASE + timedelta(seconds=181))
        result = {
            "account": dict(broker._connection.execute("SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id='default'").fetchone()),
            "fills": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_ledger ORDER BY id")],
            "costs": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_cost_ledger ORDER BY id")],
            "positions": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_positions ORDER BY symbol")],
            "valuations": {"fresh": fresh, "stale": stale, "partial": partial},
            "reconciliation": reconcile_paper(broker._connection, "default"),
        }
        result["reconciliation"].pop("checkedAt")
        return result
    finally:
        broker._connection.close()


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        (Path(__file__).parent / "fixtures" / "paper-contribution.json").write_text(json.dumps(fixture(Path(folder)), indent=2) + "\n")
