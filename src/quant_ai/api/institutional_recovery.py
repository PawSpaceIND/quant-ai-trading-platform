"""Opt-in authenticated endpoints for existing recorded paper-bookkeeping recovery."""
from __future__ import annotations

import sqlite3

from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from quant_ai.operations.institutional_operator import (
    APPLY,
    READ,
    InstitutionalRecoveryOperations,
    OperatorRecoveryError,
)
from quant_ai.security.api_keys import ApiCredential


class RecoveryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: StrictStr = Field(min_length=1, max_length=180)
    expected_context_sha256: StrictStr = Field(min_length=64, max_length=64)
    confirmation: StrictStr = Field(min_length=1, max_length=80)


def install_recovery_routes(app, state, authenticated_tenant):
    authentication = Depends(authenticated_tenant)

    @app.middleware("http")
    async def recovery_cache_control(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/institutional/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def sanitized_recovery_validation(request, error):
        if request.url.path.startswith("/v1/institutional/"):
            return JSONResponse({"detail": "invalid_institutional_recovery_request"}, status_code=422,
                                headers={"Cache-Control": "no-store"})
        return await request_validation_exception_handler(request, error)

    def current_principal(service, credential, permission, call):
        # Authentication at request arrival is not enough if the handler waits for
        # a trading/recovery operation to release the shared runtime lock.
        with service.runtime._route_lock:
            if not state.keys.is_active(credential):
                raise OperatorRecoveryError(401, "invalid_api_key")
            if selected(credential, permission) is not service:
                raise OperatorRecoveryError(503, "institutional_recovery_configuration_changed")
            return call()

    def selected(credential, permission):
        if permission not in credential.scopes:
            raise HTTPException(403, "institutional_recovery_permission_required")
        service = state.institutional_recovery.get(credential.tenant_id)
        if service is None:
            raise HTTPException(503, "institutional_recovery_not_configured")
        if (type(service) is not InstitutionalRecoveryOperations
                or service.tenant_id != credential.tenant_id):
            raise HTTPException(503, "institutional_recovery_configuration_invalid")
        return service

    def invoke(request, response, call):
        response.headers["Cache-Control"] = "no-store"
        if request.query_params:
            raise HTTPException(400, "institutional_recovery_query_not_supported")
        try:
            return call()
        except OperatorRecoveryError as error:
            raise HTTPException(error.status_code, error.code, headers={"Cache-Control": "no-store"}) from None
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, ArithmeticError, sqlite3.Error):
            # Never echo paths, raw SQL, provider messages or another tenant's state.
            raise HTTPException(503, "institutional_recovery_unavailable",
                                headers={"Cache-Control": "no-store"}) from None

    @app.get("/v1/institutional/programs/{program_id}/recovery")
    def inspect_program(program_id: str, request: Request, response: Response,
                        credential: ApiCredential = authentication):
        service = selected(credential, READ)
        return invoke(request, response, lambda: current_principal(service, credential, READ,
            lambda: service.inspect_program(program_id, credential)))

    @app.post("/v1/institutional/programs/{program_id}/recovery")
    def apply(program_id: str, payload: RecoveryPayload, request: Request, response: Response,
              credential: ApiCredential = authentication):
        service = selected(credential, APPLY)
        return invoke(request, response, lambda: current_principal(service, credential, APPLY,
            lambda: service.apply(program_id, request_id=payload.request_id,
                context_sha256=payload.expected_context_sha256,
                confirmation=payload.confirmation, credential=credential)))

    @app.get("/v1/institutional/recovery-requests/{request_id}")
    def request_status(request_id: str, request: Request, response: Response,
                       credential: ApiCredential = authentication):
        service = selected(credential, READ)
        return invoke(request, response, lambda: current_principal(service, credential, READ,
            lambda: service.request_status(request_id, credential)))
