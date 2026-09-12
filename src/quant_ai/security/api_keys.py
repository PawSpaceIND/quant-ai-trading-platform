from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from secrets import token_urlsafe


@dataclass(frozen=True)
class ApiCredential:
    tenant_id: str
    key_id: str
    key_hash: str


class ApiKeyRegistry:
    def __init__(self) -> None:
        self._credentials: dict[str, ApiCredential] = {}

    def issue(self, tenant_id: str) -> tuple[str, ApiCredential]:
        raw = token_urlsafe(32)
        key_hash = sha256(raw.encode()).hexdigest()
        key_id = key_hash[:16]
        credential = ApiCredential(tenant_id, key_id, key_hash)
        self._credentials[key_id] = credential
        return raw, credential

    def authenticate(self, raw_key: str) -> ApiCredential | None:
        digest = sha256(raw_key.encode()).hexdigest()
        credential = self._credentials.get(digest[:16])
        if credential and credential.key_hash == digest:
            return credential
        return None
