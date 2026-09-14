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
from quant_ai.execution.session import MarketCalendar, default_holidays
from quant_ai.marketdata.risk_history import risk_history_input

CONFIG = Path.home() / ".config/pramana"
TARGET = Path(os.environ.get("PRAMANA_MARKET_SNAPSHOT", str(CONFIG / "market-monitor.json")))
SYMBOLS = ["NIFTY 50", "NIFTY BANK", "INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "ITC", "LT", "TATASTEEL", "HINDALCO", "GOLDBEES", "SILVERBEES"]
history = {}


def collect():
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
    from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
    credentials = {"api_key": os.environ["ZERODHA_API_KEY"]} if os.environ.get("ZERODHA_API_KEY") else json.loads((CONFIG / "zerodha.json").read_text())
    session = {"access_token": os.environ["ZERODHA_ACCESS_TOKEN"]} if os.environ.get("ZERODHA_ACCESS_TOKEN") else json.loads((CONFIG / "zerodha-session.json").read_text())
    kite = KiteConnect(api_key=credentials["api_key"], access_token=session["access_token"], timeout=15)
    profile = kite.profile()
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    quotes = kite.quote(["NSE:" + symbol for symbol in SYMBOLS])
    rows = []
    for symbol in SYMBOLS:
        quote = quotes.get("NSE:" + symbol)
        if not quote:
            rows.append({"symbol": symbol, "available": False})
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
        rows.append({"symbol": symbol, "available": True, "price": price,
                     "change": ((price / close - 1) * 100) if close and price is not None else None,
                     "ohlc": quote.get("ohlc"), "volume": quote.get("volume"),
                     "exchangeTimestamp": str(quote.get("timestamp") or ""),
                     "lastTrade": str(quote.get("last_trade_time") or ""),
                     "history": history[token][1],
                     "instrument": {"symbol": symbol, "market": "INDIA", "currency": "INR", "exchange": "NSE",
                                    "assetClass": "INDEX" if symbol in {"NIFTY 50", "NIFTY BANK"} else "ETF" if symbol in {"GOLDBEES", "SILVERBEES"} else "EQUITY",
                                    "providerInstrumentId": str(token)}})
    news = []
    try:
        adapter = RssNewsSentimentAdapter(ResilientHttpClient(UrllibTransport()), ("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",))
        news = [{"headline": item.headline, "publishedAt": item.published_at.isoformat(), "source": "Economic Times RSS"} for item in adapter.fetch("GEOPOLITICAL", now)[:6]]
    except Exception:  # noqa: BLE001 — news availability must not suppress price observations.
        news = []
    runtime_path = CONFIG / "india-paper/status.json"
    runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {"status": "not_started"}
    return {"news": news, "runtime": runtime, "status": "ok", "fetchedAt": now.isoformat(), "source": "Zerodha REST quotes",
            "session": MarketCalendar(default_holidays()).state(Market.INDIA, now).value,
            "exchanges": profile.get("exchanges", []), "rows": rows,
            "riskHistory": risk_history_input(rows, now),
            "commodity": "MCX enabled; contract feed not configured" if "MCX" in profile.get("exchanges", []) else "MCX access not reported by this account",
            "note": "Last available quotes; poll time is not trade time. Gold/silver ETFs follow NSE hours.",
            "providers": {"Claude": "Availability is checked per copilot request", "Technical": "Real daily candle history", "News": "Economic Times RSS; rule-based sentiment" if news else "RSS unavailable; no fabricated opinions", "Macro": "Unavailable; FRED not configured", "Fundamentals": "Unavailable; licensed source needed", "US equities": "No IBKR connection"}}


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
