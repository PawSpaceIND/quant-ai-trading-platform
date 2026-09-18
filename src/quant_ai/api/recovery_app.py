"""Explicit recovery-only ASGI application; never installs trading or model routes.

The trusted host supplies existing runtimes, audits and durable credentials. This
factory does not provision accounts, select storage, resume execution or grant keys.
"""
from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from threading import RLock
from types import MappingProxyType

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from quant_ai.api.institutional_recovery import install_recovery_routes
from quant_ai.operations.institutional_operator import IDENTIFIER, InstitutionalRecoveryOperations
from quant_ai.security.api_keys import ApiCredential
from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry
from quant_ai.security.rate_limit import SlidingWindowRateLimiter

MAX_BODY_BYTES = 2048
BODY_TIMEOUT_SECONDS = 5.0
_RECOVERY_POST = re.compile(r"/v1/institutional/programs/[^/]+/recovery")
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _paper_only() -> bool:
    return os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() in {
        "false", "0", "no", ""}


@dataclass(frozen=True)
class _RecoveryState:
    keys: PersistentApiKeyRegistry
    rate_limiter: SlidingWindowRateLimiter
    institutional_recovery: Mapping[str, InstitutionalRecoveryOperations]


class _RecoveryEnvelope:
    """Bound the recognized POST before JSON parsing and redact every envelope error."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [(k, v) for k, v in message.get("headers", [])
                                      if k.lower() not in {b"cache-control", b"x-content-type-options"}]
                message["headers"] += [(k.lower().encode(), v.encode()) for k, v in _HEADERS.items()]
            await send(message)

        if scope["method"] != "POST" or not _RECOVERY_POST.fullmatch(scope["path"]):
            return await self.app(scope, receive, guarded_send)

        async def reject(code, status):
            await JSONResponse({"detail": code}, status_code=status)(scope, receive, guarded_send)

        headers = scope.get("headers", [])
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        media = [v for k, v in headers if k.lower() == b"content-type"]
        if len(lengths) > 1 or lengths and not re.fullmatch(rb"[0-9]{1,10}", lengths[0]):
            return await reject("invalid_recovery_request_length", 400)
        if lengths and int(lengths[0]) > MAX_BODY_BYTES:
            return await reject("recovery_request_too_large", 413)
        if len(media) != 1 or media[0].split(b";", 1)[0].strip().lower() != b"application/json":
            return await reject("recovery_json_required", 415)

        async def read_body():
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                if message["type"] != "http.request":
                    raise ValueError("invalid_recovery_request")
                part = message.get("body", b"")
                if len(body) + len(part) > MAX_BODY_BYTES:
                    raise OverflowError("recovery_request_too_large")
                body.extend(part)
                if not message.get("more_body", False):
                    return bytes(body)

        try:
            body = await asyncio.wait_for(read_body(), timeout=BODY_TIMEOUT_SECONDS)
        except TimeoutError:
            return await reject("recovery_request_timed_out", 408)
        except OverflowError:
            return await reject("recovery_request_too_large", 413)
        except (TypeError, ValueError):
            return await reject("invalid_recovery_request", 400)
        if body is None:
            return None
        if lengths and len(body) != int(lengths[0]):
            return await reject("invalid_recovery_request_length", 400)
        supplied = False

        async def buffered_receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        return await self.app(scope, buffered_receive, guarded_send)


def create_recovery_app(*, keys: PersistentApiKeyRegistry,
                        operations: Mapping[str, InstitutionalRecoveryOperations],
                        rate_limit_per_minute: int = 120) -> FastAPI:
    """Install only health and the three existing, permission-scoped recovery routes.

    Ownership stays with the host: constructing/shutting down this app does not
    construct or close supplied databases. The caller's mapping is copied and frozen.
    Possession credentials are still service-key authority, not verified human identity.
    """
    if (type(keys) is not PersistentApiKeyRegistry or not isinstance(operations, Mapping)
            or not 1 <= len(operations) <= 32 or not _paper_only()
            or type(rate_limit_per_minute) is not int or not 1 <= rate_limit_per_minute <= 10000):
        raise ValueError("recovery_service_configuration_invalid")
    selected = dict(operations)
    if any(type(tenant) is not str or not IDENTIFIER.fullmatch(tenant)
           or type(service) is not InstitutionalRecoveryOperations or service.tenant_id != tenant
           for tenant, service in selected.items()):
        raise ValueError("recovery_service_configuration_invalid")
    state = _RecoveryState(keys, SlidingWindowRateLimiter(rate_limit_per_minute, timedelta(minutes=1)),
                           MappingProxyType(selected))
    limiter_lock = RLock()
    app = FastAPI(title="PRAMANA recorded bookkeeping recovery", docs_url=None,
                  redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.recovery = state
    api_key_header = Header(default=None)

    def authenticated_tenant(x_api_key: str | None = api_key_header) -> ApiCredential:
        if not _paper_only():
            raise HTTPException(503, "recovery_service_paper_only")
        if not x_api_key:
            raise HTTPException(401, "missing_api_key")
        principal = state.keys.authenticate(x_api_key)
        if principal is None:
            raise HTTPException(401, "invalid_api_key")
        if principal.tenant_id not in state.institutional_recovery:
            raise HTTPException(403, "institutional_recovery_not_configured")
        with limiter_lock:
            permitted = state.rate_limiter.allow(principal.tenant_id)
        if not permitted:
            raise HTTPException(429, "rate_limit_exceeded")
        return principal

    install_recovery_routes(app, state, authenticated_tenant)
    app.add_middleware(_RecoveryEnvelope)

    @app.get("/health")
    def health():
        return {"status": "recovery_only", "live_execution_available": False,
                "trading_routes_available": False, "automatic_recovery": False,
                "account_health_verified": False}

    return app
