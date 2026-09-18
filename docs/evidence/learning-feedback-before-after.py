"""Same public-interface reproductions against unchanged main and repaired source."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from quant_ai.analytics.attribution import AgentAttributionEngine, restore_from_journal
from quant_ai.analytics.decision_journal import insert_decision
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.execution.paper_ledger import PaperBrokerService

NOW = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)

@pytest.fixture
def broker(tmp_path):
    value = PaperBrokerService(tmp_path / "paper.db")
    yield value
    value.close()

def add(broker):
    insert_decision(broker, {
        "decision_id":"entry", "tenant_id":"pilot", "order_id":"paper-entry", "symbol":"INFY",
        "market":"INDIA", "asset_class":"EQUITY", "decided_at":NOW.isoformat(),
        "exit_at":NOW.replace(hour=7).isoformat(), "side":"BUY", "stance":"BUY",
        "governance":"filled", "confidence":"0.7", "realized_net_pnl":"100", "regime":"ranging",
        "agents":json.dumps({"supporter":{"stance":"BUY","confidence":"0.7"},
                              "dissenter":{"stance":"SELL","confidence":"0.8"}}),
    })

def test_dissenter_is_not_rewarded_for_supporters_profit(broker):
    add(broker)
    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id="pilot")
    assert engine.weight_for("dissenter") == (Decimal(1), "unscored")
    assert engine.attribution()[0].pnl == Decimal(100)

def test_repeated_outcome_consumption_is_idempotent(broker):
    add(broker)
    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id="pilot")
    restore_from_journal(engine, broker, tenant_id="pilot")
    assert engine.attribution()[0].observations == 1

def test_resolved_outcome_changes_later_evidence_not_latest_trace(broker):
    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id="pilot")
    add(broker)
    ev = AgentEvidence("supporter",AgentDomain.TECHNICAL,"INFY",Stance.BUY,Decimal("0.5"),
                       Decimal("0.01"),Decimal("0.001"),(),NOW,0)
    assert engine.weight_evidence((ev,))[0].confidence == Decimal("0.625")
