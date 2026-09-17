"""Interactive Zerodha Kite login: request token -> access token -> session file.

Paper-only helper behind ``pramana zerodha-login`` and ``scripts/zerodha_login.py``.
It imports the Kite SDK lazily, refuses to run under ``TRADING_LIVE_MONEY_ACTIVE``,
validates the new token against ``kite.profile()`` and writes
``~/.config/pramana/zerodha-session.json`` through ``write_session``. It never
prints or stores ``api_secret`` or the access token.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, urlparse

from quant_ai.operations.zerodha_session import (
    SESSION_FILE_NAME,
    SessionRecord,
    next_cutoff,
    write_session,
)

CREDENTIALS_FILE_NAME = "zerodha.json"
_TRUTHY = {"true", "1", "yes", "on"}
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class LoginError(RuntimeError):
    """Actionable failure; the command turns it into a non-zero exit."""


def config_directory() -> Path:
    """Same location the paper launcher and the quote monitor read."""
    return Path.home() / ".config" / "pramana"


def live_money_active(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() in _TRUTHY


def parse_request_token(value: str) -> str:
    """Accept a bare ``request_token`` or the full Kite redirect URL that carries it."""
    text = (value or "").strip()
    if not text:
        raise LoginError("request_token is empty; paste the redirect URL or the token")
    if "://" in text or "request_token=" in text or "?" in text:
        query = parse_qs(urlparse(text).query)
        status = (query.get("status") or [None])[0]
        if status is not None and status != "success":
            raise LoginError(f"Kite redirect reported status={status}; log in again")
        tokens = [item.strip() for item in query.get("request_token") or [] if item.strip()]
        if not tokens:
            raise LoginError("redirect URL carries no request_token query parameter")
        text = tokens[0]
    if not _TOKEN_PATTERN.fullmatch(text):
        raise LoginError("request_token contains unexpected characters; paste it exactly")
    return text


def load_credentials(directory: Path) -> tuple[str, str]:
    path = directory / CREDENTIALS_FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LoginError(
            f"{path} not found; create it (mode 0600) with the Kite app's api_key and api_secret"
        ) from error
    except json.JSONDecodeError as error:
        raise LoginError(f"{path} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise LoginError(f"{path} must contain a JSON object")
    api_key = str(payload.get("api_key") or "").strip()
    api_secret = str(payload.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        raise LoginError(
            f"{path} must contain non-empty api_key and api_secret (developers.kite.trade)"
        )
    return api_key, api_secret


def _kite_client_type() -> Any:
    try:
        module = import_module("kiteconnect")
    except ImportError as error:
        raise LoginError("kiteconnect is not installed; run: pip install -e '.[pilot]'") from error
    return module.KiteConnect


def _scrub(message: str, *secrets: str) -> str:
    for item in secrets:
        if item:
            message = message.replace(item, "***")
    return message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def login(
    *,
    request_token: str | None = None,
    config_dir: Path | None = None,
    prompt: Callable[[str], str] | None = None,
    out: TextIO | None = None,
    now: Callable[[], datetime] | None = None,
    env: Mapping[str, str] | None = None,
) -> SessionRecord:
    """Run the login flow and return the persisted record. Raises ``LoginError`` on failure."""
    stream = out or sys.stdout
    if live_money_active(env):
        raise LoginError(
            "TRADING_LIVE_MONEY_ACTIVE is set; this login helper supports paper trading only"
        )
    directory = config_dir or config_directory()
    api_key, api_secret = load_credentials(directory)
    kite = _kite_client_type()(api_key=api_key)
    print(f"Log in at: {kite.login_url()}", file=stream)
    if request_token is None:
        ask = prompt or input
        request_token = ask("Paste the full redirect URL (or the request_token): ")
    token = parse_request_token(request_token)
    try:
        session = kite.generate_session(token, api_secret=api_secret)
        kite.set_access_token(session["access_token"])
        profile = kite.profile()
    except Exception:  # noqa: BLE001 - SDK errors can include the new access token.
        raise LoginError(
            "Kite rejected the login; request tokens are single-use, start again"
        ) from None
    issued_at = (now or _utc_now)()
    try:
        record = SessionRecord.from_kite_session(session, issued_at=issued_at)
    except ValueError as error:
        raise LoginError(f"Kite session response is incomplete: {error}") from error
    profile_user = profile.get("user_id") if isinstance(profile, Mapping) else None
    if profile_user != record.user_id:
        raise LoginError(
            "Kite profile user_id does not match the new session's user_id; refusing to save it"
        )
    path = write_session(directory / SESSION_FILE_NAME, record)
    expiry = next_cutoff(issued_at)
    print(f"Session saved for user_id={record.user_id}: {path}", file=stream)
    print(
        f"Expected expiry: {expiry.isoformat()} (Kite invalidates tokens daily at 06:00 IST); "
        "run pramana zerodha-login again after that.",
        file=stream,
    )
    print("Restart scripts/india_paper_runtime.py so it picks up the new token.", file=stream)
    return record


def run_login(request_token: str | None) -> int:
    """CLI entry shared by ``pramana zerodha-login`` and ``scripts/zerodha_login.py``."""
    try:
        login(request_token=request_token)
    except LoginError as error:
        print(f"zerodha-login: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"zerodha-login: could not read or write the session file: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("zerodha-login: aborted; no session written", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zerodha-login",
        description="Renew the Zerodha Kite access token for the paper pilot (daily, before "
        "09:15 IST). Prints the login URL, exchanges the request token and writes "
        "~/.config/pramana/zerodha-session.json.",
    )
    parser.add_argument(
        "--request-token",
        help="Kite request_token or the full redirect URL; prompted for when omitted",
    )
    args = parser.parse_args(argv)
    return run_login(args.request_token)
