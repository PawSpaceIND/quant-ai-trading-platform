from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed, TickBarAggregator
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer, ZerodhaKiteTicker, _coerce_time
from quant_ai.orchestration.cadence import CadenceMarketReader

T0 = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def tick(seconds=0, price=100, volume=100):
    return LiveTick("INFY", D(price), D(volume), None, None, T0 + timedelta(seconds=seconds), "synthetic")


def test_late_future_and_duplicate_ticks_cannot_replace_or_refresh_latest_or_reach_callbacks():
    now = [T0]
    buffer = TickBuffer(clock=lambda: now[0])
    delivered = []
    buffer.subscribe(delivered.append)
    assert buffer.put(tick())
    assert not buffer.put(tick(-1, 1))
    assert not buffer.put(tick(1, 200))
    assert not buffer.put(tick())
    assert buffer.latest("INFY") == tick() and delivered == [tick()]
    now[0] += timedelta(seconds=121)
    assert CadenceMarketReader(buffer).market_data_status("INFY", now[0]) == (None, "Stale Market Data")
    assert buffer.integrity()["rejected"] == {"out_of_order_tick": 1, "future_tick": 1, "duplicate_tick": 1}
    assert buffer.put(tick(121, 110))  # A rejected future observation cannot poison the watermark.


@pytest.mark.parametrize("field,value", [("ltp", D("NaN")), ("ltp", D("sNaN")), ("ltp", D("Infinity")),
    ("ltp", D("1e999")), ("ltp", D(0)), ("volume", D(-1)), ("volume", D("NaN")), ("bid", D("NaN")), ("ask", D(-1))])
def test_bad_values_do_not_replace_good_tick_or_refresh_it(field, value):
    buffer = TickBuffer(clock=lambda: T0)
    buffer.put(tick())
    assert not buffer.put(replace(tick(), **{field: value}))
    assert buffer.latest("INFY") == tick()
    assert buffer.integrity()["rejected"] == {"invalid_tick_values": 1}


def test_same_second_distinct_updates_and_other_symbols_remain_usable():
    buffer = TickBuffer(clock=lambda: T0)
    assert buffer.put(tick()) and buffer.put(tick(price=101, volume=101))
    assert buffer.put(replace(tick(-1), symbol="TCS"))
    assert buffer.latest("INFY").ltp == 101 and buffer.latest("TCS").ltp == 100


def test_callbacks_follow_acceptance_order_across_producer_threads():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=1))
    entered, release = threading.Event(), threading.Event()
    delivered = []
    def listener(value):
        if value.ltp == 100:
            entered.set()
            assert release.wait(2)
        delivered.append(value.ltp)
    buffer.subscribe(listener)
    first = threading.Thread(target=buffer.put, args=(tick(),))
    second = threading.Thread(target=buffer.put, args=(tick(1, 101),))
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2); second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert delivered == [D(100), D(101)] and buffer.latest("INFY").ltp == 101


def test_closed_bar_cannot_be_recreated_after_clock_roll_or_late_tick():
    now = T0 + timedelta(minutes=2)
    aggregator = TickBarAggregator(clock=lambda: now)
    aggregator.ingest(tick(5))
    first = aggregator.closed_bars("INFY", T0 - timedelta(minutes=1), now, now)
    aggregator.ingest(tick(10, 1, 150))
    assert aggregator.closed_bars("INFY", T0 - timedelta(minutes=1), now, now) == first
    assert len(first) == 1 and first[0].close == 100


def test_late_ticks_cannot_change_same_bar_close_or_future_volume_baseline():
    now = T0 + timedelta(minutes=3)
    aggregator = TickBarAggregator(clock=lambda: now)
    for value in [tick(20, 100, 200), tick(10, 1, 100), tick(60, 101, 300), tick(30, 1, 100), tick(80, 102, 350)]:
        aggregator.ingest(value)
    bars = aggregator.closed_bars("INFY", T0, now, now)
    assert [(b.close, b.volume) for b in bars] == [(D(100), D(0)), (D(102), D(150))]


def test_future_tick_cannot_poison_forming_bar_and_new_cash_session_rebaselines_volume():
    now = [T0]
    a = TickBarAggregator(clock=lambda: now[0])
    a.ingest(tick(3600, 1, 900))
    a.ingest(tick(0, 100, 100))
    now[0] += timedelta(days=1, seconds=1)
    a.ingest(tick(86400, 110, 1000))
    a.ingest(tick(86401, 111, 1020))
    bars = a.closed_bars("INFY", T0, now[0] + timedelta(minutes=1), now[0] + timedelta(minutes=1))
    assert [(b.open, b.close, b.volume) for b in bars] == [(D(100), D(100), D(0)), (D(110), D(111), D(20))]


@pytest.mark.parametrize("value,reason", [(tick(1), "Future Market Data"), (tick(-121), "Stale Market Data"),
    (replace(tick(), ltp=D("NaN")), "Invalid Market Data"), (replace(tick(), observed_at=None), "Invalid Market Data")])
def test_cadence_boundary_validates_even_a_replaced_buffer(value, reason):
    assert CadenceMarketReader(SimpleNamespace(latest=lambda _: value)).market_data_status("INFY", T0) == (None, reason)


def test_feed_boundary_rejects_future_tick_even_with_unlimited_maximum_age():
    buffer = SimpleNamespace(clock=lambda: T0, subscribe=lambda _: None, latest=lambda _: tick(1))
    feed = LiveTickMarketDataFeed(buffer, max_tick_age=None)
    with pytest.raises(ValueError, match="future_live_tick"):
        feed.latest_tick(INFY)


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/New_York"])
def test_sdk_naive_host_time_round_trips_epoch_in_each_host_timezone(zone):
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone; time.tzset()
        for stamp in [T0, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)]:
            assert _coerce_time(datetime.fromtimestamp(stamp.timestamp())) == stamp  # noqa: DTZ006 — reproduce the SDK conversion including DST folds
        assert _coerce_time(T0) == T0
        with pytest.raises(ValueError, match="missing_exchange_timestamp"):
            _coerce_time(None)
    finally:
        if old is None: os.environ.pop("TZ", None)
        else: os.environ["TZ"] = old
        time.tzset()


def test_zerodha_uses_exchange_time_and_skips_bad_packets_without_losing_next_symbol():
    async def scenario():
        buffer = TickBuffer(clock=lambda: T0)
        stream = ZerodhaKiteTicker("synthetic", "synthetic", [1, 2], {1: "INFY", 2: "TCS"}, buffer)
        stream._loop = asyncio.get_running_loop()
        base = {"instrument_token": 1, "exchange_timestamp": T0 - timedelta(minutes=5), "last_price": 100, "volume_traded": 200}
        stream._on_ticks(None, [base, {**base, "exchange_timestamp": None, "timestamp": T0},
            {**base, "depth": {"buy": [{"price": 100}, {"price": "broken"}]}},
            {**base, "instrument_token": 1.5}, {**base, "instrument_token": 3},
            {**base, "instrument_token": 2, "exchange_timestamp": T0, "last_price": 200}])
        await asyncio.sleep(.01)
        assert buffer.latest("INFY").observed_at == base["exchange_timestamp"]
        assert CadenceMarketReader(buffer).market_data_status("INFY", T0)[1] == "Stale Market Data"
        assert buffer.latest("TCS").ltp == 200
        assert buffer.integrity()["rejected"] == {"invalid_zerodha_payload": 3, "unmapped_tick": 1}
    asyncio.run(scenario())
