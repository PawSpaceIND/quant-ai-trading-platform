"""Failure/restart drills for the governed paper fill's canonical decision evidence."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def setup_broker(tmp_path):
    broker = PaperBrokerService(tmp_path / 'ledger.sqlite', starting_capital=D(100000))
    broker.configure_pilot((Instrument('INFY', Market.INDIA, AssetClass.EQUITY, 'INR', 'NSE'),), 'pilot')
    return broker


def execute(broker, logger, *, side=Side.BUY, decision_id='synthetic-atomic-proof'):
    now = datetime.now(timezone.utc)
    proposal = TradeProposal(
        decision_id, 'INFY', Market.INDIA, 'INDIA', AssetClass.EQUITY, side, 10,
        D(100), D(95) if side == Side.BUY else D(105), D(110) if side == Side.BUY else D(90),
        D('.8'), D('.02'), D('.01'), ('synthetic declared rationale',),
    )
    evidence = (AgentEvidence('synthetic-technical', AgentDomain.TECHNICAL, 'INFY', Stance.BUY,
                             D('.8'), D('.02'), D('.01'), ('synthetic input',), now, 0),)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D('.80'), D('.20'), expected_edge=D('.02'), requested_mode=RiskMode.BALANCED))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=logger, allow_position_scaling=True)
    return runtime._execute_proposal(
        AgentAnalysisRequest('INFY', Market.INDIA, AssetClass.EQUITY, now, {}), evidence,
        proposal, plan, PortfolioSnapshot(D(100000), D(0), D(0)), None, 'pilot',
    )


def test_file_projection_failure_preserves_fill_evidence_and_restart_replay_guard(tmp_path, caplog):
    broker = setup_broker(tmp_path)
    logger = XAITraceLogger(tmp_path / 'unavailable-projection')
    logger.directory.rmdir()
    result = execute(broker, logger)
    assert result.fill is not None
    assert result.xai_trace.order_id == result.fill.order_id
    assert 'xai_file_projection_failed' in caplog.text
    broker.close()
    restarted = setup_broker(tmp_path)
    row = restarted._connection.execute('SELECT payload FROM paper_decision_evidence').fetchone()
    payload = json.loads(row[0])
    assert payload['order_id'] == result.fill.order_id
    assert payload['tenant_id'] == 'pilot'
    assert payload['schema'] == 'pramana.swarm_fill.v1'
    assert payload['approved_order']['stop_price'] == '95'
    assert payload['input_matrix'][0]['agent_id'] == 'synthetic-technical'
    assert payload['fill']['price'] == str(result.fill.average_price)
    assert restarted._connection.execute('SELECT tenant_id FROM paper_idempotency WHERE key=?',
                                         (payload['idempotency_key'],)).fetchone()[0] == 'pilot'
    replay = execute(restarted, XAITraceLogger())
    assert replay.fill is None and replay.risk_decision.reason == 'duplicate_order'
    assert len(restarted.ledger_entries('pilot')) == 1
    assert restarted.reconcile('pilot')['status'] == 'matched'


def test_evidence_storage_failure_rolls_back_fill_fees_and_idempotency_claim(tmp_path):
    broker = setup_broker(tmp_path)
    before = tuple(broker._connection.iterdump())
    broker._connection.execute("CREATE TRIGGER fail_trace BEFORE INSERT ON paper_decision_evidence BEGIN SELECT RAISE(ABORT,'trace unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError, match='trace unavailable'):
        execute(broker, XAITraceLogger())
    broker._connection.execute('DROP TRIGGER fail_trace')
    assert tuple(broker._connection.iterdump()) == before
    # A rolled-back attempt did not consume its retry key.
    assert execute(broker, XAITraceLogger()).fill is not None
    assert broker._connection.execute('SELECT COUNT(*) FROM paper_idempotency').fetchone()[0] == 1


def test_proof_preparation_failure_has_no_execution_side_effects(tmp_path, monkeypatch):
    broker = setup_broker(tmp_path)
    before = tuple(broker._connection.iterdump())
    logger = XAITraceLogger()
    def fail(*args):
        raise ValueError('synthetic preparation failure')
    monkeypatch.setattr(logger, 'build', fail)
    with pytest.raises(ValueError, match='synthetic preparation failure'):
        execute(broker, logger)
    assert tuple(broker._connection.iterdump()) == before


def test_covered_swarm_sell_has_separate_durable_evidence(tmp_path):
    broker = setup_broker(tmp_path)
    buy = execute(broker, XAITraceLogger())
    sell = execute(broker, XAITraceLogger(), side=Side.SELL, decision_id='synthetic-sell')
    assert buy.fill and sell.fill
    assert not broker.get_positions('pilot')
    records = [json.loads(r[0]) for r in broker._connection.execute('SELECT payload FROM paper_decision_evidence')]
    assert {p['approved_order']['side'] for p in records} == {'BUY', 'SELL'}
    assert {p['order_id'] for p in records} == {buy.fill.order_id, sell.fill.order_id}
    assert broker.reconcile('pilot')['status'] == 'matched'


def test_process_exit_after_commit_before_file_projection_retains_single_fill(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    child = subprocess.run([
        sys.executable, '-c',
        ("import os, runpy, sys; from pathlib import Path; "
        "m=runpy.run_path(sys.argv[1]); b=m['setup_broker'](Path(sys.argv[2])); "
        "logger=m['XAITraceLogger'](); logger.record=lambda trace: os._exit(23); "
        "m['execute'](b,logger)"),
        str(Path(__file__).resolve()), str(tmp_path),
    ], capture_output=True, timeout=15, check=False)
    assert child.returncode == 23, child.stderr.decode()
    broker = setup_broker(tmp_path)
    assert len(broker.ledger_entries('pilot')) == 1
    assert broker._connection.execute('SELECT COUNT(*) FROM paper_decision_evidence').fetchone()[0] == 1
    assert execute(broker, XAITraceLogger()).risk_decision.reason == 'duplicate_order'
    assert len(broker.ledger_entries('pilot')) == 1
    assert broker.reconcile('pilot')['status'] == 'matched'
