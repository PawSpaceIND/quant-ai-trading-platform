from __future__ import annotations

from dataclasses import replace
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
    # Brokerage and the depository charge joined the note, and GST is charged on them:
    # 18% of (brokerage + exchange + SEBI + DP), never on STT or stamp duty.
    assert buy_charges["BROKERAGE"] == Decimal(20)
    assert buy_charges["STT"] == Decimal("10.000")
    assert buy_charges["EXCHANGE"] == Decimal("0.306990000")
    assert buy_charges["SEBI"] == Decimal("0.010000")
    assert buy_charges["GST"] == Decimal("3.65705820000")
    assert buy_charges["STAMP"] == Decimal("1.50000")
    assert "DP" not in buy_charges
    sell_charges = {item.code: item.amount for item in sell.charges}
    assert sell_charges["DP"] == Decimal("15.34")
    assert sell_charges["GST"] == Decimal("6.41825820000")
    assert sell.statutory_fees == Decimal("52.07524820000")

    legacy = MarketFrictionModel(fee_schedule=FeeSchedule.legacy_prompt_rates())
    legacy_buy = legacy.evaluate(_order(Market.INDIA, Side.BUY), _zero_context())
    assert legacy_buy.statutory_fees == Decimal("35.46226000")


def test_a_one_way_cost_is_the_sum_of_its_parts_and_a_sizeless_order_is_refused() -> None:
    """What the deleted flat-bps ``CostModel`` asserted, against the model that charges.

    That test read ``CostModel(1, 2, 1).one_way_cost(10000) == 4``: three constants added
    and divided by 10000. The constants are gone and the arithmetic with them. The property
    underneath them is not, and it is checked here from the opposite direction: the price
    displacement and the charge lines are derived independently of ``total_friction`` and
    have to reconstruct it exactly. A component charged into the execution price and then
    left out of the total - or counted into it twice - fails here, which is the failure the
    old one-line sum was standing in for.

    The deleted model also refused a nonsensical notional, and no test ever asked it to.
    The statutory model's equivalent guard is exercised here so it does not become dead
    code in its turn: an order with no size is refused rather than priced at zero.
    """
    model = MarketFrictionModel(
        fee_schedule=FeeSchedule.current_2026(),
        gamma=Decimal(0),
        fixed_slippage_bps=Decimal(5),
    )
    # An observed quote and a flat impact keep the two drags exact, so the reconstruction
    # below tests the model's bookkeeping rather than Decimal's rounding context.
    context = FrictionContext(
        Decimal(2), Decimal(1000000), Decimal(1), True, Decimal("0.001")
    )
    quantity = 100

    buy = model.evaluate(_order(Market.INDIA, Side.BUY, quantity), context)
    charged = sum((item.amount for item in buy.charges), Decimal(0))
    displacement = (buy.execution_price - buy.reference_price) * Decimal(quantity)
    assert displacement > 0
    assert buy.spread_drag + buy.slippage_drag == displacement
    assert buy.cash_charges == charged == buy.statutory_fees
    assert buy.total_friction == displacement + charged
    assert {item.code for item in buy.charges} == {
        "BROKERAGE", "STT", "EXCHANGE", "SEBI", "GST", "STAMP"
    }
    assert all(item.amount > 0 for item in buy.charges)
    # No single line is the whole cost. A total read back off the one component a broken
    # model still added would satisfy the equality above and fail this.
    assert all(item.amount < buy.total_friction for item in buy.charges)

    # A sell displaces the price the other way and the same two drags still account for it.
    sell = model.evaluate(_order(Market.INDIA, Side.SELL, quantity), context)
    sell_displacement = (sell.reference_price - sell.execution_price) * Decimal(quantity)
    assert sell_displacement > 0
    assert sell.spread_drag + sell.slippage_drag == sell_displacement
    assert sell.total_friction == sell_displacement + sell.cash_charges

    sizeless = replace(_order(Market.INDIA, Side.BUY), quantity=0)
    priceless = replace(_order(Market.INDIA, Side.BUY), reference_price=Decimal(0))
    for broken in (sizeless, priceless):
        with pytest.raises(ValueError, match="positive quantity and reference_price"):
            model.evaluate(broken, context)


def test_an_instrument_the_fee_schedules_cannot_price_is_refused_not_charged() -> None:
    """The India schedule is a cash-equity contract note, and it used to charge everything.

    ``_charges`` branched on the market alone. An MCX gold future, an NFO index future, a
    CDS currency pair - all of them are ``Market.INDIA``, so all of them were handed STT at
    the equity delivery rate, equity stamp duty, and the depository charge for delivering
    shares into a demat account. A commodity pays CTT, not STT. A future delivers nothing,
    so no DP charge exists to pay. Not one of those lines was the right number, and none of
    them announced it.

    None of the correct rates are in the module, so the fix cannot be to charge them. It is
    to refuse: the model says which instrument it cannot price and stops, and the refusal
    names the symbol, the market and the asset class so the operator is told what to add
    rather than left reading a plausible-looking total.
    """
    model = MarketFrictionModel(fee_schedule=FeeSchedule.current_2026())
    unpriced = (
        (Market.INDIA, AssetClass.METAL),      # MCX gold
        (Market.INDIA, AssetClass.COMMODITY),  # MCX crude
        (Market.INDIA, AssetClass.FUTURE),     # NFO index future
        (Market.INDIA, AssetClass.FX),         # CDS currency pair
        (Market.INDIA, AssetClass.OPTION),
        (Market.INDIA, AssetClass.INDEX),      # not buyable at all
        (Market.INDIA, AssetClass.BOND),
        (Market.USA, AssetClass.FUTURE),       # Section 31 is not levied on futures
        (Market.USA, AssetClass.OPTION),
    )
    for market, asset_class in unpriced:
        order = replace(_order(market, Side.BUY), asset_class=asset_class)
        with pytest.raises(ValueError) as refusal:
            model.evaluate(order, _zero_context())
        # The operator has to be able to read what was refused off the message alone.
        assert str(refusal.value) == (
            f"friction_unpriced_instrument:TEST:{market.value}:{asset_class.value}:"
            "cash_equity_and_etf_only"
        )
        # Selling one is refused for the same reason. A model that only guarded the buy
        # would still let a position be closed at a charge it cannot compute.
        with pytest.raises(ValueError, match="friction_unpriced_instrument"):
            model.evaluate(replace(order, side=Side.SELL), _zero_context())

    # A market with no schedule at all prices nothing, whatever it is holding. GLOBAL fell
    # past both branches of ``_charges`` and came back with an empty tuple - a fill that
    # looked costless rather than one that was never priced.
    with pytest.raises(ValueError, match="friction_unpriced_instrument:TEST:GLOBAL:EQUITY"):
        model.evaluate(_order(Market.GLOBAL, Side.BUY), _zero_context())


def test_the_cash_market_the_pilot_actually_trades_is_still_priced_in_full() -> None:
    """The guard above must not have closed the door the pilot walks through.

    An ETF is charged the same contract note as a share - it settles into the same demat
    account under the same levies - so both have to come back with the full India note, not
    an empty one. A guard that refused too much would show up here as a refusal; one that
    quietly charged nothing would show up as a missing line.
    """
    model = MarketFrictionModel(fee_schedule=FeeSchedule.current_2026())
    share = model.evaluate(_order(Market.INDIA, Side.SELL), _zero_context())
    etf = model.evaluate(
        replace(_order(Market.INDIA, Side.SELL), asset_class=AssetClass.ETF), _zero_context()
    )
    # A sell: the full note minus stamp duty, which is levied on the buy leg only.
    assert {item.code for item in share.charges} == {
        "BROKERAGE", "STT", "EXCHANGE", "SEBI", "DP", "GST"
    }
    assert [(item.code, item.amount) for item in etf.charges] == [
        (item.code, item.amount) for item in share.charges
    ]
    assert etf.statutory_fees > 0

    # And the US sell leg keeps both of its fees rather than being caught by the new guard.
    us_etf = model.evaluate(
        replace(_order(Market.USA, Side.SELL), asset_class=AssetClass.ETF), _zero_context()
    )
    assert {item.code for item in us_etf.charges} == {"SEC", "FINRA_TAF"}


def test_an_unpriceable_order_never_reaches_the_ledger(tmp_path) -> None:
    """A refusal that still filled would be worse than no refusal at all.

    ``evaluate`` is called before the fill is written, so the ValueError has to come back
    out of ``submit`` with the account untouched: no order row, no cost rows, no cash
    moved. This is the assertion that makes the guard operational rather than cosmetic.
    """
    broker = PaperBrokerService(
        tmp_path / "unpriced.db",
        starting_capital=Decimal(100000),
        friction_model=MarketFrictionModel(fee_schedule=FeeSchedule.current_2026()),
    )
    broker.set_friction_context(_zero_context())
    gold = replace(_order(Market.INDIA, Side.BUY), symbol="GOLD", asset_class=AssetClass.METAL)

    with pytest.raises(ValueError, match="friction_unpriced_instrument:GOLD:INDIA:METAL"):
        broker.submit(gold)

    assert broker.get_margin("tenant").cash_balance == Decimal(100000)
    assert broker.cost_entries("tenant") == ()
    assert broker.ledger_entries("tenant") == ()
    assert broker.get_positions("tenant") == ()

    # The share that is priced still fills through the same broker, so the refusal above
    # was about the instrument and not about a broker left in a broken state.
    broker.submit(_order(Market.INDIA, Side.BUY))
    assert broker.get_margin("tenant").cash_balance < Decimal(100000)
    assert [item.symbol for item in broker.get_positions("tenant")] == ["TEST"]


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
    assert {item.code for item in costs} == {
        "BROKERAGE", "STT", "EXCHANGE", "SEBI", "GST", "STAMP"
    }
    assert all(item.cash_debit for item in costs)
    # Brokerage (INR 20) and the GST charged on it now leave the account with the fill.
    assert broker.get_margin("tenant").cash_balance == Decimal("89964.52595180000")


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
