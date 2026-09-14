import json
import sqlite3
from decimal import Decimal

import pytest
from test_friction_replay import _dataset

from quant_ai.backtesting.replay import HistoricalReplayHarness
from quant_ai.domain.models import RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def test_replay_valuations_survive_close_and_match_every_bar(tmp_path):
    database = tmp_path / "valuations.sqlite"
    broker = PaperBrokerService(database)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    dataset = _dataset()
    result = HistoricalReplayHarness(broker, plan, quantity=10, tenant_id="demo").run(dataset)
    fills = len(broker.ledger_entries("demo"))
    broker.close()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT timestamp, payload FROM paper_replay_valuations WHERE tenant_id=? ORDER BY timestamp", ("demo",)).fetchall()
        assert db.execute("SELECT COUNT(*) FROM paper_replay_valuations WHERE tenant_id=?", ("other",)).fetchone()[0] == 0
    assert len(rows) == len(dataset.bars) > fills > 0
    for (timestamp, raw), expected_time, expected_equity in zip(rows, result.timestamps, result.equity_curve):
        value = json.loads(raw)
        assert timestamp == expected_time.isoformat()
        assert value["totalEquity"] == pytest.approx(float(expected_equity), abs=1e-8)
        assert value["totalEquity"] == pytest.approx(value["cash"] + sum(p["marketValue"] for p in value["holdings"]), abs=1e-8)
        expected_drawdown = max(0, (value["highWaterMark"] - value["totalEquity"]) / value["highWaterMark"])
        assert value["drawdown"] == pytest.approx(expected_drawdown, abs=1e-12)
        assert value["markMode"] == "historical_replay"
    assert json.loads(rows[-1][1])["totalEquity"] == pytest.approx(float(result.final_snapshot.equity), abs=1e-8)
