"""The opportunity scan: what is moving outside the traded book, behind the same gates.

The founder asked for an Atlas who finds opportunity in every market he has been given.
The book he trades is the directives' watchlist; the markets he has been given are the
NSE cash names the operator lists for scanning, read from closed daily bars the way the
watched names are. Each pre-open the scan classifies every candidate's regime, routes it
through the same playbooks, and reports the names whose playbook would trade the day as
opportunities outside the book, with the gates a promotion has to pass: a websocket token
mapping and a sector group, both operator settings. Nothing is promoted here; the plan
tells the founder what he would have to add, and the watchlist cap and every risk gate
apply to the promoted name exactly as to the rest.

Scope, honestly. Yahoo daily bars cover NSE listings (``SYMBOL.NS``); MCX, NFO, CDS and
the other derivative segments stay observation-only until contract qualification and
margin sourcing are reviewed, and US names wait on IBKR. The scan reads; it never orders.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from quant_ai.agents.playbook import playbook_for
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.regime import INSUFFICIENT_HISTORY, classify

SCAN_UNIVERSE_ENV = "PRAMANA_SCAN_UNIVERSE_JSON"
MAX_SCAN_NAMES = 100
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&._-]{0,39}\Z")
SCAN_EXCHANGES = frozenset({"NSE"})
FOCUS_PLAYBOOKS = frozenset({"trend_following", "range_trading", "unrouted"})
ROLE_OPPORTUNITY, ROLE_QUIET, ROLE_UNREAD = "opportunity", "quiet", "unread"
MAX_BRIEF_LINES = 5


def scan_universe_from_env(environ: Mapping[str, str] | None = None) -> tuple[Instrument, ...]:
    """Candidate NSE cash names to scan, from ``PRAMANA_SCAN_UNIVERSE_JSON``; malformed refuses to boot.

    A list of symbols (``["BEL", "LT"]``) or of ``{"symbol": ..., "exchange": "NSE"}`` rows.
    Candidates are read-only rows (``tradable=False``): the scan never orders them.
    """
    source = os.environ if environ is None else environ
    raw = source.get(SCAN_UNIVERSE_ENV, "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise RuntimeError(f"unsupported scan universe: {SCAN_UNIVERSE_ENV} is not JSON") from error
    try:
        return _parse_universe(payload)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"unsupported scan universe: {error}") from error


def _parse_universe(payload: Any) -> tuple[Instrument, ...]:
    if not isinstance(payload, list):
        raise TypeError(f"{SCAN_UNIVERSE_ENV} must be a JSON list")
    if len(payload) > MAX_SCAN_NAMES:
        raise ValueError(f"at most {MAX_SCAN_NAMES} names")
    result: list[Instrument] = []
    seen: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            symbol, exchange = item, "NSE"
        elif isinstance(item, dict):
            symbol, exchange = str(item.get("symbol", "")), str(item.get("exchange", "NSE"))
        else:
            raise TypeError("each entry is a symbol or a {symbol, exchange} row")
        symbol, exchange = symbol.strip().upper(), exchange.strip().upper()
        if not SYMBOL.fullmatch(symbol):
            raise ValueError(f"bad symbol {symbol[:40]!r}")
        if exchange not in SCAN_EXCHANGES:
            raise ValueError(f"{symbol} on {exchange}; the scan reads NSE cash only")
        if symbol in seen:
            raise ValueError(f"duplicate {symbol}")
        seen.add(symbol)
        result.append(Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", exchange, tradable=False))
    return tuple(result)


def _bars_for(history: Any, instrument: Instrument, now: datetime) -> tuple:
    if history is None:
        return ()
    try:
        return tuple(history.fetch(instrument, now))
    except (TimeoutError, OSError, RuntimeError, ValueError, TypeError, LookupError,
            AttributeError, ArithmeticError):
        return ()


def scan(
    candidates: Iterable[Instrument],
    *,
    history: Any,
    now: datetime,
    watched: Iterable[str] = (),
    gates: Mapping[str, Iterable[str]] | None = None,
    regime_playbooks: bool = True,
) -> dict[str, Any]:
    """Classify every candidate not already watched and report what would trade today.

    ``gates`` maps a gate name (``token``, ``sector``) to the symbols that pass it; a
    candidate is promotable when it passes every gate named. No gates named means the
    plan cannot say, and says so.
    """
    watched_set = {str(item).upper() for item in watched}
    gate_sets = {name: {str(s).upper() for s in symbols} for name, symbols in (gates or {}).items()}
    rows = []
    for instrument in candidates:
        if instrument.symbol.upper() in watched_set:
            continue
        bars = _bars_for(history, instrument, now)
        summary = classify(bars, timeframe="1d")
        playbook = playbook_for(summary.label, enabled=regime_playbooks)
        if summary.label == INSUFFICIENT_HISTORY:
            role = ROLE_UNREAD
        elif playbook.name in FOCUS_PLAYBOOKS:
            role = ROLE_OPPORTUNITY
        else:
            role = ROLE_QUIET
        missing = sorted(name for name, symbols in gate_sets.items() if instrument.symbol.upper() not in symbols)
        rows.append({
            "symbol": instrument.symbol,
            "exchange": instrument.exchange,
            "regime": summary.label,
            "daily_bars": len(bars),
            "trend_strength": str(summary.trend_strength),
            "volatility_ratio": str(summary.volatility_ratio),
            "playbook": playbook.name,
            "role": role,
            "gates_missing": missing,
            "promotable": bool(gate_sets) and not missing,
        })
    order = {ROLE_OPPORTUNITY: 0, ROLE_QUIET: 1, ROLE_UNREAD: 2}
    rows.sort(key=lambda item: (order[item["role"]], -Decimal(item["trend_strength"]), item["symbol"]))
    return {
        "scanned": len(rows),
        "skipped_watched": sum(1 for item in candidates if item.symbol.upper() in watched_set),
        "history": "daily" if history is not None else "none",
        "gates": sorted(gate_sets),
        "opportunities": [item["symbol"] for item in rows if item["role"] == ROLE_OPPORTUNITY],
        "names": rows,
    }


def brief_lines(report: Mapping[str, Any], limit: int = MAX_BRIEF_LINES) -> list[str]:
    """The lines the morning brief adds when a scan ran."""
    if not report or not report.get("scanned"):
        return []
    opportunities = [item for item in report["names"] if item["role"] == ROLE_OPPORTUNITY]
    head = f"Outside the book ({report['scanned']} scanned): {len(opportunities)} would trade today"
    if report.get("history") == "none":
        head += ", no daily history to read them"
    lines = [head + "."]
    for item in opportunities[:limit]:
        gate = "gates passed" if item["promotable"] else (
            "needs " + " and ".join(item["gates_missing"]) if item["gates_missing"] else "gates unknown"
        )
        lines.append(f"{item['symbol']} {item['regime']} {item['playbook']} trend {item['trend_strength']} ({gate})")
    if len(opportunities) > limit:
        lines.append(f"+{len(opportunities) - limit} more in the plan file")
    return lines
