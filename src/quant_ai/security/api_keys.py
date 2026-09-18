from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from secrets import token_urlsafe


@dataclass(frozen=True)
class ApiCredential:
    tenant_id: str
    key_id: str
    key_hash: str
    scopes: frozenset[str] = frozenset()


class ApiKeyRegistry:
    def __init__(self) -> None:
        self._credentials: dict[str, ApiCredential] = {}

    def issue(self, tenant_id: str, *, scopes=()) -> tuple[str, ApiCredential]:
        allowed = {"institutional.recovery.read", "institutional.recovery.apply"}
        if (not isinstance(scopes, (tuple, list, set, frozenset))
                or any(type(scope) is not str or scope not in allowed for scope in scopes)):
            raise ValueError("invalid_api_key_scopes")
        selected = frozenset(scopes)
        raw = token_urlsafe(32)
        key_hash = sha256(raw.encode()).hexdigest()
        key_id = key_hash[:16]
        credential = ApiCredential(tenant_id, key_id, key_hash, selected)
        self._credentials[key_id] = credential
        return raw, credential

    def authenticate(self, raw_key: str) -> ApiCredential | None:
        digest = sha256(raw_key.encode()).hexdigest()
        credential = self._credentials.get(digest[:16])
        if credential and credential.key_hash == digest:
            return credential
        return None

    def revoke(self, key_id: str) -> bool:
        """Remove an explicitly selected in-memory credential; never return its secret."""
        return self._credentials.pop(key_id, None) is not None

    def is_active(self, credential: ApiCredential) -> bool:
        """Revalidate a previously authenticated principal before a queued operation."""
        return (isinstance(credential, ApiCredential)
                and self._credentials.get(credential.key_id) == credential)
