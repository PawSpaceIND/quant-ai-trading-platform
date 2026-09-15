"""What a pilot-sized fill really costs, and where its inputs came from.

Two defects are pinned here. The live path used to price every fill from a fabricated
friction context - a flat 5.000 bps half spread at every price and every order size, with
impact saturating at 0.500 bps - and no brokerage or depository charge was modelled at all.
These tests hold the corrected cost against the old one, and hold the fallback to the only
safe direction: an unobserved market is never cheaper than an observed one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.friction import (
    ASSUMED,
    OBSERVED_BARS_ASSUMED_SPREAD,
    OBSERVED_QUOTE,
    OBSERVED_QUOTE_AND_BARS,
    BrokerageSchedule,
    FeeSchedule,
    FrictionContext,
    MarketFrictionModel,
)
from quant_ai.execution.live_friction import (
    LiveFrictionContextProvider,
    assumed_friction_context,
    friction_context_from_bars,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.portfolio.sizing import PositionSizer

T0 = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
# A pilot-sized position: INR 8,000 of an INR 800 share, on a ~INR 100,000 account.
PRICE = Decimal(800)
QUANTITY = 10
NOTIONAL = PRICE * Decimal(QUANTITY)


def _order(side: Side, quantity: int = QUANTITY, price: Decimal = PRICE) -> OrderIntent:
    return OrderIntent(
        "INFY", Market.INDIA, side, quantity, price, "friction-test", AssetClass.EQUITY, "tenant",
    )


def _fabricated_context(order: OrderIntent) -> FrictionContext:
    """The context the live path used to invent when none was supplied."""
    return FrictionContext(
        atr=order.reference_price * Decimal("0.01"),
        average_daily_volume=max(Decimal(1000000), Decimal(order.quantity * 10000)),
        liquidity_score=Decimal(1),
        delivery=True,
    )


def _bps(amount: Decimal, notional: Decimal = NOTIONAL) -> Decimal:
    return (amount / notional * Decimal(10000)).quantize(Decimal("0.01"))


def _round_trip(model: MarketFrictionModel, context_for) -> Decimal:
    total = Decimal(0)
    for side in (Side.BUY, Side.SELL):
        order = _order(side)
        result = model.evaluate(order, context_for(order))
        total += result.spread_drag + result.slippage_drag + result.cash_charges
    return total


def _tick(at: datetime, ltp: Decimal, volume: Decimal, bid=None, ask=None) -> LiveTick:
    return LiveTick("INFY", ltp, volume, bid, ask, at, "test")


def _observed_market(minutes: int = 10, *, bid=None, ask=None):
    """A live buffer and feed carrying real closed bars, and optionally a real quote."""
    now = [T0]
    buffer = TickBuffer(clock=lambda: now[0])
    feed = LiveTickMarketDataFeed(buffer, clock=lambda: now[0])
    volume = Decimal(100000)
    for minute in range(minutes):
        for seconds, price in ((10, PRICE - Decimal(1)), (40, PRICE + Decimal(1))):
            at = T0 + timedelta(minutes=minute, seconds=seconds)
            now[0] = at
            volume += Decimal(5000)
            buffer.put(_tick(at, price, volume))
    last = T0 + timedelta(minutes=minutes, seconds=5)
    now[0] = last
    buffer.put(_tick(last, PRICE, volume + Decimal(100), bid, ask))
    now[0] = last + timedelta(seconds=10)
    return buffer, feed, now


# ------------------------------------------------------- the measured difference

def test_pilot_sized_round_trip_costs_materially_more_than_the_fabricated_context() -> None:
    fabricated = MarketFrictionModel(
        fee_schedule=FeeSchedule.current_2026(), brokerage_schedule=BrokerageSchedule.zero()
    )
    before = _round_trip(fabricated, _fabricated_context)
    after = _round_trip(MarketFrictionModel(), assumed_friction_context)

    # Before: 5.00 bps half spread + 0.16 bps impact per side, no brokerage: 32.57 bps.
    assert _bps(before) == Decimal("32.57")
    # After: 25 bps assumed half spread + 2 bps impact per side, INR 20 brokerage on each
    # leg, INR 15.34 depository charge on the delivery sell and GST on all of them.
    assert _bps(after) == Decimal("157.88")
    assert after > before * Decimal(4)


def test_brokerage_and_depository_charges_appear_with_gst_charged_on_them() -> None:
    model = MarketFrictionModel()
    buy = {c.code: c.amount for c in model.evaluate(_order(Side.BUY), assumed_friction_context(_order(Side.BUY))).charges}
    sell_order = _order(Side.SELL)
    sell = model.evaluate(sell_order, assumed_friction_context(sell_order))
    sell_charges = {c.code: c.amount for c in sell.charges}

    assert buy["BROKERAGE"] == Decimal(20)
    assert sell_charges["BROKERAGE"] == Decimal(20)
    # The depository debits the demat account, so the DP charge is on the delivery sell only.
    assert "DP" not in buy
    assert sell_charges["DP"] == Decimal("15.34")
    # GST is 18% of brokerage + exchange + SEBI + DP, and of nothing else.
    for charges in (buy, sell_charges):
        base = (
            charges["BROKERAGE"]
            + charges["EXCHANGE"]
            + charges["SEBI"]
            + charges.get("DP", Decimal(0))
        )
        assert charges["GST"] == base * Decimal("0.18")
        assert charges["GST"] > charges["EXCHANGE"] * Decimal("0.18") * Decimal(10)

    # An intraday leg pays no DP charge, and a US order pays no Indian broker charge.
    intraday = model.evaluate(
        sell_order,
        FrictionContext(Decimal(0), Decimal(1000000), Decimal(1), False),
    )
    assert "DP" not in {c.code for c in intraday.charges}


def test_brokerage_schedule_is_documented_and_environment_overridable() -> None:
    default = BrokerageSchedule.discount_broker_2026()
    assert BrokerageSchedule.from_env({}) == default
    # INR 20 per order binds above INR 800 of turnover; the percentage binds below it.
    assert default.india_brokerage(Decimal(8000)) == Decimal(20)
    assert default.india_brokerage(Decimal(400)) == Decimal(10)
    assert default.india_brokerage(Decimal(0)) == Decimal(0)

    narrow = BrokerageSchedule.from_env(
        {"PRAMANA_BROKERAGE_PERCENT": "0.001", "PRAMANA_BROKERAGE_DP_SELL_INR": "18.50"}
    )
    assert narrow.india_brokerage(Decimal(8000)) == Decimal(8)
    assert narrow.india_dp_sell_per_scrip == Decimal("18.50")
    assert narrow.india_cap_per_order == default.india_cap_per_order

    # A typo never cheapens a fill: the documented default survives it.
    for bad in ("not-a-number", "-5", "NaN", ""):
        assert BrokerageSchedule.from_env({"PRAMANA_BROKERAGE_CAP_INR": bad}) == default


# ------------------------------------------------ spread and impact actually vary

def test_spread_and_impact_vary_with_order_size_and_volatility() -> None:
    model = MarketFrictionModel(
        fee_schedule=FeeSchedule.zero(), brokerage_schedule=BrokerageSchedule.zero()
    )
    fabricated_spreads, fabricated_impacts = set(), set()
    for quantity in (100, 1000, 10000, 100000):
        order = _order(Side.BUY, quantity)
        result = model.evaluate(order, _fabricated_context(order))
        notional = order.reference_price * order.quantity
        fabricated_spreads.add(_bps(result.spread_drag, notional))
        fabricated_impacts.add(_bps(result.slippage_drag, notional))
    # The defect, pinned: one spread and one impact number from INR 80,000 to INR 80
    # crore, because the fabricated volume grew with the order it was meant to constrain.
    assert fabricated_spreads == {Decimal("5.00")}
    assert fabricated_impacts == {Decimal("0.50")}

    # Observed inputs: a fixed traded volume, so a bigger order eats more of the book.
    observed = FrictionContext(Decimal(16), Decimal(500000), Decimal("0.85"))

    def impact_fraction(quantity: int) -> Decimal:
        order = _order(Side.BUY, quantity)
        return model.evaluate(order, observed).slippage_drag / (
            order.reference_price * Decimal(quantity)
        )

    impacts = [impact_fraction(q) for q in (10, 100, 1000, 10000)]
    assert len(set(impacts)) == 4
    assert impacts == sorted(impacts)
    # Square root, not linear: a hundredfold order is ten times the impact.
    assert impacts[2] == impacts[0] * 10
    assert _bps(impacts[0] * NOTIONAL) == Decimal("0.45")
    assert _bps(impacts[3] * NOTIONAL) == Decimal("14.14")

    # And the spread follows volatility rather than sitting at a constant.
    calm = FrictionContext(Decimal(4), Decimal(500000), Decimal("0.85"))
    wild = FrictionContext(Decimal(24), Decimal(500000), Decimal("0.85"))
    order = _order(Side.BUY)
    calm_spread = model.half_spread_fraction(order, calm)
    wild_spread = model.half_spread_fraction(order, wild)
    assert wild_spread == calm_spread * 6
    assert calm_spread != Decimal("0.0005")


# ------------------------------------------------- observed beats modelled, and
# ------------------------------------------------- assumed is never cheaper

def test_a_genuine_quote_prices_the_spread_and_is_recorded_as_observed() -> None:
    buffer, feed, now = _observed_market(bid=Decimal("799.60"), ask=Decimal("800.40"))
    provider = LiveFrictionContextProvider(feed, buffer, (INFY,), clock=lambda: now[0])
    context = provider(_order(Side.BUY))

    assert context.input_source == OBSERVED_QUOTE_AND_BARS
    assert context.priced_off_observed_quote
    # Half of the 0.80 quoted spread over the 800.00 mid: 5 bps, from the wire.
    assert context.observed_half_spread_fraction == Decimal("0.0005")
    # ATR and ADV came from the closed bars, not from a multiple of the order size: the
    # mean traded volume of those bars scaled by the 375 minutes of an NSE cash session.
    assert context.atr == Decimal(2)
    assert context.average_daily_volume == Decimal(9500) * Decimal(375)
    assert context.provenance()["inputSource"] == OBSERVED_QUOTE_AND_BARS

    model = MarketFrictionModel()
    assert model.half_spread_fraction(_order(Side.BUY), context) == Decimal("0.0005")


def test_assumed_inputs_are_never_cheaper_than_observed_inputs() -> None:
    model = MarketFrictionModel()
    order = _order(Side.BUY)
    buffer, feed, now = _observed_market(bid=Decimal("799.60"), ask=Decimal("800.40"))
    observed = LiveFrictionContextProvider(feed, buffer, (INFY,), clock=lambda: now[0])(order)
    # Same bars, no quote on the wire: the spread falls back to the assumption's floor.
    quiet_buffer, quiet_feed, quiet_now = _observed_market()
    no_quote = LiveFrictionContextProvider(
        quiet_feed, quiet_buffer, (INFY,), clock=lambda: quiet_now[0]
    )(order)
    # Nothing observed at all.
    assumed = LiveFrictionContextProvider()(order)

    assert no_quote.input_source == OBSERVED_BARS_ASSUMED_SPREAD
    assert assumed.input_source == ASSUMED
    assert no_quote.observed_half_spread_fraction is None
    assert assumed.provenance()["observedHalfSpreadFraction"] is None

    spreads = [
        model.half_spread_fraction(order, context)
        for context in (observed, no_quote, assumed)
    ]
    assert spreads[0] < spreads[1] == spreads[2]
    # The assumed spread is wider than the 5 bps the fabricated context always charged,
    # and wider than what the observed bars alone would have modelled (1.47 bps here).
    assert spreads[1] == Decimal("0.0025")
    assert spreads[1] > model.half_spread_fraction(order, _fabricated_context(order))

    costs = [
        model.evaluate(order, context).total_friction for context in (observed, no_quote, assumed)
    ]
    assert costs[0] < costs[1] <= costs[2]


def test_a_quote_without_bars_still_prices_the_spread_but_assumes_the_depth() -> None:
    order = _order(Side.BUY)
    buffer, _, now = _observed_market(minutes=1, bid=Decimal("799.60"), ask=Decimal("800.40"))
    context = LiveFrictionContextProvider(None, buffer, (INFY,), clock=lambda: now[0])(order)
    assert context.input_source == OBSERVED_QUOTE
    assert context.observed_half_spread_fraction == Decimal("0.0005")
    assert context.average_daily_volume == Decimal(25000)


@pytest.mark.parametrize(
    "bid,ask",
    [
        (None, None),
        (Decimal("800.40"), None),
        (Decimal(0), Decimal("800.40")),
        (Decimal(801), Decimal(799)),
    ],
)
def test_a_missing_or_crossed_quote_assumes_rather_than_invents_a_tight_market(bid, ask) -> None:
    order = _order(Side.BUY)
    buffer, feed, now = _observed_market(bid=bid, ask=ask)
    context = LiveFrictionContextProvider(feed, buffer, (INFY,), clock=lambda: now[0])(order)
    assert context.observed_half_spread_fraction is None
    assert context.assumed_half_spread_floor == Decimal("0.0025")


def test_a_stale_quote_is_not_treated_as_the_current_market() -> None:
    order = _order(Side.BUY)
    buffer, feed, now = _observed_market(bid=Decimal("799.60"), ask=Decimal("800.40"))
    now[0] = now[0] + timedelta(minutes=30)
    context = LiveFrictionContextProvider(feed, buffer, (INFY,), clock=lambda: now[0])(order)
    assert context.observed_half_spread_fraction is None


def test_a_failing_market_lookup_degrades_to_assumed_inputs_and_never_raises() -> None:
    class Exploding:
        def fetch_ohlcv(self, *args, **kwargs):
            raise RuntimeError("feed down")

        def latest(self, symbol):
            raise RuntimeError("buffer down")

    context = LiveFrictionContextProvider(Exploding(), Exploding(), (INFY,))(_order(Side.BUY))
    assert context.input_source == ASSUMED
    assert context.assumed_half_spread_floor == Decimal("0.0025")

    # An unknown symbol is a cold start, not an exception.
    unknown = LiveFrictionContextProvider(None, None, ())(_order(Side.BUY))
    assert unknown.input_source == ASSUMED


def test_replay_inputs_still_come_from_the_shared_helper() -> None:
    from quant_ai.backtesting.replay import HistoricalReplayHarness
    from quant_ai.marketdata.models import Candle

    bars = tuple(
        Candle(
            INFY,
            T0 + timedelta(minutes=i),
            PRICE,
            PRICE + Decimal(1),
            PRICE - Decimal(1),
            PRICE,
            Decimal(10000),
        )
        for i in range(20)
    )
    context = HistoricalReplayHarness._friction_context(bars)
    assert context == friction_context_from_bars(bars, liquidity_score=Decimal("0.90"))
    # Replay is unchanged: real ATR and ADV, no quote, no assumed floor.
    assert (context.atr, context.average_daily_volume) == (Decimal(2), Decimal(10000) * 390)
    assert context.observed_half_spread_fraction is None
    assert context.assumed_half_spread_floor == Decimal(0)


# ------------------------------------------------------------- the broker path

def _live_broker(tmp_path, provider):
    return PaperBrokerService(
        tmp_path / "live.sqlite",
        starting_capital=Decimal(100000),
        friction_context_provider=provider,
    )


def test_a_live_fill_is_priced_from_the_observed_market_and_proves_which_inputs(tmp_path) -> None:
    buffer, feed, now = _observed_market(bid=Decimal("799.60"), ask=Decimal("800.40"))
    provider = LiveFrictionContextProvider(feed, buffer, (INFY,), clock=lambda: now[0])
    broker = _live_broker(tmp_path, provider)
    evidence = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill"}
    fill = broker.submit_with_evidence(_order(Side.BUY), evidence, "live-observed-1")

    # 5 bps of observed half spread plus a fraction of a bp of impact, not 25 bps assumed.
    assert Decimal("800.40") < fill.average_price < Decimal("800.42")
    assert {item.code for item in broker.cost_entries("tenant")} >= {"BROKERAGE", "SPREAD", "STT"}

    payload = json.loads(
        broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0]
    )
    proof = payload["fill"]["friction"]
    assert proof["schema"] == "pramana.fill_friction.v1"
    assert proof["inputs"]["inputSource"] == OBSERVED_QUOTE_AND_BARS
    assert proof["inputs"]["observedHalfSpreadFraction"] == "0.0005"
    assert proof["charges"]["BROKERAGE"] == "20"
    broker.close()


def test_an_unobserved_market_fills_conservatively_and_says_so(tmp_path) -> None:
    def exploding(order):
        raise RuntimeError("no market data")

    broker = _live_broker(tmp_path, exploding)
    evidence = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill"}
    fill = broker.submit_with_evidence(_order(Side.BUY), evidence, "live-assumed-1")

    # A provider that blows up must not lose the tick, and must not fill cheaply either.
    assert fill.average_price > PRICE * Decimal("1.0025")
    payload = json.loads(
        broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0]
    )
    assert payload["fill"]["friction"]["inputs"]["inputSource"] == ASSUMED
    broker.close()


def test_an_explicit_context_still_wins_so_replay_stays_reproducible(tmp_path) -> None:
    broker = _live_broker(tmp_path, lambda order: assumed_friction_context(order))
    broker.set_friction_context(FrictionContext(Decimal(0), Decimal(1000000)))
    fill = broker.buy(_order(Side.BUY))
    assert fill.average_price == PRICE
    broker.close()


def test_sizing_still_consumes_the_worst_case_entry_price() -> None:
    model = MarketFrictionModel()
    # The caps that bound a fill are unchanged, so the size the plan allows is unchanged.
    assert model.worst_case_execution_price(PRICE, Side.BUY) == PRICE * Decimal("1.03")
    assert model.worst_case_execution_price(PRICE, Side.SELL) == PRICE * Decimal("0.97")

    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000), Decimal("0.8"), Decimal("0.2"),
            expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
        )
    )
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))
    sizer = PositionSizer()
    worst = model.worst_case_execution_price(PRICE, Side.BUY)
    assert sizer.quantity_from_plan(plan, portfolio, PRICE, worst_entry_price=worst) <= (
        sizer.quantity_from_plan(plan, portfolio, PRICE)
    )
    # Where stop risk is the binding cap, the adverse fill still shrinks the size: the
    # friction model feeds sizing through worst_entry_price and that link is intact.
    wide_stop = replace(plan, stop_loss_fraction=Decimal("0.20"))
    naive = sizer.quantity_from_plan(wide_stop, portfolio, PRICE)
    guarded = sizer.quantity_from_plan(wide_stop, portfolio, PRICE, worst_entry_price=worst)
    assert 0 < guarded < naive


def test_the_ghost_runtime_wires_observed_inputs_and_broker_charges(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    from quant_ai.daemon import build_ghost_runner

    runner = build_ghost_runner(
        zerodha_api_key="k", zerodha_access_token="t", zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: "INFY"}, ib_client=object(), ib_contracts=(),
        database=tmp_path / "ghost.db", log_path=tmp_path / "ghost.log", instrument=INFY,
        include_ibkr=False,
    )
    broker = runner.daemon.tracker.broker
    provider = broker._friction_context_provider
    assert isinstance(provider, LiveFrictionContextProvider)
    assert provider.feed is runner.daemon.scheduler.pipeline.market_feed
    assert "INFY" in provider.instruments
    assert broker.friction_model.brokerage_schedule.india_cap_per_order == Decimal(20)
    # Before the first tick arrives there is nothing to observe, so the engine assumes.
    assert broker._context_for(_order(Side.BUY)).input_source == ASSUMED
    broker.close()
