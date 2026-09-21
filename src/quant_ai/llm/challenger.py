"""Same-evidence challenger, retained inside the existing durable decision proof.

The primary result alone reaches Atlas. The challenger cannot vote, replace a failed
primary, place an order, or promote itself. Comparisons are forward observations,
not evidence of profitable trading until linked to subsequently observed outcomes.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from quant_ai.llm.anthropic_client import ConsensusSchemaError
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.openai_client import OpenAIConsensusClient
from quant_ai.llm.provenance import ConsensusPayload, content_hash


class ChallengerConsensusClient:
    def __init__(self, primary, challenger):
        self.primary, self.challenger = primary, challenger
        self.model = primary.model
        self.budget = primary.budget

    def parse_consensus(self, payload):
        return self.primary.parse_consensus(payload)

    async def score_headlines(self, subject, headlines):
        return await self.primary.score_headlines(subject, headlines)

    async def generate_trading_consensus(self, prompt):
        pair = {"schema": "pramana.model_comparison.v1", "pair_id": uuid4().hex,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "prompt_sha256": content_hash(prompt), "execution_authority": "primary_only",
                "outcome_status": "not_evaluated", "confidence_is_calibrated_probability": False}
        # Same immutable input, no new data fetch and no view of the other model's answer.
        # Primary is scheduled first so its reservation has priority under the shared cap.
        primary, challenger = await asyncio.gather(
            self.primary.generate_trading_consensus(prompt),
            self._observe_challenger(prompt), return_exceptions=True,
        )
        pair["primary"] = _observation(primary)
        pair["challenger"] = _observation(challenger)
        if isinstance(primary, ConsensusSchemaError):
            primary.provenance = {**(primary.provenance or {}), "model_comparison": pair}
            raise primary
        if isinstance(primary, BaseException):
            raise primary
        # Preserve every primary field and status, including unavailable/budget refusals.
        provenance = getattr(primary, "provenance", {"status": "unverified"})
        return ConsensusPayload(dict(primary), {**provenance, "model_comparison": pair})

    async def _observe_challenger(self, prompt):
        try:
            result = await self.challenger.generate_trading_consensus(prompt)
            if getattr(result, "provenance", {}).get("status") == "completed":
                self.challenger.parse_consensus(result)
            return result
        except ConsensusSchemaError as error:
            return error
        except Exception:  # noqa: BLE001 - a research failure must never change the primary decision
            return ConsensusSchemaError("Challenger unavailable", {
                "status": "unavailable", "provider": "openai",
                "requested_model": self.challenger.model, "failure_code": "provider_unavailable",
            })


def _observation(result):
    provenance = getattr(result, "provenance", {}) or {}
    fields = ("provider", "requested_model", "resolved_model", "response_id", "status",
              "failure_code", "started_at", "completed_at", "duration_ms", "usage",
              "prompt_sha256", "request_sha256", "system_sha256", "response_payload_sha256")
    record = {field: provenance.get(field) for field in fields}
    if isinstance(result, dict) and provenance.get("status") == "completed":
        record["consensus"] = dict(result)
    return record


class _ChallengerBudget:
    """Small persistent pilot allowance inside the account-wide budget, never extra spend."""
    def __init__(self, shared):
        if shared.database == ":memory:":
            raise ValueError("astra_challenger_requires_durable_budget")
        self.shared = shared
        path = Path(shared.database).with_name(Path(shared.database).name + ".astra-shadow")
        self.sample = SqliteAIBudget(path, daily_call_limit=20, daily_token_limit=300_000)

    def reserve(self, scope, token_reservation=0):
        return (self.sample.reserve(scope, token_reservation)
                and self.shared.reserve(scope, token_reservation))

    def record(self, scope, usage, *, token_reservation=0):
        self.sample.record(scope, usage, token_reservation=token_reservation)
        self.shared.record(scope, usage, token_reservation=token_reservation)


def with_astra_challenger(primary, budget):
    """Off by default; a present API key alone does not activate or promote Astra."""
    enabled = os.getenv("PRAMANA_ASTRA_SHADOW_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise ValueError("astra_shadow_enabled_must_be_true_or_false")
    if enabled == "false":
        return primary
    if budget is None:
        raise ValueError("astra_challenger_requires_budget")
    challenger = OpenAIConsensusClient(budget=_ChallengerBudget(budget))
    return ChallengerConsensusClient(primary, challenger)
