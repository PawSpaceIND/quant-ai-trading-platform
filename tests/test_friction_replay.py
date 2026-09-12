from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.backtesting.replay import (
    HistoricalFundamentalEvent,
    HistoricalMacroEvent,
    HistoricalNewsProvider,
    HistoricalReplayDataset,
    HistoricalReplayHarness,
)
from quant_ai.backtesting.tearsheet import build_tearsheet
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, RiskMode, Side
from quant_ai.execution.friction import FeeSchedule, FrictionContext, MarketFrictionModel
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def _order(market: Market, side: Side, quantity: int = 100) -> OrderIntent:
    return OrderIntent(
        "TEST", market, side, quantity, Decimal(100), "test", AssetClass.EQUITY,
        "tenant", Decimal(90), Decimal(120),
    )


def _zero_context() -> FrictionContext:
    return FrictionContext(Decimal(0), Decimal(1000000), Decimal(1), True)


def test_india_current_2026_contract_note_math_and_legacy_profile() -> None:
    current = MarketFrictionModel(fee_schedule=FeeSchedule.current_2026())
    buy = current.evaluate(_order(Market.INDIA, Side.BUY), _zero_context())
    sell = current.evaluate(_order(Market.INDIA, Side.SELL), _zero_context())
    buy_charges = {item.code: item.amount for item in buy.charges}
    assert buy_charges["STT"] == Decimal("10.000")
    assert buy_charges["EXCHANGE"] == Decimal("0.306990000")
    assert buy_charges["SEBI"] == Decimal("0.010000")
    assert buy_charges["GST"] == Decimal("0.05705820000")
    assert buy_charges["STAMP"] == Decimal("1.50000")
    assert sell.statutory_fees == Decimal("10.37404820000")

    legacy = MarketFrictionModel(fee_schedule=FeeSchedule.legacy_prompt_rates())
    legacy_buy = legacy.evaluate(_order(Market.INDIA, Side.BUY), _zero_context())
    assert legacy_buy.statutory_fees == Decimal("11.86226000")


def test_us_fees_sell_only_and_current_rates() -> None:
    model = MarketFrictionModel(fee_schedule=FeeSchedule.current_2026())
    assert model.evaluate(_order(Market.USA, Side.BUY), _zero_context()).charges == ()
    sell = model.evaluate(_order(Market.USA, Side.SELL), _zero_context())
    charges = {item.code: item.amount for item in sell.charges}
    assert charges == {"SEC": Decimal("0.2060000"), "FINRA_TAF": Decimal("0.019500")}


def test_square_root_slippage_is_nonlinear_and_bounded() -> None:
    model = MarketFrictionModel(
        fee_schedule=FeeSchedule.zero(),
        gamma=Decimal("0.5"),
        spread_atr_multiplier=Decimal(0),
    )
    context = FrictionContext(Decimal(2), Decimal(10000), Decimal(1), True)
    small = model.evaluate(_order(Market.USA, Side.BUY, 100), context)
    large = model.evaluate(_order(Market.USA, Side.BUY, 400), context)
    small_impact = small.execution_price - Decimal(100)
    large_impact = large.execution_price - Decimal(100)
    assert large_impact == small_impact * Decimal(2)
    assert large_impact / Decimal(100) <= Decimal("0.02")


def test_broker_logs_statutory_costs_as_separate_cash_debits(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "friction.db",
        starting_capital=Decimal(100000),
        friction_model=MarketFrictionModel(fee_schedule=FeeSchedule.current_2026()),
    )
    broker.set_friction_context(_zero_context())
    broker.submit(_order(Market.INDIA, Side.BUY))
    costs = broker.cost_entries("tenant")
    assert {item.code for item in costs} == {"STT", "EXCHANGE", "SEBI", "GST", "STAMP"}
    assert all(item.cash_debit for item in costs)
    assert broker.get_margin("tenant").cash_balance == Decimal("89988.12595180000")


def _dataset() -> HistoricalReplayDataset:
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    start = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    bars = []
    price = Decimal(100)
    for index in range(70):
        if index < 40:
            close = price + Decimal("0.10")
        else:
            close = price - Decimal("0.20")
        bars.append(
            Candle(
                instrument, start + timedelta(minutes=index), price,
                max(price, close) + Decimal("0.10"), min(price, close) - Decimal("0.10"),
                close, Decimal(10000 + index * 10),
            )
        )
        price = close
    macro = (
        HistoricalMacroEvent(
            start,
            {"US10Y": Decimal("3.5"), "INDIA10Y": Decimal("6.5"), "BRENT": Decimal(60),
             "GOLD": Decimal(2600), "DXY": Decimal(90)},
        ),
        HistoricalMacroEvent(
            start + timedelta(minutes=40),
            {"US10Y": Decimal("6.5"), "INDIA10Y": Decimal("8.0"), "BRENT": Decimal(120),
             "GOLD": Decimal(1900), "DXY": Decimal(120)},
        ),
    )
    fundamentals = (
        HistoricalFundamentalEvent(
            start, "AAPL",
            {"pe": Decimal(20), "debt_equity": Decimal("0.2"),
             "operating_margin": Decimal("0.30"), "fcf_yield": Decimal("0.04")},
        ),
        HistoricalFundamentalEvent(
            start + timedelta(minutes=40), "AAPL",
            {"pe": Decimal(60), "debt_equity": Decimal(2),
             "operating_margin": Decimal("0.05"), "fcf_yield": Decimal("0.005")},
        ),
    )
    news = [
        NewsSignal("AAPL", "strong demand", Decimal("0.9"), "hist", start),
        NewsSignal("GEOPOLITICAL", "peace", Decimal("0.9"), "hist", start + timedelta(seconds=10)),
    ]
    for minute in (40, 41, 42):
        news.append(NewsSignal(
            "AAPL", f"risk {minute}", Decimal("-0.9"), "hist",
            start + timedelta(minutes=minute),
        ))
        news.append(NewsSignal(
            "GEOPOLITICAL", f"conflict {minute}", Decimal("-0.9"), "hist",
            start + timedelta(minutes=minute, seconds=10),
        ))
    benchmark = tuple(item.close for item in bars)
    return HistoricalReplayDataset(bars=tuple(bars), macro=macro, news=tuple(news), fundamentals=fundamentals, benchmark_closes=benchmark)


def test_replay_rejects_future_events_and_provider_prevents_lookahead() -> None:
    dataset = _dataset()
    first = dataset.bars[0].timestamp
    provider = HistoricalNewsProvider(dataset.news)
    visible = provider.fetch("AAPL", first)
    assert len(visible) == 1
    assert all(item.published_at <= first for item in visible)
    future = NewsSignal(
        "AAPL", "future", Decimal(1), "hist", dataset.bars[-1].timestamp + timedelta(minutes=1)
    )
    bad = HistoricalReplayDataset(
        dataset.bars, dataset.macro, dataset.news + (future,), dataset.fundamentals,
        dataset.benchmark_closes,
    )
    broker = PaperBrokerService(slippage_bps=Decimal(0))
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"), expected_edge=Decimal("0.02"),
        requested_mode=RiskMode.BALANCED,
    ))
    with pytest.raises(ValueError, match="future-dated news event"):
        HistoricalReplayHarness(broker, plan).run(bad)


def test_multistep_replay_uses_full_pipeline_and_net_equity_includes_friction(tmp_path) -> None:
    dataset = _dataset()
    broker = PaperBrokerService(tmp_path / "replay.db", starting_capital=Decimal(100000))
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"), expected_edge=Decimal("0.02"),
        requested_mode=RiskMode.BALANCED,
    ))
    result = HistoricalReplayHarness(broker, plan, quantity=10).run(dataset)
    sheet = build_tearsheet(result, broker)
    assert result.order_ids
    assert sheet.trades == len(result.order_ids)
    assert sheet.cumulative_statutory_fees > 0
    assert sheet.realized_slippage_drag > 0
    assert result.final_snapshot.equity == result.equity_curve[-1]
    assert all(a < b for a, b in zip(result.timestamps, result.timestamps[1:]))

    frictionless = PaperBrokerService(
        tmp_path / "replay-zero.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0)
    )
    zero_result = HistoricalReplayHarness(frictionless, plan, quantity=10).run(dataset)
    assert result.final_snapshot.equity < zero_result.final_snapshot.equity
