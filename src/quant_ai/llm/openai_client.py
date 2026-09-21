"""Bounded Responses API consensus adapter. No execution or automatic model fallback."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone

import httpx

from quant_ai.llm.anthropic_client import (
    CONSENSUS_SYSTEM,
    AnthropicSwarmClient,
    ConsensusSchemaError,
    _budget_token_reservation,
    _consensus_schema,
    _strict_input_schema,
    consensus_failure_code,
    parse_consensus,
)
from quant_ai.llm.provenance import ConsensusPayload, content_hash
from quant_ai.llm.spend import SpendRefused, initialize, require_reservation, settle

ASTRA_MODEL = "gpt-6-astra"
RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIConsensusClient:
    def __init__(self, *, api_key=None, budget=None, transport=None,
                 timeout_seconds=30.0, max_output_tokens=8192, reasoning_effort="medium"):
        key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required for Astra consensus")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("astra_timeout_invalid")
        if type(max_output_tokens) is not int or not 1200 <= max_output_tokens <= 16384:
            raise ValueError("astra_output_limit_invalid")
        if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("astra_reasoning_effort_invalid")
        initialize()
        self.model = ASTRA_MODEL
        self._key, self._transport = key, transport
        self.budget = budget
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens, self.reasoning_effort = max_output_tokens, reasoning_effort

    def parse_consensus(self, payload):
        return parse_consensus(payload, self.model)

    async def generate_trading_consensus(self, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
            raise ValueError("astra_prompt_invalid")
        request = {
            "model": self.model, "store": False, "service_tier": "default",
            "instructions": CONSENSUS_SYSTEM, "input": prompt,
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": "trading_consensus",
                                "strict": True, "schema": _strict_input_schema(_consensus_schema())}},
        }
        start = time.monotonic()
        provenance = {
            "schema": "pramana.inference.v1", "provider": "openai", "requested_model": self.model,
            "transport": "httpx_mock" if self._transport is not None else "openai_responses_http",
            "resolved_model": None, "response_id": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "request": request, "request_sha256": content_hash(request),
            "prompt_sha256": content_hash(prompt), "system_sha256": content_hash(CONSENSUS_SYSTEM),
            "timeout_seconds": self.timeout_seconds,
        }

        def finish(status, code=None):
            return {**provenance, "status": status,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_ms": round((time.monotonic() - start) * 1000, 3),
                    **({"failure_code": code} if code else {})}

        def unavailable(code, status="unavailable"):
            return ConsensusPayload(AnthropicSwarmClient._unavailable_payload(code, code),
                                    finish(status, code))

        def invalid(code):
            return ConsensusSchemaError("Astra consensus failed validation", finish("invalid_schema", code))

        # Reasoning tokens consume the output ceiling too. Unknown spend retains its
        # reservation; no automatic retries can silently multiply the request budget.
        reservation = _budget_token_reservation({**request, "max_tokens": self.max_output_tokens})
        scope = "astra_consensus"
        if self.budget is not None and not self.budget.reserve(scope, reservation):
            return unavailable("budget_exhausted", "budget_exhausted")

        async def send():
            async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout_seconds,
                                         follow_redirects=False) as client:
                return await client.post(RESPONSES_URL, json=request,
                                         headers={"Authorization": "Bearer " + self._key})

        try:
            spend_ticket = require_reservation(request)
            response = await asyncio.wait_for(send(), self.timeout_seconds)
        except SpendRefused:
            return unavailable("budget_exhausted", "budget_exhausted")
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return unavailable("provider_timeout")
        except httpx.HTTPError:
            return unavailable("provider_unavailable")
        if response.status_code != 200:
            code = {401: "provider_auth", 403: "provider_auth", 429: "provider_rate_limited",
                    503: "provider_overloaded"}.get(response.status_code, "provider_unavailable")
            return unavailable(code)
        try:
            body = response.json()
        except ValueError:
            raise invalid("invalid_consensus_schema") from None
        if not isinstance(body, dict):
            raise invalid("invalid_consensus_schema")
        for source, target in (("model", "resolved_model"), ("id", "response_id")):
            value = body.get(source)
            provenance[target] = value[:200] if isinstance(value, str) else None
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        provenance["usage"] = {
            key: value if type(value := usage.get(key)) is int and value >= 0 else None
            for key in ("input_tokens", "output_tokens")
        }
        settle(spend_ticket, provenance["usage"])
        # OpenAI input_tokens already includes cached input: never add it twice.
        if self.budget is not None:
            self.budget.record(scope, provenance["usage"], token_reservation=reservation)
        if body.get("status") != "completed":
            details = body.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            raise invalid("output_truncated" if reason == "max_output_tokens" else "incomplete_turn")
        if not provenance["resolved_model"] or not provenance["response_id"]:
            raise invalid("completion_unverified")
        blocks = body.get("output")
        if not isinstance(blocks, list):
            raise invalid("invalid_consensus_schema")
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                raise invalid("invalid_consensus_schema")
            if block.get("type") == "reasoning":
                continue
            if block.get("type") != "message" or block.get("role") != "assistant":
                raise invalid("unexpected_tool")
            if block.get("status") != "completed" or not isinstance(block.get("content"), list):
                raise invalid("completion_unverified")
            for item in block["content"]:
                if not isinstance(item, dict):
                    raise invalid("invalid_consensus_schema")
                if item.get("type") == "refusal":
                    raise invalid("model_refusal")
                if item.get("type") != "output_text" or not isinstance(item.get("text"), str):
                    raise invalid("invalid_consensus_schema")
                texts.append(item["text"])
        if len(texts) != 1:
            raise invalid("invalid_consensus_schema")
        try:
            payload = json.loads(texts[0], object_pairs_hook=_unique_object)
        except ValueError:
            raise invalid("invalid_consensus_schema") from None
        if not isinstance(payload, dict):
            raise invalid("tool_input_not_object")
        try:
            self.parse_consensus(payload)
        except ConsensusSchemaError as error:
            raise invalid(consensus_failure_code(error)) from None
        return ConsensusPayload(payload, {
            **finish("completed"), "response_payload_sha256": content_hash(payload),
            "rationale_source": "rationale" if payload.get("rationale") else "xai_summary",
        })


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result
