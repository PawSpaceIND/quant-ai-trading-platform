"""Minute warm-up during NSE hours. Synthetic provider replies only; no network, no orders.

On 23 September 2026 the engine was restarted during the session. Kite's minute history
ended with the minute still in progress, the provider refused the whole reply with
``kite_history_session_invalid`` for all twelve symbols, and the technical specialist
started cold. These tests pin the fix: the in-progress bar is checked and then left out,
and nothing else that ends after ``now`` gets through.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_kite_daily_history import INSTRUMENT, Client, encoded

from quant_ai.marketdata import kite_history as kh
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.ticker_stream import TickBuffer

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 9, 23, 9, 15, tzinfo=IST)
NOW = datetime(2026, 9, 23, 10, 47, 30, tzinfo=IST)


def minute_rows(first: datetime, count: int, *, step: timedelta = timedelta(minutes=1)):
    return [
        [(first + step * index).isoformat(), 100, 101, 99, 100, 1000 + index]
        for index in range(count)
    ]


def session_rows():
    """09:15 through 10:47 inclusive: 92 closed bars, then the 10:47 bar still forming."""
    return minute_rows(OPEN, 93)


def fetch(rows):
    provider = kh.KiteMinuteWarmupProvider("fixture", "dummy", client=Client(candles=encoded(rows)))
    return provider.fetch(INSTRUMENT, NOW.astimezone(timezone.utc))


def test_in_session_reply_keeps_every_closed_bar_and_leaves_out_the_forming_one():
    result = fetch(session_rows())

    assert len(result) == 92
    assert result[-1].timestamp == datetime(2026, 9, 23, 10, 47, tzinfo=IST)
    assert all(candle.timestamp <= NOW for candle in result)
    assert result[-1].volume == Decimal(1091)


def test_in_session_reply_seeds_a_warm_feed():
    now = NOW.astimezone(timezone.utc)
    feed = LiveTickMarketDataFeed(TickBuffer(clock=lambda: now), clock=lambda: now)

    feed.seed_closed_candles(fetch(session_rows()), now)

    assert len(feed.fetch_recent_ohlcv(INSTRUMENT, now, count=60)) == 60


def test_forming_bar_is_still_validated_before_it_is_left_out():
    rows = session_rows()
    rows[-1][-1] = 1000.5

    with pytest.raises(kh.KiteHistoryError, match="kite_history_volume_invalid"):
        fetch(rows)


def test_a_bar_that_has_not_started_yet_is_refused():
    rows = session_rows()[:-1] + minute_rows(datetime(2026, 9, 23, 10, 48, tzinfo=IST), 1)

    with pytest.raises(kh.KiteHistoryError, match="kite_history_session_invalid"):
        fetch(rows)


def test_only_the_final_row_may_be_in_progress():
    rows = session_rows()[:-1] + minute_rows(
        datetime(2026, 9, 23, 10, 47, tzinfo=IST), 2, step=timedelta(seconds=10)
    )

    with pytest.raises(kh.KiteHistoryError, match="kite_history_session_invalid"):
        fetch(rows)


def test_a_reply_after_the_close_is_unchanged():
    after_close = datetime(2026, 9, 23, 16, 0, tzinfo=IST).astimezone(timezone.utc)
    rows = minute_rows(datetime(2026, 9, 23, 14, 30, tzinfo=IST), 60)
    provider = kh.KiteMinuteWarmupProvider("fixture", "dummy", client=Client(candles=encoded(rows)))

    result = provider.fetch(INSTRUMENT, after_close)

    assert len(result) == 60
    assert result[-1].timestamp == datetime(2026, 9, 23, 15, 30, tzinfo=IST)
