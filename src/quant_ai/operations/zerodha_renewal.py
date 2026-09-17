"""Paper-pilot token preflight, private atomic publication, and independent pre-open watch.

Only Kite's read-only profile endpoint is called. Interactive login remains mandatory.
Provider errors are deliberately not rendered: SDK exceptions can contain credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as wall_time
from pathlib import Path
from typing import Any

from quant_ai.config import paths
from quant_ai.notifications.trading import (
    AlertPriority,
    ConsoleLogSink,
    JsonlFileSink,
    TradingAlertCode,
    TradingNotificationDispatcher,
)
from quant_ai.operations.zerodha_session import INDIA_TZ, SessionRecord, is_expired, read_session

ISSUED_AT = "PRAMANA_ZERODHA_TOKEN_ISSUED_AT"
USER_ID = "PRAMANA_ZERODHA_USER_ID"
MANAGED = ("ZERODHA_ACCESS_TOKEN", ISSUED_AT, USER_ID)
_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._-]+")


class RenewalError(RuntimeError):
    """A fixed, credential-free refusal code, safe for console and durable alerts."""


@dataclass(frozen=True)
class VerifiedToken:
    api_key: str = field(repr=False)
    access_token: str = field(repr=False)
    user_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise RenewalError("zerodha_clock_must_be_aware")


def _paper_only(env: Mapping[str, str]) -> None:
    if env.get("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() not in {
        "false", "0", "no", "off", "",
    }:
        raise RenewalError("zerodha_renewal_paper_only")


def _kite_client(api_key: str) -> Any:
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=api_key, timeout=10)


def validate_token(
    env: Mapping[str, str], *, now: datetime | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> VerifiedToken:
    """Refuse missing, expired, future-dated, rejected or wrong-account credentials.

    Legacy environment tokens without issuance metadata are still checked against Kite.
    Once renewed with this module, local expiry also refuses before any network call.
    """
    _paper_only(env)
    at = now or _now()
    _aware(at)
    key = env.get("ZERODHA_API_KEY", "").strip()
    token = env.get("ZERODHA_ACCESS_TOKEN", "").strip()
    if not key or not token:
        raise RenewalError("zerodha_credentials_missing")
    expected_user = env.get(USER_ID, "").strip()
    issued = env.get(ISSUED_AT, "").strip()
    if issued:
        try:
            record = SessionRecord(
                expected_user or "legacy", token,
                datetime.fromisoformat(issued.replace("Z", "+00:00")),
            )
        except (TypeError, ValueError):
            raise RenewalError("zerodha_issuance_invalid") from None
        if record.issued_at > at:
            raise RenewalError("zerodha_issuance_in_future")
        if is_expired(record, at):
            raise RenewalError("zerodha_session_expired")
    try:
        kite = (client_factory or _kite_client)(key)
        kite.set_access_token(token)
        profile = kite.profile()
    except Exception:  # noqa: BLE001 - SDK errors can carry credentials; never render them.
        raise RenewalError("zerodha_profile_rejected_or_unavailable") from None
    user = profile.get("user_id") if isinstance(profile, Mapping) else None
    if not isinstance(user, str) or not user.strip():
        raise RenewalError("zerodha_profile_identity_missing")
    if expected_user and user != expected_user:
        raise RenewalError("zerodha_profile_identity_mismatch")
    return VerifiedToken(key, token, user)


def notifications() -> TradingNotificationDispatcher:
    sinks = [ConsoleLogSink(), JsonlFileSink(paths.alert_log("PRAMANA_PAPER_DB"))]
    bot = os.environ.get("PRAMANA_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("PRAMANA_TELEGRAM_CHAT_ID", "").strip()
    if bot and chat:
        from quant_ai.execution.notifications import TelegramNotificationAdapter
        sinks.append(TelegramNotificationAdapter(bot, chat))
    return TradingNotificationDispatcher(tuple(sinks))


def check_runtime_token(
    *, env: Mapping[str, str] | None = None, now: datetime | None = None,
    dispatcher: TradingNotificationDispatcher | None = None, phase: str = "boot",
    client_factory: Callable[[str], Any] | None = None,
) -> VerifiedToken:
    source = os.environ if env is None else env
    if phase not in {"boot", "preopen", "watch_start"}:
        raise RenewalError("zerodha_check_phase_invalid")
    try:
        return validate_token(source, now=now, client_factory=client_factory)
    except RenewalError as error:
        (dispatcher or notifications()).dispatch(
            TradingAlertCode.ZERODHA_SESSION_INVALID,
            "Zerodha session unavailable. Paper startup/new data cannot be trusted. "
            "Complete interactive login with scripts/renew_pilot_token.py before the open.",
            tenant_id=source.get("PRAMANA_TENANT_ID", "ghost"),
            priority=AlertPriority.CRITICAL,
            metadata={"phase": phase, "reason": str(error)},
        )
        raise


def _fields(text: str) -> dict[str, str]:
    """Read only scalar credentials/control fields; never evaluate shell or dotenv code."""
    wanted = {*MANAGED, "ZERODHA_API_KEY", "TRADING_LIVE_MONEY_ACTIVE"}
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.fullmatch(line.strip())
        if not match or match[1] not in wanted:
            continue
        name = match[1]
        if name in found:
            raise RenewalError("zerodha_env_duplicate_managed_field")
        try:
            values = shlex.split(match[2], comments=True)
        except ValueError:
            raise RenewalError("zerodha_env_scalar_invalid") from None
        if len(values) > 1 or (values and "$" in values[0]):
            raise RenewalError("zerodha_env_scalar_invalid")
        found[name] = values[0] if values else ""
    return found


def _regular_private(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        raise RenewalError("zerodha_env_missing_or_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RenewalError("zerodha_env_must_be_regular_unlinked_file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RenewalError("zerodha_env_must_be_private")
    if info.st_uid != os.geteuid():
        raise RenewalError("zerodha_env_owner_mismatch")
    return info


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _write_candidate(handle: Any, payload: bytes) -> None:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def publish_to_env(
    path: Path, record: SessionRecord, *, now: datetime | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> None:
    """Publish one verified token + issuance/account metadata, without echoing any value.

    A cooperating lock, private same-directory temporary, complete-byte verification,
    concurrent-edit check and atomic rename preserve the old file until commit. No shell
    sourcing, no direct truncation, no persistent secret backup, no container operation.
    """
    target = Path(path).absolute()
    at = now or _now()
    _aware(at)
    if record.issued_at > at or is_expired(record, at):
        raise RenewalError("zerodha_publish_session_not_current")
    if not _SAFE_TOKEN.fullmatch(record.access_token) or not _SAFE_TOKEN.fullmatch(record.user_id):
        raise RenewalError("zerodha_publish_identity_invalid")
    _regular_private(target)
    lock_path = target.with_name(target.name + ".zerodha.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    temporary: str | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        before = _regular_private(target)
        original = target.read_bytes()
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            raise RenewalError("zerodha_env_encoding_invalid") from None
        existing = _fields(text)
        _paper_only(existing)
        if existing.get(USER_ID) and existing[USER_ID] != record.user_id:
            raise RenewalError("zerodha_publish_account_change_refused")
        updates = {
            "ZERODHA_ACCESS_TOKEN": record.access_token,
            ISSUED_AT: record.issued_at.astimezone(timezone.utc).isoformat(),
            USER_ID: record.user_id,
        }
        validate_token({**existing, **updates}, now=at, client_factory=client_factory)
        seen: set[str] = set()
        lines: list[str] = []
        for line in text.splitlines(keepends=True):
            match = _ASSIGNMENT.fullmatch(line.strip())
            if match and match[1] in updates:
                name = match[1]
                lines.append(f"{name}={updates[name]}\n")
                seen.add(name)
            else:
                lines.append(line)
        result = "".join(lines)
        if result and not result.endswith("\n"):
            result += "\n"
        result += "".join(f"{name}={updates[name]}\n" for name in MANAGED if name not in seen)
        payload = result.encode("utf-8")
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".zerodha-env-", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            os.fchown(handle.fileno(), before.st_uid, before.st_gid)
            os.fchmod(handle.fileno(), 0o600)
            _write_candidate(handle, payload)
        if Path(temporary).read_bytes() != payload:
            raise RenewalError("zerodha_env_partial_write")
        if (_identity(_regular_private(target)) != _identity(before)
                or target.read_bytes() != original):
            raise RenewalError("zerodha_env_changed_concurrently")
        os.replace(temporary, target)
        temporary = None
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        os.close(lock_fd)


def preopen_due(now: datetime, previous: datetime | None) -> bool:
    """Every five minutes from 08:30 through 09:14 IST, independent of daemon health.

    Deliberately checks every calendar day, including holidays; no trading-day inference.
    """
    _aware(now)
    local = now.astimezone(INDIA_TZ)
    in_window = wall_time(8, 30) <= local.time() < wall_time(9, 15)
    return in_window and (previous is None or now - previous >= timedelta(minutes=5))


def watch_cycle(
    now: datetime, previous: datetime | None, *,
    check: Callable[..., VerifiedToken] | None = None,
) -> datetime | None:
    if not preopen_due(now, previous):
        return previous
    try:
        (check or check_runtime_token)(now=now, phase="preopen")
    except RenewalError:
        pass  # already dispatched; the independent watch must survive a bad session
    return now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "watch", "publish"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--session-file", type=Path,
                        default=Path.home() / ".config/pramana/zerodha-session.json")
    args = parser.parse_args(argv)
    try:
        if args.action == "publish":
            publish_to_env(args.env_file, read_session(args.session_file))
            print("Verified session published atomically. Recreate token-consuming services.")
        elif args.action == "check":
            check_runtime_token()
            print("Zerodha profile check passed; no credential values displayed.")
        else:
            previous = None
            try:
                check_runtime_token(phase="watch_start")
            except RenewalError:
                pass
            heartbeat = paths.alert_log("PRAMANA_PAPER_DB").parent / "zerodha-watch.json"
            while True:
                at = _now()
                previous = watch_cycle(at, previous)
                heartbeat.parent.mkdir(parents=True, exist_ok=True)
                heartbeat.write_text(json.dumps({"checkedAt": at.isoformat()}))
                time.sleep(60)
    except (RenewalError, OSError, ValueError):
        print("zerodha-renewal: refused; check private inputs and the durable alert log.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
