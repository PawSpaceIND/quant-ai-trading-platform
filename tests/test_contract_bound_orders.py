from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256

import pytest

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    InstrumentBoundOrderIntent,
    Market,
    OrderIntent,
    Side,
)
from quant_ai.execution.friction import FrictionResult
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.protective_exits import ProtectiveExitEngine
from quant_ai.instruments.identity import (
    canonical_instrument_identity,
    instrument_from_identity,
    instrument_identity_payload,
    instrument_identity_sha256,
)
from quant_ai.marketdata.feed import SandboxMarketDataFeed
from quant_ai.operations.idempotency import order_idempotency_key

D = Decimal
NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)


class ZeroFriction:
    @staticmethod
    def evaluate(order, context):
        del context
        return FrictionResult(order.reference_price, order.reference_price, D(0), D(0), ())


def mcx_contract(*, expiry=date(2026, 10, 5), symbol="GOLD26OCTFUT") -> Instrument:
    return Instrument(
        symbol, Market.INDIA, AssetClass.METAL, "INR", "MCX", True,
        {"providerInstrumentId": "123456", "tradingClass": "GOLD"},
        expiry, 10, D(1), "GOLD",
    )


def bound_order(
    instrument: Instrument | None = None, *, side=Side.BUY, quantity=20,
    price=None, tenant="tenant",
) -> InstrumentBoundOrderIntent:
    instrument = instrument or mcx_contract()
    price = D(5000) if price is None else price
    return InstrumentBoundOrderIntent(
        instrument.symbol, instrument.market, side, quantity, price, "strategy",
        instrument.asset_class, tenant, D(4900), D(5200), instrument,
    )


def cash_instrument() -> Instrument:
    return Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE",
                      metadata={"providerInstrumentId": "cash-fixture"})


def bound_cash_order(*, side=Side.BUY, tenant="tenant") -> InstrumentBoundOrderIntent:
    return InstrumentBoundOrderIntent(**vars(cash_order(side=side, tenant=tenant)),
                                      instrument=cash_instrument())


def cash_order(*, side=Side.BUY, tenant="cash") -> OrderIntent:
    return OrderIntent(
        "INFY", Market.INDIA, side, 2, D(100), "cash-strategy",
        AssetClass.EQUITY, tenant, D(95), D(110),
    )


def test_canonical_identity_round_trips_and_hashes_every_contract_field() -> None:
    instrument = mcx_contract()
    raw = canonical_instrument_identity(instrument)
    payload = json.loads(raw)
    assert payload == instrument_identity_payload(instrument)
    assert payload["exchange"] == "MCX"
    assert payload["expiry"] == "2026-10-05"
    assert payload["lotSize"] == 10
    assert payload["tickSize"] == "1"
    assert payload["underlying"] == "GOLD"
    assert payload["metadata"]["providerInstrumentId"] == "123456"
    assert instrument_from_identity(raw) == instrument
    assert len(instrument_identity_sha256(instrument)) == 64


def test_bound_order_refuses_missing_mismatched_or_fractional_contract_identity() -> None:
    with pytest.raises(ValueError, match="instrument_bound_order_requires_instrument"):
        InstrumentBoundOrderIntent(
            "GOLD26OCTFUT", Market.INDIA, Side.BUY, 20, D(5000), "strategy",
            AssetClass.METAL, "tenant", D(4900), D(5200), None,
        )
    instrument = mcx_contract()
    with pytest.raises(ValueError, match="instrument_bound_order_identity_mismatch"):
        InstrumentBoundOrderIntent(
            "WRONG", Market.INDIA, Side.BUY, 20, D(5000), "strategy",
            AssetClass.METAL, "tenant", D(4900), D(5200), instrument,
        )
    with pytest.raises(ValueError, match="instrument_bound_order_not_whole_lots"):
        bound_order(instrument, quantity=15)


def test_bound_contract_identity_persists_on_fill_and_position_across_restart(tmp_path) -> None:
    path = tmp_path / "contract.sqlite"
    broker = PaperBrokerService(
        path, starting_capital=D(200000), friction_model=ZeroFriction()
    )
    order = bound_cash_order()
    fill = broker.submit(order)
    expected = order.instrument
    assert expected is not None
    assert broker.bound_instrument_for_fill(fill.order_id, "tenant") == expected
    assert broker.bound_instrument_for_position(
        order.symbol, order.market, order.asset_class, "tenant"
    ) == expected
    stored = broker._connection.execute(
        "SELECT instrument_identity FROM paper_ledger WHERE order_id=?", (fill.order_id,)
    ).fetchone()[0]
    assert stored == canonical_instrument_identity(expected)
    broker.close()

    restarted = PaperBrokerService(
        path, starting_capital=D(200000), friction_model=ZeroFriction()
    )
    assert restarted.bound_instrument_for_fill(fill.order_id, "tenant") == expected
    assert restarted.bound_instrument_for_position(
        order.symbol, order.market, order.asset_class, "tenant"
    ) == expected


def test_bound_position_cannot_change_contract_or_be_exited_unbound(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "identity-lock.sqlite", starting_capital=D(300000),
        friction_model=ZeroFriction(),
    )
    october = cash_instrument()
    broker.submit(bound_cash_order())
    november_identity_same_symbol = replace(october, metadata={"providerInstrumentId": "changed"})
    with pytest.raises(ValueError, match="position_instrument_identity_mismatch"):
        broker.submit(replace(bound_cash_order(), instrument=november_identity_same_symbol))
    unbound_exit = OrderIntent(
        october.symbol, Market.INDIA, Side.SELL, 1, D(101), "strategy",
        AssetClass.EQUITY, "tenant", D(95), D(110),
    )
    with pytest.raises(ValueError, match="bound_position_requires_instrument_identity"):
        broker.submit(unbound_exit)
    assert broker.get_positions("tenant")[0].quantity == 2
    assert broker.submit(replace(bound_cash_order(side=Side.SELL), quantity=1)).status == "FILLED"
    assert broker.get_positions("tenant")[0].quantity == 1


def test_legacy_cash_orders_remain_unbound_and_behavior_is_unchanged(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "legacy-cash.sqlite", friction_model=ZeroFriction()
    )
    fill = broker.submit(cash_order())
    assert broker.bound_instrument_for_fill(fill.order_id, "cash") is None
    assert broker.bound_instrument_for_position(
        "INFY", Market.INDIA, AssetClass.EQUITY, "cash"
    ) is None
    assert broker.submit(cash_order(side=Side.SELL)).status == "FILLED"
    assert broker.get_positions("cash") == ()


def test_legacy_idempotency_bytes_are_unchanged_but_contract_expiry_changes_key():
    order = cash_order()
    legacy = "|".join([order.tenant_id, order.strategy_id, order.market.value,
                       order.symbol, order.side.value, str(order.quantity),
                       str(order.reference_price), "nonce"])
    assert order_idempotency_key(order, "nonce").value == sha256(legacy.encode()).hexdigest()
    october = bound_order()
    november = bound_order(mcx_contract(expiry=date(2026, 11, 5)))
    assert order_idempotency_key(october, "nonce") != order_idempotency_key(november, "nonce")
    assert order_idempotency_key(order, "nonce") != order_idempotency_key(
        InstrumentBoundOrderIntent(**vars(order), instrument=cash_instrument()), "nonce")


def test_order_snapshots_metadata_defensively_and_remains_asdict_compatible():
    source = mcx_contract()
    order = bound_order(source)
    before = order_idempotency_key(order, "nonce")
    source.metadata["providerInstrumentId"] = "substituted-after-decision"
    assert order.instrument.metadata["providerInstrumentId"] == "123456"
    assert order_idempotency_key(order, "nonce") == before
    with pytest.raises(TypeError, match="immutable"):
        order.instrument.metadata["providerInstrumentId"] = "substituted"
    with pytest.raises(TypeError, match="immutable"):
        order.instrument.metadata.update({"providerInstrumentId": "substituted"})
    assert asdict(order)["instrument"]["metadata"]["providerInstrumentId"] == "123456"
    assert replace(order, quantity=10).instrument == order.instrument


@pytest.mark.parametrize("field,value", [
    ("symbol", None), ("currency", "usd"), ("market", "unknown"),
    ("lotSize", True), ("lotSize", 1.5), ("tickSize", "NaN"),
    ("tickSize", "Infinity"), ("tickSize", None), ("metadata", []),
    ("expiry", "bad-date"), ("underlying", 42),
])
def test_malformed_identity_never_coerces_missing_or_wrong_types(field, value):
    payload = instrument_identity_payload(mcx_contract())
    payload[field] = value
    with pytest.raises(ValueError):
        instrument_from_identity(payload)


def test_duplicate_json_identity_fields_are_refused():
    raw = canonical_instrument_identity(mcx_contract())
    duplicate = raw[:-1] + ',"lotSize":1}'
    with pytest.raises(ValueError):
        instrument_from_identity(duplicate)
    assert instrument_identity_sha256(mcx_contract()) == instrument_identity_sha256(
        replace(mcx_contract(), tick_size=D("1.000")))


def test_pilot_full_identity_survives_restart_and_rejects_wrong_exchange(tmp_path):
    path = tmp_path / "pilot.sqlite"
    broker = PaperBrokerService(path, friction_model=ZeroFriction())
    broker.configure_pilot((cash_instrument(),), "tenant")
    broker.close()
    broker = PaperBrokerService(path, friction_model=ZeroFriction())
    wrong = replace(cash_instrument(), exchange="BSE")
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="pilot_order_out_of_scope"):
        broker.submit(replace(bound_cash_order(), instrument=wrong))
    assert tuple(broker._connection.iterdump()) == before
    assert broker.submit(bound_cash_order()).status == "FILLED"
    with pytest.raises(ValueError, match="pilot_existing_position_contract_mismatch"):
        broker.configure_pilot((replace(cash_instrument(), metadata={"new": "identity"}),), "tenant")
    broker.close()


def test_legacy_scope_schema_migrates_explicitly_without_relabeling_positions(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE pilot_scope (tenant_id TEXT PRIMARY KEY, currency TEXT, market TEXT, symbols TEXT)")
        db.execute("INSERT INTO pilot_scope VALUES(?,?,?,?)", ("cash", "INR", "INDIA", json.dumps({"INFY": "EQUITY"})))
    broker = PaperBrokerService(path, friction_model=ZeroFriction())
    # Old cash scope still operates, and explicit configure adds full identity.
    broker.submit(cash_order())
    broker.configure_pilot((cash_instrument(),), "cash")
    assert "instrument_identities" in {row[1] for row in broker._connection.execute("PRAGMA table_info(pilot_scope)")}
    assert broker.bound_instrument_for_position("INFY", Market.INDIA, AssetClass.EQUITY, "cash") is None
    broker.submit(cash_order(side=Side.SELL))
    assert broker.get_positions("cash") == ()
    broker.close()


def test_bound_position_protective_exit_retains_identity_after_restart(tmp_path):
    path = tmp_path / "protection.sqlite"
    broker = PaperBrokerService(path, friction_model=ZeroFriction())
    broker.configure_pilot((cash_instrument(),), "tenant")
    broker.submit(bound_cash_order())
    broker.close()
    broker = PaperBrokerService(path, friction_model=ZeroFriction())
    results = ProtectiveExitEngine(broker, lambda _: D(90), tenant_id="tenant").evaluate(NOW)
    assert len(results) == 1 and results[0].filled
    assert broker.get_positions("tenant") == ()
    assert broker.bound_instrument_for_fill(results[0].order_id, "tenant") == cash_instrument()
    proof = json.loads(broker._connection.execute("SELECT payload FROM paper_protection_evidence").fetchone()[0])
    assert proof["fill"]["instrumentIdentity"] == instrument_identity_payload(cash_instrument())
    assert broker.reconcile("tenant")["status"] == "matched"
    broker.close()


def test_portfolio_uses_persisted_snapshot_not_mutable_global_registry(tmp_path):
    broker = PaperBrokerService(tmp_path / "portfolio.sqlite", friction_model=ZeroFriction())
    broker.submit(bound_cash_order())
    def wrong_registry(_):
        raise AssertionError("a bound holding must never use the registry")
    tracker = PortfolioTracker(broker, SandboxMarketDataFeed(Market.INDIA, "NSE", D(105)),
                               tenant_id="tenant", instrument_resolver=wrong_registry)
    assert tracker.instrument_resolver(broker.get_positions("tenant")[0]) == cash_instrument()
    assert tracker.metrics(NOW).total_equity == D(100010)
    broker.close()


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_expired_bound_contract_refuses_before_pricing_or_cash_mutation(tmp_path, side):
    class NeverPrice:
        def evaluate(self, *_):
            raise AssertionError("expired contract reached pricing")
    broker = PaperBrokerService(tmp_path / "expiry.sqlite", friction_model=NeverPrice())
    broker.set_friction_context(None, execution_time=NOW)
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="contract_expired"):
        broker.submit(bound_order(mcx_contract(expiry=date(2026, 9, 15)), side=side))
    assert tuple(broker._connection.iterdump()) == before
    broker.close()


def test_off_tick_bound_contract_refuses_before_cash_mutation(tmp_path):
    broker = PaperBrokerService(tmp_path / "tick.sqlite", friction_model=ZeroFriction())
    broker.set_friction_context(None, execution_time=NOW)
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="contract_price_off_tick"):
        broker.submit(bound_order(price=D("5000.5")))
    assert tuple(broker._connection.iterdump()) == before
    broker.close()


@pytest.mark.parametrize("table", ["paper_positions", "paper_ledger"])
def test_reconciliation_detects_changed_bound_identity_without_repair(tmp_path, table):
    broker = PaperBrokerService(tmp_path / "reconcile.sqlite", friction_model=ZeroFriction())
    broker.submit(bound_cash_order())
    assert broker.reconcile("tenant")["status"] == "matched"
    altered = canonical_instrument_identity(replace(cash_instrument(), exchange="BSE"))
    with broker._connection:
        # table is restricted to the two test parameter values, never external input.
        broker._connection.execute(f"UPDATE {table} SET instrument_identity=?", (altered,))
    before = tuple(broker._connection.iterdump())
    report = broker.reconcile("tenant")
    assert report["status"] == "mismatch"
    assert "position_instrument_identity_mismatch" in {item["code"] for item in report["issues"]}
    assert tuple(broker._connection.iterdump()) == before
    broker.close()


def test_corrupt_bound_identity_is_visible_and_does_not_suppress_other_exits(tmp_path):
    broker = PaperBrokerService(tmp_path / "corrupt.sqlite", friction_model=ZeroFriction())
    broker.submit(bound_cash_order())
    broker.submit(replace(cash_order(tenant="tenant"), symbol="TCS"))
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET instrument_identity='broken' WHERE symbol='INFY'")
    report = broker.protection_coverage("tenant")
    assert report["status"] == "invalid"
    assert "invalid_position_instrument_identity" in {item["code"] for item in report["issues"]}
    with pytest.raises(ValueError):
        broker.get_positions("tenant")
    results = ProtectiveExitEngine(broker, lambda _: D(90), tenant_id="tenant").evaluate(NOW)
    assert len(results) == 1 and results[0].symbol == "TCS" and results[0].filled
    broker.close()
