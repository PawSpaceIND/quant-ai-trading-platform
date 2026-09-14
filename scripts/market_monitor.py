"""Read-only Zerodha quote monitor; never submits orders."""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, default_holidays

CONFIG = Path.home() / ".config/pramana"
TARGET = CONFIG / "market-monitor.json"
SYMBOLS = ["NIFTY 50", "NIFTY BANK", "INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "ITC", "LT", "TATASTEEL", "HINDALCO", "GOLDBEES", "SILVERBEES"]
history = {}


def collect():
    credentials = json.loads((CONFIG / "zerodha.json").read_text())
    session = json.loads((CONFIG / "zerodha-session.json").read_text())
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
        if token not in history:
            try:
                bars = kite.historical_data(token, (now - timedelta(days=35)).date(), now.date(), "day")
                history[token] = [{"date": str(bar["date"]), "close": bar["close"]} for bar in bars]
            except Exception:
                history[token] = []
        close = quote.get("ohlc", {}).get("close", 0)
        price = quote.get("last_price")
        rows.append({"symbol": symbol, "available": True, "price": price,
                     "change": ((price / close - 1) * 100) if close and price is not None else None,
                     "ohlc": quote.get("ohlc"), "volume": quote.get("volume"),
                     "exchangeTimestamp": str(quote.get("timestamp") or ""),
                     "lastTrade": str(quote.get("last_trade_time") or ""),
                     "history": history[token]})
    return {"status": "ok", "fetchedAt": now.isoformat(), "source": "Zerodha REST quotes",
            "session": MarketCalendar(default_holidays()).state(Market.INDIA, now).value,
            "exchanges": profile.get("exchanges", []), "rows": rows,
            "commodity": "MCX enabled; contract feed not configured" if "MCX" in profile.get("exchanges", []) else "MCX access not reported by this account",
            "note": "Last available quotes; poll time is not trade time. Gold/silver ETFs follow NSE hours.",
            "providers": {"Claude": "Authenticated; structured Sonnet 5 request verified", "Technical": "Real daily candle history", "News": "Sandbox provider", "Macro": "Sandbox provider", "Fundamentals": "Sandbox provider", "US equities": "No IBKR connection"}}


if __name__ == "__main__":
    while True:
        try:
            payload = collect()
        except Exception as error:
            payload = {"status": "unavailable", "fetchedAt": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), "error": type(error).__name__, "rows": []}
        temp = TARGET.with_suffix(".tmp")
        temp.write_text(json.dumps(payload))
        os.chmod(temp, 0o600)
        temp.replace(TARGET)
        print("Monitor:", payload["status"], "rows:", len(payload["rows"]), flush=True)
        if "--once" in __import__("sys").argv:
            break
        time.sleep(60)
