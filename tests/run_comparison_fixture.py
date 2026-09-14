"""Real historical harness and paper telemetry driven only by synthetic inputs."""

import argparse
import json
import logging
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from quant_ai.backtesting.replay import (
    HistoricalFundamentalEvent,
    HistoricalMacroEvent,
    HistoricalReplayDataset,
    HistoricalReplayHarness,
)
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.friction import FeeSchedule, MarketFrictionModel
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.session import MarketCalendar, default_holidays
from quant_ai.execution.telemetry import PilotTelemetry
from quant_ai.governance.runtime_manifest import digest, encoded
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.validation.run_comparison import build, capture

START = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=70)


def dataset():
    instrument = Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    bars = []
    price = Decimal(100)
    for i in range(70):
        close = price + (Decimal(".1") if i < 40 else Decimal("-.2"))
        bars.append(
            Candle(
                instrument,
                START + timedelta(minutes=i),
                price,
                max(price, close) + Decimal(".1"),
                min(price, close) - Decimal(".1"),
                close,
                Decimal(10000 + i * 10),
            )
        )
        price = close
    macro = (
        HistoricalMacroEvent(
            START,
            {
                "US10Y": Decimal("3.5"),
                "INDIA10Y": Decimal("6.5"),
                "BRENT": Decimal(60),
                "GOLD": Decimal(2600),
                "DXY": Decimal(90),
            },
        ),
        HistoricalMacroEvent(
            START + timedelta(minutes=40),
            {
                "US10Y": Decimal("6.5"),
                "INDIA10Y": Decimal(8),
                "BRENT": Decimal(120),
                "GOLD": Decimal(1900),
                "DXY": Decimal(120),
            },
        ),
    )
    fundamentals = (
        HistoricalFundamentalEvent(
            START,
            "RELIANCE",
            {
                "pe": Decimal(20),
                "debt_equity": Decimal(".2"),
                "operating_margin": Decimal(".3"),
                "fcf_yield": Decimal(".04"),
            },
        ),
        HistoricalFundamentalEvent(
            START + timedelta(minutes=40),
            "RELIANCE",
            {
                "pe": Decimal(60),
                "debt_equity": Decimal(2),
                "operating_margin": Decimal(".05"),
                "fcf_yield": Decimal(".005"),
            },
        ),
    )
    news = [
        NewsSignal("RELIANCE", "Synthetic demand", Decimal(".9"), "synthetic", START),
        NewsSignal(
            "GEOPOLITICAL",
            "Synthetic peace",
            Decimal(".9"),
            "synthetic",
            START + timedelta(seconds=10),
        ),
    ]
    for i in (40, 41, 42):
        news.extend(
            [
                NewsSignal(
                    "RELIANCE",
                    f"Synthetic risk {i}",
                    Decimal("-.9"),
                    "synthetic",
                    START + timedelta(minutes=i),
                ),
                NewsSignal(
                    "GEOPOLITICAL",
                    f"Synthetic conflict {i}",
                    Decimal("-.9"),
                    "synthetic",
                    START + timedelta(minutes=i, seconds=10),
                ),
            ]
        )
    return HistoricalReplayDataset(tuple(bars), macro, tuple(news), fundamentals)


def fixture(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal(".8"),
            Decimal(".2"),
            expected_edge=Decimal(".02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    data = dataset()
    old_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with closing(PaperBrokerService(folder / "replay.sqlite")) as broker:
            result = HistoricalReplayHarness(
                broker,
                plan,
                quantity=10,
                country="India",
                tenant_id="replay",
                xai_logger=XAITraceLogger(folder / "proofs"),
            ).run(data)
            fills = [
                dict(r)
                for r in broker._connection.execute(
                    "SELECT * FROM paper_ledger WHERE tenant_id='replay' ORDER BY id"
                )
            ]
        assert len(fills) == 2
        model = MarketFrictionModel(
            fee_schedule=replace(
                FeeSchedule.zero(), name="synthetic", india_exchange_rate=Decimal(".001")
            ),
            gamma=Decimal(0),
            spread_atr_multiplier=Decimal(0),
            fixed_slippage_bps=Decimal(0),
        )
        with closing(PaperBrokerService(folder / "paper.sqlite", friction_model=model)) as broker:
            broker.get_margin("default")
            tick = SimpleNamespace(last_price=Decimal(100), timestamp=START)
            feed = SimpleNamespace(latest_tick=lambda instrument: tick)
            tracker = PortfolioTracker(broker, feed)
            daemon = SimpleNamespace(
                tracker=tracker,
                tenant_id="default",
                instruments=(data.bars[0].instrument,),
                scheduler=SimpleNamespace(
                    calendar=MarketCalendar(holidays=default_holidays()),
                    last_run_at=None,
                    pipeline=SimpleNamespace(
                        news=None,
                        macro=None,
                        fundamentals=None,
                        runtime=SimpleNamespace(max_open_positions=4),
                    ),
                ),
                strategy_manifest=None,
                trade_evidence=None,
                reconciliation=None,
                kill_switch=SimpleNamespace(engaged=False, reason=None),
                plan=plan,
            )

            def coverage(at):
                daemon.protection_coverage = broker.protection_coverage("default", at)
                return daemon.protection_coverage["status"] == "complete"

            daemon.check_protection_coverage = coverage
            telemetry = PilotTelemetry(daemon)
            cursor = 0
            for bar in data.bars:
                while (
                    cursor < len(fills)
                    and datetime.fromisoformat(fills[cursor]["created_at"]) <= bar.timestamp
                ):
                    f = fills[cursor]
                    cursor += 1
                    # Deliberate size and price differences, not independent live strategy performance.
                    price = Decimal(f["fill_price"]) * Decimal("1.01")
                    broker.set_friction_context(
                        None, execution_time=datetime.fromisoformat(f["created_at"])
                    )
                    order = OrderIntent(
                        f["symbol"],
                        Market.INDIA,
                        Side(f["side"]),
                        8,
                        price,
                        "synthetic-run-comparison",
                        AssetClass.EQUITY,
                        "default",
                        Decimal(f["stop_price"]) if f["stop_price"] else None,
                        Decimal(f["take_profit_price"]) if f["take_profit_price"] else None,
                    )
                    broker.submit(order)
                tick.last_price = bar.close
                tick.timestamp = bar.timestamp
                telemetry.publish(bar.timestamp)
            paper = capture(folder / "paper.sqlite", "default", "paper")
            replay = capture(folder / "replay.sqlite", "replay", "replay", result.replay_run_id)
            clean = build(paper, replay, START.isoformat(), END.isoformat())
            # Preserve one invalid minute, one absent observation and one late observation.
            rows = broker._connection.execute(
                "SELECT timestamp,payload FROM paper_live_valuations WHERE tenant_id='default' ORDER BY timestamp"
            ).fetchall()
            p = json.loads(rows[20]["payload"])
            p["status"] = "invalid"
            p["totalEquity"] = None
            broker._connection.execute(
                "UPDATE paper_live_valuations SET payload=? WHERE tenant_id='default' AND timestamp=?",
                (json.dumps(p), rows[20]["timestamp"]),
            )
            broker._connection.execute(
                "DELETE FROM paper_live_valuations WHERE tenant_id='default' AND timestamp=?",
                (rows[30]["timestamp"],),
            )
            p = json.loads(rows[40]["payload"])
            p["updatedAt"] = (START + timedelta(minutes=40, seconds=15)).isoformat()
            broker._connection.execute(
                "UPDATE paper_live_valuations SET payload=? WHERE tenant_id='default' AND timestamp=?",
                (json.dumps(p), rows[40]["timestamp"]),
            )
            broker._connection.commit()
        gapped = build(
            capture(folder / "paper.sqlite", "default", "paper"),
            replay,
            START.isoformat(),
            END.isoformat(),
        )
        for name, report in [("complete", clean), ("gapped", gapped)]:
            (folder / f"{name}.json").write_text(
                json.dumps({"payload": encoded(report), "sha256": digest(report)})
            )
        return {"complete": clean, "gapped": gapped, "runId": result.replay_run_id}
    finally:
        logging.disable(old_logging)


def restore_fixture(folder):
    from quant_ai.operations.recovery_bundle import sqlite_backup
    from quant_ai.operations.research_recovery import inspect

    folder = Path(folder)
    original = json.loads(json.loads((folder / "gapped.json").read_text())["payload"])
    before = inspect(folder / "replay.sqlite", "replay_ledger")
    sqlite_backup(folder / "replay.sqlite", folder / "replay-restored.sqlite")
    after = inspect(folder / "replay-restored.sqlite", "replay_ledger")
    assert before == after
    report = build(
        capture(folder / "paper.sqlite", "default", "paper"),
        capture(
            folder / "replay-restored.sqlite",
            "replay",
            "replay",
            original["configuration"]["replayRunId"],
        ),
        START.isoformat(),
        END.isoformat(),
    )
    assert {k: v for k, v in report.items() if k != "generatedAt"} == {
        k: v for k, v in original.items() if k != "generatedAt"
    }
    (folder / "gapped-restored.json").write_text(
        json.dumps({"payload": encoded(report), "sha256": digest(report)})
    )
    return {
        "runComparisonRecovery": "pass",
        "verification": after,
        "regeneratedComparisonMatches": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.restore:
        print(json.dumps(restore_fixture(args.directory)))
        raise SystemExit(0)
    r = fixture(args.directory)
    print(
        json.dumps(
            {
                "status": "pass",
                "runId": r["runId"],
                "points": len(r["gapped"]["curve"]),
                "gaps": sum(bool(p["issues"]) for p in r["gapped"]["curve"]),
                "fills": {k: len(v) for k, v in r["gapped"]["fills"].items()},
                "externalCalls": 0,
            }
        )
    )
