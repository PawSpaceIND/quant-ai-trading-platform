from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    Side,
)
from quant_ai.execution.derivative_margin import (
    MARGINED_FUTURES_ASSET_CLASSES,
    ContractMarginRequirement,
    DerivativeMarginSource,
)
from quant_ai.execution.friction import FrictionCharge, FrictionContext, FrictionResult
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.reconciliation import FUTURES_ASSET_CLASSES
from quant_ai.marketdata.feed import SandboxMarketDataFeed
from quant_ai.risk.policy import RiskFirewall, RiskPolicy

D = Decimal
SYMBOL = "GOLD26OCTFUT"
NOW = datetime(2026, 9, 16, 12, 30, tzinfo=timezone.utc)


class ZeroDerivativeFriction:
    """Part 3 test seam while Part 2 remains an independent, unmerged PR."""

    @staticmethod
    def evaluate(order: OrderIntent, context: FrictionContext) -> FrictionResult:
        del context
        return FrictionResult(order.reference_price, order.reference_price, D(0), D(0), ())


class CashFeeDerivativeFriction:
    @staticmethod
    def evaluate(order: OrderIntent, context: FrictionContext) -> FrictionResult:
        del context
        return FrictionResult(
            order.reference_price, order.reference_price, D(0), D(0),
            (FrictionCharge("TEST_CASH_FEE", D("1")),),
        )


def _source(
    *, span: str = "8000", exposure: str = "2000", max_age_seconds: int = 3600
) -> DerivativeMarginSource:
    return DerivativeMarginSource(
        (
            ContractMarginRequirement(
                SYMBOL,
                Market.INDIA,
                AssetClass.METAL,
                10,
                D(span),
                D(exposure),
                "Zerodha margin snapshot TEST fixture",
                datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
            ),
        ),
        max_age_seconds=max_age_seconds,
    )


def _order(side: Side, price: str = "5000", quantity: int = 10) -> OrderIntent:
    reference = D(price)
    stop = (
        reference * D("0.98")
        if side == Side.BUY
        else reference * D("1.02")
    )
    return OrderIntent(
        SYMBOL,
        Market.INDIA,
        side,
        quantity,
        reference,
        "mcx-margin-test",
        AssetClass.METAL,
        "tenant",
        stop,
    )


def _portfolio(*, available: str = "50000", exposure: str = "0", quantity: int = 0) -> PortfolioSnapshot:
    symbol_exposure = {SYMBOL: D(exposure)} if quantity else {}
    return PortfolioSnapshot(
        D("100000"),
        D(0),
        D(exposure),
        peak_equity=D("100000"),
        symbol_exposure=symbol_exposure,
        asset_exposure={AssetClass.METAL: D(exposure)} if quantity else {},
        symbol_quantity={SYMBOL: quantity} if quantity else {},
        available_margin=D(available),
    )


def _loose_policy(**overrides) -> RiskPolicy:
    values = {
        "max_single_trade_notional": D("2"),
        "max_symbol_exposure": D("2"),
        "max_asset_class_exposure": D("2"),
        "max_gross_exposure": D("2"),
        "max_leverage_multiple": D("2"),
    }
    values.update(overrides)
    return RiskPolicy(**values)


def test_margin_source_is_exact_contract_evidence_not_a_percentage_guess() -> None:
    source = _source()
    requirement = source.requirement_for(_order(Side.BUY), now=NOW)
    assert requirement.total_per_lot == D("10000")
    assert source.margin_for_order(_order(Side.BUY), now=NOW) == D("10000")
    with pytest.raises(ValueError, match="derivative_quantity_not_whole_lots"):
        source.margin_for_order(_order(Side.BUY, quantity=5), now=NOW)
    missing = _order(Side.BUY)
    object.__setattr__(missing, "symbol", "GOLD26NOVFUT")
    with pytest.raises(ValueError, match="derivative_margin_requirement_unavailable"):
        source.requirement_for(missing, now=NOW)


def test_risk_caps_measure_full_notional_not_posted_margin() -> None:
    # 10,000 notional breaches a 5,000 single-trade cap even though this fixture's
    # broker margin is only 1,500. Replacing notional with margin would incorrectly pass.
    source = _source(span="1000", exposure="500")
    firewall = RiskFirewall(
        _loose_policy(max_single_trade_notional=D("0.05")), margin_source=source
    )
    decision = firewall.evaluate(_order(Side.BUY, price="1000"), _portfolio(), NOW)
    assert decision.reason == "single_trade_notional_limit"


def test_leverage_cap_is_independent_of_exposure_caps_and_margin() -> None:
    firewall = RiskFirewall(
        _loose_policy(max_leverage_multiple=D("0.50")), margin_source=_source()
    )
    decision = firewall.evaluate(_order(Side.BUY, price="6000"), _portfolio(), NOW)
    assert decision.reason == "leverage_limit"


def test_margin_shortfall_freezes_new_risk_but_never_traps_an_exit() -> None:
    firewall = RiskFirewall(_loose_policy(), margin_source=_source())
    entry = firewall.evaluate(_order(Side.BUY), _portfolio(available="5000"), NOW)
    assert entry.reason == "derivative_margin_shortfall"

    # A fully covered exit returns before the margin gate, even if the snapshot has no
    # available-margin measurement. Shortfall is not an instruction to liquidate either.
    held = _portfolio(available="0", exposure="50000", quantity=10)
    object.__setattr__(held, "available_margin", None)
    exit_decision = RiskFirewall(_loose_policy(), margin_source=None).evaluate(
        _order(Side.SELL), held
    )
    assert exit_decision.approved
    assert exit_decision.reason == "approved_risk_reducing"


def test_missing_margin_requirement_refuses_new_futures_risk(monkeypatch) -> None:
    monkeypatch.delenv("PRAMANA_DERIVATIVE_MARGIN_JSON", raising=False)
    monkeypatch.delenv("PRAMANA_DERIVATIVE_MARGIN_FILE", raising=False)
    decision = RiskFirewall(_loose_policy(), margin_source=None).evaluate(
        _order(Side.BUY), _portfolio()
    )
    assert decision.reason == f"derivative_margin_requirement_unavailable:{SYMBOL}"


def test_ledger_posts_margin_not_notional_and_realises_close_pnl(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "derivative-margin.db",
        starting_capital=D("100000"),
        friction_model=ZeroDerivativeFriction(),
        margin_source=_source(),
    )
    broker.set_friction_context(
        FrictionContext(D(0), D("1000000"), D(1), False), execution_time=NOW
    )
    buy = broker.submit(_order(Side.BUY))
    assert buy.average_price == D("5000")
    margin = broker.get_margin("tenant")
    assert margin.cash_balance == D("90000")
    assert margin.gross_position_value == D("10000")
    assert broker.get_account_summary("tenant").net_liquidation == D("100000")
    assert broker.reserved_margin_for(SYMBOL, Market.INDIA, AssetClass.METAL, "tenant") == D("10000")
    entry = broker.ledger_entries("tenant")[0]
    assert entry.notional == D("50000")
    assert entry.margin_change == D("10000")
    assert "Zerodha margin snapshot TEST fixture" in (entry.margin_provenance or "")
    assert broker.reconcile("tenant")["status"] == "matched"

    sell = broker.submit(_order(Side.SELL, price="5100"))
    assert sell.average_price == D("5100")
    # Release 10,000 margin + realise 1,000 P&L. The 51,000 notional is never credited.
    closed_margin = broker.get_margin("tenant")
    assert closed_margin.cash_balance == D("101000")
    assert closed_margin.gross_position_value == D(0)
    assert broker.reserved_margin_for(SYMBOL, Market.INDIA, AssetClass.METAL, "tenant") == 0
    assert broker.ledger_entries("tenant")[1].margin_change == D("-10000")
    assert broker.reconcile("tenant")["status"] == "matched"


def test_unrealised_futures_pnl_marks_equity_without_daily_cash_settlement(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "derivative-mtm.db",
        starting_capital=D("100000"),
        friction_model=ZeroDerivativeFriction(),
        margin_source=_source(),
    )
    broker.set_friction_context(
        FrictionContext(D(0), D("1000000"), D(1), False), execution_time=NOW
    )
    broker.submit(_order(Side.BUY))
    instrument = Instrument(
        SYMBOL,
        Market.INDIA,
        AssetClass.METAL,
        "INR",
        "MCX",
        expiry=date(2026, 10, 30),
        lot_size=10,
        tick_size=D("1"),
        underlying="GOLD",
    )
    tracker = PortfolioTracker(
        broker,
        SandboxMarketDataFeed(Market.INDIA, "MCX", D("5100")),
        tenant_id="tenant",
        instrument_resolver=lambda _: instrument,
    )
    metrics = tracker.metrics(datetime(2026, 9, 16, 12, 30, tzinfo=timezone.utc))
    assert metrics.cash_balance == D("90000")  # mark-to-market did not mutate cash
    assert metrics.reserved_margin == D("10000")
    assert metrics.unrealized_pnl == D("1000")
    assert metrics.total_equity == D("101000")
    snapshot = tracker.get_snapshot(datetime(2026, 9, 16, 12, 31, tzinfo=timezone.utc))
    assert snapshot.gross_exposure == D("51000")  # full notional remains the risk measure
    assert snapshot.available_margin == D("90000")


def test_leverage_caps_projected_book_not_only_each_ticket() -> None:
    firewall = RiskFirewall(
        _loose_policy(max_leverage_multiple=D("0.90")), margin_source=_source()
    )
    # Existing 70k gross + a 30k ticket: the ticket itself is only 0.30x equity,
    # but the projected book is 1.00x and must breach a 0.90x leverage ceiling.
    portfolio = _portfolio(exposure="70000")
    decision = firewall.evaluate(_order(Side.BUY, price="3000"), portfolio, NOW)
    assert decision.reason == "leverage_limit"


def test_stale_or_future_margin_snapshot_refuses_new_risk() -> None:
    stale = RiskFirewall(_loose_policy(), margin_source=_source(max_age_seconds=60))
    assert stale.evaluate(
        _order(Side.BUY), _portfolio(), NOW + timedelta(seconds=31 * 60)
    ).reason == f"derivative_margin_snapshot_stale:{SYMBOL}"
    assert stale.evaluate(
        _order(Side.BUY), _portfolio(), NOW - timedelta(minutes=31)
    ).reason == f"derivative_margin_snapshot_from_future:{SYMBOL}"


def test_margin_source_requires_explicit_freshness_policy() -> None:
    raw = {
        "source": "test broker",
        "observedAt": NOW.isoformat(),
        "contracts": [{
            "symbol": SYMBOL, "market": "INDIA", "assetClass": "METAL",
            "lotSize": 10, "spanPerLot": "8000", "exposurePerLot": "2000",
        }],
    }
    with pytest.raises(ValueError, match="observed_at_max_age_and_contracts_required"):
        DerivativeMarginSource.from_json(raw)
    raw["maxAgeSeconds"] = 300
    assert DerivativeMarginSource.from_json(raw).max_age_seconds == 300


def test_reconciliation_margin_classification_cannot_silently_drift() -> None:
    assert FUTURES_ASSET_CLASSES == {item.value for item in MARGINED_FUTURES_ASSET_CLASSES}
    assert AssetClass.FX not in MARGINED_FUTURES_ASSET_CLASSES


def test_new_futures_short_is_explicitly_unsupported() -> None:
    firewall = RiskFirewall(_loose_policy(), margin_source=_source())
    held = _portfolio(available="50000", exposure="25000", quantity=5)
    decision = firewall.evaluate(_order(Side.SELL, quantity=10), held, NOW)
    assert decision.reason == "derivative_short_opening_unsupported"


def test_final_broker_cash_check_includes_fees_and_is_atomic(tmp_path) -> None:
    # The risk gate answers the sourced-margin question. The transactional broker remains
    # authoritative for margin + exact cash fees and must refuse without a partial fill.
    source = _source()
    risk = RiskFirewall(_loose_policy(), margin_source=source).evaluate(
        _order(Side.BUY), _portfolio(available="10000"), NOW
    )
    assert risk.approved
    broker = PaperBrokerService(
        tmp_path / "margin-plus-fees.db", starting_capital=D("10000"),
        friction_model=CashFeeDerivativeFriction(), margin_source=source,
    )
    broker.set_friction_context(
        FrictionContext(D(0), D("1000000"), D(1), False), execution_time=NOW
    )
    with pytest.raises(ValueError, match="insufficient_derivative_margin"):
        broker.submit(_order(Side.BUY))
    assert broker.get_margin("tenant").cash_balance == D("10000")
    assert broker.ledger_entries("tenant") == ()
    assert broker.reserved_margin_for(
        SYMBOL, Market.INDIA, AssetClass.METAL, "tenant"
    ) == D(0)
