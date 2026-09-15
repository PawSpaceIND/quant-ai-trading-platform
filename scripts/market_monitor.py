"""Read-only Zerodha quote monitor; never submits orders."""
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
from kiteconnect import KiteConnect

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, default_holidays, holidays_from_json
from quant_ai.marketdata.risk_history import risk_history_input

CONFIG = Path.home() / ".config/pramana"
TARGET = Path(os.environ.get("PRAMANA_MARKET_SNAPSHOT", str(CONFIG / "market-monitor.json")))
SYMBOLS = ["NIFTY 50", "NIFTY BANK", "INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "ITC", "LT", "TATASTEEL", "HINDALCO", "GOLDBEES", "SILVERBEES"]
BSE_SYMBOLS = ["SENSEX", "RELIANCE", "TCS"]
EXTRA_INSTRUMENTS_ENV = "PRAMANA_MARKET_EXTRA_INSTRUMENTS_JSON"
_ALLOWED_EXTRA_EXCHANGES = {"MCX", "CDS", "NSE", "BSE", "NCDEX", "NFO", "BFO", "BCD", "MSEI", "IFSC"}
_ALLOWED_EXTRA_ASSET_CLASSES = {"EQUITY", "ETF", "INDEX", "OPTION", "FUTURE", "FX", "COMMODITY", "METAL", "BOND", "DEBT", "FUND", "REIT", "INVIT", "SLB", "IPO", "SGB"}
_GENERIC_DERIVATIVE_SYMBOLS = {
    "GOLD", "SILVER", "COPPER", "ALUMINIUM", "ZINC", "NICKEL", "LEAD",
    "CRUDE", "CRUDEOIL", "NATURALGAS", "USDINR", "EURINR", "GBPINR", "JPYINR",
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
}
history = {}


def _positive_number(value: object, field: str) -> float | int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"market_extra_{field}_invalid")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"market_extra_{field}_invalid")
    return value


def extra_instruments() -> list[dict[str, object]]:
    """Parse exact read-only instruments; never infer a contract from a label."""
    raw = os.environ.get(EXTRA_INSTRUMENTS_ENV, "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("market_extra_instruments_invalid") from error
    if not isinstance(payload, list) or len(payload) > 100:
        raise ValueError("market_extra_instruments_invalid")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    default_keys = {("NSE", item) for item in SYMBOLS} | {("BSE", item) for item in BSE_SYMBOLS}
    derivative_exchanges = {"MCX", "CDS", "NCDEX", "NFO", "BFO", "BCD"}
    generic_rejection_exchanges = derivative_exchanges | {"MSEI"}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("market_extra_instrument_invalid")
        symbol = str(item.get("symbol", "")).strip().upper()
        exchange = str(item.get("exchange", "")).strip().upper()
        asset_class = str(item.get("assetClass", item.get("asset_class", ""))).strip().upper()
        currency = str(item.get("currency", "INR")).strip().upper()
        market = str(item.get("market", "INDIA")).strip().upper()
        if not symbol or len(symbol) > 40 or exchange not in _ALLOWED_EXTRA_EXCHANGES:
            raise ValueError("market_extra_instrument_identity_invalid")
        valid_currency = currency == "INR" or (exchange == "IFSC" and currency in {"INR", "USD"})
        if asset_class not in _ALLOWED_EXTRA_ASSET_CLASSES or market != "INDIA" or not valid_currency:
            raise ValueError("market_extra_instrument_scope_invalid")
        key = (exchange, symbol)
        if key in seen or key in default_keys:
            raise ValueError("market_extra_instrument_duplicate")
        seen.add(key)
        contract = str(item.get("contract", "")).strip().upper()
        expiry = str(item.get("expiry", "")).strip()
        segment = str(item.get("segment", "")).strip().upper()
        product = str(item.get("product", "")).strip().upper()
        underlying = str(item.get("underlying", "")).strip().upper()
        option_type = str(item.get("optionType", item.get("option_type", ""))).strip().upper()
        provider_id = str(item.get("providerInstrumentId", item.get("provider_instrument_id", ""))).strip()
        if len(contract) > 80 or len(segment) > 40 or len(product) > 40 or len(underlying) > 40 or len(provider_id) > 100:
            raise ValueError("market_extra_instrument_metadata_invalid")
        is_derivative = exchange in derivative_exchanges or asset_class in {"OPTION", "FUTURE", "FX", "COMMODITY", "METAL"}
        if is_derivative and (not contract or not expiry):
            raise ValueError("market_extra_derivative_contract_metadata_required")
        if exchange in generic_rejection_exchanges and symbol in _GENERIC_DERIVATIVE_SYMBOLS:
            raise ValueError("market_extra_derivative_symbol_must_be_exact")
        if expiry:
            try:
                date.fromisoformat(expiry)
            except ValueError as error:
                raise ValueError("market_extra_expiry_invalid") from error
        if asset_class == "OPTION":
            if option_type not in {"CE", "PE"}:
                raise ValueError("market_extra_option_type_invalid")
            if _positive_number(item.get("strike"), "strike") is None:
                raise ValueError("market_extra_option_strike_required")
        elif option_type or item.get("strike") not in (None, ""):
            raise ValueError("market_extra_option_metadata_unexpected")
        lot_size = _positive_number(item.get("lotSize", item.get("lot_size")), "lot_size")
        tick_size = _positive_number(item.get("tickSize", item.get("tick_size")), "tick_size")
        result.append({
            "symbol": symbol, "exchange": exchange, "assetClass": asset_class,
            "currency": currency, "market": market, "contract": contract,
            "expiry": expiry, "underlying": underlying, "optionType": option_type,
            "strike": _positive_number(item.get("strike"), "strike"),
            "product": product, "segment": segment, "providerInstrumentId": provider_id,
            "lotSize": lot_size, "tickSize": tick_size,
        })
    return result


def collect():
    # Use the same additive closure override as the engine. Invalid configuration
    # fails before any source request instead of quietly reverting to a different calendar.
    holiday_override = json.loads(os.environ.get("PRAMANA_HOLIDAYS_JSON") or "{}")
    calendar = MarketCalendar(holidays_from_json(holiday_override, default_holidays()))
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
    from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
    credentials = {"api_key": os.environ["ZERODHA_API_KEY"]} if os.environ.get("ZERODHA_API_KEY") else json.loads((CONFIG / "zerodha.json").read_text())
    session = {"access_token": os.environ["ZERODHA_ACCESS_TOKEN"]} if os.environ.get("ZERODHA_ACCESS_TOKEN") else json.loads((CONFIG / "zerodha-session.json").read_text())
    kite = KiteConnect(api_key=credentials["api_key"], access_token=session["access_token"], timeout=15)
    extras = extra_instruments()
    profile = kite.profile()
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    default_specs = [
        {"symbol": symbol, "exchange": "NSE", "assetClass": "INDEX" if symbol in {"NIFTY 50", "NIFTY BANK"} else "ETF" if symbol in {"GOLDBEES", "SILVERBEES"} else "EQUITY", "currency": "INR", "market": "INDIA"}
        for symbol in SYMBOLS
    ] + [
        {"symbol": symbol, "exchange": "BSE", "assetClass": "INDEX" if symbol == "SENSEX" else "EQUITY", "currency": "INR", "market": "INDIA"}
        for symbol in BSE_SYMBOLS
    ]
    specs = default_specs + extras
    default_keys = {f"{item['exchange']}:{item['symbol']}" for item in default_specs}
    default_quotes = {}
    quote_keys = sorted(default_keys)
    try:
        default_quotes = kite.quote(quote_keys)
    except Exception:  # noqa: BLE001 — retry defaults individually so one venue cannot hide another.
        for key in quote_keys:
            try:
                default_quotes.update(kite.quote([key]))
            except Exception:  # noqa: BLE001 — an unavailable contract is explicit in the snapshot.
                pass
    rows = []
    for spec in specs:
        symbol = str(spec["symbol"])
        exchange = str(spec["exchange"])
        key = f"{exchange}:{symbol}"
        if key in default_keys:
            quote = default_quotes.get(key)
        else:
            # Query extras one at a time so one stale or mistyped contract cannot
            # hide the default observations.
            try:
                quote = kite.quote([key]).get(key)
            except Exception:  # noqa: BLE001 — an unavailable contract is explicit in the snapshot.
                quote = None
        base_instrument = {"symbol": symbol, "market": str(spec["market"]), "currency": str(spec["currency"]),
                           "exchange": exchange, "assetClass": str(spec["assetClass"])}
        for field in ("contract", "expiry", "underlying", "optionType", "strike", "product", "segment", "providerInstrumentId", "lotSize", "tickSize"):
            if spec.get(field) not in (None, ""):
                base_instrument[field] = spec[field]
        if not quote:
            rows.append({"symbol": symbol, "available": False, "instrument": base_instrument})
            continue
        token = quote["instrument_token"]
        if token not in history or history[token][0] != now.date():
            try:
                bars = kite.historical_data(token, max((now - timedelta(days=400)).date(), date(2026, 1, 1)), now.date(), "day")
                history[token] = (now.date(), [{"date": str(bar["date"]), "close": bar["close"]} for bar in bars])
            except Exception:  # noqa: BLE001 — missing history stays explicitly unavailable.
                history[token] = (now.date(), [])
        close = quote.get("ohlc", {}).get("close", 0)
        price = quote.get("last_price")
        base_instrument["providerInstrumentId"] = str(token)
        rows.append({"symbol": symbol, "available": True, "price": price,
                     "change": ((price / close - 1) * 100) if close and price is not None else None,
                     "ohlc": quote.get("ohlc"), "volume": quote.get("volume"),
                     "exchangeTimestamp": str(quote.get("timestamp") or ""),
                     "lastTrade": str(quote.get("last_trade_time") or ""),
                     "history": history[token][1], "instrument": base_instrument})
    news = []
    try:
        adapter = RssNewsSentimentAdapter(ResilientHttpClient(UrllibTransport()), ("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",))
        news = [{"headline": item.headline, "publishedAt": item.published_at.isoformat(), "source": "Economic Times RSS"} for item in adapter.fetch("GEOPOLITICAL", now)[:6]]
    except Exception:  # noqa: BLE001 — news availability must not suppress price observations.
        news = []
    runtime_path = CONFIG / "india-paper/status.json"
    runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {"status": "not_started"}
    return {"news": news, "runtime": runtime, "status": "ok", "fetchedAt": now.isoformat(), "source": "Zerodha REST quotes",
            "session": calendar.state(Market.INDIA, now).value,
            "exchanges": profile.get("exchanges", []), "rows": rows,
            "riskHistory": risk_history_input(rows, now, calendar=calendar),
            "commodity": "MCX observations configured from exact contracts" if extras else ("MCX enabled; contract feed not configured" if "MCX" in profile.get("exchanges", []) else "MCX access not reported by this account"),
            "note": "Last available quotes; poll time is not trade time. Gold/silver ETFs follow NSE hours. Extra MCX/CDS rows are read-only until separately qualified.",
            "providers": provider_status(bool(news))}


def provider_status(news_available):
    """Describe configured providers without claiming that they are healthy."""
    fundamentals = os.environ.get("PRAMANA_FUNDAMENTALS_PROVIDER", "yahoo").strip().lower() or "yahoo"
    daily_history = os.environ.get("PRAMANA_DAILY_HISTORY_PROVIDER", "yahoo").strip().lower() or "yahoo"
    ibkr = os.environ.get("PRAMANA_IBKR_ENABLED", "false").strip().lower() in {"true", "1", "yes", "on"}
    return {
        "Claude": "Availability is checked per copilot request",
        "Technical": "Real daily candle history",
        "News": "Economic Times RSS; rule-based sentiment" if news_available else "RSS unavailable; no fabricated opinions",
        "Macro": "FRED configured (FRED_API_KEY present)" if os.environ.get("FRED_API_KEY", "").strip() else "Unavailable; FRED_API_KEY not configured",
        "Fundamentals": "Yahoo quoteSummary; agents abstain unless all four ratios return" if fundamentals == "yahoo" else f"Disabled (PRAMANA_FUNDAMENTALS_PROVIDER={fundamentals}); valuation agents abstain",
        "Regime history": "Yahoo daily bars" if daily_history == "yahoo" else f"Disabled (PRAMANA_DAILY_HISTORY_PROVIDER={daily_history}); intraday-only regime",
        "US equities": "IBKR enabled" if ibkr else "No IBKR connection",
    }


if __name__ == "__main__":
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            payload = collect()
        except Exception as error:  # noqa: BLE001 — publish unavailable state without credential-bearing details.
            payload = {"status": "unavailable", "fetchedAt": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), "error": type(error).__name__, "rows": []}
        temp = TARGET.with_suffix(".tmp")
        temp.write_text(json.dumps(payload))
        os.chmod(temp, 0o600)
        temp.replace(TARGET)
        print("Monitor:", payload["status"], "rows:", len(payload["rows"]), flush=True)
        if "--once" in __import__("sys").argv:
            break
        time.sleep(60)
