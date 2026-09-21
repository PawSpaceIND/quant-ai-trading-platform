"""One synthetic, budgeted API smoke test. No trading runtime or private market evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from quant_ai.llm.anthropic_client import ConsensusSchemaError
from quant_ai.llm.budget import budget_from_env
from quant_ai.llm.openai_client import OpenAIConsensusClient

PROMPT = (
    "subject=SYNTHETIC_CONNECTIVITY_TEST\nexecution_mode=PAPER_ONLY\n"
    "No market quotes, history, fundamentals or specialist evidence are supplied. "
    "This is a connectivity and schema test, not a trading opportunity. "
    "Return a NEUTRAL consensus with zero confidence, expected return and expected risk, "
    "explaining the missing evidence. Never invent market evidence."
)


async def probe():
    path = Path(os.getenv("PRAMANA_AI_BUDGET_DB", "/data/ai-budget.sqlite"))
    if not path.is_file():
        raise ValueError("existing_ai_budget_required")
    budget = budget_from_env(path.parent)
    if budget is None:
        raise ValueError("enabled_ai_budget_required")
    try:
        client = OpenAIConsensusClient(budget=budget)
        try:
            result = await client.generate_trading_consensus(PROMPT)
            provenance = result.provenance
            passed = (provenance["status"] == "completed" and result["stance"] == "NEUTRAL"
                      and all(result[field] == 0 for field in ("confidence", "expected_return", "expected_risk")))
        except ConsensusSchemaError as error:
            provenance, passed = error.provenance or {}, False
        return {"scope": "SYNTHETIC_API_SMOKE_TEST", "orders_enabled": False,
                "passed": passed, **{key: provenance.get(key) for key in (
                    "provider", "requested_model", "resolved_model", "status", "failure_code", "usage")}}
    finally:
        budget.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid-call", action="store_true")
    if not parser.parse_args(argv).confirm_paid_call:
        print(json.dumps({"status": "refused", "reason": "paid_call_flag_required"}))
        return 2
    try:
        report = asyncio.run(probe())
    except Exception:  # noqa: BLE001 - no credentials, prompts or provider errors on stdout
        print(json.dumps({"status": "failed", "reason": "check_key_and_existing_budget_configuration"}))
        return 1
    print(json.dumps(report, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
