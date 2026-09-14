"""NSE announcement evidence with first-seen timestamps and explicit symbol mapping.

RSS summaries are company disclosures, not independently verified economic facts.
No attachment execution, arbitrary URL fetches, or inferred ticker mappings.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from quant_ai.research.lab import canonical, identity, instant

NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
ALLOWED_HOSTS = {"www.nseindia.com", "nsearchives.nseindia.com", "archives.nseindia.com"}
MAX_BYTES = 5_000_000


def official_link(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


def published_time(value):
    try:
        return instant(value)
    except (ValueError, TypeError):
        pass
    try:
        result = parsedate_to_datetime(value)
        if result.tzinfo is not None:
            return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    # NSE's local exchange timestamps explicitly use India Standard Time.
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return (
                datetime.strptime(value, fmt)
                .replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                .astimezone(timezone.utc)
            )
        except ValueError:
            continue
    raise ValueError("unrecognized_publication_time")


def parse_feed(raw, observed_at):
    observed = instant(observed_at)
    if len(raw) > MAX_BYTES or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unsafe_or_oversized_xml")
    root = ElementTree.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("rss_required")
    items, rejected = [], []
    for item in root.findall("./channel/item"):
        text = lambda name, item=item: (item.findtext(name) or "").strip()
        try:
            title, link = identity(text("title")), text("link")
            if not official_link(link):
                raise ValueError("non_official_attachment_link")
            published = published_time(text("pubDate"))
            if published > observed:
                raise ValueError("future_publication")
            body = {
                "title": title,
                "source_url": link,
                "published_at": published.isoformat(),
                "description": text("description"),
                "guid": text("guid") or link,
            }
            digest = hashlib.sha256(canonical(body).encode()).hexdigest()
            items.append(
                {
                    **body,
                    "revision_sha256": digest,
                    "first_seen_at": observed.isoformat(),
                    "feed_url": NSE_ANNOUNCEMENTS,
                    "verification": "official_feed_disclosure_unverified_content",
                }
            )
        except (ValueError, TypeError) as exc:
            rejected.append(
                {
                    "reason": str(exc),
                    "item_sha256": hashlib.sha256(ElementTree.tostring(item)).hexdigest(),
                }
            )
    return items, rejected


class CompanyEvents:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        tables = {
            r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables and tables != {"event_revisions", "feed_captures", "symbol_mappings"}:
            self.db.close()
            raise ValueError("not_an_event_database")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS event_revisions (digest TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feed_captures (id TEXT PRIMARY KEY, body TEXT NOT NULL, raw BLOB);
            CREATE TABLE IF NOT EXISTS symbol_mappings (title TEXT NOT NULL, verified_at TEXT NOT NULL,
                symbol TEXT NOT NULL, provenance TEXT NOT NULL, PRIMARY KEY(title,verified_at));
        """)

    def close(self):
        self.db.close()

    def map_company(self, exact_title, symbol, verified_at, provenance):
        identity(exact_title)
        identity(provenance)
        if not symbol.startswith("NSE:") or len(symbol) <= 4:
            raise ValueError("nse_symbol_required")
        at = instant(verified_at).isoformat()
        with self.db:
            self.db.execute(
                "INSERT INTO symbol_mappings VALUES (?,?,?,?)",
                (exact_title, at, symbol, provenance),
            )

    def ingest(self, raw, *, observed_at, capture_kind="imported"):
        if capture_kind not in ("imported", "direct_https"):
            raise ValueError("invalid_capture_kind")
        items, rejected = parse_feed(raw, observed_at)
        raw_hash = hashlib.sha256(raw).hexdigest()
        capture_id = hashlib.sha256(
            (raw_hash + instant(observed_at).isoformat()).encode()
        ).hexdigest()
        summary = {
            "feed_url": NSE_ANNOUNCEMENTS,
            "observed_at": instant(observed_at).isoformat(),
            "raw_sha256": raw_hash,
            "accepted": len(items),
            "rejected": rejected,
            "capture_kind": capture_kind,
            "status": "ok",
        }
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO feed_captures VALUES (?,?,?)",
                (capture_id, canonical(summary), raw),
            )
            for item in items:
                item["capture_kind"] = capture_kind
                self.db.execute(
                    "INSERT OR IGNORE INTO event_revisions VALUES (?,?)",
                    (item["revision_sha256"], canonical(item)),
                )
        return summary

    def fetch(self):
        try:
            return self._fetch()
        except (httpx.HTTPError, OSError, ValueError, ElementTree.ParseError) as exc:
            body = {
                "feed_url": NSE_ANNOUNCEMENTS,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error_type": type(exc).__name__,
                "capture_kind": "direct_https",
            }
            if isinstance(exc, httpx.HTTPStatusError):
                body["http_status"] = exc.response.status_code
            encoded = canonical(body)
            with self.db:
                self.db.execute(
                    "INSERT INTO feed_captures VALUES (?,?,NULL)",
                    (hashlib.sha256(encoded.encode()).hexdigest(), encoded),
                )
            raise

    def _fetch(self):
        # Fixed public endpoint, TLS verification enabled, no cookies/login or redirects.
        with (
            httpx.Client(timeout=15, follow_redirects=False) as client,
            client.stream("GET", NSE_ANNOUNCEMENTS) as response,
        ):
            response.raise_for_status()
            if "xml" not in response.headers.get("content-type", "").lower():
                raise ValueError("xml_response_required")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("feed_too_large")
                chunks.append(chunk)
        return self.ingest(
            b"".join(chunks),
            observed_at=datetime.now(timezone.utc).isoformat(),
            capture_kind="direct_https",
        )

    def sources_as_of(self, symbol, decision_at):
        """Only known-at-the-time revisions AND mappings become model evidence."""
        cutoff = instant(decision_at)
        mappings = {}
        for title, at, mapped, provenance in self.db.execute("SELECT * FROM symbol_mappings"):
            when = instant(at)
            if when <= cutoff and (title not in mappings or when > mappings[title][0]):
                mappings[title] = (when, mapped, provenance)
        latest = {}
        for (encoded,) in self.db.execute("SELECT body FROM event_revisions"):
            event = json.loads(encoded)
            original = {
                key: event[key]
                for key in ("title", "source_url", "published_at", "description", "guid")
            }
            if hashlib.sha256(canonical(original).encode()).hexdigest() != event["revision_sha256"]:
                raise ValueError("event_integrity_failure")
            seen = instant(event["first_seen_at"])
            if seen > cutoff or instant(event["published_at"]) > cutoff:
                continue
            mapping = mappings.get(event["title"])
            if mapping is None or mapping[1] != symbol:
                continue
            key = event["guid"]
            # Corrections with the same GUID supersede only from their own first-seen time.
            if key in latest and instant(latest[key]["first_seen_at"]) >= seen:
                continue
            latest[key] = {
                **event,
                "mapping_at": mapping[0].isoformat(),
                "mapping_provenance": mapping[2],
            }
        return [
            {
                "id": "nse-event:" + e["revision_sha256"],
                "available_at": max(
                    instant(e["first_seen_at"]), instant(e["mapping_at"])
                ).isoformat(),
                "provenance": e["source_url"],
                "data": e,
            }
            for e in sorted(
                latest.values(), key=lambda e: (e["first_seen_at"], e["revision_sha256"])
            )
        ]

    def status(self):
        captures = [json.loads(row[0]) for row in self.db.execute("SELECT body FROM feed_captures")]
        latest = max(captures, key=lambda c: instant(c["observed_at"]), default=None)
        return {
            "last_capture": latest,
            "revisions": self.db.execute("SELECT count(*) FROM event_revisions").fetchone()[0],
            "captures": self.db.execute("SELECT count(*) FROM feed_captures").fetchone()[0],
            "mappings": self.db.execute("SELECT count(*) FROM symbol_mappings").fetchone()[0],
            "coverage": "NSE announcement RSS only; not a complete corporate-actions database",
        }
