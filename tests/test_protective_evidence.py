import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ProtectiveExitEngine, market_feed_mark_resolver
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.orchestration.cadence import CadenceMarketReader


def setup(tmp_path):
    broker=PaperBrokerService(tmp_path/'ledger.db')
    broker.buy(OrderIntent('INFY',Market.INDIA,Side.BUY,2,D(100),'test',tenant_id='pilot',stop_price=D(95),take_profit_price=D(110)))
    return broker


def test_protection_fill_evidence_and_cooldown_survive_restart_together(tmp_path):
    broker=setup(tmp_path)
    now=datetime.now(timezone.utc)
    buffer=TickBuffer();buffer.put(LiveTick('INFY',D(90),D(100),None,None,now,'synthetic_ws'))
    instrument=Instrument('INFY',Market.INDIA,AssetClass.EQUITY,'INR','NSE')
    resolver=market_feed_mark_resolver(None,lambda _:instrument,CadenceMarketReader(buffer),lambda:now)
    result=ProtectiveExitEngine(broker,resolver,tenant_id='pilot').evaluate(now)[0]
    restarted=PaperBrokerService(tmp_path/'ledger.db')
    evidence=json.loads(restarted._connection.execute('SELECT payload FROM paper_protection_evidence WHERE order_id=?',(result.order_id,)).fetchone()[0])
    assert evidence['trigger']=='STOP_LOSS'
    assert evidence['threshold']=='95'
    assert evidence['mark_observation']['source']=='synthetic_ws'
    assert evidence['mark_observation']['source_timestamp']==now.isoformat()
    assert D(evidence['fill']['price']) < 90
    assert evidence['order_id']==result.order_id
    assert 'input_matrix' not in evidence
    assert not restarted.get_positions('pilot')
    assert restarted.exit_cooldown_until('INFY',Market.INDIA,AssetClass.EQUITY,'pilot')==now+timedelta(minutes=30)
    assert restarted.reconcile('pilot')['status']=='matched'


def test_evidence_failure_rolls_back_paper_fill_costs_and_cooldown(tmp_path):
    broker=setup(tmp_path)
    before=tuple(broker._connection.iterdump())
    broker._connection.execute("CREATE TRIGGER fail_protection BEFORE INSERT ON paper_protection_evidence BEGIN SELECT RAISE(ABORT,'test evidence failure'); END")
    engine=ProtectiveExitEngine(broker,lambda _:D(90),tenant_id='pilot')
    with pytest.raises(sqlite3.IntegrityError,match='test evidence failure'):
        engine.evaluate()
    broker._connection.execute('DROP TRIGGER fail_protection')
    assert tuple(broker._connection.iterdump())==before
    assert engine.evaluate()[0].filled
    assert engine.evaluate()==()


def test_custom_price_sources_are_not_mislabeled_live_and_targets_are_distinct(tmp_path):
    broker=setup(tmp_path)
    result=ProtectiveExitEngine(broker,lambda _:D(115),tenant_id='pilot',re_entry_cooldown=timedelta(0)).evaluate()[0]
    row=json.loads(broker._connection.execute('SELECT payload FROM paper_protection_evidence').fetchone()[0])
    assert result.filled and row['trigger']=='TAKE_PROFIT'
    assert row['mark_observation']['source']=='custom_resolver_unverified'
    assert row['mark_observation']['source_timestamp'] is None
    assert broker.exit_cooldown_until('INFY',Market.INDIA,AssetClass.EQUITY,'pilot') is None


def test_stale_feed_fallback_cannot_create_protective_fill(tmp_path):
    from types import SimpleNamespace
    broker=setup(tmp_path)
    now=datetime.now(timezone.utc)
    feed=SimpleNamespace(latest_tick=lambda _:SimpleNamespace(last_price=D(90),timestamp=now-timedelta(seconds=121)))
    resolver=market_feed_mark_resolver(feed,lambda _:None,clock=lambda:now)
    assert ProtectiveExitEngine(broker,resolver,tenant_id='pilot').evaluate(now)==()
    assert broker._connection.execute('SELECT COUNT(*) FROM paper_protection_evidence').fetchone()[0]==0
    assert broker.get_positions('pilot')[0].quantity==2
