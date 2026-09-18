"""Actual pipeline, timeframe and read-only runtime display regressions."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_pilot_closure import publish_tick, runner_for
from test_timeframes_and_regime import (
    AAPL,
    NOW,
    StubHistory,
    _live_pipeline,
    _minutes,
    _plan,
    _portfolio,
    _rising_daily,
)

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.pipeline import MarketContext, SwarmMarketAnalysisPipeline
from quant_ai.intelligence.regime import RegimeSummary, classify, primary_regime
from quant_ai.intelligence.regime_observation import MAX_OBSERVATIONS, RegimeObservationStore
from quant_ai.marketdata.models import Candle


def test_real_context_exports_each_timeframe_count_not_technical_window(tmp_path):
    pipeline, _, _ = _live_pipeline(tmp_path, StubHistory(_rising_daily()), llm=False)
    context = pipeline._market_context(AAPL, (), NOW)
    metrics = context.metrics()
    assert "regime_daily_bars_used" in metrics, "daily bars_used missing from persisted features"
    assert metrics["regime_daily_bars_used"] == Decimal(context.daily.bars_used)
    assert metrics["regime_intraday_bars_used"] == Decimal(context.intraday.bars_used)
    assert metrics["regime_timeframe"] == context.primary.timeframe


def test_pipeline_retains_an_operator_readable_observation_without_refetch(tmp_path):
    history = StubHistory(_rising_daily())
    pipeline, _, _ = _live_pipeline(tmp_path, history, llm=False)
    pipeline._market_context(AAPL, (), NOW)
    assert hasattr(pipeline, "regime_observations"), "no runtime regime observation source"
    before = history.calls
    payload = pipeline.regime_observations.snapshot(AAPL, NOW + timedelta(seconds=3))
    assert payload["state"] == "observed"
    assert payload["daily"]["barsUsed"] == 40
    assert history.calls == before



OPEN = datetime(2026, 9, 17, 3, 45, tzinfo=timezone.utc)
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def bars(count=60, pattern="up", timeframe="15m"):
    prices = [Decimal(100 + i) for i in range(count)]
    if pattern == "flat":
        prices = [Decimal(100)] * count
    elif pattern == "down":
        prices = [Decimal(200 - i) for i in range(count)]
    elif pattern == "volatile" and count >= 6:
        prices = [Decimal(100 + i % 2) for i in range(count - 6)] + list(map(Decimal, (100,140,100,145,100,150)))
    start = OPEN - timedelta(days=100)
    return tuple(Candle(INFY, start + timedelta(minutes=15 * i), p, p+1, p-1, p, Decimal(100)) for i,p in enumerate(prices))


def context(daily_count=0, intraday_count=60, pattern="up"):
    daily_bars, intraday_bars = bars(daily_count, pattern), bars(intraday_count, pattern)
    daily, intraday = classify(daily_bars,timeframe="1d"), classify(intraday_bars,timeframe="15m")
    return MarketContext(intraday_bars,daily_bars,intraday,daily,primary_regime(daily,intraday))


@pytest.mark.parametrize("pattern,label",[("up","trending_up"),("down","trending_down"),("flat","ranging"),("volatile","high_volatility")])
def test_actual_classifier_labels_and_lookback_counts_reach_observation(pattern,label):
    store=RegimeObservationStore(); c=context(pattern=pattern)
    store.record(INFY,OPEN,60,c)
    result=store.snapshot(INFY,OPEN)
    assert result["intraday"]["label"] == label
    assert result["intraday"]["barsUsed"] == 40
    assert result["intraday"]["barsAvailable"] == 60
    assert result["daily"]["barsUsed"] == 0
    assert result["selectedTimeframe"] == "15m" and result["selectionReason"] == "intraday_fallback"


def test_short_history_and_daily_priority_are_not_forced_into_a_signal():
    store=RegimeObservationStore()
    for daily,intraday,selected in [(0,19,"15m"),(19,19,"1d"),(60,60,"1d")]:
        store.record(INFY,OPEN,60,context(daily,intraday))
        result=store.snapshot(INFY,OPEN)
        assert result["selectedTimeframe"] == selected
        assert result["minimumBars"] == 20 and result["lookbackBars"] == 40
        assert result["selectedLabel"] == ("trending_up" if daily == 60 else "insufficient_history")


def test_technical_count_is_actual_length_even_at_the_rolling_window_limit():
    for count in (0,19,49,50,60):
        assert SwarmMarketAnalysisPipeline._technical_metrics((Decimal(100),)*count)["price_history_bars"] == count


def test_sixty_one_minute_bars_are_four_fifteen_minute_bars_not_sixty(tmp_path):
    pipeline,_,_=_live_pipeline(tmp_path,None,llm=False)
    pipeline.intraday_window=timedelta(minutes=60)
    minute_bars=_minutes(INFY,OPEN,60,Decimal(100))
    current=OPEN+timedelta(minutes=60)
    c=pipeline._market_context(INFY,minute_bars,current)
    payload=pipeline.regime_observations.snapshot(INFY,current)
    assert len(c.intraday_bars) == payload["intraday"]["barsAvailable"] == 4
    assert payload["technicalBars"] == 60
    assert payload["selectedLabel"] == "insufficient_history"


def test_intraday_rebuild_classifies_after_actual_twenty_closed_buckets(tmp_path):
    pipeline,_,_=_live_pipeline(tmp_path,None,llm=False)
    for minutes,label in [(285,"insufficient_history"),(300,"trending_up")]:
        current=OPEN+timedelta(minutes=minutes)
        wider = _minutes(INFY,OPEN,minutes,Decimal(100))
        pipeline.market_feed = SimpleNamespace(fetch_ohlcv=lambda *_args, saved=wider: saved)
        pipeline._market_context(INFY,wider[-60:],current)
        result=pipeline.regime_observations.snapshot(INFY,current)
        assert result["technicalBars"] == 60
        assert result["intraday"]["barsUsed"] == minutes//15
        assert result["intraday"]["label"] == label


@pytest.mark.parametrize("async_path",[False,True])
def test_real_pipeline_carries_same_timeframe_facts_into_journal_features(tmp_path,async_path):
    pipeline,_,_=_live_pipeline(tmp_path,StubHistory(_rising_daily()),llm=False)
    if async_path:
        result=asyncio.run(pipeline.run_async(AAPL,NOW,_plan(),_portfolio(),quantity=10,country="USA",tenant_id="visibility"))
    else:
        result=pipeline.run(AAPL,NOW,_plan(),_portfolio(),quantity=10,country="USA",tenant_id="visibility")
    payload=pipeline.regime_observations.snapshot(AAPL,NOW)
    assert payload["daily"]["barsUsed"] == result.features["regime_daily_bars_used"] == 40
    assert payload["intraday"]["barsUsed"] == result.features["regime_intraday_bars_used"]
    assert payload["technicalBars"] == result.features["price_history_bars"]
    assert result.features["regime_timeframe"] == payload["selectedTimeframe"]


def test_real_runtime_payload_reads_observation_without_history_requests(tmp_path):
    runner=runner_for(tmp_path)
    runner.daemon.clock=lambda: OPEN
    pipeline=runner.daemon.scheduler.pipeline
    pipeline.regime_observations.record(INFY,OPEN,60,context(60,19))
    pipeline.history=SimpleNamespace(fetch=lambda *_args: pytest.fail("Telemetry must not fetch history"))
    publish_tick(runner,"100",OPEN)
    runner.daemon.protection_tick(OPEN)
    import json
    payload=json.loads(runner.daemon.tracker.broker._connection.execute("SELECT payload FROM pilot_runtime").fetchone()[0])
    row=next(r for r in payload["watchlist"] if r["symbol"]=="INFY")
    assert "regimeContext" in row
    record=row["regimeContext"]
    assert record["state"] == "observed" and record["daily"]["barsUsed"] == 40
    assert record["intraday"]["barsUsed"] == 19
    runner.daemon.tracker.broker._connection.close()


def test_unknown_restart_and_future_context_never_fabricate_counts():
    store=RegimeObservationStore()
    assert store.snapshot(INFY,OPEN)["state"] == "not_observed"
    store.record(INFY,OPEN,60,context())
    assert store.snapshot(INFY,OPEN-timedelta(microseconds=1))["state"] == "future_observation"
    assert RegimeObservationStore().snapshot(INFY,OPEN)["state"] == "not_observed"
    assert "daily" not in store.snapshot(INFY,OPEN-timedelta(seconds=1))


def test_invalid_clock_is_unknown_not_fresh():
    store=RegimeObservationStore()
    assert store.snapshot(INFY,OPEN.replace(tzinfo=None))["state"] == "unavailable"
    with pytest.raises(ValueError,match="aware_time"):
        store.record(INFY,OPEN.replace(tzinfo=None),60,context())


@pytest.mark.parametrize("technical_count",[-1,True,1.5,1000001])
def test_bad_count_is_unavailable_not_a_misleading_number(technical_count):
    store=RegimeObservationStore(); store.record(INFY,OPEN,technical_count,context())
    assert store.snapshot(INFY,OPEN)["state"] == "unavailable"


@pytest.mark.parametrize("defect",["wrong_timeframe","wrong_count","wrong_label","nonfinite","foreign_primary","wrong_priority"])
def test_inconsistent_diagnostics_do_not_interrupt_the_pipeline_or_claim_readiness(defect):
    c=context(60,60)
    if defect == "wrong_timeframe": c=replace(c,daily=replace(c.daily,timeframe="1m"))
    if defect == "wrong_count": c=replace(c,daily=replace(c.daily,bars_used=3))
    if defect == "wrong_label": c=replace(c,daily=RegimeSummary.insufficient("1d",40))
    if defect == "nonfinite": c=replace(c,daily=replace(c.daily,trend_strength=Decimal("NaN")))
    if defect == "foreign_primary": c=replace(c,primary=replace(c.primary,timeframe="1m"))
    if defect == "wrong_priority": c=replace(c,primary=c.intraday)
    store=RegimeObservationStore(); store.record(INFY,OPEN,60,c)
    assert store.snapshot(INFY,OPEN)["state"] == "unavailable"


def test_old_analysis_cannot_replace_newer_context_and_readers_cannot_mutate_saved_facts():
    store=RegimeObservationStore(); later=OPEN+timedelta(seconds=10)
    store.record(INFY,later,60,context(60,60))
    store.record(INFY,OPEN,1,context(0,1))
    first=store.snapshot(INFY,later); first["daily"]["barsUsed"]=-1
    second=store.snapshot(INFY,later+timedelta(seconds=5))
    assert second["daily"]["barsUsed"] == 40 and second["ageSeconds"] == 5
    assert second["observedAt"] == later.isoformat()


def test_metadata_and_venue_do_not_share_an_observation():
    store=RegimeObservationStore(); store.record(INFY,OPEN,60,context())
    for other in (replace(INFY,exchange="BSE"),replace(INFY,metadata={"source":"different"})):
        assert store.snapshot(other,OPEN)["state"] == "not_observed"


def test_observation_cache_is_bounded_without_changing_watchlist_caps():
    store=RegimeObservationStore()
    for i in range(MAX_OBSERVATIONS+1): store.record(replace(INFY,symbol=f"S{i}"),OPEN,60,context())
    assert len(store._observations) == MAX_OBSERVATIONS
    assert store.snapshot(replace(INFY,symbol="S0"),OPEN)["state"] == "not_observed"


def test_protection_read_does_not_wait_on_observation_capture():
    store=RegimeObservationStore(); entered=Event(); release=Event()
    def hold():
        with store._lock:
            entered.set(); release.wait(2)
    worker=Thread(target=hold); worker.start()
    try:
        assert entered.wait(1)
        assert store.snapshot(INFY,OPEN)["state"] == "busy"
    finally:
        release.set(); worker.join(3)


@pytest.mark.parametrize("defect",["typed","timeframe","used_count","classification","metric"])
def test_summary_validation_has_direct_boundary_evidence(defect):
    from quant_ai.intelligence.regime_observation import _summary
    value=context(60,60).daily
    if defect == "typed": value=SimpleNamespace(**vars(value),classified=True)
    if defect == "timeframe": value=replace(value,timeframe="1m")
    if defect == "used_count": value=replace(value,bars_used=39)
    if defect == "classification": value=RegimeSummary.insufficient("1d",40)
    if defect == "metric": value=replace(value,trend_strength=Decimal("Infinity"))
    with pytest.raises(ValueError):
        _summary(value,60,"1d")


def test_metrics_report_intraday_fallback_without_renaming_it_daily():
    c=context(0,60)
    assert c.metrics()["regime_timeframe"] == "15m"
    assert c.metrics()["regime_daily_bars_used"] == 0
    assert c.metrics()["regime_intraday_bars_used"] == 40
