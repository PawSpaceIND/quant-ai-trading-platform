"""Durable daily cap on Anthropic consensus spend for the pilot.

The ghost daemon asks Claude for a consensus once per instrument per cadence tick, so
nothing else bounds spend on a runaway watchlist or a tight cadence. This module keeps
a tiny SQLite ledger of calls and tokens per UTC day and refuses new calls once either
limit is reached. It fails closed: a broken budget database refuses the call rather than
letting spend become unbounded.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

LOGGER = logging.getLogger("quant_ai.ai_budget")

CALL_LIMIT_ENV = "PRAMANA_AI_DAILY_CALL_LIMIT"
TOKEN_LIMIT_ENV = "PRAMANA_AI_DAILY_TOKEN_LIMIT"
DATABASE_ENV = "PRAMANA_AI_BUDGET_DB"
DEFAULT_DAILY_CALL_LIMIT = 500
DEFAULT_DAILY_TOKEN_LIMIT = 2_000_000
DEFAULT_DATABASE_NAME = "ai-budget.sqlite"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SqliteAIBudget:
    """Per-day, per-scope call and token counters with an atomic check-and-increment."""

    def __init__(
        self,
        database: str | Path,
        *,
        daily_call_limit: int,
        daily_token_limit: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if daily_call_limit <= 0:
            raise ValueError("daily_call_limit must be positive")
        if daily_token_limit <= 0:
            raise ValueError("daily_token_limit must be positive")
        self.database = str(database)
        self.daily_call_limit = int(daily_call_limit)
        self.daily_token_limit = int(daily_token_limit)
        self._clock = clock
        self._lock = RLock()
        # Same conventions as the paper ledger: WAL so the daemon's cadence and protection
        # threads (and any future reader) share the file without blocking each other, a
        # busy timeout so a contended write waits instead of failing, and one connection
        # guarded by an RLock so every thread serialises through it.
        self._connection = sqlite3.connect(self.database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=2000")
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_budget (
                    day TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, scope)
                )
                """
            )

    def current_day(self) -> str:
        """UTC calendar day that counters are keyed by. Pure clock read, no database."""
        now = self._clock()
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc)
        return now.date().isoformat()

    def reserve(self, scope: str) -> bool:
        """Admit one call for ``scope`` today, or refuse when either limit is reached.

        The conditional UPDATE is a single statement, so the check and the increment are
        one atomic step even when several threads or processes share the file. Database
        errors refuse the call: an unknown budget is treated as an exhausted one.
        """
        day = self.current_day()
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO ai_budget (day, scope) VALUES (?, ?)", (day, scope)
                )
                cursor = self._connection.execute(
                    """
                    UPDATE ai_budget SET calls = calls + 1
                    WHERE day = ? AND scope = ? AND calls < ?
                      AND input_tokens + output_tokens < ?
                    """,
                    (day, scope, self.daily_call_limit, self.daily_token_limit),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as error:
            LOGGER.error(
                "ai_budget_unavailable scope=%s day=%s detail=%s; refusing call",
                scope, day, f"{type(error).__name__}: {error}",
            )
            return False

    def record(self, scope: str, usage: Mapping[str, Any] | None) -> None:
        """Add the tokens a completed call consumed. Missing or malformed counts add zero."""
        input_tokens = _token_count(usage, "input_tokens")
        output_tokens = _token_count(usage, "output_tokens")
        if not input_tokens and not output_tokens:
            return
        day = self.current_day()
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO ai_budget (day, scope) VALUES (?, ?)", (day, scope)
                )
                self._connection.execute(
                    """
                    UPDATE ai_budget
                    SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?
                    WHERE day = ? AND scope = ?
                    """,
                    (input_tokens, output_tokens, day, scope),
                )
        except sqlite3.Error as error:
            # The call already happened; the next reserve() fails closed if the file is
            # still broken, so unrecorded tokens cannot turn into unbounded spend.
            LOGGER.error(
                "ai_budget_record_failed scope=%s day=%s detail=%s",
                scope, day, f"{type(error).__name__}: {error}",
            )

    def status(self, scope: str) -> dict[str, Any]:
        """Today's counters, limits and headroom for ``scope``."""
        day = self.current_day()
        with self._lock:
            row = self._connection.execute(
                "SELECT calls, input_tokens, output_tokens FROM ai_budget WHERE day=? AND scope=?",
                (day, scope),
            ).fetchone()
        calls = int(row["calls"]) if row else 0
        input_tokens = int(row["input_tokens"]) if row else 0
        output_tokens = int(row["output_tokens"]) if row else 0
        tokens = input_tokens + output_tokens
        return {
            "day": day,
            "scope": scope,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tokens": tokens,
            "daily_call_limit": self.daily_call_limit,
            "daily_token_limit": self.daily_token_limit,
            "remaining_calls": max(0, self.daily_call_limit - calls),
            "remaining_tokens": max(0, self.daily_token_limit - tokens),
            "exhausted": calls >= self.daily_call_limit or tokens >= self.daily_token_limit,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _token_count(usage: Mapping[str, Any] | None, key: str) -> int:
    if not isinstance(usage, Mapping):
        return 0
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _env_limit(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error


def budget_from_env(
    default_directory: str | Path, environ: Mapping[str, str] | None = None
) -> SqliteAIBudget | None:
    """Build the pilot budget from ``PRAMANA_AI_*`` variables, or ``None`` when disabled.

    A call or token limit of zero or less disables the budget explicitly. The database
    defaults to ``ai-budget.sqlite`` inside ``default_directory`` (the ledger's folder).
    """
    environ = os.environ if environ is None else environ
    call_limit = _env_limit(environ, CALL_LIMIT_ENV, DEFAULT_DAILY_CALL_LIMIT)
    token_limit = _env_limit(environ, TOKEN_LIMIT_ENV, DEFAULT_DAILY_TOKEN_LIMIT)
    if call_limit <= 0 or token_limit <= 0:
        LOGGER.warning(
            "ai_budget_disabled call_limit=%s token_limit=%s; Anthropic spend is unbounded",
            call_limit, token_limit,
        )
        return None
    configured = environ.get(DATABASE_ENV, "").strip()
    database = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(default_directory) / DEFAULT_DATABASE_NAME
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    return SqliteAIBudget(
        database, daily_call_limit=call_limit, daily_token_limit=token_limit
    )
