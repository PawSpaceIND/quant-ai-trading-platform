"""Replay ONE saved consensus request without invoking the trading runtime.

Requires --confirm-paid-call and the existing enabled AI budget. Prints only
bounded response metadata. Does not save a decision, place orders, alter credentials,
restart services or change the model/request. Runs with the currently installed adapter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

MAX_PROOF_BYTES = 2_000_000
MAX_PROOFS = 100
FIELDS = ("stance", "confidence", "expected_return", "expected_risk", "rationale", "xai_proof")
PROOF_FIELDS = ("summary", "supporting_factors", "risk_factors")
STOPS = {"tool_use", "end_turn", "stop_sequence", "max_tokens", "refusal", "pause_turn",
         "model_context_window_exceeded"}
BLOCKS = {"tool_use", "text", "thinking", "redacted_thinking", "server_tool_use"}


class ProbeRefused(RuntimeError):
    """Fixed local error codes; arbitrary exceptions never go to stdout."""


def value_type(value: Any) -> str:
    return {str: "string", int: "integer", float: "number", bool: "boolean",
            dict: "object", list: "array", type(None): "null"}.get(type(value), "other")


def response_shape(response: Any) -> dict:
    stop = getattr(response, "stop_reason", None)
    blocks = getattr(response, "content", ())
    blocks = blocks if isinstance(blocks, (list, tuple)) else ()
    tools = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
    matching = [b for b in tools if getattr(b, "name", None) == "trading_consensus"]
    usage = getattr(response, "usage", None)
    counts = {}
    for field in ("input_tokens", "output_tokens"):
        count = getattr(usage, field, None)
        counts[field] = count if type(count) is int and count >= 0 else None
    shape = {"stop_reason": stop if isinstance(stop, str) and stop in STOPS else "unknown",
             "block_types": [b.type if getattr(b, "type", None) in BLOCKS else "other" for b in blocks[:32]],
             "tool_blocks": len(tools), "matching_tool_blocks": len(matching),
             "response_received": isinstance(getattr(response, "id", None), str) and bool(response.id),
             "usage": counts}
    if len(matching) == 1:
        body = getattr(matching[0], "input", None)
        shape["tool_input_type"] = value_type(body)
        if isinstance(body, dict):
            shape["field_types"] = {f: value_type(body[f]) if f in body else "MISSING" for f in FIELDS}
            shape["unknown_field_count"] = len(set(body) - set(FIELDS))
            proof = body.get("xai_proof")
            if isinstance(proof, dict):
                shape["proof_field_types"] = {f: value_type(proof[f]) if f in proof else "MISSING" for f in PROOF_FIELDS}
    return shape


def load_latest_request(folder: Path, expected_model: str) -> tuple[dict, str]:
    from quant_ai.llm.provenance import content_hash
    candidates = sorted((p for p in folder.glob("*.json") if p.is_file() and not p.is_symlink()),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_PROOFS]
    for path in candidates:
        try:
            with path.open("rb") as source:
                raw = source.read(MAX_PROOF_BYTES + 1)
            if len(raw) > MAX_PROOF_BYTES:
                continue
            doc = json.loads(raw)
            stack = [doc]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if (node.get("schema") == "pramana.inference.v1" and
                            node.get("provider") == "anthropic" and
                            node.get("scope", "consensus") == "consensus"):
                        req = node.get("request")
                        if not isinstance(req, dict) or req.get("model") != expected_model:
                            continue
                        digest = content_hash(req)
                        if digest != node.get("request_sha256"):
                            raise ProbeRefused("saved_request_hash_mismatch")
                        return req, digest
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
        except (OSError, ValueError, RecursionError):
            continue
    raise ProbeRefused("no_matching_saved_consensus_request")


async def run_probe() -> dict:
    from anthropic import AsyncAnthropic

    from quant_ai.llm import anthropic_client as adapter
    from quant_ai.llm.budget import budget_from_env

    if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "").strip().lower() != "false":
        raise ProbeRefused("explicit_paper_mode_required")
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ProbeRefused("anthropic_key_missing")
    folder = Path(os.getenv("PRAMANA_PROOF_DIR") or os.getenv("PRAMANA_XAI_DIR") or "/data/xai")
    model = os.getenv("ANTHROPIC_MODEL", adapter.DEFAULT_MODEL)
    request, digest = load_latest_request(folder, model)
    messages = request.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict):
        raise ProbeRefused("saved_messages_not_single_request")
    prompt = messages[0].get("content")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ProbeRefused("saved_prompt_invalid_or_oversized")
    budget_path = Path(os.getenv("PRAMANA_AI_BUDGET_DB") or "/data/ai-budget.sqlite")
    if not budget_path.is_file():
        raise ProbeRefused("existing_ai_budget_required")
    budget = budget_from_env(budget_path.parent)
    if budget is None:
        raise ProbeRefused("enabled_ai_budget_required")
    result = {"scope": "ISOLATED_REPLAY_NOT_A_TRADING_DECISION", "saved_request_sha256": digest,
              "request_attempts": 0, "orders_enabled": False}
    sdk = AsyncAnthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=0)
    try:
        async def capture(**kwargs):
            # The existing adapter constructs the exact protocol. A stale/different
            # model, prompt, schema, URL-bearing extra parameter or tool definition refuses.
            if kwargs != request:
                raise ProbeRefused("current_adapter_request_does_not_match_saved_request")
            result["request_attempts"] += 1
            response = await sdk.messages.create(**kwargs)
            result.update(response_shape(response))
            result["resolved_model_matches_request"] = getattr(response, "model", None) == model
            return response
        wrapper = SimpleNamespace(messages=SimpleNamespace(create=capture))
        client = adapter.AnthropicSwarmClient(model=model, client=wrapper, budget=budget)
        try:
            reply = await client.generate_trading_consensus(prompt)
            result["validation_status"] = reply.provenance.get("status", "unverified")
        except adapter.ConsensusSchemaError as error:
            result["validation_status"] = "invalid_schema"
            classifier = getattr(adapter, "consensus_failure_code", None)
            result["validation_code"] = classifier(error) if classifier else "see_response_shape"
            details = error.provenance or {}
            if "failure_code" in details:
                result["validation_code"] = details["failure_code"]
        return result
    finally:
        await sdk.close()
        budget.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid-call", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_paid_call:
        print("STOP: --confirm-paid-call is required; no provider call was made.")
        return 2
    try:
        report = asyncio.run(run_probe())
    except ProbeRefused as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - redact every credential/provider failure at CLI boundary
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    passed = (report.get("validation_status") == "completed" and report.get("stop_reason") == "tool_use"
              and report.get("tool_blocks") == report.get("matching_tool_blocks") == 1)
    report["diagnostic_passed"] = passed
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
