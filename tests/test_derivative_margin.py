from __future__ import annotations

from datetime import date, datetime, timezone
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
    ContractMarginRequirement,
    DerivativeMarginSource,
)
from quant_ai.execution.friction import FrictionContext, FrictionResult
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.marketdata.feed import SandboxMarketDataFeed
from quant_ai.risk.policy import RiskFirewall, RiskPolicy

D = Decimal
SYMBOL = "GOLD26OCTFUT"


class ZeroDerivativeFriction:
    """Part 3 test seam while Part 2 remains an independent, unmerged PR."""

    @staticmethod
    def evaluate(order: OrderIntent, context: FrictionContext) -> FrictionResult:
        del context
        return FrictionResult(order.reference_price, order.reference_price, D(0), D(0), ())


def _source(*, span: str = "8000", exposure: str = "2000") -> DerivativeMarginSource:
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
        )
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
    requirement = source.requirement_for(_order(Side.BUY))
    assert requirement.total_per_lot == D("10000")
    assert source.margin_for_order(_order(Side.BUY)) == D("10000")
    with pytest.raises(ValueError, match="derivative_quantity_not_whole_lots"):
        source.margin_for_order(_order(Side.BUY, quantity=5))
    missing = _order(Side.BUY)
    object.__setattr__(missing, "symbol", "GOLD26NOVFUT")
    with pytest.raises(ValueError, match="derivative_margin_requirement_unavailable"):
        source.requirement_for(missing)


def test_risk_caps_measure_full_notional_not_posted_margin() -> None:
    # 10,000 notional breaches a 5,000 single-trade cap even though this fixture's
    # broker margin is only 1,500. Replacing notional with margin would incorrectly pass.
    source = _source(span="1000", exposure="500")
    firewall = RiskFirewall(
        _loose_policy(max_single_trade_notional=D("0.05")), margin_source=source
    )
    decision = firewall.evaluate(_order(Side.BUY, price="1000"), _portfolio())
    assert decision.reason == "single_trade_notional_limit"


def test_leverage_cap_is_independent_of_exposure_caps_and_margin() -> None:
    firewall = RiskFirewall(
        _loose_policy(max_leverage_multiple=D("0.50")), margin_source=_source()
    )
    decision = firewall.evaluate(_order(Side.BUY, price="6000"), _portfolio())
    assert decision.reason == "leverage_limit"


def test_margin_shortfall_freezes_new_risk_but_never_traps_an_exit() -> None:
    firewall = RiskFirewall(_loose_policy(), margin_source=_source())
    entry = firewall.evaluate(_order(Side.BUY), _portfolio(available="5000"))
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
    broker.set_friction_context(FrictionContext(D(0), D("1000000"), D(1), False))
    buy = broker.submit(_order(Side.BUY))
    assert buy.average_price == D("5000")
    assert broker.get_margin("tenant").cash_balance == D("90000")
    assert broker.reserved_margin_for(SYMBOL, Market.INDIA, AssetClass.METAL, "tenant") == D("10000")
    entry = broker.ledger_entries("tenant")[0]
    assert entry.notional == D("50000")
    assert entry.margin_change == D("10000")
    assert "Zerodha margin snapshot TEST fixture" in (entry.margin_provenance or "")
    assert broker.reconcile("tenant")["status"] == "matched"

    sell = broker.submit(_order(Side.SELL, price="5100"))
    assert sell.average_price == D("5100")
    # Release 10,000 margin + realise 1,000 P&L. The 51,000 notional is never credited.
    assert broker.get_margin("tenant").cash_balance == D("101000")
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
    broker.set_friction_context(FrictionContext(D(0), D("1000000"), D(1), False))
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
