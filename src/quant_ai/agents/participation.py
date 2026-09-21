"""Finite, durable participation reasons. Percentages alone cannot explain abstention."""
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance


def participation(evidence: AgentEvidence) -> dict[str, str]:
    reasons = evidence.rationale
    code = reasons[0].split(";", 1)[0] if reasons else ""
    role = "gate" if evidence.domain in {AgentDomain.RISK, AgentDomain.LIQUIDITY} else "directional"
    if evidence.agent_id == "etf-value-reference":
        role = "reference"
    if code in {"non_us_market", "non_india_market", "non_equity_instrument",
                "non_equity_macro_model", "non_etf_instrument"}:
        status, reason = "not_applicable", code
    elif code.startswith("etf_reference_"):
        status = "observed" if code == "etf_reference_observed" else "missing_data"
        reason = code if code in {"etf_reference_observed", "etf_reference_unconfigured",
            "etf_reference_missing", "etf_reference_invalid", "etf_reference_stale",
            "etf_reference_quote_unavailable"} else "etf_reference_invalid"
    elif code in {"india_fundamentals_insufficient", "us_fundamentals_insufficient",
                  "insufficient_price_history"}:
        status, reason = "missing_data", code
    elif "capital_preservation_stale_or_missing_data" in reasons:
        status, reason = "stale_or_missing", "required_source_unavailable"
    elif code.startswith(("liquidity_unobserved:", "risk_unobserved:")):
        status, reason = "missing_data", "desk_inputs_missing"
    elif evidence.confidence == 0:
        status, reason = "abstained", "zero_conviction"
    elif evidence.stance is Stance.AVOID:
        status, reason = "veto", "specialist_veto"
    elif evidence.stance is Stance.NEUTRAL:
        status, reason = "ready", "gate_clear" if role == "gate" else "informed_neutral"
    else:
        status, reason = "ready", "directional_view"
    return {"participation": status, "reason_code": reason, "role": role}
