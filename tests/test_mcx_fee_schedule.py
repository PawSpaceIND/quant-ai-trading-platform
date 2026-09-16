from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.derivative_fees import DerivativeContractNote, DerivativeFeeSchedule
from quant_ai.execution.friction import FeeSchedule, FrictionContext, MarketFrictionModel
from quant_ai.execution.paper_ledger import PaperBrokerService

D = Decimal


def _note(*, ctt: str = "2") -> DerivativeContractNote:
    return DerivativeContractNote(
        reference="MCX-NOTE-001",
        turnover=D("3000"),
        buy_order_turnovers=(D("1000"),),
        sell_order_turnovers=(D("2000"),),
        brokerage=D("15"),
        ctt=D(ctt),
        exchange=D("6"),
        sebi=D("9"),
        gst=D("3"),
        stamp=D("4"),
        grand_total=D("39") if ctt == "2" else D("40"),
        tolerance=D("0"),
    )


def _base_schedule() -> DerivativeFeeSchedule:
    return DerivativeFeeSchedule(
        name="operator-mcx-test",
        ctt_sell_rate_non_agri=D("0.001"),
        exchange_transaction_rate=D("0.002"),
        sebi_turnover_rate=D("0.003"),
        gst_rate=D("0.1"),
        stamp_duty_buy_rate=D("0.004"),
        brokerage_turnover_rate=D("0.005"),
        brokerage_cap_per_order=D("100"),
        brokerage_minimum_per_order=D("0"),
        rate_source="operator contract-note source",
        rates_verified_on=date(2026, 9, 16),
        agricultural_symbols=frozenset({"SOYBEAN"}),
        non_agricultural_symbols=frozenset({"CRUDE"}),
    )


def _verified_schedule() -> DerivativeFeeSchedule:
    schedule = _base_schedule().with_reconciliation(_note())
    assert schedule.verified
    return schedule


def _order(symbol: str, side: Side, asset_class: AssetClass = AssetClass.METAL) -> OrderIntent:
    return OrderIntent(symbol, Market.INDIA, side, 10, D("100"), "mcx-test", asset_class, "tenant")


def _context() -> FrictionContext:
    return FrictionContext(D("0"), D("1000000"), D("1"), False)


def test_unverified_or_mismatched_schedule_cannot_price_a_derivative() -> None:
    unverified = _base_schedule()
    model = MarketFrictionModel(fee_schedule=FeeSchedule.zero(), derivative_fee_schedule=unverified)
    with pytest.raises(ValueError) as refusal:
        model.evaluate(_order("GOLD", Side.BUY), _context())
    assert str(refusal.value).endswith(":mcx_derivative_schedule_unverified")

    mismatched = _base_schedule().with_reconciliation(_note(ctt="3"))
    assert not mismatched.verified
    assert "CTT" in mismatched.reconciliation.mismatches
    with pytest.raises(ValueError, match="mcx_derivative_schedule_unverified"):
        MarketFrictionModel(derivative_fee_schedule=mismatched).evaluate(
            _order("GOLD", Side.SELL), _context()
        )



def test_schedule_charge_api_also_refuses_until_reconciled() -> None:
    with pytest.raises(ValueError, match="mcx_derivative_schedule_unverified_internal"):
        _base_schedule().charges_for(_order("GOLD", Side.BUY), D("1000"))

def test_verified_schedule_prices_mcx_without_equity_only_charges() -> None:
    model = MarketFrictionModel(
        fee_schedule=FeeSchedule.zero(), derivative_fee_schedule=_verified_schedule()
    )
    buy = {item.code: item.amount for item in model.evaluate(_order("GOLD", Side.BUY), _context()).charges}
    sell = {item.code: item.amount for item in model.evaluate(_order("GOLD", Side.SELL), _context()).charges}
    assert buy == {
        "BROKERAGE": D("5"),
        "EXCHANGE": D("2"),
        "SEBI": D("3"),
        "GST": D("1.0"),
        "STAMP": D("4"),
    }
    assert sell == {
        "BROKERAGE": D("5"),
        "CTT": D("1.000"),
        "EXCHANGE": D("2"),
        "SEBI": D("3"),
        "GST": D("1.0"),
    }
    assert "CTT" not in buy
    assert "STAMP" not in sell
    assert "STT" not in buy | sell
    assert "DP" not in buy | sell


def test_agricultural_and_unclassified_commodities_are_refused() -> None:
    model = MarketFrictionModel(derivative_fee_schedule=_verified_schedule())
    with pytest.raises(ValueError, match="mcx_agricultural_fee_schedule_not_configured:SOYBEAN"):
        model.evaluate(_order("SOYBEAN", Side.BUY, AssetClass.COMMODITY), _context())
    with pytest.raises(ValueError, match="mcx_commodity_classification_required:NATGAS"):
        model.evaluate(_order("NATGAS", Side.BUY, AssetClass.COMMODITY), _context())
    priced = model.evaluate(_order("CRUDE", Side.SELL, AssetClass.COMMODITY), _context())
    assert any(item.code == "CTT" for item in priced.charges)


def test_missing_rate_never_defaults_to_zero() -> None:
    env = {
        "PRAMANA_MCX_FEE_NAME": "operator-mcx-test",
        "PRAMANA_MCX_FEE_RATE_SOURCE": "contract note",
        "PRAMANA_MCX_FEE_RATES_VERIFIED_ON": "2026-09-16",
        "PRAMANA_MCX_FEE_CTT_SELL_RATE_NON_AGRI": "0.001",
        "PRAMANA_MCX_FEE_EXCHANGE_TRANSACTION_RATE": "0.002",
        "PRAMANA_MCX_FEE_SEBI_TURNOVER_RATE": "0.003",
        # GST deliberately absent: absence must refuse the whole schedule, not become 0.
        "PRAMANA_MCX_FEE_STAMP_DUTY_BUY_RATE": "0.004",
        "PRAMANA_MCX_FEE_BROKERAGE_TURNOVER_RATE": "0.005",
        "PRAMANA_MCX_FEE_BROKERAGE_CAP_PER_ORDER": "100",
        "PRAMANA_MCX_FEE_BROKERAGE_MINIMUM_PER_ORDER": "0",
    }
    assert DerivativeFeeSchedule.from_env(env) is None


def test_env_schedule_stays_unverified_until_contract_note_matches() -> None:
    env = {
        "PRAMANA_MCX_FEE_NAME": "operator-mcx-test",
        "PRAMANA_MCX_FEE_RATE_SOURCE": "contract note",
        "PRAMANA_MCX_FEE_RATES_VERIFIED_ON": "2026-09-16",
        "PRAMANA_MCX_FEE_CTT_SELL_RATE_NON_AGRI": "0.001",
        "PRAMANA_MCX_FEE_EXCHANGE_TRANSACTION_RATE": "0.002",
        "PRAMANA_MCX_FEE_SEBI_TURNOVER_RATE": "0.003",
        "PRAMANA_MCX_FEE_GST_RATE": "0.1",
        "PRAMANA_MCX_FEE_STAMP_DUTY_BUY_RATE": "0.004",
        "PRAMANA_MCX_FEE_BROKERAGE_TURNOVER_RATE": "0.005",
        "PRAMANA_MCX_FEE_BROKERAGE_CAP_PER_ORDER": "100",
        "PRAMANA_MCX_FEE_BROKERAGE_MINIMUM_PER_ORDER": "0",
        "PRAMANA_MCX_FEE_NON_AGRICULTURAL_SYMBOLS": "CRUDE",
    }
    loaded = DerivativeFeeSchedule.from_env(env)
    assert loaded is not None and not loaded.verified
    env["PRAMANA_MCX_FEE_RECONCILIATION_JSON"] = json.dumps(
        {
            "reference": "MCX-NOTE-001",
            "turnover": "3000",
            "buyOrderTurnovers": ["1000"],
            "sellOrderTurnovers": ["2000"],
            "charges": {
                "BROKERAGE": "15", "CTT": "2", "EXCHANGE": "6",
                "SEBI": "9", "GST": "3", "STAMP": "4",
            },
            "grandTotal": "39",
            "tolerance": "0",
        }
    )
    reconciled = DerivativeFeeSchedule.from_env(env)
    assert reconciled is not None and reconciled.verified
    assert reconciled.reconciliation.note_reference == "MCX-NOTE-001"


def test_fill_proof_carries_rate_source_date_and_reconciled_note(tmp_path) -> None:
    schedule = _verified_schedule()
    broker = PaperBrokerService(
        tmp_path / "mcx-fee-proof.db",
        starting_capital=D("10000"),
        friction_model=MarketFrictionModel(
            fee_schedule=FeeSchedule.zero(), derivative_fee_schedule=schedule
        ),
    )
    broker.set_friction_context(_context())
    broker.submit_with_evidence(
        _order("GOLD", Side.BUY),
        {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill"},
        "mcx-fee-proof",
    )
    stored = json.loads(
        broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0]
    )
    proof = stored["fill"]["friction"]["feeSchedule"]
    assert proof["scheduleVerified"] is True
    assert proof["rateSource"] == "operator contract-note source"
    assert proof["ratesVerifiedOn"] == "2026-09-16"
    assert proof["contractNoteReference"] == "MCX-NOTE-001"
    assert proof["reconciliationStatus"] == "matched"
