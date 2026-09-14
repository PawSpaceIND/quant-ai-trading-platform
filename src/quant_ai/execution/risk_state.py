from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any


class RiskStateStore:
    """Small state interface used by the risk circuit breakers."""

    def record_equity(self, tenant_id: str, valuation_date: date, equity: Decimal) -> Decimal:
        raise NotImplementedError

    def kill_switch_state(self, tenant_id: str) -> tuple[bool, str | None]:
        raise NotImplementedError

    def set_kill_switch(self, tenant_id: str, engaged: bool, reason: str | None) -> None:
        raise NotImplementedError


class InMemoryRiskStateStore(RiskStateStore):
    def __init__(self, starting_capital: Decimal = Decimal(100000)) -> None:
        self.starting_capital = starting_capital
        self._daily: dict[tuple[str, date], tuple[Decimal, Decimal]] = {}
        self._controls: dict[str, tuple[bool, str | None]] = {}
        self._lock = RLock()

    def record_equity(self, tenant_id: str, valuation_date: date, equity: Decimal) -> Decimal:
        key = (tenant_id, valuation_date)
        with self._lock:
            current = self._daily.get(key)
            if current is not None:
                opening, _ = current
                self._daily[key] = (opening, equity)
                return opening
            prior = [
                (day, values[1])
                for (tenant, day), values in self._daily.items()
                if tenant == tenant_id and day < valuation_date
            ]
            opening = max(prior, key=lambda item: item[0])[1] if prior else self.starting_capital
            self._daily[key] = (opening, equity)
            return opening

    def kill_switch_state(self, tenant_id: str) -> tuple[bool, str | None]:
        with self._lock:
            return self._controls.get(tenant_id, (False, None))

    def set_kill_switch(self, tenant_id: str, engaged: bool, reason: str | None) -> None:
        with self._lock:
            self._controls[tenant_id] = (engaged, reason)


class SQLiteRiskStateStore(RiskStateStore):
    """Durable circuit-breaker state colocated with the paper ledger."""

    def __init__(
        self,
        database: str | Path,
        *,
        starting_capital: Decimal = Decimal(100000),
    ) -> None:
        self.starting_capital = starting_capital
        self._lock = RLock()
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=2000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS risk_daily_equity (
                    tenant_id TEXT NOT NULL,
                    valuation_date TEXT NOT NULL,
                    opening_equity TEXT NOT NULL,
                    last_equity TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, valuation_date)
                );
                CREATE TABLE IF NOT EXISTS risk_control_state (
                    tenant_id TEXT PRIMARY KEY,
                    kill_switch_engaged INTEGER NOT NULL,
                    kill_switch_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_equity(self, tenant_id: str, valuation_date: date, equity: Decimal) -> Decimal:
        day = valuation_date.isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT opening_equity FROM risk_daily_equity
                WHERE tenant_id = ? AND valuation_date = ?""",
                (tenant_id, day),
            ).fetchone()
            if row is not None:
                self._connection.execute(
                    """UPDATE risk_daily_equity SET last_equity = ?, updated_at = ?
                    WHERE tenant_id = ? AND valuation_date = ?""",
                    (str(equity), now, tenant_id, day),
                )
                return Decimal(row["opening_equity"])
            prior = self._connection.execute(
                """SELECT last_equity FROM risk_daily_equity
                WHERE tenant_id = ? AND valuation_date < ?
                ORDER BY valuation_date DESC LIMIT 1""",
                (tenant_id, day),
            ).fetchone()
            opening = Decimal(prior["last_equity"]) if prior is not None else self.starting_capital
            self._connection.execute(
                """INSERT INTO risk_daily_equity
                (tenant_id, valuation_date, opening_equity, last_equity, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (tenant_id, day, str(opening), str(equity), now),
            )
            return opening

    def kill_switch_state(self, tenant_id: str) -> tuple[bool, str | None]:
        with self._lock:
            row = self._connection.execute(
                """SELECT kill_switch_engaged, kill_switch_reason
                FROM risk_control_state WHERE tenant_id = ?""",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return False, None
        return bool(row["kill_switch_engaged"]), row["kill_switch_reason"]

    def set_kill_switch(self, tenant_id: str, engaged: bool, reason: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO risk_control_state
                (tenant_id, kill_switch_engaged, kill_switch_reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    kill_switch_engaged = excluded.kill_switch_engaged,
                    kill_switch_reason = excluded.kill_switch_reason,
                    updated_at = excluded.updated_at""",
                (tenant_id, int(engaged), reason, now),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()


def risk_state_for_broker(
    broker: Any,
    *,
    starting_capital: Decimal,
) -> RiskStateStore:
    """Use durable state whenever the broker has a file-backed SQLite connection."""
    connection = getattr(broker, "_connection", None)
    if connection is not None:
        try:
            rows = connection.execute("PRAGMA database_list").fetchall()
            for row in rows:
                database = row["file"] if hasattr(row, "keys") else row[2]
                if database:
                    return SQLiteRiskStateStore(
                        database,
                        starting_capital=starting_capital,
                    )
        except (sqlite3.Error, TypeError, IndexError, KeyError):
            pass
    return InMemoryRiskStateStore(starting_capital)
