"""Finite diagnostic labels for journal/report boundaries; never copy response text."""
from __future__ import annotations

STATUSES = frozenset({"completed", "invalid_schema", "unavailable", "budget_exhausted",
                      "unverified", "not_requested"})
SCHEMA_FAILURES = frozenset({
    "output_truncated", "model_refusal", "context_limit", "incomplete_turn",
    "completion_unverified", "missing_consensus_tool", "multiple_tool_blocks",
    "unexpected_tool", "tool_input_not_object", "missing_consensus_fields",
    "unknown_consensus_fields", "confidence_out_of_range", "negative_expected_risk",
    "stance_not_string", "unsupported_stance", "rationale_empty", "proof_not_object",
    "proof_fields_invalid", "proof_summary_empty", "invalid_consensus_schema",
    *(f"{field}_{suffix}" for field in ("confidence", "expected_return", "expected_risk")
      for suffix in ("not_numeric", "nonfinite")),
    *(f"{field}_not_string_array" for field in ("rationale", "supporting_factors", "risk_factors")),
})
PROVIDER_FAILURES = frozenset({"provider_timeout", "provider_overloaded", "provider_auth",
                             "provider_rate_limited", "provider_unavailable"})


def normalized_diagnostic(status, code) -> tuple[str | None, str | None]:
    if not isinstance(status, str) or status not in STATUSES:
        return None, None
    if status in {"completed", "not_requested"}:
        return status, None
    if status == "budget_exhausted":
        return status, "budget_exhausted"
    if status == "unverified":
        return status, "unverified_inference"
    allowed, fallback = ((SCHEMA_FAILURES, "invalid_consensus_schema") if status == "invalid_schema"
                         else (PROVIDER_FAILURES, "provider_unavailable"))
    return status, code if isinstance(code, str) and code in allowed else fallback
