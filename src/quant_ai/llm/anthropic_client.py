from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from anthropic import AsyncAnthropic

from quant_ai.agents.contracts import Stance

LOGGER = logging.getLogger("quant_ai.anthropic")

DEFAULT_MODEL = "claude-sonnet-5"
TOOL_NAME = "trading_consensus"
DEFAULT_TIMEOUT_SECONDS = 30.0


class ConsensusSchemaError(ValueError):
    """Raised when an LLM tool payload violates the strict trading schema."""


@dataclass(frozen=True)
class TradeSignal:
    stance: Stance
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    rationale: tuple[str, ...]

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ConsensusSchemaError("confidence must be between 0 and 1")
        if self.expected_risk < 0:
            raise ConsensusSchemaError("expected_risk must be non-negative")


@dataclass(frozen=True)
class XAIProof:
    model: str
    summary: str
    supporting_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]


class AnthropicSwarmClient:
    """Async Anthropic adapter returning validated, structured trading consensus."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        key = (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        if not key and client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic swarm consensus")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self.timeout_seconds = timeout_seconds
        self._client = client or AsyncAnthropic(api_key=key)

    async def generate_trading_consensus(self, prompt: str) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self.model,
                    max_tokens=1200,
                    system=(
                        "You are Pramana's advisory quant consensus engine. Use only the supplied "
                        "market context. Never claim execution capability. Return the structured "
                        "trading_consensus tool payload only."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                    tools=[
                        {
                            "name": TOOL_NAME,
                            "description": "Structured Pramana trading consensus and XAI proof",
                            "input_schema": _consensus_schema(),
                        }
                    ],
                    tool_choice={"type": "tool", "name": TOOL_NAME},
                ),
                timeout=self.timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return self._unavailable_payload("API Timeout")
        except ConsensusSchemaError:
            raise
        except Exception as error:  # noqa: BLE001 - no provider fault may kill the cadence
            # C3: every provider failure degrades to a NEUTRAL consensus. Auth errors, rate
            # limits, 5xx, stale model ids and socket drops must not terminate the daemon —
            # an unavailable opinion is "no opinion", never an exception the cadence cannot
            # survive. The deterministic Atlas path still governs the tick.
            status = getattr(error, "status_code", None)
            if status == 529:
                # Overload is transient unavailability; keep the established timeout label.
                LOGGER.warning("anthropic_consensus_unavailable detail=HTTP 529")
                return self._unavailable_payload("API Timeout")
            label = f"HTTP {status}" if status is not None else type(error).__name__
            LOGGER.warning("anthropic_consensus_unavailable detail=%s", label)
            return self._unavailable_payload(label)

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
                payload = getattr(block, "input", None)
                if not isinstance(payload, dict):
                    raise ConsensusSchemaError("tool payload must be an object")
                self.parse_consensus(payload)
                return payload
        raise ConsensusSchemaError("Anthropic response did not contain structured trading consensus")

    def parse_consensus(self, payload: dict[str, Any]) -> tuple[TradeSignal, XAIProof]:
        required = {
            "stance",
            "confidence",
            "expected_return",
            "expected_risk",
            "rationale",
            "xai_proof",
        }
        missing = required - payload.keys()
        if missing:
            raise ConsensusSchemaError(f"consensus payload missing fields: {sorted(missing)}")
        unknown = payload.keys() - required
        if unknown:
            raise ConsensusSchemaError(f"consensus payload has unknown fields: {sorted(unknown)}")
        stance = payload["stance"]
        if not isinstance(stance, str):
            raise ConsensusSchemaError("stance must be a string")
        rationale = _string_list(payload["rationale"], "rationale", require_nonempty=True)
        proof = payload["xai_proof"]
        if not isinstance(proof, dict):
            raise ConsensusSchemaError("xai_proof must be an object")
        proof_required = {"summary", "supporting_factors", "risk_factors"}
        if set(proof) != proof_required:
            raise ConsensusSchemaError("xai_proof fields do not match strict schema")
        summary = proof["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ConsensusSchemaError("xai_proof.summary is required")
        try:
            parsed_stance = Stance(stance)
        except ValueError as error:
            raise ConsensusSchemaError("stance is not a supported enum value") from error
        signal = TradeSignal(
            parsed_stance,
            _strict_decimal(payload["confidence"], "confidence"),
            _strict_decimal(payload["expected_return"], "expected_return"),
            _strict_decimal(payload["expected_risk"], "expected_risk"),
            rationale,
        )
        xai = XAIProof(
            self.model,
            summary,
            _string_list(proof["supporting_factors"], "supporting_factors"),
            _string_list(proof["risk_factors"], "risk_factors"),
        )
        return signal, xai

    @staticmethod
    def _unavailable_payload(detail: str) -> dict[str, Any]:
        return {
            "stance": "NEUTRAL",
            "confidence": 0.0,
            "expected_return": 0.0,
            "expected_risk": 0.0,
            "rationale": [f"Consensus Skipped: {detail}"],
            "xai_proof": {
                "summary": f"Consensus Skipped: {detail}",
                "supporting_factors": [],
                "risk_factors": ["anthropic_api_unavailable"],
            },
        }

    @classmethod
    def _timeout_payload(cls) -> dict[str, Any]:
        """Retained for callers that assert the original timeout shape."""
        return cls._unavailable_payload("API Timeout")


def _strict_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConsensusSchemaError(f"{field} must be a number")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ConsensusSchemaError(f"{field} must be finite")
    return parsed


def _string_list(value: Any, field: str, *, require_nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConsensusSchemaError(f"{field} must be an array of strings")
    if require_nonempty and not value:
        raise ConsensusSchemaError(f"{field} must not be empty")
    return tuple(value)


def _consensus_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "stance",
            "confidence",
            "expected_return",
            "expected_risk",
            "rationale",
            "xai_proof",
        ],
        "properties": {
            "stance": {"type": "string", "enum": [item.value for item in Stance]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expected_return": {"type": "number"},
            "expected_risk": {"type": "number", "minimum": 0},
            "rationale": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "xai_proof": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary", "supporting_factors", "risk_factors"],
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "supporting_factors": {"type": "array", "items": {"type": "string"}},
                    "risk_factors": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }
