"""Atomic, source-labelled valuations consumed by the UI and evidence exporter."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument
from quant_ai.execution.protection_state import positive_level
from quant_ai.execution.session import MarketState
from quant_ai.intelligence.regime_observation import RegimeObservationStore


class PilotTelemetry:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.broker = daemon.tracker.broker
        self._strategy_report = None
        self._strategy_sha = None
        self._strategy_evidence = None
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
        try:
            return self._publish_valid(now)
        except (ValueError, DecimalException, OverflowError) as error:
            from quant_ai.execution.ledger_integrity import PaperLedgerDataError
            reason = str(error) if isinstance(error, PaperLedgerDataError) else "invalid_account_or_valuation"
            return self.publish_unavailable(now, reason)

    def _publish_valid(self, now: datetime) -> dict:
        daemon = self.daemon
        with self.broker._lock, self.broker._connection:
            daemon.check_protection_coverage(now)
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
            manifest = daemon.strategy_manifest.summary if daemon.strategy_manifest else None
            strategy_sha = manifest.get("sha256") if manifest else None
            if self._strategy_report is not daemon.trade_evidence or self._strategy_sha != strategy_sha:
                from quant_ai.validation.strategy_attribution import select_strategy_evidence
                selected = select_strategy_evidence(daemon.trade_evidence, strategy_sha)
                self._strategy_evidence = ({key: value for key, value in selected.items()
                    if key not in {"completedEpisodeOrderIds", "openEpisodeOrderIds", "unresolvedOrderIds"}}
                    if selected else None)
                self._strategy_report = daemon.trade_evidence
                self._strategy_sha = strategy_sha
            strategy_evidence = self._strategy_evidence if self._strategy_evidence and self._strategy_evidence["ledgerId"] == ledger_id else None
            binding_age = ((now - datetime.fromisoformat(manifest["checkedAt"])).total_seconds()
                if manifest and manifest.get("checkedAt") else float("inf"))
            strategy_observation = {"manifestSha256": strategy_sha,
                "eligible": bool(manifest and manifest.get("status") == "matched" and -5 <= binding_age <= 10
                    and strategy_evidence and strategy_evidence["foreignOpenEpisodes"] == 0
                    and strategy_evidence["unresolvedEpisodes"] == 0 and not daemon.kill_switch.engaged)}
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
                "strategyObservation": strategy_observation,
                "qualifyingSession": all_fresh and all(self.fresh(i, now)[0] for i in daemon.instruments)
                    and daemon.scheduler.calendar.state(daemon.instruments[0].market, now) == MarketState.REGULAR_HOURS,
            }
            runtime = self._runtime_payload(now, ledger_id, strategy_evidence,
                {"status": "available", "checkedAt": now.isoformat(), "ledgerId": ledger_id},
                # Gross exposure as the snapshot provider computes it, so the figure the
                # overnight cap is reported against is the one the gate actually judges.
                book={"grossExposure": sum((item.market_value for item in metrics.positions), Decimal(0)),
                      "equity": metrics.total_equity})
            self._write_valuation(now, ledger_id, payload)
            self.broker._connection.execute("INSERT OR REPLACE INTO pilot_runtime VALUES (?,?,?)",
                                            (daemon.tenant_id, now.isoformat(), json.dumps(runtime, allow_nan=False)))
            return payload

    def _write_valuation(self, now, ledger_id, payload):
        bucket = now.replace(second=0, microsecond=0).isoformat()
        if payload["status"] != "invalid":
            row = self.broker._connection.execute(
                "SELECT payload FROM paper_live_valuations WHERE tenant_id=? AND timestamp=?",
                (self.daemon.tenant_id, bucket),
            ).fetchone()
            if row and json.loads(row[0]).get("status") == "invalid":
                return  # Preserve the failed minute even after source repair within it.
        self.broker._connection.execute("INSERT OR REPLACE INTO paper_live_valuations VALUES (?,?,?,?)",
            (self.daemon.tenant_id, bucket, ledger_id, json.dumps(payload, allow_nan=False)))

    def _runtime_payload(self, now, ledger_id, strategy_evidence, valuation, book=None):
        daemon = self.daemon
        watchlist = []
        for instrument in daemon.instruments:
            fresh, timestamp = self.fresh(instrument, now)
            watchlist.append({"symbol": instrument.symbol, "currency": instrument.currency,
                "market": instrument.market.value, "assetClass": instrument.asset_class.value,
                "exchange": instrument.exchange, "fresh": fresh, "tickTimestamp": timestamp,
                "regimeContext": self._regime_context(instrument, now)})
        return {
            "status": "running", "mode": "paper", "updatedAt": now.isoformat(),
            "halted": daemon.kill_switch.engaged, "haltReason": daemon.kill_switch.reason,
            "watchlist": watchlist, "protectionIntervalSeconds": 1,
            "strategyManifest": daemon.strategy_manifest.summary if daemon.strategy_manifest else None,
            "strategyEvidence": strategy_evidence,
            "valuation": valuation,
            "marketDataIntegrity": (daemon.tracker.market_feed.buffer.integrity()
                if hasattr(daemon.tracker.market_feed, "buffer") else None),
            "protectionCoverage": daemon.protection_coverage,
            "protectionSweep": self._protection_sweep(now),
            "riskGates": self._risk_gates(now, book),
            "exploration": self._exploration(now),
            "tradeEvidence": ({key: value for key, value in daemon.trade_evidence.items()
                if key not in {"episodes", "openPositions", "strategyAttribution"}}
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

    def _regime_context(self, instrument, now):
        # Read only the snapshot already produced by analysis; never invoke a provider.
        store = getattr(self.daemon.scheduler.pipeline, "regime_observations", None)
        if not isinstance(store, RegimeObservationStore):
            return {"schema": "pramana.regime_observation.v1", "state": "not_observed"}
        return store.snapshot(instrument, now)

    def _protection_sweep(self, now: datetime) -> dict:
        """Whether each stored stop can currently be acted on, symbol by symbol.

        ``protectionCoverage`` proves a stop is *stored*. This is the other half: whether
        the engine was able to evaluate it on the last sweep. A symbol it could not price,
        or one whose quote a corporate action re-based, is carrying an unenforced stop
        right now, and nothing says so today until the grace period expires and a halt
        reason appears. By then the position has been unprotected for minutes.

        ``sweptAt`` is published because an empty list means two different things - swept
        and clean, or never swept - and a page that rendered them alike would be reporting
        the engine's silence as safety.
        """
        daemon = self.daemon
        # Every read here is defensive on purpose. This block only ever describes the
        # protection machinery; a daemon assembled without a piece of it must produce a
        # payload that says so, not an exception that costs the publish its valuation.
        engine = getattr(daemon, "exit_engine", None)
        monitor = getattr(engine, "gap_monitor", None)
        swept_at = getattr(engine, "last_sweep_at", None)
        since = getattr(daemon, "unprotected_since", None) or {}
        halt_after = getattr(daemon, "unprotected_halt_seconds", None)
        return {
            "schema": "pramana.protection_sweep.v1",
            "tenantId": getattr(daemon, "tenant_id", None),
            "checkedAt": now.isoformat(),
            "sweptAt": swept_at.isoformat() if swept_at is not None else None,
            "unprotected": [
                self._unpriced_row(symbol, since, now)
                for symbol in sorted(getattr(engine, "unprotected", ()) or ())
            ],
            "rebased": sorted(getattr(engine, "rebased", ()) or ()),
            # None where the operator disabled the clock with a non-finite value: a
            # missing deadline is not a deadline of zero.
            "haltAfterSeconds": halt_after if isinstance(halt_after, (int, float))
                and math.isfinite(halt_after) else None,
            "gapMonitor": {"armed": monitor is not None,
                "unresolved": list(monitor.unresolved_state()) if monitor is not None else []},
        }

    def _unpriced_row(self, symbol: str, since: dict, now: datetime) -> dict:
        """One unpriced stop, with whether its market is open and when its halt clock started.

        ``unpricedSince`` runs at every hour; the halt counts only session time. After the
        close the two disagree, and a page that knew only the first told the operator,
        every evening a position was held, that entries would halt in 120 seconds when
        the halt clock was paused until the open (23 September 2026).

        Each added field is left out, never guessed, when the daemon cannot state it: a
        page must not read an unknown as "market closed".
        """
        row = {"symbol": symbol,
               "unpricedSince": since[symbol].isoformat() if symbol in since else None}
        expected = self._prices_expected(symbol, now)
        if expected is not None:
            row["pricesExpected"] = expected
        halt_clock = getattr(self.daemon, "unpriced_in_session_since", None)
        if isinstance(halt_clock, dict):
            started = halt_clock.get(symbol)
            row["haltClockSince"] = started.isoformat() if started is not None else None
        return row

    def _prices_expected(self, symbol: str, now: datetime) -> bool | None:
        """The daemon's own session answer for ``symbol``, or None when it cannot give one."""
        expected = getattr(self.daemon, "prices_expected", None)
        if not callable(expected):
            return None
        try:
            return bool(expected(symbol, now))
        except Exception:  # noqa: BLE001 - describing the book must not stop the publish
            return None

    def _risk_gates(self, now: datetime, book: dict | None) -> dict:
        """Which opt-in entry gates are armed, and what each one measures against.

        Every control here arms on data an operator supplies, and an unarmed gate is
        silent in precisely the way an armed gate that found nothing is silent. On a page
        those two must never look alike: "no refusals" from a control that is switched off
        is not evidence about the book. So arming is published as its own fact, beside the
        setting that turns it on, instead of being inferred from an absence.

        ``records`` counts what the operator actually supplied - mapped symbols, declared
        events, declared ex-dates - because armed with an empty file is a third state
        again. Nothing here is estimated: a figure the account cannot support is withheld
        with its cause, the way ``valuation`` already withholds equity.
        """
        daemon = self.daemon
        engine = getattr(daemon, "exit_engine", None)
        pipeline = getattr(getattr(daemon, "scheduler", None), "pipeline", None)
        warden = getattr(getattr(pipeline, "runtime", None), "warden", None)
        book_risk = getattr(warden, "book_risk", None)
        book_policy = getattr(book_risk, "policy", None)
        overnight = getattr(warden, "overnight_risk", None)
        overnight_policy = getattr(overnight, "policy", None)
        sector_map = dict(getattr(book_risk, "sector_map", None) or {})
        provider = getattr(book_risk, "history_provider", None)
        history_armed = callable(provider)
        symbols = tuple(item.symbol for item in getattr(daemon, "instruments", ()))
        covered = sum(bool(sector_map.get(symbol.upper())) for symbol in symbols)
        required = bool(getattr(book_risk, "required_symbols", ()))
        history_state = {"dataReady": None, "records": None, "reason": "not_observed"}
        # Protect the independent heartbeat: no provider call and no exception can halt it.
        from quant_ai.risk.book_history import DailyCloseHistory
        if isinstance(provider, DailyCloseHistory):
            history_state = provider.readiness(symbols, now)
        corporate = getattr(engine, "corporate_calendar", None)
        try:
            declared = len(corporate) if corporate is not None else 0
        except TypeError:  # an operator's own lookup need not be sized
            declared = None
        calendar = getattr(daemon, "event_calendar", None)
        window = getattr(overnight_policy, "closing_window", None)
        gates = [
            {"id": "sector_concentration", "setting": "PRAMANA_SECTOR_MAP_JSON",
             "armed": bool(sector_map) and (not required or covered == len(symbols)),
              "records": len(sector_map), "coveredSymbols": covered,
              "required": required, "dataReady": bool(symbols) and covered == len(symbols),
             "groups": len(set(sector_map.values())),
             "limit": self._finite(getattr(book_policy, "max_sector_exposure", None))},
            {"id": "correlation_adjusted_gross", "setting": "PRAMANA_BOOK_RISK_HISTORY",
             "armed": history_armed, "required": required, **history_state,
             "limit": self._finite(getattr(book_policy, "max_correlation_adjusted_gross", None))},
            {"id": "book_expected_shortfall", "setting": "PRAMANA_BOOK_RISK_HISTORY",
             "armed": history_armed, "required": required, **history_state,
             "limit": self._finite(getattr(book_policy, "max_book_expected_shortfall", None))},
            {"id": "overnight_exposure", "setting": "PRAMANA_OVERNIGHT_GROSS_CAP",
             "armed": bool(getattr(overnight, "armed", False)),
             "limit": self._finite(getattr(overnight_policy, "max_overnight_gross", None)),
             "closingWindowSeconds": self._finite(
                 window.total_seconds() if window is not None else None),
             **self._observed_gross(book)},
            {"id": "overnight_gap_monitor", "setting": "PRAMANA_OVERNIGHT_GAP_MONITOR",
             "armed": getattr(engine, "gap_monitor", None) is not None},
            {"id": "event_blackout", "setting": "PRAMANA_EVENT_CALENDAR",
             "armed": calendar is not None,
             "records": len(getattr(calendar, "events", ()) or ()) if calendar is not None else 0,
             "blackouts": self._blackouts(calendar, now)},
            {"id": "corporate_actions", "setting": "PRAMANA_CORPORATE_ACTIONS",
             # The step guard suspends an undeclared re-basing without this file; what
             # the calendar adds is the declaration that resolves one.
             "armed": bool(declared), "records": declared},
        ]
        return {"schema": "pramana.risk_gates.v1",
                "tenantId": getattr(daemon, "tenant_id", None),
                "checkedAt": now.isoformat(), "gates": gates}

    def _exploration(self, now: datetime) -> dict | None:
        """The probe budget the running Atlas applies, so a cap of 0 reads as off.

        Taken from the policy object the engine is running, not re-read from the
        environment: the page shows what the process applies. Settings only; the day's
        count is the journal's, which the dashboard already reads. None when the running
        pipeline has no Atlas policy to report, which the page shows as not reported
        rather than as off. Nothing here raises the cap, lowers a bar or turns a hard hold
        (a veto, stale evidence, missing coverage) into a probe.
        """
        from quant_ai.agents.atlas import EXPLORATION_MAX_ENV
        from quant_ai.agents.playbook import PLAYBOOKS

        pipeline = getattr(getattr(self.daemon, "scheduler", None), "pipeline", None)
        cio = getattr(getattr(pipeline, "runtime", None), "cio", None)
        policy = getattr(getattr(cio, "atlas", None), "policy", None)
        if policy is None:
            return None
        try:
            cap = int(policy.exploration_max_per_day)
            withheld = (sorted(item.regime for item in PLAYBOOKS.values() if not item.probes_allowed)
                        if policy.regime_playbooks else [])
            return {
                "schema": "pramana.exploration.v1",
                "tenantId": getattr(self.daemon, "tenant_id", None),
                "checkedAt": now.isoformat(),
                "setting": EXPLORATION_MAX_ENV,
                "armed": cap > 0,
                "maxPerDay": cap,
                "minWeightedScore": self._finite(policy.exploration_min_weighted_score),
                "minConfidence": self._finite(policy.exploration_min_confidence),
                "notionalFraction": self._finite(policy.exploration_notional_fraction),
                # Regimes whose playbook never probes, whatever the budget says.
                "withheldInRegimes": withheld,
            }
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _finite(value) -> float | None:
        """A published number as a finite float, or None when the gate declared none.

        None rather than zero, always: these travel to a page that compares them against
        an observed figure, and a zero threshold reads as a control that refuses
        everything rather than as one that never stated a limit.
        """
        try:
            number = float(value)
        except (TypeError, ValueError, DecimalException):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _observed_gross(book: dict | None) -> dict:
        """Gross exposure as a fraction of equity, or the reason it cannot be stated.

        Never a zero standing in for an unknown: against a cap, zero reads as an empty
        book, which is the most reassuring thing the number can say and the one thing an
        unusable account does not entitle it to say.
        """
        if not book:
            return {"observed": None, "observedUnavailable": "no_account_snapshot"}
        if book.get("unavailable"):
            return {"observed": None, "observedUnavailable": str(book["unavailable"])[:200]}
        try:
            equity, gross = book["equity"], book["grossExposure"]
            if equity is None or equity <= 0:
                return {"observed": None, "observedUnavailable": "invalid_equity"}
            observed = float(Decimal(gross) / Decimal(equity))
        except (KeyError, TypeError, ValueError, ArithmeticError, DecimalException):
            return {"observed": None, "observedUnavailable": "invalid_account_or_valuation"}
        return ({"observed": observed, "observedUnavailable": None} if math.isfinite(observed)
                else {"observed": None, "observedUnavailable": "invalid_account_or_valuation"})

    @staticmethod
    def _blackouts(calendar, now: datetime) -> list:
        """Events the operator's calendar is suppressing entries under right now.

        A journalled refusal says a blackout fired once; this says one is in force, which
        is what stops an operator reading a deliberately quiet engine as a broken one.
        Reporting must never break the tick, and ``EventCalendarError`` is a
        ``ValueError``, so a bad calendar here would otherwise reach the publish guard,
        be read as an invalid valuation and halt the pilot.
        """
        if calendar is None:
            return []
        try:
            day = calendar.local_day(now)
            return [{"category": str(event.category)[:60],
                     "symbol": str(event.symbol)[:40] if event.symbol else None}
                    for event in calendar.events[:2000]
                    if event.start <= day <= event.end][:50]
        except Exception:  # noqa: BLE001 - an operator's file is not a dependency
            return []

    def publish_unavailable(self, now: datetime, reason: str) -> dict:
        """Publish liveness and an explicit valuation gap, never partial/zero equity.

        Independent protective sweeps already ran; invalid rows stay in the ledger.
        An unavailable observation cannot be counted toward strategy qualification.
        """
        daemon = self.daemon
        with self.broker._lock, self.broker._connection:
            daemon.engage_kill_switch("paper_valuation_invalid")
            daemon.check_protection_coverage(now)
            daemon._reconcile_pilot()
            ledger_id = self.broker._connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM paper_ledger WHERE tenant_id=?", (daemon.tenant_id,)
            ).fetchone()[0]
            payload = {
                "status": "invalid", "tenantId": daemon.tenant_id,
                "currency": daemon.instruments[0].currency, "markMode": "engine_live",
                "markDisclaimer": "Portfolio totals withheld: " + reason + ". Stored records require review.",
                "updatedAt": now.isoformat(), "allMarksFresh": False, "qualifyingSession": False,
                "sessionDate": now.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat(),
                "strategyObservation": {"eligible": False}, "holdings": [],
                "cash": None, "totalEquity": None, "startingCapital": None,
                "realizedPnl": None, "unrealizedPnl": None, "dailyPnl": None,
                "highWaterMark": None, "drawdown": None,
            }
            runtime = self._runtime_payload(now, ledger_id, None,
                {"status": "unavailable", "reason": reason, "checkedAt": now.isoformat(), "ledgerId": ledger_id},
                # No usable account: the overnight gate's observed exposure is withheld
                # and says why, rather than reporting a zero that reads as headroom.
                book={"unavailable": reason})
            self._write_valuation(now, ledger_id, payload)
            self.broker._connection.execute("INSERT OR REPLACE INTO pilot_runtime VALUES (?,?,?)",
                (daemon.tenant_id, now.isoformat(), json.dumps(runtime, allow_nan=False)))
            return payload

    def fresh(self, instrument: Instrument, now: datetime, expected_price: Decimal | None = None) -> tuple[bool, str | None]:
        try:
            tick = self.daemon.tracker.market_feed.latest_tick(instrument)
            if tick.timestamp.utcoffset() is None:
                return False, None
            if positive_level(tick.last_price) is None:
                return False, None
            if expected_price is not None and tick.last_price != expected_price:
                return False, None
            age = (now.astimezone(timezone.utc) - tick.timestamp.astimezone(timezone.utc)).total_seconds()
            return 0 <= age <= 120, tick.timestamp.isoformat()
        except (ValueError, DecimalException, RuntimeError, TimeoutError, ConnectionError, OSError):
            return False, None
