from __future__ import annotations

import json
import logging
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
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.friction import FrictionContext, FrictionResult, MarketFrictionModel
from quant_ai.execution.ledger_integrity import (
    PaperLedgerDataError,
    finite_amount,
    position_geometry_issues,
    whole_quantity,
)
from quant_ai.execution.live_friction import assumed_friction_context
from quant_ai.execution.protection_state import positive_level, protection_coverage
from quant_ai.instruments.contract import assert_contract_tradable, assert_order_fits_contract
from quant_ai.instruments.identity import (
    canonical_instrument_identity,
    instrument_from_identity,
)

LOGGER = logging.getLogger(__name__)


class PaperBrokerDatabaseLockedError(RuntimeError):
    """Paper broker exhausted bounded retries while SQLite remained locked."""


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _instrument_identity_for_order(order: OrderIntent) -> str | None:
    instrument = getattr(order, "instrument", None)
    if instrument is None:
        return None
    if (order.symbol, order.market, order.asset_class) != (
        instrument.symbol, instrument.market, instrument.asset_class
    ):
        raise ValueError("instrument_bound_order_identity_mismatch")
    return canonical_instrument_identity(instrument)


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
        friction_context_provider: Callable[[OrderIntent], FrictionContext | None] | None = None,
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
        # The live source of observed friction inputs. Unset means no market was observed,
        # and an unobserved market is priced from the conservative assumption.
        self._friction_context_provider = friction_context_provider
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
                    instrument_identity TEXT,
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
                    take_profit_price TEXT,
                    instrument_identity TEXT
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
        ("paper_positions", "instrument_identity", "TEXT"),
        ("paper_ledger", "stop_price", "TEXT"),
        ("paper_ledger", "take_profit_price", "TEXT"),
        ("paper_ledger", "instrument_identity", "TEXT"),
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

    def set_friction_context_provider(
        self, provider: Callable[[OrderIntent], FrictionContext | None] | None
    ) -> None:
        """Install the live source of friction inputs (observed bars and quotes).

        A harness that pins one context per step - historical replay - still wins over the
        provider, so replay stays reproducible.
        """
        self._friction_context_provider = provider

    def _context_for(self, order: OrderIntent) -> FrictionContext:
        if self._friction_context is not None:
            return self._friction_context
        provider = self._friction_context_provider
        if provider is not None:
            try:
                supplied = provider(order)
            except Exception:  # market data must never take down a fill
                LOGGER.warning(
                    "friction_context_provider_failed symbol=%s; pricing from assumed inputs",
                    order.symbol,
                    exc_info=True,
                )
                supplied = None
            if supplied is not None:
                return supplied
        # No observed inputs at all: assume a wide, thin market rather than a cheap one.
        return assumed_friction_context(order)

    @staticmethod
    def _friction_proof(
        friction: FrictionResult, context: FrictionContext | None
    ) -> dict[str, object]:
        """The cost inputs and the charge lines behind one fill, as plain strings."""
        proof: dict[str, object] = {
            "schema": "pramana.fill_friction.v1",
            "referencePrice": str(friction.reference_price),
            "executionPrice": str(friction.execution_price),
            "spreadDrag": str(friction.spread_drag),
            "slippageDrag": str(friction.slippage_drag),
            "charges": {item.code: str(item.amount) for item in friction.charges},
        }
        if context is not None:
            proof["inputs"] = context.provenance()
        if friction.fee_schedule_provenance is not None:
            # Statutory rates are evidence too. MCX fills carry the source/date and the
            # real contract note that reconciled those rates, beside market-input provenance.
            proof["feeSchedule"] = friction.fee_schedule_provenance
        return proof

    def configure_pilot(self, instruments, tenant_id: str) -> None:
        from quant_ai.governance.pilot import validate_pilot_instruments
        instruments = tuple(instruments)
        validate_pilot_instruments(instruments)
        symbols = {item.symbol: item.asset_class.value for item in instruments}
        identities = {
            item.symbol: json.loads(canonical_instrument_identity(item))
            for item in instruments
        }
        with self._lock, self._connection:
            self._connection.execute("""CREATE TABLE IF NOT EXISTS pilot_scope (
                tenant_id TEXT PRIMARY KEY, currency TEXT NOT NULL, market TEXT NOT NULL,
                symbols TEXT NOT NULL, instrument_identities TEXT)""")
            scope_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(pilot_scope)")
            }
            if "instrument_identities" not in scope_columns:
                self._connection.execute(
                    "ALTER TABLE pilot_scope ADD COLUMN instrument_identities TEXT"
                )
            positions = self._connection.execute(
                "SELECT symbol,market,asset_class,instrument_identity FROM paper_positions WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
            if any(p["market"] != Market.INDIA.value or symbols.get(p["symbol"]) != p["asset_class"] for p in positions):
                raise ValueError("pilot_existing_positions_out_of_scope")
            for position in positions:
                raw = position["instrument_identity"]
                if raw is not None and json.loads(raw) != identities[position["symbol"]]:
                    raise ValueError("pilot_existing_position_contract_mismatch")
            previous = self._connection.execute(
                "SELECT currency, market FROM pilot_scope WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            if previous and (previous["currency"], previous["market"]) != ("INR", "INDIA"):
                raise ValueError("pilot_account_currency_mismatch")
            # Legacy mixed-market accounts cannot be relabeled as INR.
            markets = self._connection.execute("SELECT DISTINCT market FROM paper_ledger WHERE tenant_id=?", (tenant_id,))
            if any(row["market"] != Market.INDIA.value for row in markets):
                raise ValueError("pilot_legacy_currency_ambiguous")
            self._connection.execute(
                """INSERT OR REPLACE INTO pilot_scope
                (tenant_id,currency,market,symbols,instrument_identities)
                VALUES (?, 'INR', 'INDIA', ?, ?)""",
                (
                    tenant_id, json.dumps(symbols, sort_keys=True),
                    json.dumps(identities, sort_keys=True, separators=(",", ":")),
                ),
            )

    def _assert_pilot_order(self, order: OrderIntent) -> bool:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_scope'"
        ).fetchone()
        if not exists:
            return False
        scope = self._connection.execute("SELECT * FROM pilot_scope WHERE tenant_id=?",
                                         (order.tenant_id,)).fetchone()
        symbols = json.loads(scope["symbols"]) if scope else {}
        identities = (
            json.loads(scope["instrument_identities"])
            if scope and "instrument_identities" in dict(scope)
            and scope["instrument_identities"]
            else {}
        )
        order_identity = _instrument_identity_for_order(order)
        configured_identity = identities.get(order.symbol) if isinstance(identities, dict) else None
        if scope and (
            order.market.value != scope["market"]
            or order.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
            or order.symbol not in symbols
            or (isinstance(symbols, dict) and symbols[order.symbol] != order.asset_class.value)
            or (
                order_identity is not None
                and (configured_identity is None or json.loads(order_identity) != configured_identity)
            )
        ):
            raise ValueError("pilot_order_out_of_scope")
        if scope and order.side == Side.BUY:
            if not isinstance(symbols, dict):
                # Older scopes recorded symbols only. The normal pilot factory refreshes
                # this from configured instruments; never guess a contract for new risk.
                raise ValueError("pilot_scope_requires_instrument_configuration")
            controls = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_control_state'"
            ).fetchone()
            state = self._connection.execute(
                "SELECT kill_switch_engaged FROM risk_control_state WHERE tenant_id=?", (order.tenant_id,)
            ).fetchone() if controls else None
            if state is not None and state[0] != 0:
                raise ValueError("pilot_halted")
            if self.protection_coverage(order.tenant_id)["status"] != "complete":
                raise ValueError("pilot_protection_incomplete")
            if self.reconcile(order.tenant_id)["status"] != "matched":
                raise ValueError("pilot_reconciliation_failed")
        return scope is not None

    def protection_coverage(self, tenant_id: str = "default", now: datetime | None = None) -> dict:
        with self._lock:
            return protection_coverage(self._connection, tenant_id, now)

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
        whole_quantity(order.quantity, "positive quantity and reference_price required")
        if positive_level(order.reference_price) is None:
            raise ValueError("positive quantity and reference_price required")
        now = self._execution_time or datetime.now(timezone.utc)
        instrument = getattr(order, "instrument", None)
        if instrument is not None:
            _instrument_identity_for_order(order)
            assert_contract_tradable(instrument, order.side, now)
            assert_order_fits_contract(instrument, order.quantity, order.reference_price)
        context = self._context_for(order)
        friction = self.friction_model.evaluate(order, context)
        fill_price = friction.execution_price
        if positive_level(fill_price) is None:
            raise ValueError("positive finite fill_price required")
        if instrument is not None and instrument.is_dated_contract:
            # Never invent a tick-rounded execution here; the pricing layer must supply
            # a price the contract could actually print.
            assert_order_fits_contract(instrument, order.quantity, fill_price)
        notional = fill_price * order.quantity
        order_id = f"PAPER-{uuid4().hex[:16].upper()}"
        for attempt in range(self.lock_retries + 1):
            try:
                return self._execute_once(
                    order, friction, fill_price, notional, order_id, now,
                    evidence=evidence, cooldown_until=cooldown_until,
                    idempotency_key=idempotency_key, context=context,
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
        idempotency_key: str | None = None, context: FrictionContext | None = None,
    ) -> ExecutionResult:
        tenant_id = order.tenant_id
        statutory_fees = finite_amount(friction.statutory_fees, "invalid_execution_fees", nonnegative=True)
        finite_amount(notional, "invalid_execution_notional", positive=True)
        with self._lock, self._connection:
            # Acquire the write transaction before reading scope or risk state. The
            # final checks and fill cannot race another connection's configuration/halt.
            self._ensure_account(tenant_id)
            pilot_order = self._assert_pilot_order(order)
            if idempotency_key is not None:
                inserted = self._connection.execute(
                    "INSERT OR IGNORE INTO paper_idempotency (key,tenant_id,claimed_at) VALUES (?,?,?)",
                    (idempotency_key, tenant_id, now.isoformat()),
                )
                if inserted.rowcount != 1:
                    raise ValueError("duplicate_order")
            account = self._connection.execute(
                "SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            finite_amount(account["starting_capital"], "invalid_starting_capital", positive=True)
            cash = finite_amount(account["cash_balance"], "invalid_account_cash")
            order_identity = _instrument_identity_for_order(order)
            position = self._connection.execute(
                """SELECT quantity, average_price, stop_price, take_profit_price,
                instrument_identity FROM paper_positions
                WHERE tenant_id = ? AND symbol = ? AND market = ? AND asset_class = ?""",
                (tenant_id, order.symbol, order.market.value, order.asset_class.value),
            ).fetchone()
            current_qty = whole_quantity(position["quantity"]) if position else 0
            current_avg = finite_amount(position["average_price"], "invalid_position_average", positive=True) if position else Decimal(0)
            held_stop = position["stop_price"] if position else None
            held_take_profit = position["take_profit_price"] if position else None
            held_identity = position["instrument_identity"] if position else None
            if position is not None and held_identity != order_identity:
                if held_identity is None:
                    raise ValueError("position_instrument_identity_missing")
                if order_identity is None:
                    raise ValueError("bound_position_requires_instrument_identity")
                raise ValueError("position_instrument_identity_mismatch")
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
            if pilot_order and order.side == Side.BUY:
                stop, target = positive_level(order.stop_price), positive_level(new_take_profit)
                if stop is None:
                    raise ValueError("pilot_valid_stop_required")
                if stop >= min(order.reference_price, fill_price):
                    raise ValueError("pilot_stop_must_be_below_entry")
                if new_take_profit is not None and (target is None or target <= max(order.reference_price, fill_price)):
                    raise ValueError("pilot_target_must_be_above_entry")
                if held_stop is not None and stop < positive_level(held_stop):
                    raise ValueError("pilot_cannot_loosen_held_stop")
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
            finite_amount(new_cash, "invalid_resulting_cash")
            if new_qty:
                whole_quantity(new_qty, "invalid_resulting_quantity")
                finite_amount(new_avg, "invalid_resulting_average", positive=True)
            self._connection.execute(
                "UPDATE paper_accounts SET cash_balance = ?, updated_at = ? WHERE tenant_id = ?",
                (str(new_cash), now.isoformat(), tenant_id),
            )
            if new_qty:
                self._connection.execute(
                    """INSERT INTO paper_positions
                    (tenant_id, symbol, market, asset_class, quantity, average_price,
                     stop_price, take_profit_price, instrument_identity)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, symbol, market, asset_class)
                    DO UPDATE SET quantity = excluded.quantity,
                                  average_price = excluded.average_price,
                                  stop_price = excluded.stop_price,
                                  take_profit_price = excluded.take_profit_price,
                                  instrument_identity = excluded.instrument_identity""",
                    (
                        tenant_id,
                        order.symbol,
                        order.market.value,
                        order.asset_class.value,
                        new_qty,
                        str(new_avg),
                        new_stop,
                        new_take_profit,
                        order_identity,
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
                 fill_price, notional, status, created_at, stop_price, take_profit_price,
                 instrument_identity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?, ?, ?, ?)""",
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
                    order_identity,
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
                        "cash_fees": str(statutory_fees), "status": "FILLED",
                        # What priced this fill: observed market inputs, or an assumption.
                        "friction": self._friction_proof(friction, context)}}
                if order_identity is not None:
                    payload["fill"]["instrumentIdentity"] = json.loads(order_identity)
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
        """Complete position book. Any invalid row fails, never a partial valuation."""
        return tuple(self._decode_position(row) for row in self._position_rows(tenant_id))

    def get_protection_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        """Valid rows for independent exits only, never for totals or sizing.

        Excluded rows are reported by protection_coverage and halt pilot entries.
        No quantity, cost basis or instrument identity is guessed for a corrupt row.
        """
        return tuple(self._decode_position(row) for row in self._position_rows(tenant_id)
                     if not position_geometry_issues(row))

    def _position_rows(self, tenant_id: str):
        with self._lock:
            return self._connection.execute(
                """SELECT tenant_id, symbol, market, asset_class, quantity, average_price,
                stop_price, take_profit_price, instrument_identity
                FROM paper_positions WHERE tenant_id = ? ORDER BY symbol""",
                (tenant_id,),
            ).fetchall()
    @staticmethod
    def _decode_position(row) -> BrokerPosition:
        issues = position_geometry_issues(row)
        if issues:
            raise PaperLedgerDataError(issues[0])
        return BrokerPosition(
                row["tenant_id"],
                row["symbol"],
                Market(row["market"]),
                AssetClass(row["asset_class"]),
                row["quantity"],
                Decimal(row["average_price"]),
                # Raw invalid values remain visible in protection_coverage. Exclude them
                # from comparisons so one corrupt threshold cannot suppress other exits.
                positive_level(row["stop_price"]),
                positive_level(row["take_profit_price"]),
        )

    def bound_instrument_for_position(
        self, symbol: str, market: Market, asset_class: AssetClass,
        tenant_id: str = "default",
    ) -> Instrument | None:
        """Immutable instrument snapshot attached to this position, when one exists."""
        with self._lock:
            row = self._connection.execute(
                """SELECT symbol,market,asset_class,instrument_identity FROM paper_positions
                WHERE tenant_id=? AND symbol=? AND market=? AND asset_class=?""",
                (tenant_id, symbol, market.value, asset_class.value),
            ).fetchone()
        if row is None:
            raise KeyError((tenant_id, market.value, asset_class.value, symbol))
        raw = row["instrument_identity"]
        return self._decode_bound_identity(row, raw)

    @staticmethod
    def _decode_bound_identity(row, raw: str | None) -> Instrument | None:
        if raw is None:
            return None
        instrument = instrument_from_identity(raw)
        if (instrument.symbol, instrument.market.value, instrument.asset_class.value) != (
            row["symbol"], row["market"], row["asset_class"]
        ):
            raise PaperLedgerDataError("stored_instrument_identity_mismatch")
        return instrument

    def bound_instrument_for_fill(
        self, order_id: str, tenant_id: str = "default"
    ) -> Instrument | None:
        """Immutable instrument snapshot committed beside one fill, if bound."""
        with self._lock:
            row = self._connection.execute(
                "SELECT symbol,market,asset_class,instrument_identity FROM paper_ledger WHERE order_id=? AND tenant_id=?",
                (order_id, tenant_id),
            ).fetchone()
        if row is None:
            raise KeyError((tenant_id, order_id))
        raw = row["instrument_identity"]
        return self._decode_bound_identity(row, raw)

    def get_starting_capital(self, tenant_id: str = "default") -> Decimal:
        """Initialize/read capital without projecting possibly damaged positions."""
        with self._lock, self._connection:
            self._ensure_account(tenant_id)
            row = self._connection.execute(
                "SELECT starting_capital FROM paper_accounts WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
        return finite_amount(row[0], "invalid_starting_capital", positive=True)

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
            positions = self.get_positions(tenant_id)
        gross = sum(
            (row.average_price * row.quantity for row in positions),
            Decimal(0),
        )
        cash = finite_amount(account["cash_balance"], "invalid_account_cash")
        return BrokerMargin(
            tenant_id,
            finite_amount(account["starting_capital"], "invalid_starting_capital", positive=True),
            cash,
            finite_amount(gross, "invalid_gross_position_value", nonnegative=True),
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
                whole_quantity(row["quantity"], "invalid_fill_quantity"),
                finite_amount(row["fill_price"], "invalid_fill_price", positive=True),
                finite_amount(row["notional"], "invalid_fill_notional", positive=True),
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
                finite_amount(row["amount"], "invalid_cost_amount", nonnegative=True),
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
    def record_replay_valuation(self, timestamp: str, payload: str, tenant_id: str, run_id: str | None = None) -> None:
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
            if run_id is not None:
                run = self._connection.execute("SELECT tenant_id,status FROM paper_replay_runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None or tuple(run) != (tenant_id, "running"):
                    raise ValueError("replay_run_not_active_for_tenant")
                sequence = self._connection.execute("SELECT coalesce(max(sequence),0)+1 FROM paper_replay_run_points WHERE run_id=?", (run_id,)).fetchone()[0]
                self._connection.execute("INSERT INTO paper_replay_run_points VALUES (?,?,?,?)", (run_id, sequence, ledger_id, payload))

    def flush(self) -> None:
        with self._lock:
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()
