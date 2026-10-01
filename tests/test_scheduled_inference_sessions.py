"""Frozen official NSE sessions; mocked models, real reservations, no network/orders."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_astra_challenger import client, payload, sdk
from test_pilot_closure import INSTRUMENT, runner_for

from quant_ai.domain.models import Market, PortfolioSnapshot
from quant_ai.execution.opportunity_universe import CatalogResearchSnapshot
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.intelligence.headline_sentiment import HeadlineSentimentScorer
from quant_ai.intelligence.sandbox import (
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.llm import spend
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.challenger import ChallengerConsensusClient, _ChallengerBudget
from quant_ai.marketdata.timeframes import DailyHistoryProvider

IST = ZoneInfo('Asia/Kolkata')


def at(day, hour=10):
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=IST)


class AnalysisReached(Exception):
    pass


def tables(db, names):
    return {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in names}


@pytest.mark.parametrize('moment', [at('2026-10-02'), at('2026-10-03'), at('2026-10-04'),
                                    at('2026-10-05', 8), at('2026-10-05', 9), at('2026-10-05', 16)])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_closed_session_precedes_sentiment_models_and_all_reservations(tmp_path, monkeypatch, moment, asynchronous):
    monkeypatch.setenv(spend.LIMIT_ENV, '2.50')
    monkeypatch.setenv(spend.DB_ENV, str(tmp_path / 'spend.sqlite'))
    monkeypatch.setattr(spend, 'utc_day', lambda: moment.date().isoformat())
    assert spend.initialize(day=(moment.date() - timedelta(days=1)).isoformat())
    budget = SqliteAIBudget(tmp_path / 'tokens.sqlite', daily_call_limit=20,
                           daily_token_limit=300000, clock=lambda: moment)
    adapter = sdk(payload(stance='NEUTRAL'))
    primary = AnthropicSwarmClient(client=adapter, budget=budget)
    shadow_budget = _ChallengerBudget(budget)
    challenger, requests = client(budget=shadow_budget)
    pair = ChallengerConsensusClient(primary, challenger)
    scorer = HeadlineSentimentScorer(client=primary)
    class Pipeline:
        macro = SandboxMacroIndicatorProvider()
        news = SandboxNewsSentimentProvider()
        def run(self, *args, **kwargs):
            pytest.fail('closed synchronous analysis entered')
        async def run_async(self, *args, **kwargs):
            await scorer.score('INFY', ['synthetic growth'])
            await pair.generate_trading_consensus('synthetic identical evidence')
            raise AnalysisReached()
    scheduler = AutonomousCadenceScheduler(Pipeline(), calendar=MarketCalendar(holidays=default_holidays()))
    before_tokens = tables(budget._connection, ('ai_budget', 'ai_budget_aggregate', 'ai_budget_requests'))
    before_shadow = tables(shadow_budget.sample._connection, ('ai_budget', 'ai_budget_aggregate', 'ai_budget_requests'))
    with sqlite3.connect(tmp_path / 'spend.sqlite') as db:
        before_dollars = tables(db, ('ai_spend_days', 'ai_spend_requests'))
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), Decimal(100000))
    try:
        args = (INSTRUMENT, moment, None, portfolio)
        result = asyncio.run(scheduler.run_tick_async(*args, country='INDIA')) if asynchronous else scheduler.run_tick(*args, country='INDIA')
        assert result.market_state != MarketState.REGULAR_HOURS
        assert result.paper_order_ids == () and scheduler.last_result is None
        assert adapter.messages.create.await_count == 0 and not requests and scorer.calls == 0
        assert tables(budget._connection, ('ai_budget', 'ai_budget_aggregate', 'ai_budget_requests')) == before_tokens
        assert tables(shadow_budget.sample._connection, ('ai_budget', 'ai_budget_aggregate', 'ai_budget_requests')) == before_shadow
        with sqlite3.connect(tmp_path / 'spend.sqlite') as db:
            assert tables(db, ('ai_spend_days', 'ai_spend_requests')) == before_dollars
    finally:
        shadow_budget.sample.close()
        budget.close()


@pytest.mark.parametrize('moment', [at('2026-10-05'), at('2026-02-01')])
def test_normal_and_official_special_open_sessions_can_reach_mocked_analysis(tmp_path, monkeypatch, moment):
    monkeypatch.setenv(spend.LIMIT_ENV, '2.50')
    monkeypatch.setenv(spend.DB_ENV, str(tmp_path / 'spend.sqlite'))
    monkeypatch.setattr(spend, 'utc_day', lambda: moment.date().isoformat())
    assert spend.initialize(day=(moment.date() - timedelta(days=1)).isoformat())
    budget = SqliteAIBudget(tmp_path / 'tokens.sqlite', daily_call_limit=20,
                           daily_token_limit=300000, clock=lambda: moment)
    adapter = sdk(payload(stance='NEUTRAL'))
    primary = AnthropicSwarmClient(client=adapter, budget=budget)
    shadow_budget = _ChallengerBudget(budget)
    challenger, requests = client(budget=shadow_budget)
    pair, scorer = ChallengerConsensusClient(primary, challenger), HeadlineSentimentScorer(client=primary)
    class Pipeline:
        async def run_async(self, *args, **kwargs):
            await scorer.score('INFY', ['synthetic growth'])
            await pair.generate_trading_consensus('synthetic identical evidence')
            raise AnalysisReached()
    scheduler = AutonomousCadenceScheduler(Pipeline(), calendar=MarketCalendar(holidays=default_holidays()))
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), Decimal(100000))
    try:
        assert scheduler.calendar.state(Market.INDIA, moment, exchange='NSE') == MarketState.REGULAR_HOURS
        with pytest.raises(AnalysisReached):
            asyncio.run(scheduler.run_tick_async(INSTRUMENT, moment, None, portfolio, country='INDIA'))
        assert adapter.messages.create.await_count == 2 and len(requests) == 1 and scorer.calls == 1
        assert budget._connection.execute('SELECT calls FROM ai_budget_aggregate').fetchone()[0] == 3
        with sqlite3.connect(tmp_path / 'spend.sqlite') as db:
            assert db.execute('SELECT COUNT(*) FROM ai_spend_requests').fetchone()[0] == 3
    finally:
        shadow_budget.sample.close()
        budget.close()


@pytest.mark.parametrize('moment', [at('2026-10-02'), at('2026-10-03'), at('2026-10-04'), at('2026-10-05', 16)])
def test_closed_selector_never_reads_history_or_starts_worker(monkeypatch, moment):
    history = DailyHistoryProvider(None)
    monkeypatch.setattr(history, 'cached', lambda *a: pytest.fail('closed scan read history'))
    snapshot = CatalogResearchSnapshot((INSTRUMENT,), history, {}, MarketCalendar(holidays=default_holidays()))
    assert snapshot(moment) is None
    assert snapshot._worker is None


def test_actual_holiday_daemon_keeps_protection_and_reconciliation_without_analysis(tmp_path, monkeypatch):
    runner = runner_for(tmp_path)
    daemon, counts = runner.daemon, {'protection': 0, 'reconcile': 0}
    moment = at('2026-10-02')
    monkeypatch.setattr(daemon.scheduler.pipeline, 'run_async', lambda *a, **k: pytest.fail('holiday pipeline'))
    original_protection, original_reconcile = daemon.sweep_protective_exits, daemon._reconcile_pilot
    def protect(now):
        counts['protection'] += 1
        return original_protection(now)
    def reconcile():
        counts['reconcile'] += 1
        return original_reconcile()
    monkeypatch.setattr(daemon, 'sweep_protective_exits', protect)
    monkeypatch.setattr(daemon, '_reconcile_pilot', reconcile)
    try:
        result = asyncio.run(daemon.run_once(moment))
        assert result.market_state == MarketState.CLOSED and result.paper_order_ids == ()
        assert counts == {'protection': 1, 'reconcile': 1}
        assert not daemon.tracker.broker.ledger_entries('pilot')
    finally:
        daemon.tracker.broker.close()


@pytest.mark.parametrize('holidays', [{}, {'NSE': frozenset()}])
def test_pilot_refuses_calendar_override_that_reopens_known_nse_holidays_before_store_creation(tmp_path, holidays):
    from types import SimpleNamespace

    from quant_ai.daemon import build_ghost_runner
    from quant_ai.governance.directives import FounderDirectives

    ledger = tmp_path / 'must-not-exist.sqlite'
    with pytest.raises(ValueError, match='pilot_nse_known_holiday_reopened'):
        build_ghost_runner(zerodha_api_key='synthetic', zerodha_access_token='synthetic',
            zerodha_instrument_tokens=(1,), zerodha_symbol_by_token={1: 'INFY'},
            ib_client=SimpleNamespace(), ib_contracts=(), include_ibkr=False,
            database=ledger, tenant_id='pilot', log_path=tmp_path / 'events.jsonl',
            xai_directory=tmp_path / 'proofs', halt_file=tmp_path / 'HALT',
            directives=FounderDirectives(watchlist=(INSTRUMENT,)), pilot_mode=True, holidays=holidays)
    assert not ledger.exists()
