"""Synthetic cross-language event evidence; no live retrieval or credentials."""
import base64
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from test_research_extensions import at, feed

from quant_ai.research.company_events import CompanyEvents


def build(path):
    with closing(CompanyEvents(path)) as store:
        store.ingest(feed("Original disclosure"), observed_at=at(10))
        store.map_company("Infosys Limited", "NSE:INFY", at(11), "SYNTHETIC_REFERENCE")
        store.ingest(feed("Corrected disclosure"), observed_at=at(20))
        store.ingest(feed("Company renamed").replace(b"Infosys Limited", b"Renamed Limited"), observed_at=at(30))
        store.ingest(feed("Conflicting A").replace(b"one", b"conflict"), observed_at=at(40))
        store.ingest(feed("Conflicting B").replace(b"one", b"conflict"), observed_at=at(40))
        for i in range(12):
            raw = feed(f"Synthetic company statement {i}").replace(b"Infosys Limited", f"Example {i:02} Limited".encode()).replace(b"<guid>one</guid>", f"<guid>example-{i}</guid>".encode())
            store.ingest(raw, observed_at=at(50+i))
        store.ingest(feed("Unicode ₹ தமிழ் 🧪").replace(b"<guid>one</guid>", b"<guid>unicode</guid>"), observed_at=at(70))
        def timeout():
            raise httpx.ReadTimeout("PRIVATE_EXCEPTION_SENTINEL")
        store._fetch = timeout
        try:
            store.fetch()
        except httpx.ReadTimeout:
            pass
        # Normalize the synthetic error observation time to make fixture output reproducible.
        row = store.db.execute("SELECT id,body FROM feed_captures WHERE raw IS NULL").fetchone()
        import hashlib

        from quant_ai.research.lab import canonical
        body=json.loads(row[1]);body['observed_at']=at(80); encoded=canonical(body)
        with store.db:
            store.db.execute("UPDATE feed_captures SET id=?,body=? WHERE id=?",(hashlib.sha256(encoded.encode()).hexdigest(),encoded,row[0]))
    return path


def export(path):
    with closing(sqlite3.connect(path)) as db:
        result={
            'event_revisions':db.execute('SELECT digest,body FROM event_revisions ORDER BY digest').fetchall(),
            'feed_captures':[[ident,body,base64.b64encode(raw).decode() if raw else None] for ident,body,raw in db.execute('SELECT id,body,raw FROM feed_captures ORDER BY id')],
            'symbol_mappings':db.execute('SELECT title,verified_at,symbol,provenance FROM symbol_mappings ORDER BY title,verified_at').fetchall(),
        }
    with closing(CompanyEvents(path,readonly=True)) as store:
        result['pythonAsOfSources']={at(i):store.sources_as_of('NSE:INFY',at(i)) for i in (10,12,25,35,45,90)}
    return result


if __name__ == '__main__':
    with TemporaryDirectory() as temporary:
        result=export(build(Path(temporary)/'events.sqlite'))
        Path(sys.argv[1]).write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n')
