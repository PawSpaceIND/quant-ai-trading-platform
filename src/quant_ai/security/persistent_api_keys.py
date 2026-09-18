"""Explicit durable possession credentials; no deployed account is selected here.

Only hashes are stored. Secrets are returned once at issuance. The existing default
in-memory registry is unchanged; selecting this store requires server configuration.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock
from uuid import uuid4

from quant_ai.security.api_keys import ApiCredential, ApiKeyRegistry

SCHEMA = "pramana.operator_credentials.v1"
SCOPES = frozenset({"institutional.recovery.read", "institutional.recovery.apply"})
MAX_GRANTS = 10000
MAX_LIFETIME = timedelta(days=365)
MAX_PAYLOAD = 4096
ID = re.compile(r"[a-zA-Z0-9._:-]{1,180}")
KEY_ID = re.compile(r"[a-f0-9]{16}")
HASH = re.compile(r"[a-f0-9]{64}")
RAW_KEY = re.compile(r"[a-zA-Z0-9_-]{43}")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class CredentialStoreError(ValueError):
    """Bounded non-secret refusal; no raw database errors or credentials."""


def _require(condition, code="credential_store_unavailable"):
    if not condition:
        raise CredentialStoreError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _moment(value):
    _require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None,
             "credential_aware_time_required")
    return value.astimezone(timezone.utc)


def _parse_time(value):
    _require(type(value) is str and len(value) <= 40)
    decoded = _moment(datetime.fromisoformat(value))
    _require(decoded.isoformat() == value)
    return decoded


def _scopes(value):
    _require(isinstance(value, (tuple, list, set, frozenset))
             and all(type(v) is str and v in SCOPES for v in value), "invalid_api_key_scopes")
    return frozenset(value)


def _microseconds(value):
    delta = value - EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    _require(0 <= result <= 2**63 - 1, "credential_clock_invalid")
    return result


class PersistentApiKeyRegistry(ApiKeyRegistry):
    """Opt-in SQLite grants, explicit expiry and cross-connection revocation.

    Creation is exclusive and explicit. This is local integrity, not a remote
    identity service, encrypted secret store or rollback-proof authorization.
    """

    def __init__(self, path: str | Path, *, create: bool = False,
                 clock: Callable[[], datetime] | None = None):
        _require(type(create) is bool, "credential_store_creation_flag_invalid")
        _require(isinstance(path, (str, Path)) and str(path).strip() == str(path)
                 and bool(str(path)) and str(path) != ":memory:", "credential_store_path_required")
        _require(clock is None or callable(clock), "credential_clock_invalid")
        self.path = Path(path).absolute()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = RLock()
        self._closed = False
        self.db = None
        try:
            if create:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            info = self.path.lstat()
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                     and stat.S_IMODE(info.st_mode) & 0o077 == 0)
            self._file_identity = (info.st_dev, info.st_ino)
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True,
                                     check_same_thread=False, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("PRAGMA synchronous=FULL")
            if create:
                self._create_schema()
            self._store_id = self._metadata()[0]
            with self._transaction():
                count = self.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0]
                _require(count <= MAX_GRANTS)
                for row in self.db.execute("SELECT * FROM operator_credential_grants"):
                    self._decode(row)
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RuntimeError):
            if self.db is not None:
                self.db.close()
            self._closed = True
            raise CredentialStoreError("credential_store_unavailable") from None

    def _create_schema(self):
        with self.db:
            self.db.execute("""CREATE TABLE operator_credential_meta(
                id INTEGER PRIMARY KEY CHECK(id=1),schema TEXT NOT NULL,
                store_id TEXT NOT NULL,last_observed_us INTEGER NOT NULL)""")
            self.db.execute("INSERT INTO operator_credential_meta VALUES(1,?,?,0)", (SCHEMA, uuid4().hex))
            self.db.execute("""CREATE TABLE operator_credential_grants(
                key_id TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                revoked_at TEXT)""")
            self.db.execute("""CREATE TRIGGER credential_identity_immutable
                BEFORE UPDATE OF id,schema,store_id ON operator_credential_meta
                BEGIN SELECT RAISE(ABORT,'Credential identity is immutable'); END""")
            self.db.execute("""CREATE TRIGGER credential_clock_monotone
                BEFORE UPDATE OF last_observed_us ON operator_credential_meta
                WHEN NEW.last_observed_us < OLD.last_observed_us
                BEGIN SELECT RAISE(ABORT,'Credential clock cannot move backwards'); END""")
            self.db.execute("""CREATE TRIGGER credential_grant_immutable
                BEFORE UPDATE OF key_id,payload,payload_sha256 ON operator_credential_grants
                BEGIN SELECT RAISE(ABORT,'Credential grant is immutable'); END""")
            self.db.execute("""CREATE TRIGGER credential_revocation_final
                BEFORE UPDATE OF revoked_at ON operator_credential_grants
                WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
                BEGIN SELECT RAISE(ABORT,'Credential revocation is final'); END""")
            for table in ("operator_credential_meta", "operator_credential_grants"):
                self.db.execute(f"""CREATE TRIGGER {table}_retained BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT,'Credential records are retained'); END""")

    def _metadata(self):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _require(tables == {"operator_credential_meta", "operator_credential_grants"})
        rows = self.db.execute("SELECT id,schema,store_id,last_observed_us FROM operator_credential_meta").fetchall()
        _require(len(rows) == 1)
        row = rows[0]
        _require(type(row[0]) is int and row[0] == 1 and row[1] == SCHEMA
                 and type(row[2]) is str and re.fullmatch(r"[a-f0-9]{32}", row[2])
                 and type(row[3]) is int and 0 <= row[3] <= 2**63 - 1)
        return row[2], row[3]

    def _check_file(self):
        _require(not self._closed and self.db is not None)
        info = self.path.lstat()
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) & 0o077 == 0
                 and (info.st_dev, info.st_ino) == self._file_identity)

    def _rollback(self):
        try:
            if self.db is not None and not self._closed and self.db.in_transaction:
                self.db.rollback()
        except sqlite3.Error:
            pass

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._check_file()
                self.db.execute("BEGIN IMMEDIATE")
                store_id, observed = self._metadata()
                _require(store_id == self._store_id)
                now = _moment(self._clock())
                instant = _microseconds(now)
                _require(instant >= observed, "credential_clock_regressed")
                self.db.execute("UPDATE operator_credential_meta SET last_observed_us=? WHERE id=1", (instant,))
                yield now
                self._check_file()
                self.db.commit()
            except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, RuntimeError) as error:
                self._rollback()
                code = str(error) if isinstance(error, CredentialStoreError) else "credential_store_unavailable"
                raise CredentialStoreError(code) from None
            except BaseException:
                self._rollback()
                raise

    @staticmethod
    def _decode(row):
        raw = row["payload"]
        _require(type(raw) is str and len(raw.encode()) <= MAX_PAYLOAD)
        value = json.loads(raw)
        _require(type(value) is dict and set(value) == {
            "schema", "keyId", "keyHash", "tenant", "scopes", "issuedAt", "expiresAt"}
            and _canonical(value) == raw and sha256(raw.encode()).hexdigest() == row["payload_sha256"])
        _require(value["schema"] == SCHEMA and type(value["keyId"]) is str
                 and KEY_ID.fullmatch(value["keyId"]) and value["keyId"] == row["key_id"]
                 and type(value["keyHash"]) is str and HASH.fullmatch(value["keyHash"])
                 and value["keyHash"][:16] == value["keyId"]
                 and type(value["tenant"]) is str and ID.fullmatch(value["tenant"])
                 and type(value["scopes"]) is list and value["scopes"] == sorted(set(value["scopes"])))
        scopes = _scopes(value["scopes"])
        issued, expires = _parse_time(value["issuedAt"]), _parse_time(value["expiresAt"])
        _require(timedelta(0) < expires - issued <= MAX_LIFETIME)
        revoked = None if row["revoked_at"] is None else _parse_time(row["revoked_at"])
        _require(revoked is None or revoked >= issued)
        return ApiCredential(value["tenant"], value["keyId"], value["keyHash"], scopes), issued, expires, revoked

    def _row(self, key_id):
        row = self.db.execute("SELECT * FROM operator_credential_grants WHERE key_id=?", (key_id,)).fetchone()
        return None if row is None else self._decode(row)

    @staticmethod
    def _usable(record, now):
        return record is not None and record[3] is None and record[1] <= now < record[2]

    def _insert(self, tenant_id, scopes, expires_at, now):
        _require(type(tenant_id) is str and ID.fullmatch(tenant_id), "credential_tenant_invalid")
        selected = _scopes(scopes)
        expiry = _moment(expires_at)
        _require(timedelta(0) < expiry - now <= MAX_LIFETIME, "credential_expiry_invalid")
        _require(self.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0]
                 < MAX_GRANTS, "credential_capacity_exhausted")
        raw = token_urlsafe(32)
        hashed = sha256(raw.encode()).hexdigest()
        principal = ApiCredential(tenant_id, hashed[:16], hashed, selected)
        payload = _canonical({"schema": SCHEMA, "keyId": principal.key_id, "keyHash": hashed,
            "tenant": tenant_id, "scopes": sorted(selected), "issuedAt": now.isoformat(),
            "expiresAt": expiry.isoformat()})
        self.db.execute("INSERT INTO operator_credential_grants VALUES(?,?,?,NULL)",
                        (principal.key_id, payload, sha256(payload.encode()).hexdigest()))
        return raw, principal

    def issue(self, tenant_id: str, *, scopes=(), expires_at=None) -> tuple[str, ApiCredential]:
        with self._transaction() as now:
            result = self._insert(tenant_id, scopes, expires_at, now)
        return result

    def authenticate(self, raw_key: str) -> ApiCredential | None:
        if type(raw_key) is not str or RAW_KEY.fullmatch(raw_key) is None:
            return None
        digest = sha256(raw_key.encode()).hexdigest()
        try:
            with self._transaction() as now:
                record = self._row(digest[:16])
                result = record[0] if (self._usable(record, now)
                    and compare_digest(record[0].key_hash, digest)) else None
            return result
        except CredentialStoreError:
            return None

    def is_active(self, credential: ApiCredential) -> bool:
        if type(credential) is not ApiCredential or type(credential.key_id) is not str:
            return False
        try:
            with self._transaction() as now:
                record = self._row(credential.key_id)
                result = self._usable(record, now) and record[0] == credential
            return bool(result)
        except CredentialStoreError:
            return False

    def revoke(self, key_id: str) -> bool:
        _require(type(key_id) is str and KEY_ID.fullmatch(key_id), "credential_id_invalid")
        with self._transaction() as now:
            row = self._row(key_id)
            changed = row is not None and row[3] is None
            if changed:
                self.db.execute("UPDATE operator_credential_grants SET revoked_at=? WHERE key_id=?",
                                (now.isoformat(), key_id))
        return changed

    def rotate(self, key_id: str, *, expires_at: datetime) -> tuple[str, ApiCredential]:
        _require(type(key_id) is str and KEY_ID.fullmatch(key_id), "credential_id_invalid")
        with self._transaction() as now:
            record = self._row(key_id)
            _require(self._usable(record, now), "credential_rotation_source_unavailable")
            principal = record[0]
            result = self._insert(principal.tenant_id, principal.scopes, expires_at, now)
            self.db.execute("UPDATE operator_credential_grants SET revoked_at=? WHERE key_id=?",
                            (now.isoformat(), key_id))
        return result

    def close(self):
        with self._lock:
            if not self._closed:
                self.db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
