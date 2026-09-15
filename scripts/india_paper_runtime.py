"""Local India paper-only launcher. Credentials stay outside the repository."""
import asyncio
import fcntl
import json
import os
import signal
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import certifi
from kiteconnect import KiteConnect

CONFIG = Path.home() / ".config/pramana"
RUNTIME = Path(os.environ.get("PRAMANA_PILOT_RUNTIME", str(CONFIG / "india-paper")))
SYMBOLS = ("INFY", "RELIANCE", "TCS")


def configure():
    if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").lower() in {"true", "1", "yes", "on"}:
        raise RuntimeError("This launcher supports paper trading only")
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = json.loads((CONFIG / "zerodha.json").read_text())
    session = json.loads((CONFIG / "zerodha-session.json").read_text())
    kite = KiteConnect(api_key=credentials["api_key"], access_token=session["access_token"], timeout=15)
    profile = kite.profile()
    if profile["user_id"] != session["user_id"] or "NSE" not in profile.get("exchanges", []):
        raise RuntimeError("Account validation failed")
    quotes = kite.quote(["NSE:" + symbol for symbol in SYMBOLS])
    mappings = {str(quotes["NSE:" + symbol]["instrument_token"]): symbol for symbol in SYMBOLS}
    os.environ.update({
        "TRADING_LIVE_MONEY_ACTIVE": "false", "PRAMANA_IBKR_ENABLED": "false",
        "ZERODHA_API_KEY": credentials["api_key"], "ZERODHA_ACCESS_TOKEN": session["access_token"],
        "ANTHROPIC_API_KEY": (CONFIG / "anthropic.key").read_text().strip(),
        "SSL_CERT_FILE": certifi.where(),
        "PRAMANA_NEWS_RSS_URLS": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "PRAMANA_ZERODHA_TOKENS_JSON": json.dumps(list(mappings)),
        "PRAMANA_ZERODHA_SYMBOLS_JSON": json.dumps(mappings),
        "PRAMANA_LEDGER_PATH": str(RUNTIME / "ledger.sqlite"),
        "PRAMANA_PROOF_DIR": str(RUNTIME / "proofs"),
        "PRAMANA_TENANT_ID": "india-paper",
        "PRAMANA_HALT_FILE": str(RUNTIME / "HALT"),
        "PRAMANA_GHOST_LOG": str(RUNTIME / "events.jsonl"),
        "PRAMANA_TARGET_SYMBOL": "INFY", "PRAMANA_TARGET_MARKET": "INDIA",
        "PRAMANA_TARGET_CURRENCY": "INR", "PRAMANA_TARGET_EXCHANGE": "NSE",
        "PRAMANA_FOUNDER_DIRECTIVES_JSON": json.dumps({
            "starting_capital": 100000, "allowed_markets": ["INDIA"],
            "allowed_asset_classes": ["EQUITY"], "max_open_positions": 3,
            "watchlist": [{"symbol": symbol, "market": "INDIA", "asset_class": "EQUITY",
                           "currency": "INR", "exchange": "NSE"} for symbol in SYMBOLS],
            "instructions": "Paper validation only. Abstain when required evidence is unavailable or stale."
        })
    })
    # External messaging is intentionally not part of this local runtime.
    os.environ.pop("PRAMANA_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("PRAMANA_TELEGRAM_CHAT_ID", None)


def save_status(payload):
    target = RUNTIME / "status.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, default=str))
    temporary.chmod(0o600)
    temporary.replace(target)


async def run():
    from quant_ai.daemon import build_ghost_runner_from_env
    runner = build_ghost_runner_from_env()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, runner.request_stop)
    if runner.daemon.heartbeat().active_market_sessions["INDIA"] == "CLOSED":
        await runner.daemon.run_once()
    task = asyncio.create_task(runner.start())
    try:
        while not task.done() and not runner.daemon.heartbeat().stopping:
            beat = asdict(runner.daemon.heartbeat())
            save_status({"status": "running", "updatedAt": datetime.now(timezone.utc).isoformat(),
                         "mode": "paper", "watchlist": SYMBOLS, "heartbeat": beat,
                         "cadenceFailures": runner.consecutive_failures,
                         "halted": (RUNTIME / "HALT").exists(),
                         "providers": {"News": "Economic Times RSS; rule-based sentiment",
                                       "Macro": "Unavailable unless FRED configured",
                                       "Fundamentals": "Unavailable; affected agents abstain"}})
            await asyncio.sleep(15)
        if task.done():
            await task
    finally:
        runner.request_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        save_status({"status": "stopped", "updatedAt": datetime.now(timezone.utc).isoformat(), "mode": "paper"})


if __name__ == "__main__":
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / "runtime.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Paper runtime already running")
            raise SystemExit(1)
        try:
            configure()
            asyncio.run(run())
        except Exception as error:
            save_status({"status": "blocked", "updatedAt": datetime.now(timezone.utc).isoformat(),
                         "error": type(error).__name__, "mode": "paper"})
            print("Paper runtime blocked:", type(error).__name__)
            raise SystemExit(1)
