"""Durable observation history; no broker writes or paper-account mutations.

All reports replay the retained captures in one SQLite snapshot. Hashes prove
internal continuity, not source authenticity or that no capture was omitted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.execution.broker_observation import (
    IST,
    SCOPE,
    TERMINAL,
    _rows,
    canonical,
    digest,
    inspect_capture,
)
from quant_ai.execution.broker_reads import BrokerReadError, identifier

MAX_ENTRIES = 10000
MAX_BYTES = 128_000_000
TABLES = {"broker_journal_meta", "broker_captures"}
CAPTURE_FIELDS = {"schema", "scope", "tenantId", "broker", "accountRef", "startedAt", "finishedAt",
    "ordersBefore", "tradesBefore", "orders", "trades", "sha256"}


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?\+00:00", value):
        raise BrokerReadError("Invalid normalized capture timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise BrokerReadError("Invalid normalized capture date") from error


def validate_capture(c, tenant, ref):
    if not isinstance(c, dict) or set(c) != CAPTURE_FIELDS or c["schema"] != "pramana.broker_observation.v1" or c["scope"] != SCOPE or c["broker"] != "zerodha-kite" or c["tenantId"] != tenant or c["accountRef"] != ref:
        raise BrokerReadError("Capture schema/account mismatch")
    if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref):
        raise BrokerReadError("Invalid external account reference")
    start, end = timestamp(c["startedAt"]), timestamp(c["finishedAt"])
    if not 0 <= (end-start).total_seconds() <= 30 or start.astimezone(IST).date() != end.astimezone(IST).date():
        raise BrokerReadError("Invalid capture interval")
    for name in ("ordersBefore", "orders", "tradesBefore", "trades"):
        rows = c[name]
        trades = name.startswith("trades")
        if not isinstance(rows, list) or len(rows) > (10000 if trades else 5000):
            raise BrokerReadError("Invalid capture collection")
        raw = []
        for row in rows:
            fields = {"orderId", "instrumentId", "symbol", "exchange", "product", "side", "quantity", "at"}
            fields |= {"tradeId", "exchangeOrderId", "price"} if trades else {"status", "variety", "filled", "pending", "cancelled", "averagePrice", "exchangeOrderId"}
            if not isinstance(row, dict) or set(row) != fields:
                raise BrokerReadError("Unexpected normalized broker fields")
            if any(type(row[k]) is not int for k in (["quantity"] if trades else ["quantity", "filled", "pending", "cancelled"])):
                raise BrokerReadError("Normalized quantities must be exact integers")
            item = {"order_id":row["orderId"], "instrument_token":row["instrumentId"], "tradingsymbol":row["symbol"],
                "exchange":row["exchange"], "product":row["product"], "transaction_type":row["side"], "quantity":row["quantity"], "exchange_order_id":row["exchangeOrderId"]}
            at = timestamp(row["at"]).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
            if trades:
                item.update(trade_id=row["tradeId"], average_price=row["price"], fill_timestamp=at)
            else:
                item.update(status=row["status"], variety=row["variety"], filled_quantity=row["filled"], pending_quantity=row["pending"],
                    cancelled_quantity=row["cancelled"], average_price=row["averagePrice"], order_timestamp=at)
            raw.append(item)
        if _rows({"status":"success", "data":raw}, trades=trades) != rows:
            raise BrokerReadError("Capture rows are not canonical")
    unsigned = {k:v for k,v in c.items() if k != "sha256"}
    if len(canonical(c)) > 8_000_000 or c["sha256"] != digest(unsigned):
        raise BrokerReadError("Capture hash mismatch or oversized evidence")
    return c


def replay(captures):
    """Yield cumulative history with explicit daily-book boundaries and gaps."""
    known_orders, known_trades, terminals, max_filled, exchange_ids = {}, {}, {}, {}, {}
    previous, current_day, stable_seen = None, None, False
    issue_total = 0
    for sequence, c in enumerate(captures, 1):
        inspection = inspect_capture(c)
        day = timestamp(c["finishedAt"]).astimezone(IST).date().isoformat()
        issues, changes = [], []
        issue_count = change_count = 0
        def issue(code, order_id=None, issues=issues):
            nonlocal issue_count
            issue_count += 1
            if len(issues) < 100:
                issues.append({"code":code, "orderId":order_id})
        def change(kind, order_id, before=None, after=None, changes=changes):
            nonlocal change_count
            change_count += 1
            if len(changes) < 250:
                changes.append({"kind":kind, "orderId":order_id, "before":before, "after":after})
        boundary = day != current_day
        if boundary:
            unresolved = sum(o["status"] not in TERMINAL for o in known_orders.values())
            if current_day and unresolved:
                issue("day_boundary_unresolved_orders")
            known_orders, known_trades, terminals, max_filled, exchange_ids = {}, {}, {}, {}, {}
            current_day, stable_seen = day, False
        else:
            unresolved = 0
        comparable = stable_seen and inspection["status"] != "changing"
        if inspection["status"] != "changing":
            now_orders = {o["orderId"]:o for o in c["orders"]}
            now_trades = {(t["exchange"], t["tradeId"]):t for t in c["trades"]}
            for key, old in known_orders.items():
                if key not in now_orders:
                    issue("previously_observed_order_missing", key)
            for key, old in known_trades.items():
                if key not in now_trades:
                    issue("previously_observed_trade_missing", old["orderId"])
            for key, o in now_orders.items():
                old = known_orders.get(key)
                if old:
                    if any(o[f] != old[f] for f in ("instrumentId", "symbol", "exchange", "product", "side", "variety")):
                        issue("order_identity_changed", key)
                    if key in exchange_ids and exchange_ids[key] != o["exchangeOrderId"]:
                        issue("exchange_order_identity_changed", key)
                    if o["filled"] < max_filled[key]:
                        issue("filled_quantity_regressed", key)
                    if key in terminals and any(o[f] != terminals[key][f] for f in (["status", "quantity", "filled", "averagePrice"] if terminals[key]["status"] == "COMPLETE" else ["status", "quantity", "filled"])):
                        issue("terminal_order_changed", key)
                    for field in ("status", "quantity", "filled", "pending", "cancelled", "averagePrice", "exchangeOrderId", "at"):
                        if old[field] != o[field]:
                            change(field, key, old[field], o[field])
                else:
                    change("order_first_observed", key, None, o["status"])
                if o["exchangeOrderId"] is not None:
                    exchange_ids.setdefault(key, o["exchangeOrderId"])
                max_filled[key] = max(max_filled.get(key, 0), o["filled"])
                if o["status"] in TERMINAL:
                    terminals.setdefault(key, o)
                # Keep initial identity separate from the latest observed quantities/status.
                if old:
                    for field in ("status", "quantity", "filled", "pending", "cancelled", "averagePrice", "exchangeOrderId", "at"):
                        old[field] = o[field]
                else:
                    known_orders[key] = dict(o)
            for key, t in now_trades.items():
                if key in known_trades:
                    if known_trades[key] != t:
                        issue("previous_execution_changed", t["orderId"])
                else:
                    known_trades[key] = t
                    change("execution_first_observed", t["orderId"], None, t["tradeId"])
            stable_seen = True
        issue_total += issue_count + inspection["issueCount"]
        temporal = {"status":"unverified" if inspection["status"] == "changing" else "issues" if issue_count else "compared" if comparable else "baseline",
            "issueCount":issue_count, "issues":issues, "changeCount":change_count, "changes":changes,
            "dayBoundary":boundary, "unresolvedPriorDayOrders":unresolved,
            "gapSeconds":(timestamp(c["startedAt"])-timestamp(previous["finishedAt"])).total_seconds() if previous else None}
        yield {"sequence":sequence, "capture":c, "inspection":inspection, "temporal":temporal, "cumulativeIssueCount":issue_total, "day":day}
        previous = c


def entry_hash(journal_id, sequence, previous_hash, capture_sha):
    return digest({"journalId":journal_id,"sequence":sequence,"previousHash":previous_hash,"captureSha256":capture_sha})


class BrokerJournal:
    def __init__(self, path: Path, tenant: str | None = None, account_ref: str | None = None, *, readonly=False):
        self.path = Path(path)
        if self.path.is_symlink():
            raise BrokerReadError("Broker journal symlinks are unsupported")
        if readonly:
            self.db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro",uri=True)
            self.db.execute("PRAGMA query_only=ON")
        else:
            identifier(tenant,"tenant ID")
            if not isinstance(account_ref,str) or not re.fullmatch(r"[a-f0-9]{64}",account_ref):
                raise BrokerReadError("Explicit account reference required")
            # Create privately without replacing an existing path.
            try:
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                os.close(fd)
            except FileExistsError:
                # Reject foreign/corrupt databases before changing their journal mode.
                self.db=sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro",uri=True)
                try:
                    self.db.execute("PRAGMA query_only=ON")
                    self.db.execute("BEGIN")
                    self._load(tenant,account_ref)
                finally:
                    self.db.close()
            self.db = sqlite3.connect(self.path,timeout=10,isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    self.db.execute("CREATE TABLE broker_journal_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,journal_id TEXT NOT NULL,tenant TEXT NOT NULL,account_ref TEXT NOT NULL)")
                    self.db.execute("CREATE TABLE broker_captures(sequence INTEGER PRIMARY KEY,previous_hash TEXT,entry_hash TEXT NOT NULL UNIQUE,capture_sha TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)")
                    self.db.execute("INSERT INTO broker_journal_meta VALUES (1,1,?,?,?)",(uuid.uuid4().hex,tenant,account_ref))
                    for table in sorted(TABLES):
                        for verb in ("UPDATE","DELETE"):
                            self.db.execute(f"CREATE TRIGGER {table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Broker observations are append-only'); END")
                self._load(tenant,account_ref)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                self.db.close()
                raise
        try:
            self.db.execute("BEGIN")
            self.meta,_=self._load(tenant,account_ref)
            self.db.rollback()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _load(self,tenant=None,account_ref=None):
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != TABLES:
            raise BrokerReadError("Broker journal schema mismatch")
        meta=self.db.execute("SELECT id,version,journal_id,tenant,account_ref FROM broker_journal_meta").fetchall()
        if len(meta)!=1 or meta[0][:2]!=(1,1) or not re.fullmatch(r"[a-f0-9]{32}",meta[0][2]):
            raise BrokerReadError("Invalid broker journal identity")
        _,_,journal_id,stored_tenant,ref=meta[0]
        identifier(stored_tenant,"tenant ID")
        if tenant is not None and tenant!=stored_tenant or account_ref is not None and account_ref!=ref:
            raise BrokerReadError("Broker journal account binding mismatch")
        if not re.fullmatch(r"[a-f0-9]{64}",ref):
            raise BrokerReadError("Invalid broker journal account reference")
        count,size=self.db.execute("SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM broker_captures").fetchone()
        if count>MAX_ENTRIES or size>MAX_BYTES:
            raise BrokerReadError("Broker journal exceeds replay bounds; preserve and archive before selecting a new journal")
        entries,previous_hash,last_end=[],None,None
        for seq,prev,entry,sha,payload in self.db.execute("SELECT sequence,previous_hash,entry_hash,capture_sha,payload FROM broker_captures ORDER BY sequence"):
            if seq!=len(entries)+1 or prev!=previous_hash or entry!=entry_hash(journal_id,seq,prev,sha):
                raise BrokerReadError("Broker journal chain is incomplete or changed")
            c=validate_capture(json.loads(payload),stored_tenant,ref)
            if sha!=c["sha256"] or last_end and timestamp(c["startedAt"])<last_end:
                raise BrokerReadError("Broker journal capture hash or chronology mismatch")
            entries.append(c)
            previous_hash,last_end=entry,timestamp(c["finishedAt"])
        return {"journalId":journal_id,"tenantId":stored_tenant,"accountRef":ref,"headHash":previous_hash,"captureCount":count,"bytes":size},entries

    def append(self,capture,*,now=None):
        now=now or datetime.now(timezone.utc)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            meta,entries=self._load(self.meta["tenantId"],self.meta["accountRef"])
            c=validate_capture(capture,meta["tenantId"],meta["accountRef"])
            if timestamp(c["finishedAt"])>now:
                raise BrokerReadError("Cannot append a future broker capture")
            for index,old in enumerate(entries,1):
                if old["sha256"]==c["sha256"]:
                    self.db.rollback()
                    return {"sequence":index,"duplicate":True,"captureSha256":c["sha256"]}
            if entries and timestamp(c["startedAt"])<timestamp(entries[-1]["finishedAt"]):
                raise BrokerReadError("Capture overlaps or precedes the journal head")
            raw=canonical(c).decode()
            if len(entries)>=MAX_ENTRIES or meta["bytes"]+len(raw.encode())>MAX_BYTES:
                raise BrokerReadError("Broker journal replay bound reached")
            sequence=len(entries)+1
            hashed=entry_hash(meta["journalId"],sequence,meta["headHash"],c["sha256"])
            self.db.execute("INSERT INTO broker_captures VALUES (?,?,?,?,?)",(sequence,meta["headHash"],hashed,c["sha256"],raw))
            self.db.commit()
            return {"sequence":sequence,"duplicate":False,"captureSha256":c["sha256"],"entryHash":hashed}
        except BaseException:
            self.db.rollback()
            raise

    def report(self,sequence=None):
        self.db.execute("BEGIN")
        try:
            meta,entries=self._load(self.meta["tenantId"],self.meta["accountRef"])
            if not entries:
                return {**meta,"history":[],"selected":None}
            selected_sequence=len(entries) if sequence is None else sequence
            if type(selected_sequence) is not int or not 1<=selected_sequence<=len(entries):
                raise BrokerReadError("Capture sequence is unavailable")
            history,selected=[],None
            for item in replay(entries):
                if item["sequence"]==selected_sequence:
                    selected=item
                history.append({k:v for k,v in item.items() if k not in {"capture","temporal","inspection"}} | {
                    "finishedAt":item["capture"]["finishedAt"],"captureSha256":item["capture"]["sha256"],
                    "inspectionStatus":item["inspection"]["status"],"temporalStatus":item["temporal"]["status"],
                    "issueCount":item["temporal"]["issueCount"]+item["inspection"]["issueCount"]})
            return {**meta,"history":history[-100:],"selected":selected,"historyOmitted":max(0,len(history)-100)}
        finally:
            self.db.rollback()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["append","inspect"])
    parser.add_argument("--database",type=Path,required=True)
    parser.add_argument("--tenant",required=True)
    parser.add_argument("--account-ref",required=True)
    parser.add_argument("--capture",type=Path)
    parser.add_argument("--sequence",type=int)
    args=parser.parse_args()
    if args.action=="append" and not args.capture:
        parser.error("append requires --capture")
    journal=BrokerJournal(args.database,args.tenant,args.account_ref,readonly=args.action=="inspect")
    try:
        if args.action=="append":
            with args.capture.open("rb") as source:
                raw=source.read(8_000_001)
            if len(raw)>8_000_000:
                raise BrokerReadError("Capture exceeds 8 MB")
            print(json.dumps(journal.append(json.loads(raw))))
        else:
            print(json.dumps(journal.report(args.sequence),ensure_ascii=False))
    finally:
        journal.close()


if __name__=="__main__":
    main()
