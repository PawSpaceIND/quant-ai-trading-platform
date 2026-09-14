"""Synthetic research fixture; never calls models or submits orders."""

import hashlib
import json
import sys
from pathlib import Path

from quant_ai.research.lab import ResearchLab, canonical


def main():
    path = Path(sys.argv[1])
    # Exclusive creation prevents accidental modification of an existing database.
    with path.open("x"):
        pass
    lab = ResearchLab(path)
    try:
        lab.create(
            "demo",
            {
                "candidates": ["synthetic-claude", "synthetic-astra", "cash"],
                "baseline": "cash",
                "mode": "historical",
                "expected_cases": 1,
                "max_quantity": 10,
                "max_quote_age_seconds": 60,
                "capital_per_case": "1000",
                "fee_bps": "10",
                "slippage_bps": "10",
                "protocol_version": "synthetic-v1",
            },
        )
        packet = {
            "decision_at": "2026-09-11T10:00:00+05:30",
            "quote_at": "2026-09-11T09:59:50+05:30",
            "exchange": "NSE",
            "asset_class": "EQUITY",
            "currency": "INR",
            "symbol": "SYNTHETIC",
            "reference_price": "100",
            "adjustment_status": "unverified",
            "sources": [
                {
                    "id": "fixture",
                    "available_at": "2026-09-11T09:59:50+05:30",
                    "provenance": "synthetic fixture; not market data",
                }
            ],
        }
        lab.add_case("demo", "case-1", packet)
        digest = hashlib.sha256(canonical(packet).encode()).hexdigest()
        for candidate, quantity in [("synthetic-claude", 10), ("synthetic-astra", 5), ("cash", 0)]:
            lab.record_decision(
                "demo",
                "case-1",
                candidate,
                {
                    "input_digest": digest,
                    "model_version": "fixture-only",
                    "prompt_version": "fixture-v1",
                    "decided_at": "2026-09-11T10:00:01+05:30",
                    "status": "ok",
                    "action": "BUY" if quantity else "HOLD",
                    "quantity": quantity,
                    "rationale": "Synthetic plumbing test",
                    "evidence_ids": ["fixture"],
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "api_cost_usd": "0",
                },
            )
        lab.record_outcome(
            "demo",
            "case-1",
            {
                "entry_at": "2026-09-11T10:00:02+05:30",
                "exit_at": "2026-09-11T10:01:00+05:30",
                "entry_ask": "100",
                "exit_bid": "99",
                "entry_available_quantity": 10,
                "exit_available_quantity": 10,
                "provenance": "synthetic fixture",
            },
        )
        print(json.dumps(lab.report("demo"), indent=2))
    finally:
        lab.close()


if __name__ == "__main__":
    main()
