"""Release-bound operator attestations. These never enable real-money execution."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.validation.promotion import StrategyEvidence, evaluate_promotion


def review(artifact: dict, gate: str, *, tenant: str, revision: str) -> None:
    if artifact.get("tenant_id") != tenant or artifact.get("release_revision") != revision:
        raise ValueError("Artifact must match tenant and exact release revision")
    if gate == "strategy":
        if artifact.get("schema") != "pramana.strategy.review.v1" or not artifact.get("strategy_id"):
            raise ValueError("AI strategy-specific reviewed evidence required; a baseline is not sufficient")
        if not all(artifact.get(k) is True for k in ("holdout_reviewed", "costs_reviewed", "trial_register_reviewed", "ai_calibration_reviewed")):
            raise ValueError("Strategy review is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("strategy_config_sha256", ""))):
            raise ValueError("Pin the reviewed strategy configuration and model/prompt versions")
        if not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("strategy_evidence_sha256", ""))):
            raise ValueError("Pin the reviewed strategy-linked episode evidence hash")
        if any(type(artifact.get(k)) is not int or artifact[k] < 0 for k in ("sample_trades", "profitable_regimes", "paper_days")):
            raise ValueError("Evidence counts must be nonnegative integers")
        evidence = StrategyEvidence(**{k: int(artifact[k]) if k in {"sample_trades", "profitable_regimes", "paper_days"} else Decimal(str(artifact[k])) for k in StrategyEvidence.__dataclass_fields__})
        if (not all(x.is_finite() for x in (evidence.expectancy, evidence.max_drawdown, evidence.profit_factor))
                or not 0 <= evidence.max_drawdown <= 1 or evidence.profit_factor < 0):
            raise ValueError("Evidence metrics must be finite and valid fractions")
        decision = evaluate_promotion(evidence)
        if not decision.approved:
            raise ValueError("Strategy policy rejected: " + ",".join(decision.reasons))
    elif gate == "recovery":
        if artifact.get("schema") != "pramana.recovery.review.v1" or not artifact.get("target_host"):
            raise ValueError("Target-host recovery evidence required")
        required = ("clean_start", "private_access", "token_renewal", "independent_alert_received",
                    "full_bundle_restored", "rollback_verified")
        if not all(artifact.get(k) is True for k in required):
            raise ValueError("Recovery review is incomplete")
    else:
        raise ValueError("Unsupported review gate")
    references = artifact.get("evidence_references", [])
    if not references or any(not isinstance(r, str) or not r.strip() for r in references):
        raise ValueError("Review must reference the retained evidence bundle")


def sign_review(artifact_path: Path, *, gate: str, tenant: str, revision: str,
                reviewer: str, secret: str, now: datetime | None = None) -> dict:
    if len(secret) < 32 or not reviewer.strip() or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Strong review key, reviewer name and full git revision are required")
    if artifact_path.stat().st_size > 100000:
        raise ValueError("Review artifact exceeds 100 KB")
    data = artifact_path.read_bytes()
    artifact = json.loads(data)
    review(artifact, gate, tenant=tenant, revision=revision)
    now = now or datetime.now(timezone.utc)
    payload = json.dumps({"schema": "pramana.pilot.acceptance.v1", "scope": "private-paper-pilot",
                          "gate": gate, "tenant_id": tenant, "release_revision": revision,
                          "reviewer": reviewer.strip(), "reviewed_at": now.isoformat(),
                          "expires_at": (now + timedelta(days=7)).isoformat(),
                          "artifact": artifact, "artifact_sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True)
    return {"payload": payload, "signature": hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--gate", choices=["strategy", "recovery"], required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = sign_review(args.artifact, gate=args.gate, tenant=args.tenant, revision=args.revision,
                         reviewer=args.reviewer, secret=os.environ.get("PRAMANA_REVIEW_SECRET", ""))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(result, out, indent=2)
    args.output.chmod(0o600)
    print("Operator attestation recorded; it does not enable live orders or certify future returns.")


if __name__ == "__main__":
    main()
