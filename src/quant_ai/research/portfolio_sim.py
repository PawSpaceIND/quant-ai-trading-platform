"""Deterministic continuous portfolio research; no broker or live-order interface."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from quant_ai.research.lab import canonical, identity, instant, integer, number


def validate_config(config):
    candidates = config["candidates"]
    if (
        not isinstance(candidates, list)
        or not candidates
        or len(candidates) != len(set(candidates))
    ):
        raise ValueError("distinct_candidates_required")
    for c in candidates:
        identity(c)
    number(config["starting_cash_inr"], positive=True)
    for field in ("fee_bps", "slippage_bps"):
        if number(config[field]) >= 10000:
            raise ValueError("invalid_costs")
    for field in ("max_position_fraction", "max_gross_fraction", "max_drawdown_fraction"):
        if not 0 < number(config[field]) <= 1:
            raise ValueError("invalid_risk_limit")
    for field in ("max_quote_age_seconds", "order_ttl_seconds", "max_order_quantity"):
        integer(config[field], positive=True)
    if (
        not isinstance(config["symbols"], list)
        or not config["symbols"]
        or any(
            not isinstance(s, str) or not s.startswith("NSE:") or len(s) <= 4
            for s in config["symbols"]
        )
    ):
        raise ValueError("nse_cash_symbols_required")
    if len(config["symbols"]) != len(set(config["symbols"])):
        raise ValueError("duplicate_symbol")


def replay(config, events):
    """Replay one common quote timeline into separate long-only candidate books.

    Quotes carry executable bid/ask and side liquidity. Orders are immediate-or-cancel
    on the next eligible quote; unfilled remainder is cancelled. Caller supplies
    session status and corporate-action-adjusted prices. Marks use the last bid.
    """
    validate_config(config)
    fee, slip = number(config["fee_bps"]) / 10000, number(config["slippage_bps"]) / 10000
    initial = number(config["starting_cash_inr"])
    books = {
        c: {
            "cash": initial,
            "positions": {},
            "orders": [],
            "fills": [],
            "cancelled": [],
            "curve": [],
            "peak": initial,
            "halted": False,
            "fees": Decimal(0),
            "realized_pnl": Decimal(0),
        }
        for c in config["candidates"]
    }
    quotes, ids, last_at = {}, set(), None

    def value(book, at):
        stale = [
            s
            for s, p in book["positions"].items()
            if p["quantity"]
            and (
                s not in quotes
                or (at - instant(quotes[s]["at"])).total_seconds() > config["max_quote_age_seconds"]
            )
        ]
        if stale:
            return None, stale
        return book["cash"] + sum(
            (
                p["quantity"] * number(quotes[s]["bid"])
                for s, p in book["positions"].items()
                if p["quantity"]
            ),
            Decimal(0),
        ), []

    def observe(book, at, event_id):
        equity, stale = value(book, at)
        if equity is None:
            point = {
                "event_id": event_id,
                "at": at.isoformat(),
                "equity_inr": None,
                "drawdown_fraction": None,
                "stale_symbols": stale,
            }
        else:
            book["peak"] = max(book["peak"], equity)
            drawdown = (book["peak"] - equity) / book["peak"]
            if drawdown >= number(config["max_drawdown_fraction"]):
                book["halted"] = True
            point = {
                "event_id": event_id,
                "at": at.isoformat(),
                "equity_inr": str(equity),
                "drawdown_fraction": str(drawdown),
                "stale_symbols": [],
            }
        book["curve"].append(point)

    for event in events:
        event_id = identity(event["id"])
        if event_id in ids:
            raise ValueError("duplicate_event")
        ids.add(event_id)
        at = instant(event["at"])
        if last_at is not None and at < last_at:
            raise ValueError("non_chronological_event")
        last_at = at
        kind = event["kind"]
        if kind not in ("quote", "order", "clock"):
            raise ValueError("unsupported_event")
        if kind != "clock" and event["symbol"] not in config["symbols"]:
            raise ValueError("unknown_symbol")
        for book in books.values():
            kept = []
            for order in book["orders"]:
                if (at - instant(order["at"])).total_seconds() > config["order_ttl_seconds"]:
                    book["cancelled"].append({"id": order["id"], "reason": "expired"})
                else:
                    kept.append(order)
            book["orders"] = kept
        if kind == "quote":
            symbol = event["symbol"]
            bid, ask = number(event["bid"], positive=True), number(event["ask"], positive=True)
            if ask < bid:
                raise ValueError("crossed_quote")
            integer(event["bid_quantity"])
            integer(event["ask_quantity"])
            if type(event["session_open"]) is not bool:
                raise ValueError("session_status_required")
            identity(event["provenance"])
            quotes[symbol] = event
            for book in books.values():
                # Apply gap drawdown before allowing new buys at this quote.
                equity, stale = value(book, at)
                if equity is not None:
                    book["peak"] = max(book["peak"], equity)
                if equity is not None and (book["peak"] - equity) / book["peak"] >= number(
                    config["max_drawdown_fraction"]
                ):
                    book["halted"] = True
                available = {"BUY": event["ask_quantity"], "SELL": event["bid_quantity"]}
                retained = []
                for order in book["orders"]:
                    if order["symbol"] != symbol or instant(order["at"]) >= at:
                        retained.append(order)
                        continue
                    side = order["side"]
                    price = ask * (1 + slip) if side == "BUY" else bid * (1 - slip)
                    position = book["positions"].get(symbol, {"quantity": 0, "cost": Decimal(0)})
                    qty = min(order["quantity"], available[side])
                    reason = "liquidity_or_risk_cap"
                    if not event["session_open"]:
                        qty, reason = 0, "session_closed"
                    elif side == "BUY":
                        equity, stale = value(book, at)
                        if equity is not None and (book["peak"] - equity) / book["peak"] >= number(
                            config["max_drawdown_fraction"]
                        ):
                            book["halted"] = True
                        if book["halted"] or stale or equity is None:
                            qty, reason = 0, "halted_or_stale"
                        else:
                            unit = price * (1 + fee)
                            # Post-fill equity includes spread, slippage and fee loss.
                            drag = unit - bid
                            position_cap = number(config["max_position_fraction"])
                            gross_cap = number(config["max_gross_fraction"])
                            gross = equity - book["cash"]
                            position_room = equity * position_cap - position["quantity"] * bid
                            gross_room = equity * gross_cap - gross
                            qty = min(
                                qty,
                                int(book["cash"] // unit),
                                max(0, int(position_room // (bid + position_cap * drag))),
                                max(0, int(gross_room // (bid + gross_cap * drag))),
                            )
                    else:
                        qty = min(qty, position["quantity"])
                    if qty:
                        charge = price * qty * fee
                        if side == "BUY":
                            book["cash"] -= price * qty + charge
                            position["cost"] += price * qty + charge
                            position["quantity"] += qty
                        else:
                            cost = position["cost"] * qty / position["quantity"]
                            book["cash"] += price * qty - charge
                            book["realized_pnl"] += price * qty - charge - cost
                            position["cost"] -= cost
                            position["quantity"] -= qty
                        available[side] -= qty
                        book["positions"][symbol] = position
                        book["fees"] += charge
                        book["fills"].append(
                            {
                                "order_id": order["id"],
                                "quote_id": event_id,
                                "symbol": symbol,
                                "side": side,
                                "quantity": qty,
                                "price": str(price),
                                "fee_inr": str(charge),
                                "at": at.isoformat(),
                            }
                        )
                    if qty < order["quantity"]:
                        book["cancelled"].append(
                            {
                                "id": order["id"],
                                "reason": reason,
                                "quantity": order["quantity"] - qty,
                            }
                        )
                book["orders"] = retained
        elif kind == "order":
            candidate = event["candidate"]
            if candidate not in books or event["side"] not in ("BUY", "SELL"):
                raise ValueError("invalid_order")
            if integer(event["quantity"], positive=True) > config["max_order_quantity"]:
                raise ValueError("order_quantity_limit")
            identity(event["decision_ref"])
            books[candidate]["orders"].append(copy.deepcopy(event))
        for book in books.values():
            observe(book, at, event_id)

    for book in books.values():
        book["cash_inr"] = str(book.pop("cash"))
        book["peak_equity_inr"] = str(book.pop("peak"))
        book["fees_inr"] = str(book.pop("fees"))
        book["realized_pnl_inr"] = str(book.pop("realized_pnl"))
        for position in book["positions"].values():
            position["cost_inr"] = str(position.pop("cost"))
    return {
        "mode": "research_simulation",
        "automatic_promotion": False,
        "books": books,
        "limitations": [
            "Supplied session calendar, liquidity and price provenance",
            "Long-only NSE equity/ETF; no derivatives or corporate-action processing",
            "INR trading costs exclude model API charges",
            "Market movement can exceed configured loss limits",
        ],
    }


class PortfolioJournal:
    """Separate, append-only SQLite journal; deterministic replay reconstructs all state."""

    def __init__(self, path, config=None, *, readonly=False):
        self.readonly = readonly
        if readonly:
            uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
        else:
            if config is None:
                raise ValueError("simulation_config_required")
            validate_config(config)
            self.db = sqlite3.connect(path)
        tables = {
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        expected = {"simulation_config", "simulation_events"}
        if (tables and tables != expected) or (readonly and tables != expected):
            self.db.close()
            raise ValueError("not_a_simulation_database")
        if readonly:
            try:
                self.db.execute("PRAGMA query_only=ON")
                rows = self.db.execute("SELECT id,body FROM simulation_config").fetchall()
                if len(rows) != 1 or rows[0][0] != 1:
                    raise ValueError("simulation_config_missing")
                stored = json.loads(rows[0][1])
                validate_config(stored)
                if config is not None and canonical(config) != canonical(stored):
                    raise ValueError("simulation_config_changed")
                self.config = copy.deepcopy(stored)
                return
            except Exception:
                self.db.close()
                raise
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS simulation_config (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT);
            CREATE TABLE IF NOT EXISTS simulation_events (id TEXT PRIMARY KEY, seq INTEGER UNIQUE,
                body TEXT NOT NULL, digest TEXT NOT NULL);
        """)
        encoded = canonical(config)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO simulation_config VALUES (1,?)", (encoded,))
        if self.db.execute("SELECT body FROM simulation_config").fetchone()[0] != encoded:
            self.db.close()
            raise ValueError("simulation_config_changed")
        self.config = copy.deepcopy(config)

    def close(self):
        self.db.close()

    def events(self):
        rows = self.db.execute("SELECT id,body FROM simulation_config").fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or rows[0][1] != canonical(self.config):
            raise ValueError("simulation_config_changed")
        result = []
        for event_id, seq, body, digest in self.db.execute(
            "SELECT id,seq,body,digest FROM simulation_events ORDER BY seq"
        ):
            if hashlib.sha256(body.encode()).hexdigest() != digest:
                raise ValueError("event_integrity_failure")
            event = json.loads(body)
            if seq != len(result) or event.get("id") != event_id:
                raise ValueError("event_sequence_or_identity_failure")
            result.append(event)
        return result

    def append(self, event):
        if self.readonly:
            raise ValueError("readonly_simulation")
        encoded = canonical(event)
        with self.db:
            self.db.execute("UPDATE simulation_config SET id=id WHERE id=1")
            previous = self.db.execute(
                "SELECT body FROM simulation_events WHERE id=?", (event["id"],)
            ).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError("event_id_conflict")
                return self.report()
            events = self.events()
            report = replay(self.config, events + [event])
            self.db.execute(
                "INSERT INTO simulation_events VALUES (?,?,?,?)",
                (event["id"], len(events), encoded, hashlib.sha256(encoded.encode()).hexdigest()),
            )
            return report

    def report(self):
        body = self.export_evidence()["body"]
        return replay(body["config"], body["events"])

    def export_evidence(self):
        """Consistent private snapshot; the caller must retain its hash independently."""
        self.db.execute("SAVEPOINT portfolio_evidence")
        try:
            body = {
                "schema": "pramana.portfolio_journal.v1",
                "config": copy.deepcopy(self.config),
                "events": self.events(),
            }
            return {"sha256": hashlib.sha256(canonical(body).encode()).hexdigest(), "body": body}
        finally:
            self.db.execute("RELEASE portfolio_evidence")
