"""Canonical, versioned edge-policy authority for a single paper execution program.

This binds declared policy and request identity, not reviewer authentication, live
permission, a shared account budget or the truth of market evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields
from decimal import Decimal

from quant_ai.decision.edge import EdgePolicy
from quant_ai.domain.models import Side
from quant_ai.orders.intent import order_from_snapshot

SCHEMA = "pramana.execution_risk_authority.v1"
ENGINE = "calibrated-edge.after-cost-kelly.parent-max-stop-adverse.v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,180}")
MAX_BYTES = 8192


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def authority_digest(raw: str) -> str:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
        raise ValueError("execution_risk_authority_payload_invalid")
    return hashlib.sha256(raw.encode()).hexdigest()


def policy_values(policy: EdgePolicy) -> dict:
    if not isinstance(policy, EdgePolicy):
        raise TypeError("execution_risk_policy_type_invalid")
    values = {}
    for field in fields(EdgePolicy):
        value = getattr(policy, field.name)
        if field.name == "min_resolved_samples":
            if type(value) is not int or not 0 < value <= 2**53 - 1:
                raise ValueError("execution_risk_policy_sample_invalid")
            values[field.name] = value
        else:
            if not isinstance(value, Decimal) or not value.is_finite() or len(str(value)) > 128:
                raise ValueError("execution_risk_policy_number_invalid")
            values[field.name] = str(value)
    # Revalidate even when callers bypassed the frozen dataclass's normal constructor.
    _restore_policy(values)
    return values


def _restore_policy(values) -> EdgePolicy:
    if not isinstance(values, dict) or set(values) != {f.name for f in fields(EdgePolicy)}:
        raise ValueError("execution_risk_policy_fields_invalid")
    decoded = {}
    for name, value in values.items():
        if name == "min_resolved_samples":
            if type(value) is not int or not 0 < value <= 2**53 - 1:
                raise ValueError("execution_risk_policy_sample_invalid")
            decoded[name] = value
        else:
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
                raise ValueError("execution_risk_policy_number_invalid")
            try:
                decoded[name] = Decimal(value)
            except ArithmeticError as error:
                raise ValueError("execution_risk_policy_number_invalid") from error
            if str(decoded[name]) != value:
                raise ValueError("execution_risk_policy_number_noncanonical")
    try:
        return EdgePolicy(**decoded)
    except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
        raise ValueError("execution_risk_policy_invalid") from error


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("execution_risk_authority_duplicate_key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ValueError("execution_risk_authority_nonfinite_json")


def parse_authority(raw: str) -> dict:
    authority_digest(raw)
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, RecursionError) as error:
        raise ValueError("execution_risk_authority_json_invalid") from error
    expected = {"schema", "engine", "programId", "tenantId", "requestSha256",
                "parentSha256", "mode", "policy"}
    if not isinstance(value, dict) or set(value) != expected or _canonical(value) != raw:
        raise ValueError("execution_risk_authority_fields_invalid")
    if value["schema"] != SCHEMA or value["engine"] != ENGINE:
        raise ValueError("execution_risk_authority_version_unsupported")
    for name in ("programId", "tenantId"):
        if not isinstance(value[name], str) or not _ID.fullmatch(value[name]):
            raise ValueError("execution_risk_authority_identity_invalid")
    for name in ("requestSha256", "parentSha256"):
        if not isinstance(value[name], str) or not _SHA.fullmatch(value[name]):
            raise ValueError("execution_risk_authority_digest_invalid")
    if value["mode"] == "ENTRY":
        _restore_policy(value["policy"])
    elif value["mode"] != "COVERED_EXIT" or value["policy"] is not None:
        raise ValueError("execution_risk_authority_mode_invalid")
    return value


def validate_authority(version, raw, *, program_id, tenant_id, parent_payload, runtime_digest):
    if type(version) is not int or version not in (0, 1):
        raise ValueError("execution_risk_authority_version_unsupported")
    if version == 0:
        if raw is not None:
            raise ValueError("execution_risk_authority_legacy_payload_unexpected")
        return None
    value = parse_authority(raw)
    if (value["programId"] != program_id or value["tenantId"] != tenant_id
            or not isinstance(parent_payload, str)
            or value["parentSha256"] != hashlib.sha256(parent_payload.encode()).hexdigest()
            or authority_digest(raw) != runtime_digest):
        raise ValueError("execution_risk_authority_binding_mismatch")
    parent = order_from_snapshot(parent_payload)
    expected_side = Side.BUY if value["mode"] == "ENTRY" else Side.SELL
    if parent.tenant_id != tenant_id or parent.side is not expected_side:
        raise ValueError("execution_risk_authority_parent_mismatch")
    return value


def build_authority(*, program_id, tenant_id, request_sha256, parent_payload,
                    policy: EdgePolicy | None) -> str:
    value = {"schema": SCHEMA, "engine": ENGINE, "programId": program_id,
        "tenantId": tenant_id, "requestSha256": request_sha256,
        "parentSha256": hashlib.sha256(parent_payload.encode()).hexdigest(),
        "mode": "COVERED_EXIT" if policy is None else "ENTRY",
        "policy": None if policy is None else policy_values(policy)}
    raw = _canonical(value)
    validate_authority(1, raw, program_id=program_id, tenant_id=tenant_id,
                       parent_payload=parent_payload, runtime_digest=authority_digest(raw))
    return raw


def bound_policy(raw: str) -> EdgePolicy | None:
    value = parse_authority(raw)
    return None if value["mode"] == "COVERED_EXIT" else _restore_policy(value["policy"])
