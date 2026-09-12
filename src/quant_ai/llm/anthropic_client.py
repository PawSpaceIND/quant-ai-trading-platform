from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from anthropic import AsyncAnthropic

from quant_ai.agents.contracts import Stance

DEFAULT_MODEL = "claude-sonnet-4-6"
TOOL_NAME = "trading_consensus"


@dataclass(frozen=True)
class TradeSignal:
    stance: Stance
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    rationale: tuple[str, ...]

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("confidence must be between 0 and 1")
        if self.expected_risk < 0:
            raise ValueError("expected_risk must be non-negative")


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
    ) -> None:
        key = (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        if not key and client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic swarm consensus")
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = client or AsyncAnthropic(api_key=key)

    async def generate_trading_consensus(self, prompt: str) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1200,
            temperature=0,
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
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    self.parse_consensus(payload)
                    return payload
        raise ValueError("Anthropic response did not contain structured trading consensus")

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
            raise ValueError(f"consensus payload missing fields: {sorted(missing)}")
        proof = payload["xai_proof"]
        if not isinstance(proof, dict):
            raise TypeError("xai_proof must be an object")
        signal = TradeSignal(
            Stance(str(payload["stance"])),
            Decimal(str(payload["confidence"])),
            Decimal(str(payload["expected_return"])),
            Decimal(str(payload["expected_risk"])),
            tuple(str(item) for item in payload["rationale"]),
        )
        xai = XAIProof(
            self.model,
            str(proof.get("summary", "")),
            tuple(str(item) for item in proof.get("supporting_factors", ())),
            tuple(str(item) for item in proof.get("risk_factors", ())),
        )
        if not xai.summary:
            raise ValueError("xai_proof.summary is required")
        return signal, xai


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
