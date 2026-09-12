from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from uuid import uuid4

from quant_ai.brokers.adapter import BrokerAdapter, BrokerMargin, BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side


@dataclass(frozen=True)
class PaperLedgerEntry:
    order_id: str
    tenant_id: str
    symbol: str
    side: Side
    quantity: int
    fill_price: Decimal
    notional: Decimal
    status: str
    created_at: datetime


class PaperBrokerService(BrokerAdapter):
    """SQLite-backed local paper broker. No network clients are used or accepted."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        starting_capital: Decimal = Decimal(100000),
        slippage_bps: Decimal = Decimal(1),
    ) -> None:
        if starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        self.starting_capital = starting_capital
        self.slippage_bps = slippage_bps
        self._lock = RLock()
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    tenant_id TEXT PRIMARY KEY,
                    starting_capital TEXT NOT NULL,
                    cash_balance TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    tenant_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_price TEXT NOT NULL,
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
                    created_at TEXT NOT NULL
                );
                """
            )

    def _ensure_account(self, tenant_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_accounts
            (tenant_id, starting_capital, cash_balance, updated_at) VALUES (?, ?, ?, ?)""",
            (tenant_id, str(self.starting_capital), str(self.starting_capital), now),
        )

    def _fill_price(self, order: OrderIntent) -> Decimal:
        direction = Decimal(1) if order.side == Side.BUY else Decimal(-1)
        return order.reference_price + (
            order.reference_price * self.slippage_bps / Decimal(10000) * direction
        )

    def buy(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.BUY:
            raise ValueError("buy requires BUY side")
        return self._execute(order)

    def sell(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.SELL:
            raise ValueError("sell requires SELL side")
        return self._execute(order)

    def _execute(self, order: OrderIntent) -> ExecutionResult:
        if order.quantity <= 0 or order.reference_price <= 0:
            raise ValueError("positive quantity and reference_price required")
        tenant_id = order.tenant_id
        fill_price = self._fill_price(order)
        notional = fill_price * order.quantity
        order_id = f"PAPER-{uuid4().hex[:16].upper()}"
        now = datetime.now(timezone.utc)
        with self._lock, self._connection:
            self._ensure_account(tenant_id)
            account = self._connection.execute(
                "SELECT cash_balance FROM paper_accounts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            cash = Decimal(account["cash_balance"])
            position = self._connection.execute(
                """SELECT quantity, average_price FROM paper_positions
                WHERE tenant_id = ? AND symbol = ? AND market = ? AND asset_class = ?""",
                (tenant_id, order.symbol, order.market.value, order.asset_class.value),
            ).fetchone()
            current_qty = int(position["quantity"]) if position else 0
            current_avg = Decimal(position["average_price"]) if position else Decimal(0)
            if order.side == Side.BUY:
                if notional > cash:
                    raise ValueError("insufficient_paper_cash")
                new_qty = current_qty + order.quantity
                new_avg = ((current_avg * current_qty) + notional) / new_qty
                new_cash = cash - notional
            else:
                if order.quantity > current_qty:
                    raise ValueError("insufficient_paper_position")
                new_qty = current_qty - order.quantity
                new_avg = current_avg if new_qty else Decimal(0)
                new_cash = cash + notional
            self._connection.execute(
                "UPDATE paper_accounts SET cash_balance = ?, updated_at = ? WHERE tenant_id = ?",
                (str(new_cash), now.isoformat(), tenant_id),
            )
            if new_qty:
                self._connection.execute(
                    """INSERT INTO paper_positions
                    (tenant_id, symbol, market, asset_class, quantity, average_price)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, symbol, market, asset_class)
                    DO UPDATE SET quantity = excluded.quantity, average_price = excluded.average_price""",
                    (
                        tenant_id,
                        order.symbol,
                        order.market.value,
                        order.asset_class.value,
                        new_qty,
                        str(new_avg),
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
                 fill_price, notional, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?)""",
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
                ),
            )
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
        rows = self._connection.execute(
            """SELECT tenant_id, symbol, market, asset_class, quantity, average_price
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
            )
            for row in rows
        )

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
        rows = self._connection.execute(
            """SELECT order_id, tenant_id, symbol, side, quantity, fill_price,
            notional, status, created_at FROM paper_ledger
            WHERE tenant_id = ? ORDER BY id""",
            (tenant_id,),
        ).fetchall()
        return tuple(
            PaperLedgerEntry(
                row["order_id"],
                row["tenant_id"],
                row["symbol"],
                Side(row["side"]),
                int(row["quantity"]),
                Decimal(row["fill_price"]),
                Decimal(row["notional"]),
                row["status"],
                datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )
