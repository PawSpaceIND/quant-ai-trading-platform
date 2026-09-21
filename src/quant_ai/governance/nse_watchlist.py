"""Offline, source-bound preparation of an NSE fifty-name proposal, never deployment.

A supplied Nifty CSV is a selection input, not attested membership or liquidity.
Instrument identifiers come from the supplied broker master, never a symbol table.
The operator must qualify the inputs and later verify all fifty on the real host.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument, Market
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.pilot import validate_pilot_instruments
from quant_ai.marketdata.instrument_master import normalize_row
from quant_ai.risk.book_history import normalize_sector_map

BASE_SYMBOLS = ("INFY", "TCS", "RELIANCE", "GOLDBEES", "SILVERBEES")
RETAINED_EQUITIES = frozenset(BASE_SYMBOLS[:3])
RETAINED_ETFS = frozenset(BASE_SYMBOLS[3:])
CONSTITUENT_COLUMNS = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")
MASTER_COLUMNS = ("instrument_token", "exchange_token", "tradingsymbol", "name", "last_price",
                  "expiry", "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange")
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&._-]{0,39}\Z")
# Preparation policies, not exchange rules or changes to the existing trading limits.
MIN_GROUPS = 5
MAX_GROUP_NAMES = 12
CONSTITUENT_MAX_AGE = timedelta(days=7)
MASTER_MAX_AGE = timedelta(hours=24)


class WatchlistPreparationError(ValueError):
    """Fixed refusal codes; never render the contents of input files."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise WatchlistPreparationError(code)


def _aware(value: datetime) -> datetime:
    _require(isinstance(value, datetime) and value.utcoffset() is not None,
             "watchlist_aware_timestamp_required")
    return value.astimezone(timezone.utc)


def _fresh(observed_at: datetime, now: datetime, maximum_age: timedelta) -> None:
    age = _aware(now) - _aware(observed_at)
    _require(timedelta(0) <= age <= maximum_age, "watchlist_source_stale_or_future")


def _csv(payload: bytes, columns: tuple[str, ...], max_bytes: int, max_rows: int):
    _require(isinstance(payload, bytes) and 0 < len(payload) <= max_bytes,
             "watchlist_source_size_invalid")
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
        _require(reader.fieldnames == list(columns), "watchlist_source_header_invalid")
        rows = []
        for row in reader:
            _require(len(rows) < max_rows, "watchlist_source_row_limit")
            _require(None not in row and all(isinstance(v, str) for v in row.values()),
                     "watchlist_source_row_invalid")
            rows.append(row)
    except (UnicodeError, csv.Error):
        raise WatchlistPreparationError("watchlist_source_csv_invalid") from None
    _require(bool(rows), "watchlist_source_empty")
    return rows


def _symbol(value: str) -> str:
    _require(isinstance(value, str) and SYMBOL.fullmatch(value) is not None,
             "watchlist_symbol_invalid")
    return value


def parse_constituents(payload: bytes) -> dict[str, str]:
    """Validate the publisher CSV shape without inventing an effective/as-of date."""
    rows = _csv(payload, CONSTITUENT_COLUMNS, 200_000, 50)
    _require(len(rows) == 50, "watchlist_constituent_count_not_fifty")
    sectors: dict[str, str] = {}
    isins: set[str] = set()
    for row in rows:
        symbol = _symbol(row["Symbol"])
        group = row["Industry"].strip().upper()
        isin = row["ISIN Code"]
        _require(row["Series"] == "EQ" and bool(row["Company Name"].strip()),
                 "watchlist_constituent_not_cash_equity")
        _require(bool(group) and len(group) <= 100 and group.isprintable(),
                 "watchlist_sector_invalid")
        _require(re.fullmatch(r"IN[A-Z0-9]{10}", isin) is not None,
                 "watchlist_isin_invalid")
        _require(symbol not in sectors and isin not in isins,
                 "watchlist_constituent_duplicate")
        sectors[symbol] = group
        isins.add(isin)
    _require(RETAINED_EQUITIES <= sectors.keys(), "watchlist_retained_equity_missing")
    _require(not RETAINED_ETFS.intersection(sectors), "watchlist_constituent_etf_conflict")
    return sectors


def select_equities(sectors: Mapping[str, str]) -> tuple[str, ...]:
    """48 from the validated 50 + the two retained ETFs; no invented liquidity rank.

    Remove from the most represented industry, breaking ties by industry then symbol.
    Preserve the existing equity names and never remove the last member of a group.
    """
    selected = set(sectors)
    _require(len(selected) == 50 and RETAINED_EQUITIES <= selected,
             "watchlist_selection_scope_invalid")
    while len(selected) > 48:
        counts = Counter(sectors[s] for s in selected)
        eligible = [s for s in selected - RETAINED_EQUITIES if counts[sectors[s]] > 1]
        _require(bool(eligible), "watchlist_selection_cannot_preserve_groups")
        removed = min(eligible, key=lambda s: (-counts[sectors[s]], sectors[s], s))
        selected.remove(removed)
    return tuple(sorted(selected))


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise WatchlistPreparationError("watchlist_master_tick_invalid") from None
    _require(parsed.is_finite() and parsed > 0, "watchlist_master_tick_invalid")
    return parsed


def _token(value: str) -> int:
    _require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value) is not None,
             "watchlist_master_token_invalid")
    result = int(value)
    _require(result <= 4_294_967_295, "watchlist_master_token_invalid")
    return result


def derive_master_identities(payload: bytes, symbols: Sequence[str]) -> dict[str, dict]:
    """Validate exact NSE cash identities, reusing the repository's master normalizer."""
    wanted = set(symbols)
    _require(bool(wanted) and len(wanted) == len(symbols), "watchlist_master_scope_invalid")
    rows = _csv(payload, MASTER_COLUMNS, 10_000_000, 250_000)
    candidates: dict[str, dict] = {}
    selected_tokens: set[int] = set()
    for row in rows:
        if row["exchange"] != "NSE" or row["tradingsymbol"] not in wanted:
            continue
        symbol = _symbol(row["tradingsymbol"])
        _require(symbol not in candidates, "watchlist_master_ambiguous_symbol")
        _require(row["segment"] == "NSE" and row["instrument_type"] == "EQ"
                 and not row["expiry"] and row["strike"] in {"", "0", "0.0"},
                 "watchlist_master_not_cash")
        _require(row["lot_size"] == "1", "watchlist_master_cash_lot_invalid")
        token = _token(row["instrument_token"])
        _require(token not in selected_tokens, "watchlist_master_duplicate_token")
        tick = _positive_decimal(row["tick_size"])
        normalized = normalize_row(row)
        _require(normalized is not None and normalized["symbol"] == symbol
                 and normalized["exchange"] == "NSE" and normalized["currency"] == "INR"
                 and normalized["providerInstrumentId"] == str(token),
                 "watchlist_master_normalization_mismatch")
        candidates[symbol] = {"token": token, "lotSize": 1, "tickSize": str(tick)}
        selected_tokens.add(token)
    _require(set(candidates) == wanted, "watchlist_symbol_without_master_token")
    # A token reused by a different row is ambiguous even when that row is unselected.
    counts = Counter(_token(row["instrument_token"]) for row in rows)
    _require(all(counts[token] == 1 for token in selected_tokens),
             "watchlist_master_token_alias")
    return candidates


def validate_subscription_mapping(
    instruments: tuple[Instrument, ...], tokens: Sequence[int], mapping: Mapping[int, str],
) -> None:
    """Bijection and scope guard for an expanded pilot; no broker/ledger/network work."""
    validate_pilot_instruments(instruments)
    _require(all(type(t) is int and 0 < t <= 4_294_967_295 for t in tokens),
             "watchlist_subscription_token_invalid")
    _require(len(set(tokens)) == len(tokens), "watchlist_subscription_duplicate_token")
    _require(all(type(t) is int and 0 < t <= 4_294_967_295 for t in mapping),
             "watchlist_subscription_key_invalid")
    _require(set(tokens) == set(mapping), "watchlist_token_without_symbol_mapping")
    _require(all(isinstance(s, str) and SYMBOL.fullmatch(s) for s in mapping.values()),
             "watchlist_subscription_symbol_invalid")
    _require(len(set(mapping.values())) == len(mapping), "watchlist_subscription_duplicate_symbol")
    _require(set(mapping.values()) == {i.symbol for i in instruments},
             "watchlist_symbol_without_subscription_token")


def validate_sector_spread(symbols: Sequence[str], sectors: Mapping[str, str]) -> None:
    normalized = normalize_sector_map(sectors)
    _require(set(normalized) == set(symbols), "watchlist_sector_scope_incomplete")
    counts = Counter(normalized.values())
    _require(len(counts) >= MIN_GROUPS and max(counts.values()) <= MAX_GROUP_NAMES,
             "watchlist_sector_spread_insufficient")


def workload_report(day: date, daily_call_limit: int, daily_token_limit: int) -> dict:
    """Logical call opportunities, not billable attempts, actual tokens or currency spend."""
    _require(type(day) is date, "watchlist_workload_day_invalid")
    _require(type(daily_call_limit) is int and daily_call_limit > 0
             and type(daily_token_limit) is int and daily_token_limit > 0,
             "watchlist_ai_budget_must_remain_enabled")
    india = ZoneInfo("Asia/Kolkata")
    start = datetime.combine(day, time(), tzinfo=india)
    calendar = MarketCalendar(holidays=default_holidays())
    ticks = sum(calendar.state(Market.INDIA, start + timedelta(minutes=m), exchange="NSE")
                == MarketState.REGULAR_HOURS for m in range(0, 24 * 60, 10))
    return {
        "sessionDateIST": day.isoformat(), "calendarBasis": "repository_default_NSE_calendar",
        "cadenceMinutes": 10, "scheduledRegularTicks": ticks,
        "fiveNameConsensusOpportunities": 5 * ticks,
        "fiftyNameConsensusOpportunities": 50 * ticks, "opportunityMultiplier": "10",
        "dailyCallLimitShared": daily_call_limit, "dailyTokenLimitShared": daily_token_limit,
        "callLimitCoversFiftyConsensus": daily_call_limit >= 50 * ticks,
        "maxCompleteFiftyNameSweepsByCalls": min(ticks, daily_call_limit // 50),
        "headlineScope": "shares the daily ceiling with consensus; demand depends on headlines/cache",
        "actualTokenDemand": None, "currencyCost": None,
        "budgetChanged": False,
        "limitations": ["Model invocation opportunities, not guaranteed calls or fills",
                        "Provider retries, prompt length, token usage and prices not measured",
                        "Tokens are reserved before requests and reconciled to reported usage; not a currency ceiling",
                        "Call-only coverage and complete sweeps are upper bounds before other scopes and token reservations",
                        "Delays or other gates can reduce throughput; unchanged cap may exhaust early"],
    }


def prepare_bundle(
    base: dict, constituents: bytes, master: bytes, *, now: datetime,
    constituents_at: datetime, master_at: datetime, daily_call_limit: int, daily_token_limit: int,
) -> dict:
    """Return an offline proposal. No automatic file, account, budget or service changes."""
    _fresh(constituents_at, now, CONSTITUENT_MAX_AGE)
    _fresh(master_at, now, MASTER_MAX_AGE)
    before = FounderDirectives.from_json(base)
    validate_pilot_instruments(before.watchlist)
    _require(tuple(i.symbol for i in before.watchlist) == BASE_SYMBOLS,
             "watchlist_expected_five_name_base")
    _require(all(i.asset_class.value == ("ETF" if i.symbol in RETAINED_ETFS else "EQUITY")
                 for i in before.watchlist), "watchlist_base_asset_class_invalid")
    constituents_map = parse_constituents(constituents)
    equities = select_equities(constituents_map)
    symbols = BASE_SYMBOLS + tuple(s for s in equities if s not in RETAINED_EQUITIES)
    sectors = {s: constituents_map[s] for s in equities}
    # Retain the reviewed five-name policy for the two metal ETFs, not an inferred industry.
    sectors.update(dict.fromkeys(RETAINED_ETFS, "PRECIOUS_METALS"))
    validate_sector_spread(symbols, sectors)
    identities = derive_master_identities(master, symbols)
    proposal = copy.deepcopy(base)
    existing = {row["symbol"]: copy.deepcopy(row) for row in base["watchlist"]}
    watchlist = []
    for symbol in symbols:
        item = existing.get(symbol, {"symbol": symbol, "market": "INDIA", "currency": "INR",
                                     "exchange": "NSE", "asset_class": "EQUITY"})
        # Reject declared identities that conflict; do not silently alter existing ones.
        _require(item.get("lot_size") in (None, identities[symbol]["lotSize"]),
                 "watchlist_existing_lot_conflict")
        if item.get("tick_size") is not None:
            _require(Decimal(str(item["tick_size"])) == Decimal(identities[symbol]["tickSize"]),
                     "watchlist_existing_tick_conflict")
        item.update(lot_size=1, tick_size=identities[symbol]["tickSize"])
        watchlist.append(item)
    proposal["watchlist"] = watchlist
    proposal["sector_map"] = dict(sorted(sectors.items()))
    actual = FounderDirectives.from_json(proposal)
    tokens = [identities[s]["token"] for s in symbols]
    mapping = {identities[s]["token"]: s for s in symbols}
    validate_subscription_mapping(actual.watchlist, tokens, mapping)
    _require(len(actual.watchlist) == 50, "watchlist_expansion_not_fifty")
    cost = workload_report(_aware(now).astimezone(ZoneInfo("Asia/Kolkata")).date(),
                           daily_call_limit, daily_token_limit)
    return {
        "schema": "pramana.nse_watchlist_proposal.v1", "status": "prepared_offline",
        "preparedAt": _aware(now).isoformat(), "hostAcceptance": False,
        "sourceQualification": "operator_review_required_no_effective_date_inferred",
        "constituentsObservedAt": _aware(constituents_at).isoformat(),
        "masterObservedAt": _aware(master_at).isoformat(),
        "sourceDigests": {"constituents": hashlib.sha256(constituents).hexdigest(),
                          "master": hashlib.sha256(master).hexdigest()},
        "directives": proposal, "sectorMap": proposal["sector_map"],
        "environment": {"PRAMANA_ZERODHA_TOKENS_JSON": json.dumps(tokens, separators=(",", ":")),
                        "PRAMANA_ZERODHA_SYMBOLS_JSON": json.dumps(mapping, separators=(",", ":"))},
        "selection": {"retainedSymbols": list(BASE_SYMBOLS), "equities": 48, "etfs": 2,
                      "excludedConstituents": sorted(set(constituents_map) - set(equities)),
                      "sectorCounts": dict(sorted(Counter(sectors.values()).items())),
                      "basis": "supplied_Nifty50_membership_proxy_not_measured_liquidity"},
        "workload": cost,
    }
