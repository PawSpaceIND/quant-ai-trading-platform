"""Local India paper-only launcher. Credentials stay outside the repository."""
import asyncio
import fcntl
import json
import os
import signal
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.execution.session import INDIA_EXCHANGE_SESSIONS
from quant_ai.operations.zerodha_session import is_expired, read_session

CONFIG = Path.home() / ".config/pramana"
RUNTIME = Path(os.environ.get("PRAMANA_PILOT_RUNTIME", str(CONFIG / "india-paper")))
DEFAULT_DIRECTIVES = Path(__file__).resolve().parents[1] / "deploy" / "founder-directives.example.json"
DIRECTIVES_PATH = Path(os.environ.get("PRAMANA_PILOT_DIRECTIVES") or DEFAULT_DIRECTIVES)


def load_directives() -> tuple[dict, tuple[tuple[str, str], ...]]:
    """The pilot mandate and the instruments it names, read rather than restated here.

    This launcher used to carry its own copy: three equities, three open positions and
    EQUITY only, while the operator's directives named five instruments, five positions and
    EQUITY plus ETF. Both were internally consistent, which is what made the disagreement
    survive - the restated copy kept working while describing a different portfolio than
    the one the operator had configured, and the ETF restriction quietly explained why two
    of their five instruments were absent.

    Fails closed. A directives file that cannot be read stops the launcher rather than
    starting the pilot on whatever list happened to be compiled into it.
    """
    try:
        directives = json.loads(DIRECTIVES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Founder directives unreadable at {DIRECTIVES_PATH}: {error}") from error
    if not isinstance(directives, dict):
        # RuntimeError, not TypeError: every refusal in this launcher is a RuntimeError the
        # caller prints as a startup failure, and one odd type here would break that contract
        # for no gain to the operator reading it.
        raise RuntimeError(  # noqa: TRY004
            f"Founder directives must be a JSON object: {DIRECTIVES_PATH}"
        )
    watchlist = directives.get("watchlist") or []
    # Every exchange the operator names, not NSE alone. Filtering to NSE here silently
    # discarded the rest of the mandate: an MCX metal trades for eight hours after the cash
    # market shuts, and dropping it at the launcher made the engine judge the whole pilot by
    # the NSE clock. The calendar downstream already keeps MCX and CDS hours.
    instruments = tuple(
        (str(item["exchange"]).strip().upper(), str(item["symbol"]).strip().upper())
        for item in watchlist
        if isinstance(item, dict) and item.get("exchange") and item.get("symbol")
    )
    if not instruments:
        raise RuntimeError(f"Founder directives name no instruments: {DIRECTIVES_PATH}")
    # An exchange with no published session would be judged by the venue fallback, which is
    # the NSE cash session - the quiet mis-timing this change exists to remove. Refuse it.
    unknown = sorted({code for code, _ in instruments} - set(INDIA_EXCHANGE_SESSIONS))
    if unknown:
        raise RuntimeError(
            f"Founder directives name exchanges with no published session: {', '.join(unknown)}"
        )
    symbols = [symbol for _, symbol in instruments]
    # Still keyed by symbol alone: the calendar maps symbol to exchange, so one symbol on two
    # exchanges would leave half the book judged by the wrong clock.
    if len(set(symbols)) != len(symbols):
        raise RuntimeError(f"Founder directives list a symbol twice: {DIRECTIVES_PATH}")
    return directives, instruments


def configure():
    # Imported here rather than at module scope. Both are broker-side dependencies from the
    # optional ``pilot`` extra, and requiring them to merely read the pilot's own
    # configuration made that configuration untestable without a broker SDK installed.
    import certifi
    from kiteconnect import KiteConnect

    if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").lower() in {"true", "1", "yes", "on"}:
        raise RuntimeError("This launcher supports paper trading only")
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = json.loads((CONFIG / "zerodha.json").read_text())
    try:
        session = read_session(CONFIG / "zerodha-session.json")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Zerodha session unusable: {error}; run: pramana zerodha-login") from error
    # Kite invalidates access tokens daily at about 06:00 IST. A dead token does not error;
    # the websocket goes quiet and stop enforcement pauses, so refuse it here instead.
    if is_expired(session, datetime.now(timezone.utc)):
        raise RuntimeError("Zerodha session expired at 06:00 IST; run: pramana zerodha-login")
    kite = KiteConnect(api_key=credentials["api_key"], access_token=session.access_token, timeout=15)
    directives, instruments = load_directives()
    profile = kite.profile()
    # Each segment is enabled separately on a Zerodha account. An MCX instrument on an
    # equity-only account does not fail at the quote call with anything an operator can read,
    # so name the missing segment here rather than let the pilot start half-subscribed.
    absent = [code for code, _ in instruments if code not in profile.get("exchanges", [])]
    if profile["user_id"] != session.user_id:
        raise RuntimeError("Account validation failed")
    if absent:
        raise RuntimeError(
            "Account validation failed: segments not enabled on this account: "
            + ", ".join(sorted(set(absent)))
        )
    print(f"pilot universe from {DIRECTIVES_PATH}: "
          + ", ".join(f"{code}:{symbol}" for code, symbol in instruments))
    quotes = kite.quote([f"{code}:{symbol}" for code, symbol in instruments])
    mappings = {str(quotes[f"{code}:{symbol}"]["instrument_token"]): symbol
                for code, symbol in instruments}
    # Optional real providers are taken from the process environment only; nothing is hardcoded.
    passthrough = {name: os.environ[name] for name in ("FRED_API_KEY", "PRAMANA_FUNDAMENTALS_PROVIDER")
                   if os.environ.get(name, "").strip()}
    os.environ.update({
        "TRADING_LIVE_MONEY_ACTIVE": "false", "PRAMANA_IBKR_ENABLED": "false",
        "ZERODHA_API_KEY": credentials["api_key"], "ZERODHA_ACCESS_TOKEN": session.access_token,
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
        "PRAMANA_TARGET_SYMBOL": instruments[0][1], "PRAMANA_TARGET_MARKET": "INDIA",
        "PRAMANA_TARGET_CURRENCY": "INR", "PRAMANA_TARGET_EXCHANGE": instruments[0][0],
        # Monday paper-pilot safety controls. Universe and exchange still come only from
        # the operator directives above; these arm history, book risk and deterministic close.
        "PRAMANA_DAILY_HISTORY_PROVIDER": "kite",
        "PRAMANA_INTRADAY_WARMUP_PROVIDER": "kite",
        "PRAMANA_BOOK_RISK_HISTORY": "daily",
        "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_SESSION_FLATTEN_MINUTES": "15",
        "PRAMANA_OVERNIGHT_GROSS_CAP": "0.25",
        "PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES": "15",
        # Passed through unchanged. Rewriting any field here is how the launcher and the
        # operator's mandate came to disagree in the first place.
        "PRAMANA_FOUNDER_DIRECTIVES_JSON": json.dumps(directives),
        **passthrough,
    })
    # External messaging is intentionally not part of this local runtime.
    os.environ.pop("PRAMANA_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("PRAMANA_TELEGRAM_CHAT_ID", None)



def release_revision():
    configured = os.environ.get("PRAMANA_RELEASE_REVISION", "").strip()
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def runtime_certification(runner):
    directives, expected_instruments = load_directives()
    expected_symbols = sorted(symbol for _exchange, symbol in expected_instruments)
    required_capital = directives.get("starting_capital")
    broker = runner.daemon.tracker.broker
    reconciliation = broker.reconcile("india-paper")
    protection = broker.protection_coverage("india-paper")
    starting_capital = broker.get_starting_capital("india-paper")
    paper_only = os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "").strip().lower() == "false"
    mapped = sorted(json.loads(os.environ["PRAMANA_ZERODHA_SYMBOLS_JSON"]).values())
    warmup = dict(runner.intraday_warmup_status)
    briefs = tuple(getattr(runner.daemon, "briefs", ()) or ())
    cadence_subjects = sorted({item.subject for item in briefs})
    cadence_provider_status = {
        item.subject: list(item.provider_status) for item in briefs
    }
    traces = runner.daemon.scheduler.pipeline.runtime.xai_logger.traces()
    cadence_at = max((item.generated_at for item in briefs), default=None)
    inference_modes = {}
    for trace in traces:
        if trace.subject not in expected_symbols or cadence_at is None or trace.generated_at != cadence_at:
            continue
        provenance = trace.provenance if isinstance(trace.provenance, dict) else {}
        mode = provenance.get("mode")
        inference_modes[trace.subject] = mode if isinstance(mode, str) else "unverified"

    flatten_minutes = int(os.environ["PRAMANA_SESSION_FLATTEN_MINUTES"])
    history_provider = os.environ["PRAMANA_DAILY_HISTORY_PROVIDER"]
    book_risk_required = os.environ["PRAMANA_REQUIRE_BOOK_RISK_GATES"] == "true"
    overnight_cap = os.environ.get("PRAMANA_OVERNIGHT_GROSS_CAP", "").strip()
    closing_window = os.environ.get("PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES", "").strip()

    reasons = []
    if not paper_only:
        reasons.append("live_money_not_disabled")
    if required_capital != 100000 or starting_capital != 100000:
        reasons.append("starting_capital_not_100000")
    if mapped != expected_symbols:
        reasons.append("watchlist_mapping_mismatch")
    if flatten_minutes != 15:
        reasons.append("session_flatten_not_15_minutes")
    if history_provider != "kite":
        reasons.append("daily_history_not_kite")
    if not book_risk_required:
        reasons.append("book_risk_not_required")
    if overnight_cap != "0.25":
        reasons.append("overnight_gross_firewall_not_armed")
    if closing_window != "15":
        reasons.append("closing_window_firewall_not_15_minutes")
    if set(warmup) != set(expected_symbols) or any(
        item.get("status") != "ready" or int(item.get("bars", 0)) < 50
        for item in warmup.values()
    ):
        reasons.append("intraday_warmup_not_ready")
    if cadence_subjects != expected_symbols:
        reasons.append("five_symbol_cadence_not_observed")
    if any("price=FRESH" not in cadence_provider_status.get(symbol, ()) for symbol in expected_symbols):
        reasons.append("five_symbol_price_freshness_not_observed")
    if any(inference_modes.get(symbol) != "llm" for symbol in expected_symbols):
        reasons.append("five_symbol_llm_inference_not_observed")
    if reconciliation["status"] != "matched":
        reasons.append("paper_ledger_reconciliation_failed")
    if protection["status"] != "complete":
        reasons.append("paper_position_protection_incomplete")

    return {
        "schema": "pramana.monday_pilot_runtime.v2",
        "revision": release_revision(),
        "ready": not reasons,
        "reasons": reasons,
        "paperOnly": paper_only,
        "startingCapital": str(starting_capital),
        "watchlist": [symbol for _exchange, symbol in expected_instruments],
        "watchlistInstruments": [
            {"exchange": exchange, "symbol": symbol}
            for exchange, symbol in expected_instruments
        ],
        "mappedSymbols": mapped,
        "sessionFlattenMinutes": flatten_minutes,
        "dailyHistoryProvider": history_provider,
        "bookRiskRequired": book_risk_required,
        "overnightGrossCap": overnight_cap,
        "overnightClosingWindowMinutes": closing_window,
        "intradayWarmup": warmup,
        "cadenceSubjects": cadence_subjects,
        "cadenceProviderStatus": cadence_provider_status,
        "inferenceModeBySymbol": inference_modes,
        "reconciliation": reconciliation["status"],
        "protection": protection["status"],
    }


def provider_status():
    """What is configured, not what is healthy; freshness lives in the heartbeat and collector."""
    fred = bool(os.environ.get("FRED_API_KEY", "").strip())
    fundamentals = os.environ.get("PRAMANA_FUNDAMENTALS_PROVIDER", "").strip()
    return {"News": "Economic Times RSS; rule-based sentiment",
            "Macro": ("FRED configured (FRED_API_KEY present)" if fred
                      else "Unavailable; FRED_API_KEY not configured"),
            "Fundamentals": (f"{fundamentals} configured (PRAMANA_FUNDAMENTALS_PROVIDER)" if fundamentals
                             else "Unavailable; affected agents abstain")}


def running_watchlist() -> tuple:
    """What this process is actually subscribed to, read from its own environment.

    Reporting a module constant here would let the status file keep naming a universe the
    engine had stopped watching. The token map is what the feed subscribes with, so it is
    the truest available statement of what is being observed.
    """
    try:
        mappings = json.loads(os.environ.get("PRAMANA_ZERODHA_SYMBOLS_JSON", "{}"))
    except ValueError:
        return ()
    if not isinstance(mappings, dict):
        return ()
    return tuple(sorted(str(symbol) for symbol in mappings.values()))


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
                         "mode": "paper", "watchlist": running_watchlist(), "heartbeat": beat,
                         "cadenceFailures": runner.consecutive_failures,
                         "halted": (RUNTIME / "HALT").exists(),
                         "providers": provider_status(),
                         "certification": runtime_certification(runner)})
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
            # RuntimeError messages are this launcher's own operator-facing refusals (paper-only,
            # session expired, account validation); other exception text stays out of the terminal.
            detail = f" {error}" if isinstance(error, RuntimeError) else ""
            print(f"Paper runtime blocked: {type(error).__name__}{detail}")
            raise SystemExit(1)
