"""Pre-market readiness of the running paper engine: one verdict per condition.

The morning of 21 September 2026 the engine came up healthy by every container check and
still could not have placed an order, because its strategy manifest was incomplete and the
only place that said so was a field inside the runtime record. This reads that record,
the engine's latest strategy manifest and the environment the container was started
with, and prints one OK, FAIL or INFO line per condition that decides whether the session
can trade. It never calls a provider or the broker, and it never prints a credential.

The conditions are the ones that have actually stopped this pilot: the manifest gate, a
watchlist that does not match the websocket token map, a token that dies at 06:00 IST
before the session ends, a provider left out of the graph, a risk gate that never armed,
a calendar that failed to load, and a feed that is not delivering ticks after the open.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

from quant_ai.operations.zerodha_session import INDIA_TZ, latest_cutoff, next_cutoff

MAX_PAYLOAD = 1_000_000
HEARTBEAT_MAX_AGE_SECONDS = 90
PRE_OPEN_IST = time(9, 0)
OPEN_IST = time(9, 15)
CLOSE_IST = time(15, 30)


@dataclass(frozen=True)
class Check:
    id: str
    state: str  # "OK", "FAIL" or "INFO"
    detail: str


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None


def _type_name(entry: object) -> str:
    return str(entry.get("type", "")).rsplit(".", 1)[-1] if isinstance(entry, dict) else ""


def _registry_providers(manifest: Mapping, component: str, category: str) -> list:
    providers = ((manifest.get("components") or {}).get(component) or {}).get("providers") or {}
    return list(providers.get(category) or [])


def _engine_running(payload: Mapping, now: datetime) -> Check:
    status = payload.get("status")
    halted = payload.get("halted")
    stamp = _timestamp(payload.get("updatedAt"))
    age = None if stamp is None else round((now - stamp).total_seconds())
    ok = status == "running" and halted is False and age is not None and 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS
    detail = f"status={status} halted={halted} heartbeat_age_s={age}"
    if halted and payload.get("haltReason"):
        detail += f" halt_reason={payload.get('haltReason')}"
    return Check("engine_running", "OK" if ok else "FAIL", detail)


def _manifest_matched(payload: Mapping) -> Check:
    summary = payload.get("strategyManifest") or {}
    status = summary.get("status")
    issues = summary.get("issues") or []
    ok = status == "matched" and not issues
    detail = f"status={status}" + (f" issues={';'.join(map(str, issues))}" if issues else "")
    return Check("manifest_matched", "OK" if ok else "FAIL", detail)


def _watchlist_mapped(payload: Mapping, env: Mapping[str, str]) -> Check:
    watch = payload.get("watchlist") or []
    listed = {
        str(item.get("symbol")).upper() for item in watch if isinstance(item, dict) and item.get("symbol")
    }
    try:
        mapping = json.loads(env.get("PRAMANA_ZERODHA_SYMBOLS_JSON") or "{}")
        mapped = {str(symbol).upper() for symbol in mapping.values()} if isinstance(mapping, dict) else set()
    except ValueError:
        mapped = set()
    unmapped = sorted(listed - mapped)
    unlisted = sorted(mapped - listed)
    ok = bool(listed) and not unmapped and not unlisted
    detail = f"{len(listed)} names on the watchlist, {len(mapped)} tokens mapped"
    if unmapped:
        detail += f"; no token for {','.join(unmapped)}"
    if unlisted:
        detail += f"; token but not watched {','.join(unlisted)}"
    return Check("watchlist_mapped", "OK" if ok else "FAIL", detail)


def _token_covers_session(env: Mapping[str, str], now: datetime) -> Check:
    issued = _timestamp(env.get("PRAMANA_ZERODHA_TOKEN_ISSUED_AT", ""))
    if issued is None:
        return Check("token_covers_session", "FAIL", "PRAMANA_ZERODHA_TOKEN_ISSUED_AT missing or invalid")
    if not env.get("ZERODHA_ACCESS_TOKEN", "").strip():
        return Check("token_covers_session", "FAIL", "ZERODHA_ACCESS_TOKEN missing")
    if issued > now:
        return Check("token_covers_session", "FAIL", "token issuance is in the future")
    expires = next_cutoff(issued)
    local = now.astimezone(INDIA_TZ)
    close = local.replace(hour=CLOSE_IST.hour, minute=CLOSE_IST.minute, second=0, microsecond=0)
    stamp = expires.astimezone(INDIA_TZ).strftime("%Y-%m-%d %H:%M IST")
    if issued < latest_cutoff(now):
        return Check("token_covers_session", "FAIL", f"token expired at {stamp}; log in again")
    if expires <= close:
        return Check("token_covers_session", "FAIL", f"token dies at {stamp}, before today's close")
    return Check("token_covers_session", "OK", f"valid until {stamp}")


def _specialists_supported(manifest: Mapping | None) -> Check:
    if manifest is None:
        return Check("specialists_supported", "FAIL", "no strategy manifest recorded yet")
    specialists = manifest.get("specialists") or []
    unsupported = [str(s.get("agent_id")) for s in specialists if isinstance(s, dict) and not s.get("supported")]
    ok = bool(specialists) and not unsupported
    detail = f"{len(specialists)} specialists"
    if unsupported:
        detail += f"; unsupported {','.join(unsupported)}"
    return Check("specialists_supported", "OK" if ok else "FAIL", detail)


def _providers_configured(manifest: Mapping | None, env: Mapping[str, str]) -> Check:
    if manifest is None:
        return Check("providers_configured", "FAIL", "no strategy manifest recorded yet")
    missing: list[str] = []
    parts: list[str] = []
    feeds_wanted = len([u for u in (env.get("PRAMANA_NEWS_RSS_URLS") or "").split(",") if u.strip()])
    news = _registry_providers(manifest, "news", "NEWS")
    feeds_seen = len(news[0].get("feed_identities") or []) if news and isinstance(news[0], dict) else 0
    if feeds_wanted and feeds_seen != feeds_wanted:
        missing.append(f"news feeds {feeds_seen}/{feeds_wanted}")
    parts.append(f"news feeds {feeds_seen}/{feeds_wanted}")
    fundamentals = _registry_providers(manifest, "fundamentals", "FUNDAMENTALS")
    fundamentals_source = (env.get("PRAMANA_FUNDAMENTALS_PROVIDER") or "yahoo").strip().lower()
    if fundamentals_source != "none":
        if not any(_type_name(p) == "YahooFundamentalsProvider" for p in fundamentals):
            missing.append("fundamentals yahoo")
        parts.append("fundamentals yahoo")
    macro = _registry_providers(manifest, "macro", "MACRO")
    macro_parts = [_type_name(p) for p in (macro[0].get("parts") or [])] if macro and isinstance(macro[0], dict) else []
    expected = []
    if env.get("FRED_API_KEY", "").strip():
        expected.append(("fred", "FredMacroProvider"))
    if (env.get("PRAMANA_INDIA_VIX_PROVIDER") or "none").strip().lower() == "yahoo":
        expected.append(("india_vix", "YahooIndiaVixProvider"))
    if (env.get("PRAMANA_INDIA_FLOWS_PROVIDER") or "none").strip().lower() == "nse":
        expected.append(("fii_dii", "NseInstitutionalFlowsProvider"))
    for label, type_name in expected:
        if type_name not in macro_parts:
            missing.append(f"macro {label}")
    parts.append("macro " + ("+".join(label for label, _ in expected) or "none"))
    detail = ", ".join(parts) + (f"; missing {'; '.join(missing)}" if missing else "")
    return Check("providers_configured", "FAIL" if missing else "OK", detail)


def _risk_gates_armed(payload: Mapping) -> Check:
    gates = (payload.get("riskGates") or {}).get("gates") or []
    unarmed = [str(g.get("id")) for g in gates if isinstance(g, dict) and not g.get("armed")]
    armed = [str(g.get("id")) for g in gates if isinstance(g, dict) and g.get("armed")]
    ok = bool(gates) and not unarmed
    detail = f"armed {','.join(armed) or 'none'}" + (f"; unarmed {','.join(unarmed)}" if unarmed else "")
    return Check("risk_gates_armed", "OK" if ok else "FAIL", detail)


def _event_calendar(env: Mapping[str, str]) -> Check:
    from quant_ai.governance.event_calendar import EventCalendarError, event_calendar_from_env

    try:
        calendar = event_calendar_from_env(environ=env)
    except EventCalendarError as error:
        return Check("event_calendar", "FAIL", str(error)[:160])
    if calendar is None:
        return Check("event_calendar", "INFO", "no calendar configured; no blackouts")
    return Check("event_calendar", "OK", f"{len(calendar.events)} events, timezone {calendar.timezone}")


def _market_data(payload: Mapping, now: datetime) -> Check:
    integrity = payload.get("marketDataIntegrity") or {}
    accepted = integrity.get("accepted")
    local = now.astimezone(INDIA_TZ).time()
    if isinstance(accepted, int) and accepted > 0:
        rejected = integrity.get("rejected") or {}
        return Check("market_data", "OK", f"{accepted} ticks accepted, rejected {json.dumps(rejected)}")
    if local < PRE_OPEN_IST:
        return Check("market_data", "INFO", "no ticks yet; NSE pre-open starts 09:00 IST")
    if local < OPEN_IST:
        return Check("market_data", "INFO", "no ticks yet in pre-open; the feed usually starts with the open")
    return Check("market_data", "FAIL", "market is open and no tick has been accepted")


def premarket_checks(
    payload: Mapping, manifest: Mapping | None, env: Mapping[str, str], now: datetime
) -> list[Check]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("premarket clock must include a timezone")
    return [
        _engine_running(payload, now),
        _manifest_matched(payload),
        _watchlist_mapped(payload, env),
        _token_covers_session(env, now),
        _specialists_supported(manifest),
        _providers_configured(manifest, env),
        _risk_gates_armed(payload),
        _event_calendar(env),
        _market_data(payload, now),
    ]


def load_engine_records(database: Path, tenant: str) -> tuple[dict, dict | None]:
    """The runtime record and the latest strategy manifest, read-only; {} and None if absent."""
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        row = db.execute(
            "SELECT substr(payload,1,?) FROM pilot_runtime WHERE tenant_id=?", (MAX_PAYLOAD, tenant)
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        manifest = None
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_strategy_manifests'"
        ).fetchone():
            row = db.execute(
                "SELECT substr(payload,1,?) FROM pilot_strategy_manifests WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (MAX_PAYLOAD, tenant),
            ).fetchone()
            manifest = json.loads(row[0]) if row and row[0] else None
    if not isinstance(payload, dict):
        payload = {}
    if manifest is not None and not isinstance(manifest, dict):
        manifest = None
    return payload, manifest


def render(checks: list[Check]) -> str:
    width = max(len(check.id) for check in checks)
    lines = [f"{check.state:<4} {check.id:<{width}}  {check.detail}" for check in checks]
    failed = [check.id for check in checks if check.state == "FAIL"]
    lines.append("")
    lines.append("READY TO TRADE" if not failed else "NOT READY: fix " + ", ".join(failed))
    return "\n".join(lines)
