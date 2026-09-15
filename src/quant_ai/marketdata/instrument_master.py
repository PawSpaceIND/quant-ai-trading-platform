"""Read-only broker instrument-master normalisation.

The quote collector intentionally keeps a small live observation list.  A broker
instrument master is the source of truth for the available universe, though:
it contains every exact symbol, segment, expiry, strike, lot and tick that the
account can see. This module normalises that master into a bounded dashboard
summary without inventing contracts or claiming that an account is entitled to
trade them.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

INSTRUMENT_MASTER_FILE_ENV = "PRAMANA_ZERODHA_INSTRUMENTS_FILE"
INSTRUMENT_MASTER_CACHE_ENV = "PRAMANA_ZERODHA_INSTRUMENTS_CACHE"
INSTRUMENT_UNIVERSE_RECORDS_ENV = "PRAMANA_INSTRUMENT_UNIVERSE_RECORDS"
INSTRUMENT_MASTER_MAX_AGE_SECONDS = 24 * 60 * 60
MAX_MASTER_ROWS = 250_000
MAX_SAMPLE_ROWS = 100

INDIA_EXCHANGES = {
    "NSE", "BSE", "NFO", "BFO", "MCX", "CDS", "BCD", "NCDEX", "MSEI", "IFSC",
}


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _positive(value: Any) -> float | None:
    if value in (None, "", 0, "0", 0.0):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _expiry(value: Any) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def asset_class_for(row: dict[str, Any]) -> str:
    """Map broker instrument_type/segment to the platform's declared classes."""
    instrument_type = _text(row.get("instrument_type", row.get("instrumentType"))).upper()
    segment = _text(row.get("segment")).upper()
    exchange = _text(row.get("exchange")).upper()
    name = _text(row.get("name")).upper()
    if instrument_type in {"CE", "PE"} or "OPT" in instrument_type:
        return "OPTION"
    if "FUT" in instrument_type:
        if exchange in {"CDS", "BCD"} or segment in {"CDS-FUT", "BCD-FUT"}:
            return "FX"
        if exchange == "MCX" and any(token in name for token in ("GOLD", "SILVER", "COPPER", "ZINC", "ALUMINIUM", "NICKEL", "LEAD")):
            return "METAL"
        if exchange in {"MCX", "NCDEX", "NSE", "BFO"}:
            return "COMMODITY" if exchange in {"MCX", "NCDEX"} else "FUTURE"
        return "FUTURE"
    if exchange in {"CDS", "BCD"} or segment.startswith(("CDS", "BCD")):
        return "FX"
    if instrument_type in {"INDEX", "INDICES"} or segment in {"INDICES", "INDEX"}:
        return "INDEX"
    if instrument_type in {"ETF", "ETN"}:
        return "ETF"
    if segment in {"DEBT", "BOND"}:
        return "BOND"
    if segment in {"MF", "MUTUAL-FUND", "FUNDS"}:
        return "FUND"
    if segment == "SLB":
        return "SLB"
    return "EQUITY"


def normalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return one canonical India instrument or None for an unusable row."""
    exchange = _text(row.get("exchange")).upper()
    symbol = _text(row.get("tradingsymbol", row.get("symbol"))).upper()
    if exchange not in INDIA_EXCHANGES or not symbol or len(symbol) > 80:
        return None
    segment = _text(row.get("segment")).upper()
    instrument_type = _text(row.get("instrument_type", row.get("instrumentType"))).upper()
    expiry = _expiry(row.get("expiry"))
    option_type = instrument_type if instrument_type in {"CE", "PE"} else None
    strike = _positive(row.get("strike"))
    token = _text(row.get("instrument_token", row.get("instrumentToken", row.get("providerInstrumentId"))))
    exchange_token = _text(row.get("exchange_token", row.get("exchangeToken")))
    currency = _text(row.get("currency")).upper() or ("USD" if exchange == "IFSC" and _text(row.get("quote_currency")).upper() == "USD" else "INR")
    normalized: dict[str, Any] = {
        "symbol": symbol,
        "contract": symbol,
        "market": "INDIA",
        "exchange": exchange,
        "currency": currency,
        "assetClass": asset_class_for(row),
        "segment": segment or None,
        "underlying": _text(row.get("name", row.get("underlying"))).upper() or None,
        "expiry": expiry,
        "optionType": option_type,
        "strike": strike,
        "lotSize": _positive(row.get("lot_size", row.get("lotSize"))),
        "tickSize": _positive(row.get("tick_size", row.get("tickSize"))),
        "providerInstrumentId": token or None,
        "providerExchangeToken": exchange_token or None,
        "product": _text(row.get("product")).upper() or None,
        "instrumentType": instrument_type or None,
        # Borrow/short eligibility is not encoded by an instrument master and
        # depends on broker segment, margin and SLB/MTF rules at run time.
        "shortability": "broker_rules_required",
    }
    return {key: value for key, value in normalized.items() if value not in (None, "")}


def parse_instrument_master(payload: Any) -> list[dict[str, Any]]:
    """Parse Kite's list, JSON export or CSV export into canonical rows."""
    rows: Iterable[Any]
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            rows = csv.DictReader(io.StringIO(text))
        else:
            rows = parsed if isinstance(parsed, list) else parsed.get("data", []) if isinstance(parsed, dict) else []
    elif isinstance(payload, dict):
        rows = payload.get("data", [])
    elif isinstance(payload, (list, tuple)):
        rows = payload
    else:
        return []
    result: list[dict[str, Any]] = []
    for item in rows:
        if len(result) >= MAX_MASTER_ROWS:
            break
        if isinstance(item, dict):
            normalized = normalize_row(item)
            if normalized is not None:
                result.append(normalized)
    return result


def _read_path(path: Path) -> Any:
    return path.read_bytes()


def _write_cache(path: Path, payload: Any, fetched_at: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        if isinstance(payload, (bytes, bytearray)):
            temporary.write_bytes(payload)
        else:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.utime(path, (fetched_at.timestamp(), fetched_at.timestamp()))
    except (OSError, TypeError, ValueError):
        return


def _records_path(target_dir: Path, cache: Path) -> Path:
    configured = os.environ.get(INSTRUMENT_UNIVERSE_RECORDS_ENV, "").strip()
    return Path(configured).expanduser() if configured else cache.with_name("instrument-universe.normalized.json")


def _cache_path(target_dir: Path) -> Path:
    configured = os.environ.get(INSTRUMENT_MASTER_CACHE_ENV, "").strip()
    return Path(configured).expanduser() if configured else target_dir / "zerodha-instruments.json"


def _summary(rows: list[dict[str, Any]], *, fetched_at: datetime, source: str, cache_age: float | None = None) -> dict[str, Any]:
    exchanges = Counter(str(row["exchange"]) for row in rows)
    segments = Counter(str(row.get("segment") or "UNSPECIFIED") for row in rows)
    classes = Counter(str(row["assetClass"]) for row in rows)
    instrument_types = Counter(str(row.get("instrumentType") or "UNSPECIFIED") for row in rows)
    expiries = Counter(str(row["expiry"]) for row in rows if row.get("expiry"))
    options = classes.get("OPTION", 0)
    futures = classes.get("FUTURE", 0) + classes.get("FX", 0) + classes.get("COMMODITY", 0) + classes.get("METAL", 0)
    samples = sorted(rows, key=lambda row: (str(row["exchange"]), str(row.get("segment") or ""), str(row.get("expiry") or "9999-99-99"), str(row["symbol"])))[:MAX_SAMPLE_ROWS]
    return {
        "schema": "pramana.instrument_universe.v1",
        "status": "available" if rows else "unavailable",
        "source": source,
        "fetchedAt": fetched_at.astimezone(timezone.utc).isoformat(),
        "cacheAgeSeconds": round(cache_age, 3) if cache_age is not None else 0,
        "total": len(rows),
        "byExchange": dict(sorted(exchanges.items())),
        "bySegment": dict(sorted(segments.items())),
        "byAssetClass": dict(sorted(classes.items())),
        "byInstrumentType": dict(sorted(instrument_types.items())),
        "expiringContracts": len(expiries),
        "optionContracts": options,
        "futureContracts": futures,
        "derivativeContracts": sum(value for key, value in classes.items() if key in {"OPTION", "FUTURE", "FX", "COMMODITY", "METAL"}),
        "shortSide": "Research metadata only; broker shortability, borrow/SLB, margin and product rules require separate evidence.",
        "entitlement": "The master reflects the account/provider response; it is not an approval to place orders.",
        "sample": samples,
    }


def instrument_universe(kite: Any, now: datetime, target_dir: Path) -> dict[str, Any]:
    """Load a cached/file master, refreshing from kite.instruments at most daily."""
    configured = os.environ.get(INSTRUMENT_MASTER_FILE_ENV, "").strip()
    cache = _cache_path(target_dir)
    source = ""
    payload: Any = None
    fetched_at = now
    if configured:
        path = Path(configured).expanduser()
        try:
            payload = _read_path(path)
            source = f"file:{path}"
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            payload = None
    if payload is None:
        try:
            age = now.timestamp() - cache.stat().st_mtime
        except OSError:
            age = float("inf")
        if age <= INSTRUMENT_MASTER_MAX_AGE_SECONDS:
            try:
                payload = _read_path(cache)
                source = f"cache:{cache}"
                fetched_at = datetime.fromtimestamp(cache.stat().st_mtime, tz=timezone.utc)
            except OSError:
                payload = None
        if payload is None and callable(getattr(kite, "instruments", None)):
            try:
                payload = kite.instruments()
                source = "Zerodha Kite instrument master"
                fetched_at = now
                _write_cache(cache, payload, now)
            except Exception:  # noqa: BLE001 - unavailable master must stay explicit.
                payload = None
    rows = parse_instrument_master(payload)
    if not rows:
        return {
            "schema": "pramana.instrument_universe.v1",
            "status": "unavailable",
            "source": source or "not configured",
            "fetchedAt": now.astimezone(timezone.utc).isoformat(),
            "total": 0,
            "byExchange": {}, "bySegment": {}, "byAssetClass": {},
            "byInstrumentType": {}, "expiringContracts": 0,
            "optionContracts": 0, "futureContracts": 0, "derivativeContracts": 0,
            "shortSide": "No broker master supplied; shorting remains unavailable.",
            "entitlement": "Configure a broker instrument master to discover exact symbols.",
            "sample": [],
            "recordsPath": str(_records_path(target_dir, cache)),
        }
    age = max(0.0, now.timestamp() - fetched_at.timestamp())
    summary = _summary(rows, fetched_at=fetched_at, source=source, cache_age=age)
    records = _records_path(target_dir, cache)
    try:
        records_current = records.stat().st_mtime >= fetched_at.timestamp() - 1
    except OSError:
        records_current = False
    if not records_current:
        _write_cache(records, {"schema": "pramana.instrument_universe.v1", "fetchedAt": fetched_at.isoformat(), "rows": rows}, fetched_at)
    summary["recordsPath"] = str(records)
    return summary
