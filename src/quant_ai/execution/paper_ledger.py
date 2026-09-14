from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from quant_ai.brokers.adapter import BrokerAdapter, BrokerMargin, BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.friction import FrictionContext, FrictionResult, MarketFrictionModel


class PaperBrokerDatabaseLockedError(RuntimeError):
    """Paper broker exhausted bounded retries while SQLite remained locked."""


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@dataclass(frozen=True)
class PaperCostEntry:
    order_id: str
    tenant_id: str
    code: str
    amount: Decimal
    cash_debit: bool
    created_at: datetime


@dataclass(frozen=True)
class PaperLedgerEntry:
    order_id: str
    tenant_id: str
    symbol: str
    market: Market
    asset_class: AssetClass
    side: Side
    quantity: int
    fill_price: Decimal
    notional: Decimal
    status: str
    created_at: datetime
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None


class PaperBrokerService(BrokerAdapter):
    """SQLite-backed local paper broker. No network clients are used or accepted."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        starting_capital: Decimal = Decimal(100000),
        slippage_bps: Decimal | None = None,
        friction_model: MarketFrictionModel | None = None,
        lock_retries: int = 3,
        lock_backoff_seconds: float = 0.05,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if slippage_bps is not None and slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if slippage_bps is not None and friction_model is not None:
            raise ValueError("choose slippage_bps compatibility mode or friction_model")
        if lock_retries < 0:
            raise ValueError("lock_retries cannot be negative")
        if lock_backoff_seconds < 0:
            raise ValueError("lock_backoff_seconds cannot be negative")
        self.starting_capital = starting_capital
        self.slippage_bps = slippage_bps or Decimal(0)
        self.friction_model = friction_model or (
            MarketFrictionModel.compatibility(slippage_bps)
            if slippage_bps is not None
            else MarketFrictionModel()
        )
        self._friction_context: FrictionContext | None = None
        self._execution_time: datetime | None = None
        self.lock_retries = lock_retries
        self.lock_backoff_seconds = lock_backoff_seconds
        self._sleep = sleep_fn
        self._jitter = jitter_fn
        self._lock = RLock()
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # WAL lets the read-only UI and the daemon share the ledger without blocking each
        # other; the busy timeout keeps a contended write from burning the whole cadence tick.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=2000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    tenant_id TEXT PRIMARY KEY,
                    starting_capital TEXT NOT NULL,
                    cash_balance TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    peak_equity TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_price TEXT NOT NULL,
                    stop_price TEXT,
                    take_profit_price TEXT,
                    PRIMARY KEY (tenant_id, symbol, market, asset_class)
                );
                CREATE TABLE IF NOT EXISTS paper_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    fill_price TEXT NOT NULL,
                    notional TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    stop_price TEXT,
                    take_profit_price TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_cost_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    cash_debit INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_protection_evidence (
                    order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_decision_evidence (
                    order_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_idempotency (
                    key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_exit_cooldowns (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    until TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, symbol, market, asset_class)
                );
                """
            )
            self._migrate_columns()

    _EXPECTED_COLUMNS = (
        ("paper_accounts", "peak_equity", "TEXT"),
        ("paper_positions", "stop_price", "TEXT"),
        ("paper_positions", "take_profit_price", "TEXT"),
        ("paper_ledger", "stop_price", "TEXT"),
        ("paper_ledger", "take_profit_price", "TEXT"),
    )

    def _migrate_columns(self) -> None:
        """Forward-migrate ledgers created before protective levels were persisted."""
        for table, column, column_type in self._EXPECTED_COLUMNS:
            existing = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    def _ensure_account(self, tenant_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_accounts
            (tenant_id, starting_capital, cash_balance, updated_at) VALUES (?, ?, ?, ?)""",
            (tenant_id, str(self.starting_capital), str(self.starting_capital), now),
        )

    def set_friction_context(
        self, context: FrictionContext | None, *, execution_time: datetime | None = None
    ) -> None:
        self._friction_context = context
        self._execution_time = execution_time

    def _context_for(self, order: OrderIntent) -> FrictionContext:
        if self._friction_context is not None:
            return self._friction_context
        return FrictionContext(
            atr=order.reference_price * Decimal("0.01"),
            average_daily_volume=max(Decimal(1000000), Decimal(order.quantity * 10000)),
            liquidity_score=Decimal(1),
            delivery=True,
        )

    def configure_pilot(self, instruments, tenant_id: str) -> None:
        from quant_ai.governance.pilot import validate_pilot_instruments
        validate_pilot_instruments(tuple(instruments))
        symbols = {item.symbol for item in instruments}
        with self._lock, self._connection:
            self._connection.execute("""CREATE TABLE IF NOT EXISTS pilot_scope (
                tenant_id TEXT PRIMARY KEY, currency TEXT NOT NULL, market TEXT NOT NULL,
                symbols TEXT NOT NULL)""")
            positions = self.get_positions(tenant_id)
            if any(p.market != Market.INDIA or p.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
                   or p.symbol not in symbols for p in positions):
                raise ValueError("pilot_existing_positions_out_of_scope")
            previous = self._connection.execute(
                "SELECT currency, market FROM pilot_scope WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            if previous and (previous["currency"], previous["market"]) != ("INR", "INDIA"):
                raise ValueError("pilot_account_currency_mismatch")
            # Legacy mixed-market accounts cannot be relabeled as INR.
            if any(e.market != Market.INDIA for e in self.ledger_entries(tenant_id)):
                raise ValueError("pilot_legacy_currency_ambiguous")
            self._connection.execute("INSERT OR REPLACE INTO pilot_scope VALUES (?, 'INR', 'INDIA', ?)",
                                     (tenant_id, json.dumps(sorted(symbols))))

    def _assert_pilot_order(self, order: OrderIntent) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_scope'"
        ).fetchone()
        if not exists:
            return
        scope = self._connection.execute("SELECT * FROM pilot_scope WHERE tenant_id=?",
                                         (order.tenant_id,)).fetchone()
        if scope and (order.market.value != scope["market"]
                      or order.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
                      or order.symbol not in json.loads(scope["symbols"])):
            raise ValueError("pilot_order_out_of_scope")
        if scope and order.side == Side.BUY:
            self._ensure_account(order.tenant_id)
            if self.reconcile(order.tenant_id)["status"] != "matched":
                raise ValueError("pilot_reconciliation_failed")

    def reconcile(self, tenant_id: str = "default") -> dict:
        from quant_ai.execution.reconciliation import reconcile_paper
        with self._lock:
            return reconcile_paper(self._connection, tenant_id)

    def buy(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.BUY:
            raise ValueError("buy requires BUY side")
        return self._execute(order)

    def sell(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.SELL:
            raise ValueError("sell requires SELL side")
        return self._execute(order)

    def sell_protected(self, order: OrderIntent, evidence: dict, cooldown_until: datetime | None) -> ExecutionResult:
        if order.side != Side.SELL or evidence.get("schema") != "pramana.protective_exit.v1":
            raise ValueError("invalid_protective_exit")
        # Serialize before any state mutation, with reserved fill fields added by the broker.
        json.dumps(evidence, allow_nan=False)
        return self._execute(order, evidence=evidence, cooldown_until=cooldown_until)

    def submit_with_evidence(self, order: OrderIntent, evidence: dict, idempotency_key: str) -> ExecutionResult:
        """Commit a governed paper fill, decision evidence and replay guard together."""
        if (not idempotency_key or evidence.get("schema") != "pramana.swarm_fill.v1"
                or evidence.get("event_type") != "swarm_fill"):
            raise ValueError("invalid_swarm_fill_evidence")
        json.dumps(evidence, allow_nan=False)
        return self._execute(order, evidence=evidence, idempotency_key=idempotency_key)

    def _execute(self, order: OrderIntent, *, evidence: dict | None = None,
                 cooldown_until: datetime | None = None, idempotency_key: str | None = None) -> ExecutionResult:
        if order.quantity <= 0 or order.reference_price <= 0:
            raise ValueError("positive quantity and reference_price required")
        friction = self.friction_model.evaluate(order, self._context_for(order))
        fill_price = friction.execution_price
        notional = fill_price * order.quantity
        order_id = f"PAPER-{uuid4().hex[:16].upper()}"
        now = self._execution_time or datetime.now(timezone.utc)
        for attempt in range(self.lock_retries + 1):
            try:
                return self._execute_once(
                    order, friction, fill_price, notional, order_id, now,
                    evidence=evidence, cooldown_until=cooldown_until, idempotency_key=idempotency_key,
                )
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                self._connection.rollback()
                if attempt >= self.lock_retries:
                    raise PaperBrokerDatabaseLockedError(
                        f"sqlite database remained locked after {self.lock_retries} retries"
                    ) from error
                base = self.lock_backoff_seconds * (2**attempt)
                delay = base + self._jitter(0.0, self.lock_backoff_seconds)
                self._sleep(delay)
        raise AssertionError("unreachable sqlite retry state")

    def _execute_once(
        self,
        order: OrderIntent,
        friction: FrictionResult,
        fill_price: Decimal,
        notional: Decimal,
        order_id: str,
        now: datetime,
        *, evidence: dict | None = None, cooldown_until: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionResult:
        tenant_id = order.tenant_id
        statutory_fees = friction.statutory_fees
        with self._lock, self._connection:
            self._assert_pilot_order(order)
            if idempotency_key is not None:
                inserted = self._connection.execute(
                    "INSERT OR IGNORE INTO paper_idempotency (key,tenant_id,claimed_at) VALUES (?,?,?)",
                    (idempotency_key, tenant_id, now.isoformat()),
                )
                if inserted.rowcount != 1:
                    raise ValueError("duplicate_order")
            self._ensure_account(tenant_id)
            account = self._connection.execute(
                "SELECT cash_balance FROM paper_accounts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            cash = Decimal(account["cash_balance"])
            position = self._connection.execute(
                """SELECT quantity, average_price, stop_price, take_profit_price
                FROM paper_positions
                WHERE tenant_id = ? AND symbol = ? AND market = ? AND asset_class = ?""",
                (tenant_id, order.symbol, order.market.value, order.asset_class.value),
            ).fetchone()
            current_qty = int(position["quantity"]) if position else 0
            current_avg = Decimal(position["average_price"]) if position else Decimal(0)
            held_stop = position["stop_price"] if position else None
            held_take_profit = position["take_profit_price"] if position else None
            # A BUY carries the protective levels forward onto the position; a SELL that only
            # trims the position must not erase the stop that still guards the remainder.
            if order.side == Side.BUY:
                new_stop = str(order.stop_price) if order.stop_price is not None else held_stop
                new_take_profit = (
                    str(order.take_profit_price)
                    if order.take_profit_price is not None
                    else held_take_profit
                )
            else:
                new_stop, new_take_profit = held_stop, held_take_profit
            if order.side == Side.BUY:
                if notional + statutory_fees > cash:
                    raise ValueError("insufficient_paper_cash")
                new_qty = current_qty + order.quantity
                new_avg = ((current_avg * current_qty) + notional) / new_qty
                new_cash = cash - notional - statutory_fees
            else:
                if order.quantity > current_qty:
                    raise ValueError("insufficient_paper_position")
                new_qty = current_qty - order.quantity
                new_avg = current_avg if new_qty else Decimal(0)
                new_cash = cash + notional - statutory_fees
            self._connection.execute(
                "UPDATE paper_accounts SET cash_balance = ?, updated_at = ? WHERE tenant_id = ?",
                (str(new_cash), now.isoformat(), tenant_id),
            )
            if new_qty:
                self._connection.execute(
                    """INSERT INTO paper_positions
                    (tenant_id, symbol, market, asset_class, quantity, average_price,
                     stop_price, take_profit_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, symbol, market, asset_class)
                    DO UPDATE SET quantity = excluded.quantity,
                                  average_price = excluded.average_price,
                                  stop_price = excluded.stop_price,
                                  take_profit_price = excluded.take_profit_price""",
                    (
                        tenant_id,
                        order.symbol,
                        order.market.value,
                        order.asset_class.value,
                        new_qty,
                        str(new_avg),
                        new_stop,
                        new_take_profit,
                    ),
                )
            else:
                self._connection.execute(
                    """DELETE FROM paper_positions
                    WHERE tenant_id = ? AND symbol = ? AND market = ? AND asset_class = ?""",
                    (tenant_id, order.symbol, order.market.value, order.asset_class.value),
                )
            self._connection.execute(
                """INSERT INTO paper_ledger
                (order_id, tenant_id, symbol, market, asset_class, side, quantity,
                 fill_price, notional, status, created_at, stop_price, take_profit_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?, ?)""",
                (
                    order_id,
                    tenant_id,
                    order.symbol,
                    order.market.value,
                    order.asset_class.value,
                    order.side.value,
                    order.quantity,
                    str(fill_price),
                    str(notional),
                    now.isoformat(),
                    str(order.stop_price) if order.stop_price is not None else None,
                    str(order.take_profit_price)
                    if order.take_profit_price is not None
                    else None,
                ),
            )
            cost_rows = [
                ("SPREAD", friction.spread_drag, False),
                ("SLIPPAGE", friction.slippage_drag, False),
            ]
            cost_rows.extend((item.code, item.amount, True) for item in friction.charges)
            self._connection.executemany(
                """INSERT INTO paper_cost_ledger
                (order_id, tenant_id, code, amount, cash_debit, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (order_id, tenant_id, code, str(amount), int(cash_debit), now.isoformat())
                    for code, amount, cash_debit in cost_rows
                    if amount > 0
                ],
            )
            if evidence is not None:
                payload = {**evidence, "order_id": order_id, "tenant_id": tenant_id,
                    "subject": order.symbol, "filled_at": now.isoformat(),
                    "fill": {"quantity": order.quantity, "price": str(fill_price),
                        "cash_fees": str(statutory_fees), "status": "FILLED"}}
                if idempotency_key is not None:
                    payload["idempotency_key"] = idempotency_key
                # Names are fixed here, never taken from the evidence or an API parameter.
                table = "paper_decision_evidence" if evidence.get("schema") == "pramana.swarm_fill.v1" else "paper_protection_evidence"
                self._connection.execute(f"INSERT INTO {table} VALUES (?,?,?)",
                    (order_id, tenant_id, json.dumps(payload, allow_nan=False, sort_keys=True)))
                if cooldown_until is not None:
                    self._connection.execute("INSERT OR REPLACE INTO paper_exit_cooldowns VALUES (?,?,?,?,?)",
                        (tenant_id, order.symbol, order.market.value, order.asset_class.value, cooldown_until.isoformat()))
        return ExecutionResult(order_id, "FILLED", order.quantity, fill_price)

    def cancel(self, order_id: str, tenant_id: str = "default") -> bool:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status FROM paper_ledger WHERE order_id = ? AND tenant_id = ?",
                (order_id, tenant_id),
            ).fetchone()
            if row is None or row["status"] != "OPEN":
                return False
            self._connection.execute(
                "UPDATE paper_ledger SET status = 'CANCELLED' WHERE order_id = ?",
                (order_id,),
            )
            return True

    def get_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT tenant_id, symbol, market, asset_class, quantity, average_price,
                stop_price, take_profit_price
                FROM paper_positions WHERE tenant_id = ? ORDER BY symbol""",
                (tenant_id,),
            ).fetchall()
        return tuple(
            BrokerPosition(
                row["tenant_id"],
                row["symbol"],
                Market(row["market"]),
                AssetClass(row["asset_class"]),
                int(row["quantity"]),
                Decimal(row["average_price"]),
                _optional_decimal(row["stop_price"]),
                _optional_decimal(row["take_profit_price"]),
            )
            for row in rows
        )

    def get_peak_equity(self, tenant_id: str = "default") -> Decimal | None:
        """Durable high-water mark. None when the tenant has never been marked."""
        with self._lock:
            self._ensure_account(tenant_id)
            row = self._connection.execute(
                "SELECT peak_equity FROM paper_accounts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return _optional_decimal(row["peak_equity"]) if row else None

    def record_peak_equity(self, equity: Decimal, tenant_id: str = "default") -> Decimal:
        """Raise the stored high-water mark. Returns the mark in force after the write."""
        with self._lock, self._connection:
            self._ensure_account(tenant_id)
            row = self._connection.execute(
                "SELECT peak_equity FROM paper_accounts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            stored = _optional_decimal(row["peak_equity"]) if row else None
            peak = equity if stored is None else max(stored, equity)
            if stored is None or peak != stored:
                self._connection.execute(
                    "UPDATE paper_accounts SET peak_equity = ? WHERE tenant_id = ?",
                    (str(peak), tenant_id),
                )
            return peak

    def record_exit_cooldown(
        self,
        symbol: str,
        market: Market,
        asset_class: AssetClass,
        until: datetime,
        tenant_id: str = "default",
    ) -> None:
        """Bar re-entry into a symbol until `until`. Durable, so a restart cannot clear it."""
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO paper_exit_cooldowns
                (tenant_id, symbol, market, asset_class, until) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, symbol, market, asset_class)
                DO UPDATE SET until = excluded.until""",
                (tenant_id, symbol, market.value, asset_class.value, until.isoformat()),
            )

    def exit_cooldown_until(
        self,
        symbol: str,
        market: Market,
        asset_class: AssetClass,
        tenant_id: str = "default",
    ) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT until FROM paper_exit_cooldowns
                WHERE tenant_id = ? AND symbol = ? AND market = ? AND asset_class = ?""",
                (tenant_id, symbol, market.value, asset_class.value),
            ).fetchone()
        return datetime.fromisoformat(row["until"]) if row else None

    def claim_idempotency_key(self, key: str, tenant_id: str = "default") -> bool:
        """Durable single-claim guard. False when this key was already consumed."""
        if not key:
            raise ValueError("idempotency key is required")
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM paper_idempotency WHERE key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return False
            self._connection.execute(
                "INSERT INTO paper_idempotency (key, tenant_id, claimed_at) VALUES (?, ?, ?)",
                (key, tenant_id, datetime.now(timezone.utc).isoformat()),
            )
            return True

    def get_margin(self, tenant_id: str = "default") -> BrokerMargin:
        with self._lock, self._connection:
            self._ensure_account(tenant_id)
            account = self._connection.execute(
                "SELECT starting_capital, cash_balance FROM paper_accounts WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            rows = self._connection.execute(
                "SELECT quantity, average_price FROM paper_positions WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        gross = sum(
            (Decimal(row["average_price"]) * int(row["quantity"]) for row in rows),
            Decimal(0),
        )
        cash = Decimal(account["cash_balance"])
        return BrokerMargin(
            tenant_id,
            Decimal(account["starting_capital"]),
            cash,
            gross,
            cash,
        )

    def ledger_entries(self, tenant_id: str = "default") -> tuple[PaperLedgerEntry, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT order_id, tenant_id, symbol, market, asset_class, side, quantity,
                fill_price, notional, status, created_at, stop_price, take_profit_price
                FROM paper_ledger WHERE tenant_id = ? ORDER BY id""",
                (tenant_id,),
            ).fetchall()
        return tuple(
            PaperLedgerEntry(
                row["order_id"],
                row["tenant_id"],
                row["symbol"],
                Market(row["market"]),
                AssetClass(row["asset_class"]),
                Side(row["side"]),
                int(row["quantity"]),
                Decimal(row["fill_price"]),
                Decimal(row["notional"]),
                row["status"],
                datetime.fromisoformat(row["created_at"]),
                _optional_decimal(row["stop_price"]),
                _optional_decimal(row["take_profit_price"]),
            )
            for row in rows
        )

    def cost_entries(self, tenant_id: str = "default") -> tuple[PaperCostEntry, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT order_id, tenant_id, code, amount, cash_debit, created_at
                FROM paper_cost_ledger WHERE tenant_id = ? ORDER BY id""",
                (tenant_id,),
            ).fetchall()
        return tuple(
            PaperCostEntry(
                row["order_id"],
                row["tenant_id"],
                row["code"],
                Decimal(row["amount"]),
                bool(row["cash_debit"]),
                datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def friction_totals(self, tenant_id: str = "default") -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for entry in self.cost_entries(tenant_id):
            totals[entry.code] = totals.get(entry.code, Decimal(0)) + entry.amount
        return totals
    def record_replay_valuation(self, timestamp: str, payload: str, tenant_id: str) -> None:
        """Persist one complete replay valuation without altering trading state."""
        with self._lock, self._connection:
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS paper_replay_valuations (
                    tenant_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    ledger_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, timestamp)
                )"""
            )
            ledger_id = self._connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM paper_ledger WHERE tenant_id=?",
                (tenant_id,),
            ).fetchone()[0]
            self._connection.execute(
                """INSERT INTO paper_replay_valuations
                   (tenant_id, timestamp, ledger_id, payload) VALUES (?, ?, ?, ?)
                   ON CONFLICT(tenant_id, timestamp) DO UPDATE SET
                   ledger_id=excluded.ledger_id, payload=excluded.payload""",
                (tenant_id, timestamp, ledger_id, payload),
            )

    def flush(self) -> None:
        with self._lock:
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()
