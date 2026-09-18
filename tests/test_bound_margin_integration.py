"""Synthetic integration arithmetic only; not MCX admission or real contract-note proof."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Instrument, InstrumentBoundOrderIntent, Market, Side
from quant_ai.execution.derivative_margin import ContractMarginRequirement, DerivativeMarginSource
from quant_ai.execution.friction import FrictionResult
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ProtectiveExitEngine

D = Decimal
NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)


class SyntheticNoFees:
    def evaluate(self, order, _context):
        return FrictionResult(order.reference_price, order.reference_price, D(0), D(0), ())


def contract():
    return Instrument("GOLD26OCTFUT", Market.INDIA, AssetClass.METAL, "INR", "MCX",
                      metadata={"source": "synthetic integration fixture"},
                      expiry=date(2026, 10, 30), lot_size=10, tick_size=D(1), underlying="GOLD")


def order(side=Side.BUY, price="5000"):
    item = contract()
    return InstrumentBoundOrderIntent(item.symbol, item.market, side, 10, D(price),
                                      "synthetic", item.asset_class, "tenant", D(4950), D(5200), item)


def source():
    return DerivativeMarginSource((ContractMarginRequirement(
        symbol=contract().symbol, market=Market.INDIA, asset_class=AssetClass.METAL,
        lot_size=10, span_per_lot=D(8000), exposure_per_lot=D(2000),
        source="synthetic margin: never production evidence", observed_at=NOW,
    ),), max_age_seconds=300)


def opened(path):
    broker = PaperBrokerService(path, starting_capital=D(100000),
                                friction_model=SyntheticNoFees(), margin_source=source())
    broker.set_friction_context(None, execution_time=NOW)
    fill = broker.submit(order())
    assert broker.get_margin("tenant").cash_balance == D(90000)
    assert broker.get_margin("tenant").gross_position_value == D(10000)
    assert broker.bound_instrument_for_fill(fill.order_id, "tenant") == contract()
    assert broker.ledger_entries("tenant")[0].margin_change == D(10000)
    assert broker.reconcile("tenant")["status"] == "matched"
    return broker


def test_bound_futures_restart_releases_collateral_not_notional_without_margin_source(tmp_path):
    path = tmp_path / "bound-margin.sqlite"
    broker = opened(path)
    broker.close()
    broker = PaperBrokerService(path, friction_model=SyntheticNoFees(), margin_source=None)
    broker.set_friction_context(None, execution_time=NOW + timedelta(days=1))
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="derivative_margin_requirement_unavailable"):
        broker.submit(order())
    assert tuple(broker._connection.iterdump()) == before
    fill = broker.submit(order(Side.SELL, "5100"))
    assert broker.get_margin("tenant").cash_balance == D(101000)
    assert broker.get_margin("tenant").gross_position_value == D(0)
    assert broker.ledger_entries("tenant")[-1].margin_change == D(-10000)
    assert broker.bound_instrument_for_fill(fill.order_id, "tenant") == contract()
    assert broker.reconcile("tenant")["status"] == "matched"
    broker.close()


def test_bound_margin_identity_substitution_refuses_and_protection_still_closes(tmp_path):
    broker = opened(tmp_path / "protected-margin.sqlite")
    before = tuple(broker._connection.iterdump())
    changed = replace(contract(), expiry=date(2026, 11, 30))
    with pytest.raises(ValueError, match="position_instrument_identity_mismatch"):
        broker.submit(replace(order(Side.SELL), instrument=changed))
    assert tuple(broker._connection.iterdump()) == before
    broker.margin_source = None
    results = ProtectiveExitEngine(broker, lambda _: D(4900), tenant_id="tenant").evaluate(NOW)
    assert len(results) == 1 and results[0].filled
    assert broker.get_margin("tenant").cash_balance == D(99000)
    assert broker.bound_instrument_for_fill(results[0].order_id, "tenant") == contract()
    assert broker.reconcile("tenant")["status"] == "matched"
    broker.close()
