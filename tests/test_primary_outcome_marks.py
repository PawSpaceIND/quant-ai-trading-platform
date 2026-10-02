"""Primary journal marks must have fresh observed timestamps, not old prices."""
import logging
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_decision_journal import CALENDAR, INFY, SESSION, TENANT, pilot_broker, seed, synthetic_row

from quant_ai.analytics import decision_journal
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer


def daemon_mark_fixture(now):
    buffer=TickBuffer(clock=lambda:now)
    feed=LiveTickMarketDataFeed(buffer)
    daemon=object.__new__(AutonomousTradingDaemon)
    daemon.instruments=(INFY,)
    daemon.clock=lambda:now
    daemon.tracker=SimpleNamespace(market_feed=feed)
    return daemon,buffer


@pytest.mark.parametrize('age,expected',[(3600,None),(61,None),(60,Decimal(110)),(0,Decimal(110))])
def test_primary_mark_refuses_stale_quotes_even_when_shared_feed_accepts_them(age,expected):
    now=SESSION+timedelta(minutes=60)
    daemon,buffer=daemon_mark_fixture(now)
    buffer.put(LiveTick('INFY',Decimal(110),Decimal(10),None,None,
                        now-timedelta(seconds=age),'zerodha'))
    assert daemon.tracker.market_feed.latest_tick(INFY).last_price==Decimal(110)
    assert daemon._decision_mark('INFY')==expected


def test_actual_daemon_outcome_resolution_keeps_stale_unknown_then_accepts_fresh(tmp_path):
    now=SESSION+timedelta(minutes=60)
    daemon,buffer=daemon_mark_fixture(now)
    broker=pilot_broker(tmp_path)
    daemon.tracker.broker=broker
    daemon.tenant_id=TENANT
    daemon.instrument=INFY
    daemon.scheduler=SimpleNamespace(calendar=CALENDAR)
    daemon.trade_evidence=None
    daemon._logger=logging.getLogger(__name__)
    seed(broker,[synthetic_row('stale-outcome',reference_price='100')])
    buffer.put(LiveTick('INFY',Decimal(110),Decimal(10),None,None,SESSION,'zerodha'))
    daemon._resolve_decision_outcomes(now)
    row=decision_journal.load_rows(broker,tenant_id=TENANT)[0]
    assert row['forward_return_60m'] is None
    assert row['resolved_at'] is None
    assert daemon.decision_outcomes['skipped']==1
    buffer.put(LiveTick('INFY',Decimal(110),Decimal(10),None,None,now,'zerodha'))
    daemon._resolve_decision_outcomes(now)
    assert decision_journal.load_rows(broker,tenant_id=TENANT)[0]['forward_return_60m']=='0.1'


@pytest.mark.parametrize('observed',['missing','naive','future'])
def test_unqualified_timestamp_cannot_become_primary_outcome(observed):
    daemon,_=daemon_mark_fixture(SESSION)
    stamp={'missing':None,'naive':SESSION.replace(tzinfo=None),
           'future':SESSION+timedelta(seconds=1)}[observed]
    daemon.tracker.market_feed=SimpleNamespace(latest_tick=lambda instrument:
        SimpleNamespace(instrument=instrument,timestamp=stamp,last_price=Decimal(110)))
    assert daemon._decision_mark('INFY') is None


def test_resolver_cutoff_is_used_instead_of_later_clock():
    now=SESSION+timedelta(minutes=60)
    daemon,buffer=daemon_mark_fixture(now)
    buffer.put(LiveTick('INFY',Decimal(110),Decimal(10),None,None,now,'zerodha'))
    assert daemon._decision_mark('INFY',as_of=now-timedelta(seconds=1)) is None


@pytest.mark.parametrize('prior_age,expected',[(1,Decimal(110)),(61,None)])
def test_newer_tick_during_analysis_does_not_hide_qualified_cutoff_evidence(prior_age,expected):
    cutoff=SESSION+timedelta(minutes=60)
    daemon,buffer=daemon_mark_fixture(cutoff+timedelta(seconds=5))
    buffer.put(LiveTick('INFY',Decimal(110),Decimal(10),None,None,
                        cutoff-timedelta(seconds=prior_age),'zerodha'))
    buffer.put(LiveTick('INFY',Decimal(200),Decimal(10),None,None,
                        cutoff+timedelta(seconds=5),'zerodha'))
    assert daemon.tracker.market_feed.latest_tick(INFY).last_price==Decimal(200)
    assert daemon._decision_mark('INFY',as_of=cutoff)==expected
