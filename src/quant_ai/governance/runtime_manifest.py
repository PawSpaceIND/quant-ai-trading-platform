"""Fingerprint explicit engine configuration and retain its source inventory.

This trusts the local process/ledger; it is not remote attestation or model validation.
Credentials, market observations and mutable HTTP caches are intentionally excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Explicit fields prevent credentials and transient provider/cache state entering a manifest.
FIELDS = {
    "quant_ai.execution.daemon.AutonomousTradingDaemon": (
        "quantity",
        "country",
        "tenant_id",
        "instruments",
        "plan",
    ),
    "quant_ai.execution.scheduler.AutonomousCadenceScheduler": ("cadence",),
    "quant_ai.intelligence.pipeline.SwarmMarketAnalysisPipeline": (),
    "quant_ai.agents.swarm.AtlasCIOAgent": (),
    "quant_ai.analytics.attribution.AgentAttributionEngine": (),
    "quant_ai.execution.session.MarketCalendar": ("holidays",),
    "quant_ai.agents.swarm_runtime.SwarmPaperTradingService": (
        "allow_position_scaling",
        "max_open_positions",
    ),
    "quant_ai.agents.atlas.AtlasInvestmentAgent": (
        "policy",
        "founder_policy",
        "founder_instructions",
    ),
    "quant_ai.risk.warden.RiskWarden": ("blocked_asset_classes",),
    "quant_ai.intelligence.adversarial.AdversarialStressAgent": (
        "tolerance_fraction",
        "DEFAULT_SCENARIOS",
    ),
    "quant_ai.portfolio.sizing.PositionSizer": ("policy",),
    "quant_ai.intelligence.regime.MarketRegimeDetector": (
        "crisis_atr_fraction",
        "trend_adx_threshold",
        "adx_period",
    ),
    "quant_ai.intelligence.freshness.FreshnessValidator": ("TTL",),
    "quant_ai.orchestration.cadence.CadenceMarketReader": ("max_tick_age",),
    "quant_ai.execution.protective_exits.ProtectiveExitEngine": (
        "tenant_id",
        "strategy_id",
        "re_entry_cooldown",
    ),
    "quant_ai.marketdata.live_feed.LiveTickMarketDataFeed": ("max_tick_age",),
    "quant_ai.marketdata.live_feed.TickBarAggregator": ("bar_length", "max_bars"),
    "quant_ai.marketdata.feed.UsaSandboxMarketDataFeed": ("market", "exchange", "base_price"),
    "quant_ai.marketdata.feed.IndiaSandboxMarketDataFeed": ("market", "exchange", "base_price"),
    "quant_ai.intelligence.sandbox.SandboxNewsSentimentProvider": ("provider_id", "age_seconds"),
    "quant_ai.intelligence.sandbox.SandboxFundamentalDataProvider": ("provider_id", "age_seconds"),
    "quant_ai.intelligence.sandbox.SandboxMacroIndicatorProvider": ("provider_id", "age_seconds"),
    "quant_ai.intelligence.external.fred.FredMacroProvider": ("series",),
    "quant_ai.intelligence.external.rss.RssNewsSentimentAdapter": (),
    "quant_ai.intelligence.failover.FailoverNewsProvider": (),
    "quant_ai.intelligence.failover.FailoverMacroProvider": (),
    "quant_ai.intelligence.failover.FailoverFundamentalProvider": (),
    "quant_ai.intelligence.resilience.ResilientHttpClient": (
        "timeout_seconds",
        "max_attempts",
        "max_payload_bytes",
    ),
    "quant_ai.intelligence.resilience.TokenBucketRateLimiter": ("rate", "capacity"),
    "quant_ai.intelligence.resilience.CircuitBreaker": (
        "failure_threshold",
        "recovery_timeout_seconds",
    ),
    "quant_ai.intelligence.resilience.UrllibTransport": (),
    "quant_ai.execution.friction.MarketFrictionModel": (
        "fee_schedule",
        "gamma",
        "spread_atr_multiplier",
        "max_slippage_fraction",
        "max_half_spread_fraction",
        "fixed_slippage_bps",
    ),
    "quant_ai.execution.paper_ledger.PaperBrokerService": (
        "starting_capital",
        "lock_retries",
        "lock_backoff_seconds",
    ),
    "quant_ai.llm.anthropic_client.AnthropicSwarmClient": (
        "model",
        "timeout_seconds",
        "transport_kind",
    ),
    "quant_ai.marketdata.ticker_stream.ZerodhaKiteTicker": ("instrument_tokens", "symbol_by_token"),
    "quant_ai.marketdata.ticker_stream.IBKRAsyncTicker": (
        "connect_host",
        "connect_port",
        "client_id",
    ),
}


def stable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return stable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k.value if isinstance(k, Enum) else k): stable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((stable(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, (tuple, list)):
        return [stable(v) for v in value]
    return value


def encoded(value) -> str:
    return json.dumps(stable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def source_identity(url: str) -> str:
    parsed = urlsplit(url)
    secret_keys = {
        "token",
        "api_key",
        "apikey",
        "key",
        "access_token",
        "signature",
        "auth",
        "password",
    }
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in secret_keys)
    )
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return digest(urlunsplit((parsed.scheme, host, parsed.path, query, "")))


def describe(obj, issues: list[str]) -> dict | None:
    if obj is None:
        return None
    name = f"{type(obj).__module__}.{type(obj).__qualname__}"
    if name not in FIELDS:
        issues.append(f"unsupported_component:{name}")
        return {"type": name, "supported": False}
    result = {"type": name, "parameters": {key: stable(getattr(obj, key)) for key in FIELDS[name]}}
    if name.endswith("AutonomousCadenceScheduler"):
        result["calendar"] = describe(obj.calendar, issues)
    if name.endswith("MarketCalendar"):
        from quant_ai.execution.session import SESSIONS

        result["sessions"] = stable(SESSIONS)
    if name.endswith("ProtectiveExitEngine"):
        resolver = obj.mark_resolver
        identity = f"{getattr(resolver, '__module__', '')}.{getattr(resolver, '__qualname__', type(resolver).__qualname__)}"
        result["mark_resolver"] = identity
        if (
            identity
            != "quant_ai.execution.protective_exits.market_feed_mark_resolver.<locals>.resolve"
        ):
            issues.append("unsupported_protective_mark_resolver")
    if name.endswith("AnthropicSwarmClient"):
        from anthropic import AsyncAnthropic

        client = obj._client
        if type(client) is not AsyncAnthropic or obj.transport_kind != "anthropic_sdk":
            issues.append("unsupported_inference_transport")
            result["sdk_configuration"] = {"supported": False}
        else:
            timeout = client.timeout
            timeout_fields = ("connect", "read", "write", "pool")
            result["sdk_configuration"] = {
                "supported": True,
                "endpoint_sha256": source_identity(str(client.base_url)),
                "max_retries": client.max_retries,
                "timeout": timeout
                if timeout is None or isinstance(timeout, (float, int))
                else {key: getattr(timeout, key) for key in timeout_fields},
            }
    if name.endswith("LiveTickMarketDataFeed"):
        result["aggregator"] = describe(obj.aggregator, issues)
    if name.endswith("FredMacroProvider"):
        result["endpoint_sha256"] = source_identity(obj.endpoint)
        result["http"] = describe(obj.client, issues)
    if name.endswith("RssNewsSentimentAdapter"):
        result["feed_identities"] = [source_identity(url) for url in obj.feed_urls]
        result["http"] = describe(obj.client, issues)
    if ".failover." in name:
        result["providers"] = {
            category.value: [describe(p, issues) for p in providers]
            for category, providers in obj.registry._providers.items()
        }
    if name.endswith("ResilientHttpClient"):
        result["transport"] = describe(obj.transport, issues)
        result["rate_limiter"] = describe(obj.rate_limiter, issues)
        result["circuit_breaker"] = describe(obj.circuit_breaker, issues)
    if name.endswith("IBKRAsyncTicker"):
        fields = (
            "conId",
            "symbol",
            "localSymbol",
            "secType",
            "exchange",
            "currency",
            "multiplier",
            "lastTradeDateOrContractMonth",
            "tradingClass",
            "primaryExchange",
        )
        result["contracts"] = [
            {k: stable(getattr(c, k, None)) for k in fields} for c in obj.contracts
        ]
    return result


class RuntimeManifest:
    def __init__(
        self, daemon, streams=(), *, source_root: Path | None = None, revision: str | None = None
    ):
        self.daemon = daemon
        self.streams = tuple(streams)
        self.source_root = source_root or Path(__file__).resolve().parents[1]
        self.revision = (
            revision if revision is not None else os.getenv("PRAMANA_RELEASE_REVISION", "")
        )
        self._source = None
        self._source_at = 0.0
        self._boot_hash = None
        self._dependencies = None
        self.summary = None
        with daemon.tracker.broker._lock, daemon.tracker.broker._connection as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pilot_strategy_manifests (tenant_id TEXT,sha256 TEXT,created_at TEXT,payload TEXT,PRIMARY KEY(tenant_id,sha256))"
            )

    def _source_inventory(self, force=False):
        if force or self._source is None or monotonic() - self._source_at >= 60:
            paths = sorted(self.source_root.rglob("*.py"))
            if any(p.is_symlink() for p in paths):
                raise ValueError("source_symlink_unsupported")
            files = {
                str(p.relative_to(self.source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths
                if p.is_file()
            }
            if not files:
                raise ValueError("source_inventory_empty")
            self._source = {"files": files, "sha256": digest(files)}
            self._source_at = monotonic()
        return self._source

    def capture(self, *, force_source=False) -> dict:
        d = self.daemon
        p = d.scheduler.pipeline
        r = p.runtime
        issues = []
        from quant_ai.risk.policy import RiskPolicy

        components = {
            name: describe(obj, issues)
            for name, obj in {
                "daemon": d,
                "scheduler": d.scheduler,
                "pipeline": p,
                "runtime": r,
                "cio": r.cio,
                "attribution": r.attribution,
                "atlas": r.cio.atlas,
                "llm": r.cio.atlas.llm_client,
                "risk": r.warden,
                "stress": r.stress_agent,
                "sizer": p.sizer,
                "regime": p.regime_detector,
                "freshness": p.freshness,
                "ticks": p.tick_reader,
                "protection": d.exit_engine,
                "market_feed": p.market_feed,
                "news": p.news,
                "fundamentals": p.fundamentals,
                "macro": p.macro,
                "friction": r.broker.friction_model,
                "broker": r.broker,
            }.items()
        }
        supported_agents = {
            "GeopoliticalAnalystAgent",
            "CommodityYieldAgent",
            "IndianEquitiesAgent",
            "USEquitiesAgent",
            "TechnicalQuantAgent",
        }
        agents = []
        for agent in p.agents:
            name = f"{type(agent).__module__}.{type(agent).__qualname__}"
            supported = (
                type(agent).__module__ == "quant_ai.agents.swarm"
                and type(agent).__qualname__ in supported_agents
            )
            if not supported:
                issues.append(f"unsupported_specialist:{name}")
            agents.append(
                {
                    "type": name,
                    "agent_id": agent.agent_id,
                    "domain": stable(agent.domain),
                    "supported": supported,
                }
            )
        if not self.streams:
            issues.append("market_stream_configuration_unbound")
        # Package metadata is cached with the source scan, not read from disk every heartbeat.
        refresh = self._source is None or force_source or monotonic() - self._source_at >= 60
        if refresh or self._dependencies is None:
            self._dependencies = {}
            for package in ("anthropic", "numpy", "pandas", "pydantic", "kiteconnect", "ib_async"):
                try:
                    self._dependencies[package] = version(package)
                except PackageNotFoundError:
                    self._dependencies[package] = None
        manifest = {
            "schema": "pramana.runtime_strategy.v1",
            "tenant_id": d.tenant_id,
            "release_revision": self.revision,
            "execution_mode": "paper",
            "shared_broker_wiring": r.broker is d.tracker.broker
            and d.exit_engine.broker is r.broker,
            "shared_market_feed_wiring": p.market_feed is d.tracker.market_feed,
            "source": self._source_inventory(force_source),
            "python": sys.version.split()[0],
            "dependencies": self._dependencies,
            "components": components,
            "streams": [describe(s, issues) for s in self.streams],
            "specialists": agents,
            "news_window_seconds": p.news_window.total_seconds(),
            "baseline_risk_policy": stable(RiskPolicy()),
        }
        if not manifest["shared_broker_wiring"] or not manifest["shared_market_feed_wiring"]:
            issues.append("unsupported_engine_wiring")
        if not re.fullmatch("[0-9a-f]{40}", self.revision):
            issues.append("release_revision_unconfigured")
        return {"manifest": manifest, "sha256": digest(manifest), "issues": sorted(set(issues))}

    def check(self, now: datetime | None = None, *, force_source=False) -> dict:
        # The broker's shared reentrant lock orders protection and pre-submit checks.
        with self.daemon.tracker.broker._lock:
            return self._check_locked(now or datetime.now(timezone.utc), force_source=force_source)

    def _check_locked(self, now, *, force_source=False):
        try:
            result = self.capture(force_source=force_source)
            if self._boot_hash is None:
                self._boot_hash = result["sha256"]
            status = (
                "changed"
                if result["sha256"] != self._boot_hash
                else "incomplete"
                if result["issues"]
                else "matched"
            )
            payload = encoded(result["manifest"])
            with self.daemon.tracker.broker._lock, self.daemon.tracker.broker._connection as db:
                db.execute(
                    "INSERT OR IGNORE INTO pilot_strategy_manifests VALUES (?,?,?,?)",
                    (self.daemon.tenant_id, result["sha256"], now.isoformat(), payload),
                )
            self.summary = {
                "status": status,
                "sha256": result["sha256"],
                "bootSha256": self._boot_hash,
                "releaseRevision": self.revision,
                "checkedAt": now.isoformat(),
                "issues": result["issues"],
                "sourceCheckAgeSeconds": max(0, round(monotonic() - self._source_at, 3)),
                "sourceSha256": result["manifest"]["source"]["sha256"],
            }
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as error:
            self.summary = {
                "status": "unavailable",
                "checkedAt": now.isoformat(),
                "issues": [type(error).__name__],
            }
        return self.summary


def export_manifest(ledger: Path, tenant: str, output: Path, sha256: str | None = None) -> str:
    with closing(sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if sha256 is None:
            row = db.execute(
                "SELECT payload FROM pilot_runtime WHERE tenant_id=?", (tenant,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            summary = runtime.get("strategyManifest")
            sha256 = summary.get("sha256") if isinstance(summary, dict) else None
        if not sha256 or not re.fullmatch("[0-9a-f]{64}", sha256):
            raise ValueError("No recorded manifest selected")
        row = db.execute(
            "SELECT payload FROM pilot_strategy_manifests WHERE tenant_id=? AND sha256=?",
            (tenant, sha256),
        ).fetchone()
        if not row or hashlib.sha256(row[0].encode()).hexdigest() != sha256:
            raise ValueError("Manifest missing or checksum mismatch")
        manifest = json.loads(row[0])
        if (
            manifest.get("schema") != "pramana.runtime_strategy.v1"
            or manifest.get("tenant_id") != tenant
        ):
            raise ValueError("Manifest identity mismatch")
    # Exact canonical bytes: the exported file's ordinary SHA-256 equals the manifest ID.
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        file.write(row[0])
    return sha256


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export a recorded engine manifest; this does not approve a strategy."
    )
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    print(export_manifest(args.ledger, args.tenant, args.output, args.sha256))
