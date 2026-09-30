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
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from quant_ai.agents.playbook import playbook_for
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.regime import INSUFFICIENT_HISTORY, classify
from quant_ai.marketdata.timeframes import closed_sessions, venue_for

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


def research_shortlist(
    candidates: Iterable[Instrument], *, history: Any, as_of: datetime,
    target_session: date, previous_session: date, sectors: Mapping[str, str],
    fixed_watchlist: Iterable[str] = (), observations: Mapping[str, Mapping] | None = None,
    limit: int = 15, min_turnover: Decimal = Decimal(50000000), sector_limit: int = 3,
) -> dict[str, Any]:
    """Offline research selection; no directives, subscriptions, orders or inference.

    The caller supplies exchange-calendar dates. Both publication and availability must
    precede as_of; a later-arriving catalyst cannot enter an earlier decision. Daily
    close times are filtered independently of provider behavior. Liquidity is an OHLCV
    turnover proxy, not an observed rupee trading total or a promise of execution.
    """
    zone = ZoneInfo("Asia/Kolkata")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    # Morning research may freeze before the open; it may not use target-day bars.
    if previous_session >= target_session or cutoff.astimezone(zone).date() > target_session:
        raise ValueError("session dates do not describe prospective research")
    if not 10 <= limit <= 15 or min_turnover <= 0 or sector_limit < 1:
        raise ValueError("invalid research screening policy")
    universe = tuple(candidates)
    if len(universe) > MAX_SCAN_NAMES or len({i.symbol for i in universe}) != len(universe):
        raise ValueError("research universe must be unique and at most100 names")
    rows = []
    for instrument in universe:
        if (instrument.market != Market.INDIA or instrument.exchange != "NSE"
                or instrument.asset_class != AssetClass.EQUITY or instrument.is_dated_contract):
            raise ValueError("research universe must be NSE cash stocks")
        bars = tuple(b for b in closed_sessions(_bars_for(history, instrument, cutoff), cutoff,
                                                venue_for(Market.INDIA))
                     if b.timestamp.astimezone(zone).date() <= previous_session)
        reasons = []
        if len(bars) < 21:
            reasons.append("insufficient_prior_sessions")
        if not bars or bars[-1].timestamp.astimezone(zone).date() != previous_session:
            reasons.append("missing_previous_session")
        sector = sectors.get(instrument.symbol)
        if not sector:
            reasons.append("unknown_sector")
        metrics = None
        if len(bars) >= 21:
            prior = bars[-21:-1]
            avg_volume = sum((b.volume for b in prior), Decimal(0)) / 20
            turnover = sum((b.close * b.volume for b in bars[-20:]), Decimal(0)) / 20
            if avg_volume <= 0 or turnover < min_turnover:
                reasons.append("liquidity_proxy_below_screen")
            metrics = {"turnover_proxy_20d": str(turnover),
                       "momentum_5d": str(bars[-1].close / bars[-6].close - 1),
                       "relative_volume": str(bars[-1].volume / avg_volume) if avg_volume > 0 else None}
        observed = dict((observations or {}).get(instrument.symbol, {}))
        available = False
        try:
            published = datetime.fromisoformat(observed["published_at"])
            received = datetime.fromisoformat(observed["received_at"])
            available = (published.utcoffset() is not None and received.utcoffset() is not None
                         and published <= received <= cutoff)
        except (KeyError, ValueError, TypeError):
            pass
        rows.append({"symbol": instrument.symbol, "sector": sector, "eligible": not reasons,
                     "reasons": reasons, "metrics": metrics,
                     "last_session": bars[-1].timestamp.astimezone(zone).date().isoformat() if bars else None,
                     "catalyst_observed": observed.get("catalyst") if available and type(observed.get("catalyst")) is bool else None,
                     "spread_bps": str(observed["spread_bps"]) if available and isinstance(observed.get("spread_bps"), Decimal)
                         and observed["spread_bps"].is_finite() and observed["spread_bps"] >= 0 else None,
                     "execution_authorized": False})
    eligible = [r for r in rows if r["eligible"]]
    # Transparent cross-sectional ranks; no fitted weights or probability claim.
    ranks = {}
    for metric in ("momentum_5d", "relative_volume", "turnover_proxy_20d"):
        for rank, row in enumerate(sorted(eligible, key=lambda r: (Decimal(r["metrics"][metric]), r["symbol"]))):
            ranks[row["symbol"]] = ranks.get(row["symbol"], 0) + rank
    eligible.sort(key=lambda r: (-ranks[r["symbol"]], r["symbol"]))
    selected, sector_counts = [], {}
    for row in eligible:
        row["rank_score"] = ranks[row["symbol"]]
        if len(selected) >= limit or sector_counts.get(row["sector"], 0) >= sector_limit:
            row["reasons"].append("research_capacity_or_sector_limit")
            continue
        selected.append(row["symbol"])
        sector_counts[row["sector"]] = sector_counts.get(row["sector"], 0) + 1
    return {"schema": "pramana.research_shortlist.v1", "as_of": cutoff.isoformat(),
            "target_session": target_session.isoformat(), "previous_session": previous_session.isoformat(),
            "universe": sorted(i.symbol for i in universe), "shortlist": selected,
            "fixed_watchlist": sorted(set(fixed_watchlist)), "names": rows,
            "policy": {"limit": limit, "min_turnover_proxy": str(min_turnover), "sector_limit": sector_limit},
            "limitations": ["Shortlist is research, not trades; fewer than10 is valid when evidence is insufficient.",
                            "Catalyst/spread observations are reported, not scored; unknown is not zero.",
                            "Point-in-time universe membership/provider availability must be supplied and archived.",
                            "No realized edge, forecast probability or prospective comparison result is implied."]}
