"""Atomic, source-labelled valuations consumed by the UI and evidence exporter."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument
from quant_ai.execution.session import MarketState


class PilotTelemetry:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.broker = daemon.tracker.broker
        with self.broker._lock, self.broker._connection:
            self.broker._connection.executescript("""
                CREATE TABLE IF NOT EXISTS paper_live_valuations (
                    tenant_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    ledger_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,timestamp));
                CREATE TABLE IF NOT EXISTS pilot_runtime (
                    tenant_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def publish(self, now: datetime) -> dict:
        daemon = self.daemon
        with self.broker._lock, self.broker._connection:
            metrics = daemon.tracker.metrics(now)
            positions = self.broker.get_positions(daemon.tenant_id)
            instruments = {item.symbol: item for item in daemon.instruments}
            holdings = []
            for marked, position in zip(metrics.positions, positions):
                instrument = instruments.get(position.symbol) or daemon.tracker.instrument_resolver(position)
                fresh, timestamp = self.fresh(instrument, now, marked.current_price)
                holdings.append({
                    "symbol": marked.symbol, "market": marked.market.value,
                    "assetClass": marked.asset_class.value, "currency": instrument.currency,
                    "quantity": marked.quantity, "averageEntry": float(marked.average_entry_price),
                    "markPrice": float(marked.current_price), "marketValue": float(marked.market_value),
                    "unrealizedPnl": float(marked.unrealized_pnl),
                    "markSource": "live_tick" if fresh else "stale_or_entry_fallback",
                    "markTimestamp": timestamp, "fresh": fresh,
                    "stopPrice": float(position.stop_price) if position.stop_price else None,
                    "takeProfitPrice": float(position.take_profit_price) if position.take_profit_price else None,
                })
            margin = self.broker.get_margin(daemon.tenant_id)
            ledger_id = self.broker._connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM paper_ledger WHERE tenant_id=?", (daemon.tenant_id,)
            ).fetchone()[0]
            all_fresh = all(row["fresh"] for row in holdings)
            previous_session = now.astimezone(ZoneInfo("Asia/Kolkata")).replace(hour=13, minute=0, second=0, microsecond=0) - timedelta(days=1)
            for _ in range(370):
                if daemon.scheduler.calendar.state(daemon.instruments[0].market, previous_session) == MarketState.REGULAR_HOURS:
                    break
                previous_session -= timedelta(days=1)
            payload = {
                "status": "ok" if all_fresh else "degraded", "tenantId": daemon.tenant_id,
                "currency": daemon.instruments[0].currency, "markMode": "engine_live",
                "markDisclaimer": "Engine valuations from timestamped ticks. Stale marks are flagged; no guarantee of fill price.",
                "cash": float(metrics.cash_balance), "totalEquity": float(metrics.total_equity),
                "startingCapital": float(margin.starting_capital),
                "realizedPnl": float(metrics.realized_pnl), "unrealizedPnl": float(metrics.unrealized_pnl),
                "dailyPnl": float(metrics.daily_total_pnl), "highWaterMark": float(metrics.high_water_mark),
                "drawdown": float(metrics.drawdown_fraction), "holdings": holdings,
                "updatedAt": now.isoformat(), "allMarksFresh": all_fresh,
                "previousSessionDate": previous_session.date().isoformat(),
                "sessionDate": now.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
                "qualifyingSession": all_fresh and all(self.fresh(i, now)[0] for i in daemon.instruments)
                    and daemon.scheduler.calendar.state(daemon.instruments[0].market, now) == MarketState.REGULAR_HOURS,
            }
            watchlist = [{"symbol": i.symbol, "currency": i.currency, "market": i.market.value,
                          "assetClass": i.asset_class.value, "exchange": i.exchange,
                          "fresh": self.fresh(i, now)[0]} for i in daemon.instruments]
            runtime = {
                "status": "running", "mode": "paper", "updatedAt": now.isoformat(),
                "halted": daemon.kill_switch.engaged, "haltReason": daemon.kill_switch.reason,
                "watchlist": watchlist, "protectionIntervalSeconds": 1,
                "strategyManifest": daemon.strategy_manifest.summary if daemon.strategy_manifest else None,
                "tradeEvidence": ({key: value for key, value in daemon.trade_evidence.items()
                    if key not in {"episodes", "openPositions"}}
                    if daemon.trade_evidence and daemon.trade_evidence["ledgerId"] == ledger_id else None),
                "reconciliation": ({**daemon.reconciliation,
                    "status": "outdated" if daemon.reconciliation["status"] == "matched"
                        and daemon.reconciliation["ledgerId"] != ledger_id
                        else daemon.reconciliation["status"]}
                    if daemon.reconciliation else None),
                "lastAnalysisAt": daemon.scheduler.last_run_at.isoformat() if daemon.scheduler.last_run_at else None,
                "providers": {"news": type(daemon.scheduler.pipeline.news).__name__,
                              "macro": type(daemon.scheduler.pipeline.macro).__name__,
                              "fundamentals": type(daemon.scheduler.pipeline.fundamentals).__name__},
                "limits": {"dailyLoss": float(min(daemon.plan.max_daily_loss_fraction, Decimal('.02'))),
                           "grossExposure": float(min(daemon.plan.max_gross_exposure_fraction, Decimal('.60'))),
                           "drawdown": float(min(daemon.plan.max_drawdown_fraction, Decimal('.10'))),
                           "maxPositions": daemon.scheduler.pipeline.runtime.max_open_positions},
            }
            bucket = now.replace(second=0, microsecond=0).isoformat()
            self.broker._connection.execute("INSERT OR REPLACE INTO paper_live_valuations VALUES (?,?,?,?)",
                                            (daemon.tenant_id, bucket, ledger_id, json.dumps(payload)))
            self.broker._connection.execute("INSERT OR REPLACE INTO pilot_runtime VALUES (?,?,?)",
                                            (daemon.tenant_id, now.isoformat(), json.dumps(runtime)))
            return payload

    def fresh(self, instrument: Instrument, now: datetime, expected_price: Decimal | None = None) -> tuple[bool, str | None]:
        try:
            tick = self.daemon.tracker.market_feed.latest_tick(instrument)
            if expected_price is not None and tick.last_price != expected_price:
                return False, None
            age = (now.astimezone(timezone.utc) - tick.timestamp.astimezone(timezone.utc)).total_seconds()
            return 0 <= age <= 120, tick.timestamp.isoformat()
        except (ValueError, RuntimeError, TimeoutError, ConnectionError, OSError):
            return False, None
