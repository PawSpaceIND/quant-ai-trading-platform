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
from quant_ai.execution.friction import FrictionContext, MarketFrictionModel


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


class PaperBrokerService(BrokerAdapter):
    """SQLite-backed local paper broker. No network clients are used or accepted."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        starting_capital: Decimal = Decimal(100000),
        slippage_bps: Decimal | None = None,
        friction_model: MarketFrictionModel | None = None,
    ) -> None:
        if starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if slippage_bps is not None and slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
        if slippage_bps is not None and friction_model is not None:
            raise ValueError("choose slippage_bps compatibility mode or friction_model")
        self.starting_capital = starting_capital
        self.slippage_bps = slippage_bps or Decimal(0)
        self.friction_model = friction_model or (
            MarketFrictionModel.compatibility(slippage_bps)
            if slippage_bps is not None
            else MarketFrictionModel()
        )
        self._friction_context: FrictionContext | None = None
        self._execution_time: datetime | None = None
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
                CREATE TABLE IF NOT EXISTS paper_cost_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    cash_debit INTEGER NOT NULL,
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
        friction = self.friction_model.evaluate(order, self._context_for(order))
        fill_price = friction.execution_price
        notional = fill_price * order.quantity
        statutory_fees = friction.statutory_fees
        order_id = f"PAPER-{uuid4().hex[:16].upper()}"
        now = self._execution_time or datetime.now(timezone.utc)
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
            cost_rows = [("SPREAD", friction.spread_drag, False), ("SLIPPAGE", friction.slippage_drag, False)]
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
            """SELECT order_id, tenant_id, symbol, market, asset_class, side, quantity, fill_price,
            notional, status, created_at FROM paper_ledger
            WHERE tenant_id = ? ORDER BY id""",
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
            )
            for row in rows
        )

    def cost_entries(self, tenant_id: str = "default") -> tuple[PaperCostEntry, ...]:
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
    def flush(self) -> None:
        with self._lock:
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()

