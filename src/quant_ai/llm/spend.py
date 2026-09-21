"""Shared USD admission ledger for Atlas text inference, independent of trading.

Integer microdollars; unknown completion keeps its reservation. This is a conservative
API cost estimate, not the provider invoice. Pricing is reviewed through 2026-10-21.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

LIMIT_ENV = "PRAMANA_AI_DAILY_USD_LIMIT"
DB_ENV = "PRAMANA_AI_SPEND_DB"
POLICY = "atlas-text-usd-2026-09-21"
EXPIRES = "2026-10-21"
SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_spend_policy(id INTEGER PRIMARY KEY CHECK(id=1), activation_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_spend_days(day TEXT PRIMARY KEY, limit_micro INTEGER NOT NULL, spent_micro INTEGER NOT NULL DEFAULT 0, reserved_micro INTEGER NOT NULL DEFAULT 0, carried_micro INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ai_spend_requests(id TEXT PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL, policy TEXT NOT NULL, reserved_micro INTEGER NOT NULL, spent_micro INTEGER, status TEXT NOT NULL);
"""


class SpendRefused(RuntimeError):
    def __init__(self):
        super().__init__("daily_usd_budget_unavailable_or_exhausted")


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def valid_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise SpendRefused()
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SpendRefused() from None
    return value


def initialize(environ=None, *, day=None):
    """Record activation at startup without admitting an API request."""
    env = os.environ if environ is None else environ
    if LIMIT_ENV not in env:
        return True
    try:
        cap = limit_micro(env[LIMIT_ENV])
        day = valid_day(day or utc_day())
        database = env.get(DB_ENV, "/data/ai-spend.sqlite")
        if not database or database == ":memory:" or not Path(database).parent.is_dir():
            return False
        with sqlite3.connect(database, timeout=2) as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ai_spend_policy VALUES(1,?)", (day,))
            activation = valid_day(db.execute("SELECT activation_day FROM ai_spend_policy WHERE id=1").fetchone()[0])
            db.execute("INSERT OR IGNORE INTO ai_spend_days(day,limit_micro,carried_micro) VALUES(?,?,?)", (day, cap, cap if day <= activation else 0))
        return True
    except (SpendRefused, TypeError, OSError, sqlite3.Error):
        return False


def limit_micro(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,6}(\.[0-9]{1,2})?", raw):
        raise SpendRefused()
    value = int(Decimal(raw) * 1_000_000)
    if not 0 < value <= 1_000_000_000:
        raise SpendRefused()
    return value


def estimate(payload, day):
    """Upper allowance for the supported, bounded text-only request shapes."""
    if day > EXPIRES or not isinstance(payload, dict):
        raise SpendRefused()
    model = payload.get("model")
    maximum = payload.get("max_tokens", payload.get("max_output_tokens"))
    if model not in {"claude-sonnet-5", "gpt-6-astra"} or type(maximum) is not int or not 0 < maximum <= 16384:
        raise SpendRefused()
    if set(payload) - {"model", "max_tokens", "max_output_tokens", "system", "messages", "tools", "tool_choice",
                       "store", "instructions", "input", "reasoning", "text", "service_tier"}:
        raise SpendRefused()
    # Reject hosted tools, images, continuation IDs and tiers with unknown prices.
    def validate(node):
        if isinstance(node, dict):
            if any(k in node for k in ("image_url", "source", "cache_control", "file_id", "audio")):
                raise SpendRefused()
            if node.get("type") in {"image", "input_image", "document", "input_file"}:
                raise SpendRefused()
            for v in node.values():
                validate(v)
        elif isinstance(node, list):
            for v in node:
                validate(v)
    validate(payload)
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict) or set(tool) - {"name", "description", "input_schema", "strict"}:
            raise SpendRefused()
    if model == "gpt-6-astra" and payload.get("tools"):
        raise SpendRefused()
    if payload.get("service_tier") not in {None, "default", "standard_only"}:
        raise SpendRefused()
    length = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
    # Text token allowance includes schema/framing overhead. Large requests are refused
    # rather than silently entering long-context or hosted-tool price tiers.
    tokens = length * 2 + 4096
    if tokens > 250000:
        raise SpendRefused()
    # Sonnet: highest cache-write input $4/MTok; Astra: $12.50/MTok.
    return (tokens * 40 + maximum * 100 + 9) // 10 if model == "claude-sonnet-5" else (tokens * 125 + maximum * 500 + 9) // 10


def usage_micro(model, usage):
    if not isinstance(usage, dict):
        return None
    def count(k, optional=False):
        v = usage.get(k, 0 if optional else None)
        if optional and v is None:
            v = 0
        if type(v) is not int or not 0 <= v <= 10_000_000:
            raise ValueError("usage_invalid")
        return v
    try:
        inp, out = count("input_tokens"), count("output_tokens")
        if model == "claude-sonnet-5":
            amount = inp * 20 + out * 100 + count("cache_creation_input_tokens", True) * 40 + count("cache_read_input_tokens", True) * 2
        elif model == "gpt-6-astra":
            # Input already includes cached tokens. Charge all at highest cache-write
            # rate until detailed cache-write accounting is proven; reasoning is output.
            amount = inp * 125 + out * 500
        else:
            return None
        return (amount + 9) // 10
    except ValueError:
        return None


def reserve(payload, environ=None, *, day=None):
    env = os.environ if environ is None else environ
    if LIMIT_ENV not in env:
        return None  # Non-pilot library use; deployment sets the cap for every service.
    day = day or utc_day()
    try:
        day = valid_day(day)
        cap = limit_micro(env[LIMIT_ENV])
        amount = estimate(payload, day)
        database = env.get(DB_ENV, "/data/ai-spend.sqlite")
        if not database or database == ":memory:" or not Path(database).parent.is_dir():
            raise SpendRefused()
        with sqlite3.connect(database, timeout=2) as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO ai_spend_policy VALUES(1,?)", (day,))
            activation = valid_day(db.execute("SELECT activation_day FROM ai_spend_policy WHERE id=1").fetchone()[0])
            # Historical calls lacked dollars. Never start with fictional zero spend on
            # the activation day; the first fresh UTC day gets the normal allowance.
            seed = cap if day <= activation else 0
            db.execute("INSERT OR IGNORE INTO ai_spend_days(day,limit_micro,carried_micro) VALUES(?,?,?)", (day, cap, seed))
            db.execute("UPDATE ai_spend_days SET limit_micro=MIN(limit_micro,?) WHERE day=?", (cap, day))
            counters = db.execute("SELECT limit_micro,spent_micro,reserved_micro,carried_micro FROM ai_spend_days WHERE day=?", (day,)).fetchone()
            if not counters or any(type(v) is not int or v < 0 for v in counters):
                raise SpendRefused()
            row = db.execute("UPDATE ai_spend_days SET reserved_micro=reserved_micro+? WHERE day=? AND carried_micro+spent_micro+reserved_micro+?<=limit_micro", (amount, day, amount))
            if row.rowcount != 1:
                return False
            ticket = uuid4().hex
            db.execute("INSERT INTO ai_spend_requests VALUES(?,?,?,?,?,NULL,'reserved')", (ticket, day, payload["model"], POLICY, amount))
        return {"database": database, "id": ticket}
    except (ValueError, TypeError, OSError, sqlite3.Error, RecursionError):
        raise SpendRefused() from None


def settle(ticket, usage):
    if ticket is None:
        return
    try:
        with sqlite3.connect(ticket["database"], timeout=2) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT day,model,reserved_micro,status FROM ai_spend_requests WHERE id=?", (ticket["id"],)).fetchone()
            if not row or row[3] != "reserved":
                return
            day, model, reserved, _ = row
            spent = usage_micro(model, usage)
            if spent is None:
                return
            db.execute("UPDATE ai_spend_requests SET spent_micro=?,status='settled' WHERE id=?", (spent, ticket["id"]))
            db.execute("UPDATE ai_spend_days SET spent_micro=spent_micro+?,reserved_micro=reserved_micro-? WHERE day=?", (spent, reserved, day))
    except (sqlite3.Error, KeyError, TypeError):
        pass  # Keep unknown usage reserved; never reopen headroom on an error.


def require_reservation(payload):
    ticket = reserve(payload)
    if ticket is False:
        raise SpendRefused()
    return ticket
