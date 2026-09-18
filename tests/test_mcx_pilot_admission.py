"""MCX pilot admission reuses existing contract, fee, margin and identity controls."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_mcx_fee_schedule import _base_schedule, _context, _verified_schedule

from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    InstrumentBoundOrderIntent,
    Market,
    OrderIntent,
    Side,
)
from quant_ai.execution.derivative_margin import (
    ContractMarginRequirement,
    DerivativeMarginSource,
)
from quant_ai.execution.friction import MarketFrictionModel
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.pilot import validate_pilot_instruments

D = Decimal
NOW = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
def gold(**changes) -> Instrument:
    base = Instrument(
        "GOLD26DECFUT", Market.INDIA, AssetClass.METAL, "INR", "MCX",
        metadata={"source": "synthetic admission fixture"},
        expiry=date(2026, 12, 5), lot_size=10, tick_size=D("1"), underlying="GOLD",
    )
    return replace(base, **changes)


def margin_source(
    instrument: Instrument | None = None,
    *,
    observed_at: datetime = NOW,
    max_age_seconds: int = 3600,
    lot_size: int | None = None,
) -> DerivativeMarginSource:
    item = instrument or gold()
    return DerivativeMarginSource((
        ContractMarginRequirement(
            symbol=item.symbol,
            market=item.market,
            asset_class=item.asset_class,
            lot_size=item.lot_size if lot_size is None else lot_size,
            span_per_lot=D("8000"),
            exposure_per_lot=D("2000"),
            source="synthetic margin fixture; not production evidence",
            observed_at=observed_at,
        ),
    ), max_age_seconds=max_age_seconds)
def bound_order(item: Instrument | None = None, side: Side = Side.BUY):
    item = item or gold()
    return InstrumentBoundOrderIntent(
        item.symbol, item.market, side, item.lot_size or 10, D("5000"),
        "synthetic-mcx", item.asset_class, "pilot", D("4900"), D("5200"), item,
    )


def plain_order(side: Side = Side.BUY):
    item = gold()
    return OrderIntent(
        item.symbol, item.market, side, item.lot_size or 10, D("5000"),
        "synthetic-mcx", item.asset_class, "pilot", D("4900"), D("5200"),
    )


def validated_kwargs():
    return {
        "derivative_fee_schedule": _verified_schedule(),
        "margin_source": margin_source(),
        "now": NOW,
    }


def test_verified_fee_margin_and_live_contract_admit_mcx():
    validate_pilot_instruments((gold(),), **validated_kwargs())


def test_cash_pilot_still_needs_no_derivative_configuration():
    validate_pilot_instruments((
        Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),
    ))
@pytest.mark.parametrize("fee", [None, _base_schedule()])
def test_mcx_fee_schedule_must_be_reconciled(fee):
    with pytest.raises(ValueError, match="pilot_mcx_fee_schedule_unverified"):
        validate_pilot_instruments(
            (gold(),), derivative_fee_schedule=fee,
            margin_source=margin_source(), now=NOW,
        )


def test_mcx_margin_source_is_required():
    with pytest.raises(ValueError, match="pilot_mcx_margin_source_required"):
        validate_pilot_instruments(
            (gold(),), derivative_fee_schedule=_verified_schedule(),
            margin_source=None, now=NOW,
        )


def test_mcx_margin_must_match_contract_lot():
    with pytest.raises(ValueError, match="pilot_mcx_margin_contract_mismatch"):
        validate_pilot_instruments(
            (gold(),), derivative_fee_schedule=_verified_schedule(),
            margin_source=margin_source(lot_size=5), now=NOW,
        )


def test_mcx_margin_snapshot_must_be_current():
    stale = margin_source(observed_at=NOW - timedelta(hours=2), max_age_seconds=60)
    with pytest.raises(ValueError, match="derivative_margin_snapshot_stale"):
        validate_pilot_instruments(
            (gold(),), derivative_fee_schedule=_verified_schedule(),
            margin_source=stale, now=NOW,
        )
@pytest.mark.parametrize("item", [
    gold(underlying=None),
    gold(tick_size=None),
])
def test_mcx_requires_complete_contract_identity(item):
    with pytest.raises(ValueError, match="pilot_mcx_contract_identity_incomplete"):
        validate_pilot_instruments((item,), **validated_kwargs())


@pytest.mark.parametrize(("expiry", "reason"), [
    (date(2026, 9, 17), "contract_expired"),
    (date(2026, 9, 20), "contract_in_rollover"),
])
def test_mcx_entry_contract_must_be_live(expiry, reason):
    item = gold(expiry=expiry)
    with pytest.raises(ValueError, match=reason):
        validate_pilot_instruments(
            (item,), derivative_fee_schedule=_verified_schedule(),
            margin_source=margin_source(item), now=NOW,
        )


@pytest.mark.parametrize(("symbol", "classification", "reason"), [
    ("SOYBEAN", "agricultural", "mcx_agricultural_fee_schedule_not_configured"),
    ("UNCLASSIFIED", "unclassified", "mcx_commodity_classification_required"),
])
def test_mcx_commodity_requires_supported_fee_classification(symbol, classification, reason):
    item = gold(symbol=symbol, asset_class=AssetClass.COMMODITY, underlying=symbol)
    schedule = replace(
        _verified_schedule(),
        agricultural_symbols=frozenset({"SOYBEAN"}),
        non_agricultural_symbols=frozenset({"CRUDE"}),
    )
    with pytest.raises(ValueError, match=reason):
        validate_pilot_instruments(
            (item,), derivative_fee_schedule=schedule,
            margin_source=margin_source(item), now=NOW,
        )
def test_other_derivative_venues_remain_outside_private_pilot():
    future = Instrument(
        "NIFTY26DECFUT", Market.INDIA, AssetClass.FUTURE, "INR", "NFO",
        expiry=date(2026, 12, 31), lot_size=50, tick_size=D("0.05"),
        underlying="NIFTY",
    )
    with pytest.raises(ValueError, match="pilot_instrument_not_supported"):
        validate_pilot_instruments((future,), **validated_kwargs())


def mcx_broker(tmp_path) -> PaperBrokerService:
    broker = PaperBrokerService(
        tmp_path / "mcx.sqlite",
        friction_model=MarketFrictionModel(
            derivative_fee_schedule=_verified_schedule()
        ),
        margin_source=margin_source(),
    )
    broker.set_friction_context(_context(), execution_time=NOW)
    return broker


def bind_runtime_identity(broker, instruments, tmp_path):
    from quant_ai.governance.runtime_identity import configure_runtime_identity
    from quant_ai.orders.oms import DurableOms

    oms_path = tmp_path / "mcx-oms.sqlite"
    with DurableOms(oms_path) as oms:
        configure_runtime_identity(
            broker, tuple(instruments), "pilot", "bound_v1", oms_path, oms=oms
        )


def configure_mcx_scope(broker, instruments, tmp_path):
    instruments = tuple(instruments)
    bind_runtime_identity(broker, instruments, tmp_path)
    broker.configure_pilot(instruments, "pilot")


def test_configured_mcx_pilot_requires_bound_order_identity(tmp_path):
    broker = mcx_broker(tmp_path)
    configure_mcx_scope(broker, (gold(),), tmp_path)
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="runtime_identity_bound_order_required"):
        broker.buy(plain_order())
    assert tuple(broker._connection.iterdump()) == before


def test_bound_mcx_pilot_fill_persists_exact_contract_identity(tmp_path):
    broker = mcx_broker(tmp_path)
    configuration_error = None
    try:
        configure_mcx_scope(broker, (gold(),), tmp_path)
    except ValueError as caught:
        configuration_error = caught
    assert configuration_error is None
    fill = broker.buy(bound_order())
    assert broker.bound_instrument_for_fill(fill.order_id, "pilot") == gold()
    assert broker.reconcile("pilot")["status"] == "matched"
def test_pilot_cannot_adopt_unbound_existing_derivative_position(tmp_path):
    broker = mcx_broker(tmp_path)
    bind_runtime_identity(broker, (gold(),), tmp_path)
    with broker._connection:
        broker._connection.execute(
            """INSERT INTO paper_positions
            (tenant_id,symbol,market,asset_class,quantity,average_price,
             stop_price,take_profit_price,instrument_identity)
            VALUES (?,?,?,?,?,?,?,?,NULL)""",
            ("pilot", gold().symbol, Market.INDIA.value, AssetClass.METAL.value,
             10, "5000", "4900", "5200"),
        )
    with pytest.raises(
        ValueError, match="pilot_existing_derivative_contract_identity_missing"
    ):
        broker.configure_pilot((gold(),), "pilot")


def test_same_symbol_different_contract_cannot_trade_in_pilot_scope(tmp_path):
    broker = mcx_broker(tmp_path)
    configure_mcx_scope(broker, (gold(),), tmp_path)
    other = gold(expiry=date(2027, 1, 5))
    with pytest.raises(ValueError, match="runtime_identity_order_out_of_scope"):
        broker.buy(bound_order(other))


def test_mcx_pilot_requires_bound_runtime_before_broker_initialization(tmp_path):
    directives = FounderDirectives(watchlist=(gold(),))
    with pytest.raises(ValueError, match="pilot_derivative_requires_bound_identity"):
        build_ghost_runner(
            zerodha_api_key="test",
            zerodha_access_token="test",
            zerodha_instrument_tokens=(1,),
            zerodha_symbol_by_token={1: gold().symbol},
            ib_client=SimpleNamespace(),
            ib_contracts=(),
            include_ibkr=False,
            database=tmp_path / "ledger.sqlite",
            tenant_id="pilot",
            log_path=tmp_path / "events.jsonl",
            xai_directory=tmp_path / "proofs",
            halt_file=tmp_path / "HALT",
            directives=directives,
            pilot_mode=True,
            order_identity_mode="legacy_cash",
        )


def test_mcx_and_cash_can_share_the_same_inr_pilot_scope(tmp_path):
    cash = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    broker = mcx_broker(tmp_path)
    configure_mcx_scope(broker, (cash, gold()), tmp_path)
    row = broker._connection.execute(
        "SELECT market,currency FROM pilot_scope WHERE tenant_id='pilot'"
    ).fetchone()
    assert tuple(row) == ("INDIA", "INR")


def test_corrupted_nonlot_bound_order_is_refused_by_ledger_contract_check(tmp_path):
    broker = mcx_broker(tmp_path)
    configure_mcx_scope(broker, (gold(),), tmp_path)
    order = bound_order()
    object.__setattr__(order, "quantity", 7)
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="contract_quantity_not_whole_lots"):
        broker.buy(order)
    assert tuple(broker._connection.iterdump()) == before


def test_contract_expiring_after_scope_configuration_cannot_fill(tmp_path):
    item = gold(expiry=date(2026, 9, 30))
    source = margin_source(item)
    broker = PaperBrokerService(
        tmp_path / "expiry.sqlite",
        friction_model=MarketFrictionModel(
            derivative_fee_schedule=_verified_schedule()
        ),
        margin_source=source,
    )
    broker.set_friction_context(_context(), execution_time=NOW)
    configure_mcx_scope(broker, (item,), tmp_path)
    broker.set_friction_context(
        _context(), execution_time=datetime(2026, 10, 1, 6, tzinfo=timezone.utc)
    )
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="contract_expired"):
        broker.buy(bound_order(item))
    assert tuple(broker._connection.iterdump()) == before


def test_mcx_scope_survives_restart_and_keeps_exact_contract(tmp_path):
    path = tmp_path / "restart.sqlite"
    broker = PaperBrokerService(
        path,
        friction_model=MarketFrictionModel(
            derivative_fee_schedule=_verified_schedule()
        ),
        margin_source=margin_source(),
    )
    broker.set_friction_context(_context(), execution_time=NOW)
    configure_mcx_scope(broker, (gold(),), tmp_path)
    broker.close()

    restarted = PaperBrokerService(
        path,
        friction_model=MarketFrictionModel(
            derivative_fee_schedule=_verified_schedule()
        ),
        margin_source=margin_source(),
    )
    restarted.set_friction_context(_context(), execution_time=NOW)
    fill = restarted.buy(bound_order())
    assert restarted.bound_instrument_for_fill(fill.order_id, "pilot") == gold()
    other = gold(expiry=date(2027, 1, 5))
    with pytest.raises(ValueError, match="runtime_identity_order_out_of_scope"):
        restarted.buy(bound_order(other))


def test_bound_ghost_factory_admits_mcx_with_existing_verified_sources(
    tmp_path, monkeypatch
):
    from quant_ai.execution.derivative_fees import DerivativeFeeSchedule
    from quant_ai.execution.derivative_margin import DerivativeMarginSource

    fee = _verified_schedule()
    margin = margin_source(
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    monkeypatch.setattr(
        DerivativeFeeSchedule, "from_env",
        classmethod(lambda cls, environ=None: fee),
    )
    monkeypatch.setattr(
        DerivativeMarginSource, "from_env",
        classmethod(lambda cls, environ=None: margin),
    )
    item = gold()
    runner = build_ghost_runner(
        zerodha_api_key="test",
        zerodha_access_token="test",
        zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: item.symbol},
        ib_client=SimpleNamespace(),
        ib_contracts=(),
        include_ibkr=False,
        database=tmp_path / "factory-ledger.sqlite",
        oms_database=tmp_path / "factory-oms.sqlite",
        tenant_id="pilot",
        log_path=tmp_path / "events.jsonl",
        xai_directory=tmp_path / "proofs",
        halt_file=tmp_path / "HALT",
        directives=FounderDirectives(watchlist=(item,)),
        pilot_mode=True,
        order_identity_mode="bound_v1",
    )
    try:
        scope = runner.daemon.tracker.broker._connection.execute(
            "SELECT instrument_identities FROM pilot_scope WHERE tenant_id='pilot'"
        ).fetchone()
        assert scope is not None
        assert item.symbol in scope[0]
        assert runner.daemon.scheduler.pipeline.bind_order_instruments is True
    finally:
        runtime = runner.daemon.scheduler.pipeline.runtime
        if runtime.oms is not None:
            runtime.oms.close()
        runner.daemon.tracker.broker.close()

@pytest.mark.parametrize("item", [
    Instrument(
        "GOLDUSD26DECFUT", Market.INDIA, AssetClass.METAL, "USD", "MCX",
        expiry=date(2026, 12, 5), lot_size=10, tick_size=D("1"), underlying="GOLD",
    ),
    Instrument(
        "GUAR26DECFUT", Market.INDIA, AssetClass.COMMODITY, "INR", "NCDEX",
        expiry=date(2026, 12, 5), lot_size=10, tick_size=D("1"), underlying="GUAR",
    ),
])
def test_mcx_admission_does_not_widen_currency_or_exchange_scope(item):
    with pytest.raises(ValueError, match="pilot_instrument_not_supported"):
        validate_pilot_instruments(
            (item,), derivative_fee_schedule=_verified_schedule(),
            margin_source=margin_source(gold()), now=NOW,
        )
