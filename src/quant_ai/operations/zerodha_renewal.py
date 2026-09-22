"""Paper-pilot token preflight, private atomic publication, and independent pre-open watch.

Only Kite's read-only profile endpoint is called. Interactive login remains mandatory.
Provider errors are deliberately not rendered: SDK exceptions can contain credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
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
_ASSIGNMENT = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*[=:](.*)$")
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


# Kite SDK exception names that mean the provider answered and refused these credentials.
# Matched by name so this module still never imports the SDK, and so a stand-in client in
# a test can raise either class without one installed.
PROVIDER_REFUSALS = frozenset({"TokenException", "PermissionException", "InputException"})
# Anything else - a socket, TLS, timeout or transport error - means the question was never
# answered. A container's first second frequently has no working route, and the boot check
# exited the daemon on that, dispatching the same CRITICAL alert as a stolen token. Retry
# is bounded and short: it must not delay a genuine refusal, and it must not turn a real
# outage into a slow boot.
PROFILE_ATTEMPTS = 3
PROFILE_BACKOFF_SECONDS = (1.0, 2.0)


def _profile(kite: Any, *, attempts: int, sleep: Callable[[float], None]) -> Any:
    """Read the profile, retrying only what was never answered.

    A refusal fails on the first attempt: repeating a rejected token cannot change the
    answer and would only delay the alert. The exception is classified by its type name
    and then dropped - never stored, re-raised or rendered - because the SDK puts request
    context, and sometimes the credential itself, in the message.
    """
    total = max(1, attempts)
    for attempt in range(1, total + 1):
        refused = False
        try:
            return kite.profile()
        except Exception as error:  # noqa: BLE001 - classified by type name, never rendered.
            refused = type(error).__name__ in PROVIDER_REFUSALS
            del error
        # Raised after the handler has exited, not inside it. `from None` only suppresses
        # the chain when a traceback is *formatted*; the exception object still holds the
        # provider's exception on __context__, and anything that walks that chain - a log
        # handler, an error reporter - would surface a message that is sometimes the
        # credential. Outside the handler there is no active exception to attach.
        if refused:
            raise RenewalError("zerodha_profile_rejected")
        if attempt >= total:
            raise RenewalError("zerodha_profile_unavailable")
        sleep(PROFILE_BACKOFF_SECONDS[min(attempt - 1, len(PROFILE_BACKOFF_SECONDS) - 1)])
    raise RenewalError("zerodha_profile_unavailable")


def validate_token(
    env: Mapping[str, str], *, now: datetime | None = None,
    client_factory: Callable[[str], Any] | None = None,
    attempts: int = 1, sleep: Callable[[float], None] = time.sleep,
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
    # Suppressed rather than caught-and-re-raised: constructing the client or setting the
    # token can fail with an SDK error that carries credentials, and suppressing it leaves
    # nothing attached to the refusal below. Logging it is exactly what must not happen.
    prepared: Any = None
    with contextlib.suppress(Exception):
        kite = (client_factory or _kite_client)(key)
        kite.set_access_token(token)
        prepared = kite  # Only once both steps succeeded, so a half-built client is never used.
    if prepared is None:
        raise RenewalError("zerodha_profile_unavailable")
    profile = _profile(prepared, attempts=attempts, sleep=sleep)
    user = profile.get("user_id") if isinstance(profile, Mapping) else None
    if not isinstance(user, str) or not user.strip():
        raise RenewalError("zerodha_profile_identity_missing")
    if expected_user and user != expected_user:
        raise RenewalError("zerodha_profile_identity_mismatch")
    return VerifiedToken(key, token, user)


LOGGER = logging.getLogger("quant_ai.zerodha_renewal")
# A container that finds the token expired at boot exits, and Docker restarts it a minute
# later; the boot check used to dispatch the same CRITICAL alert on every retry, 183 times
# between 05:31 and 08:39 IST on 21 September 2026. One alert says it. The next says the
# same thing only after this interval, unless the reason changes. The pre-open reminders
# the token watch sends every five minutes from 08:30 are deliberate and do not use this.
SESSION_ALERT_REPEAT = timedelta(minutes=30)
SESSION_ALERT_STATE_NAME = "zerodha-session-alert.json"


def default_alert_state() -> Path:
    """The dedupe record, beside the durable alert log so every container shares it."""
    return paths.alert_log("PRAMANA_PAPER_DB").parent / SESSION_ALERT_STATE_NAME


def _alert_due(state: Path | None, reason: str, at: datetime) -> bool:
    """Dispatch when there is no record, the reason changed, or the interval has passed.

    A missing, unreadable or malformed record counts as due: a fault here can only ever
    alert more, never less.
    """
    if state is None:
        return True
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(payload["alerted_at"]))
    except (OSError, ValueError, TypeError, KeyError):
        return True
    if last.tzinfo is None or last.utcoffset() is None:
        return True
    return payload.get("reason") != reason or at < last or at - last >= SESSION_ALERT_REPEAT


def _record_alert(state: Path | None, reason: str, phase: str, at: datetime) -> None:
    if state is None:
        return
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        scratch = state.with_name(state.name + ".tmp")
        scratch.write_text(
            json.dumps({"reason": reason, "phase": phase, "alerted_at": at.isoformat()}),
            encoding="utf-8",
        )
        os.replace(scratch, state)
    except OSError:
        LOGGER.warning("zerodha_session_alert_state_unwritable path=%s", state)


def notifications() -> TradingNotificationDispatcher:
    sinks = [ConsoleLogSink(), JsonlFileSink(paths.alert_log("PRAMANA_PAPER_DB"))]
    bot = os.environ.get("PRAMANA_TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("PRAMANA_TELEGRAM_CHAT_ID", "").strip()
    if bot and chat:
        from quant_ai.execution.notifications import TelegramNotificationAdapter
        sinks.append(TelegramNotificationAdapter(bot, chat))
    return TradingNotificationDispatcher(tuple(sinks))


# What the operator should actually do, per reason. A session the provider refused needs
# a new login; a provider that never answered needs nothing from the login flow at all,
# and telling someone to re-authenticate sends them to re-enter working credentials.
_REMEDY = {
    "zerodha_profile_unavailable":
        "Zerodha could not be reached to verify the session, so paper startup/new data "
        "cannot be trusted. The stored token may be perfectly good; check connectivity "
        "from the host before assuming it is not.",
}
_DEFAULT_REMEDY = (
    "Zerodha session unavailable. Paper startup/new data cannot be trusted. "
    "Complete interactive login with scripts/renew_pilot_token.py before the open."
)


def check_runtime_token(
    *, env: Mapping[str, str] | None = None, now: datetime | None = None,
    dispatcher: TradingNotificationDispatcher | None = None, phase: str = "boot",
    client_factory: Callable[[str], Any] | None = None,
    alert_state: Path | None = None,
    attempts: int = PROFILE_ATTEMPTS, sleep: Callable[[float], None] = time.sleep,
) -> VerifiedToken:
    """Validate the runtime token; on refusal alert, then re-raise.

    ``alert_state`` names the dedupe record: with it, a refusal for the same reason within
    ``SESSION_ALERT_REPEAT`` of the last alert is logged but not dispatched again. Without
    it every refusal alerts, which is what the pre-open reminders want.
    """
    source = os.environ if env is None else env
    if phase not in {"boot", "preopen", "watch_start"}:
        raise RenewalError("zerodha_check_phase_invalid")
    try:
        return validate_token(source, now=now, client_factory=client_factory,
                              attempts=attempts, sleep=sleep)
    except RenewalError as error:
        at = now or _now()
        reason = str(error)
        if _alert_due(alert_state, reason, at):
            (dispatcher or notifications()).dispatch(
                TradingAlertCode.ZERODHA_SESSION_INVALID,
                _REMEDY.get(reason, _DEFAULT_REMEDY),
                tenant_id=source.get("PRAMANA_TENANT_ID", "ghost"),
                priority=AlertPriority.CRITICAL,
                metadata={"phase": phase, "reason": reason},
            )
            _record_alert(alert_state, reason, phase, at)
        else:
            LOGGER.warning(
                "zerodha_session_alert_suppressed phase=%s reason=%s repeat_after_s=%d",
                phase, reason, int(SESSION_ALERT_REPEAT.total_seconds()),
            )
        raise


def _quoted_end(value: str) -> int:
    """Locate a same-line closing quote without interpreting the enclosed value."""
    index = 1
    while index < len(value):
        if value[index] == "\\":
            index += 2
        elif value[index] == value[0]:
            return index
        else:
            index += 1
    raise RenewalError("zerodha_env_scalar_invalid_multiline_unsupported")


def _env_lines(text: str) -> list[tuple[str, re.Match[str] | None]]:
    """Keep original bytes/lines, but refuse syntax whose field boundaries are ambiguous.

    Both documented Compose delimiters are recognized. Multiline values are valid
    Compose syntax, but deliberately unsupported by this atomic scalar updater.
    Refuse the whole file before login/publication rather than edit inside a value.
    """
    if any((ord(char) < 32 and char not in "\t\r\n")
           or 0x7F <= ord(char) <= 0x9F or char in "\u2028\u2029" for char in text):
        raise RenewalError("zerodha_env_control_unsupported")
    result: list[tuple[str, re.Match[str] | None]] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip(" \t\r\n")
        match = None
        if stripped and not stripped.startswith("#"):
            match = _ASSIGNMENT.fullmatch(stripped)
            if match is None:
                raise RenewalError("zerodha_env_syntax_unsupported")
            value = match[2].lstrip(" \t")
            if value.startswith(("'", '"')):
                end = _quoted_end(value)
                suffix = value[end + 1:].lstrip(" \t")
                if suffix and not suffix.startswith("#"):
                    raise RenewalError("zerodha_env_scalar_invalid")
        result.append((line, match))
    return result


def _scalar(value: str) -> str:
    """Parse only literal, one-line managed values; never apply shell transformations."""
    stripped = value.strip(" \t")
    if stripped.startswith(("'", '"')):
        scalar = stripped[1:_quoted_end(stripped)]
    else:
        # Compose treats # as a comment only after whitespace, unlike shlex.
        scalar = re.split(r"[ \t]+#", value, maxsplit=1)[0].strip(" \t")
    if any(char.isspace() or char in "\\$'\"" for char in scalar):
        raise RenewalError("zerodha_env_scalar_invalid")
    return scalar


def _fields(text: str) -> dict[str, str]:
    """Read only scalar credentials/control fields from validated top-level lines."""
    wanted = {*MANAGED, "ZERODHA_API_KEY", "TRADING_LIVE_MONEY_ACTIVE"}
    found: dict[str, str] = {}
    for _, match in _env_lines(text):
        if match is None or match[1] not in wanted:
            continue
        name = match[1]
        if name in found:
            raise RenewalError("zerodha_env_duplicate_managed_field")
        found[name] = _scalar(match[2])
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
        for line, match in _env_lines(text):
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
            check_runtime_token(alert_state=default_alert_state())
            print("Zerodha profile check passed; no credential values displayed.")
        else:
            previous = None
            try:
                check_runtime_token(phase="watch_start", alert_state=default_alert_state())
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
