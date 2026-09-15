"""Release-bound operator attestations. These never enable real-money execution.

Two kinds of claim arrive in a review artifact and they are treated differently.

A human attesting that they looked at something - ``holdout_reviewed``,
``costs_reviewed`` and the rest - is a boolean only a person can supply, and it stays a
boolean. A machine-checkable claim is machine-checked: every pinned digest is recomputed
from the retained files with the same hasher the external gate uses, and every promotion
figure is recomputed from the decision journal and the daily equity marks inside the
retained ledger. A figure that cannot be recomputed refuses the review; it is never taken
from the artifact. The typed figures are still required and still validated, but they now
have to agree with what the ledger says.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from quant_ai.governance.derived_evidence import derive_strategy_evidence
from quant_ai.operations.pilot_gate import evidence_bundle_digest
from quant_ai.validation.promotion import StrategyEvidence, evaluate_promotion
from quant_ai.validation.trial_register import register_summary

DIGEST = re.compile(r"[0-9a-f]{64}")

HUMAN_REVIEW_FLAGS = (
    "holdout_reviewed",
    "costs_reviewed",
    "trial_register_reviewed",
    "ai_calibration_reviewed",
    "forward_paper_reviewed",
    "execution_stress_reviewed",
)

NAMED_EVIDENCE_DIGESTS = (
    "holdout_evidence_sha256",
    "forward_paper_evidence_sha256",
    "execution_stress_evidence_sha256",
    "calibration_evidence_sha256",
)

# Each of these pins exactly one file, because the gate has to open it, not just hash it.
SINGLE_FILE_DIGESTS = ("ledger_evidence_sha256", "trial_register_sha256")

PINNED_DIGESTS = (
    *NAMED_EVIDENCE_DIGESTS,
    "strategy_config_sha256",
    "strategy_evidence_sha256",
    *SINGLE_FILE_DIGESTS,
)

COUNT_FIELDS = ("sample_trades", "profitable_regimes", "paper_days")
AMOUNT_FIELDS = ("expectancy", "max_drawdown", "profit_factor")
CENT = Decimal("0.000001")


def _same(left: Decimal, right: Decimal) -> bool:
    """Equal to six places, so a rounded typed figure is not called a disagreement."""
    return (left.quantize(CENT, rounding=ROUND_HALF_EVEN)
            == right.quantize(CENT, rounding=ROUND_HALF_EVEN))


def _claimed_evidence(artifact: dict) -> StrategyEvidence:
    """The operator's typed figures, validated exactly as before they were machine-checked."""
    if any(type(artifact.get(key)) is not int or artifact[key] < 0 for key in COUNT_FIELDS):
        raise ValueError("Evidence counts must be nonnegative integers")
    claimed = StrategyEvidence(**{
        key: int(artifact[key]) if key in set(COUNT_FIELDS) else Decimal(str(artifact[key]))
        for key in StrategyEvidence.__dataclass_fields__
    })
    if (not all(x.is_finite() for x in (claimed.expectancy, claimed.max_drawdown, claimed.profit_factor))
            or not 0 <= claimed.max_drawdown <= 1 or claimed.profit_factor < 0):
        raise ValueError("Evidence metrics must be finite and valid fractions")
    return claimed


def _resolve_evidence(artifact: dict, evidence_root: Path) -> dict[str, list[str]]:
    """Recompute every pinned digest from the retained files; refuse on any disagreement."""
    listing = artifact.get("evidence_files")
    if not isinstance(listing, dict) or not listing:
        raise ValueError("Review must list the retained files behind every pinned digest")
    resolved: dict[str, list[str]] = {}
    for name in PINNED_DIGESTS:
        entry = listing.get(name)
        if isinstance(entry, str):
            entry = [entry]
        if (not isinstance(entry, list) or not entry
                or not all(isinstance(item, str) and item.strip() for item in entry)):
            raise ValueError(f"List the retained files behind {name}")
        if name in SINGLE_FILE_DIGESTS and len(entry) != 1:
            raise ValueError(f"{name} must pin exactly one retained file")
        try:
            recomputed = evidence_bundle_digest(list(entry), evidence_root)
        except (OSError, ValueError, RuntimeError) as error:
            raise ValueError(f"Retained evidence for {name} is unreadable or unsafe: {error}") from error
        if recomputed != str(artifact[name]).lower():
            raise ValueError(
                f"Recomputed digest for {name} does not match the retained files; "
                "the pinned hash describes content that is not there"
            )
        resolved[name] = list(entry)
    return resolved


def _review_strategy(artifact: dict, *, tenant: str, evidence_root: Path | None) -> dict:
    if artifact.get("schema") != "pramana.strategy.review.v1" or not artifact.get("strategy_id"):
        raise ValueError("AI strategy-specific reviewed evidence required; a baseline is not sufficient")
    if not all(artifact.get(key) is True for key in HUMAN_REVIEW_FLAGS):
        raise ValueError("Strategy review is incomplete")
    for evidence_name in NAMED_EVIDENCE_DIGESTS:
        if not DIGEST.fullmatch(str(artifact.get(evidence_name, ""))):
            raise ValueError(
                f"Pin the reviewed {evidence_name.removesuffix('_evidence_sha256').replace('_', ' ')} evidence digest"
            )
    if not DIGEST.fullmatch(str(artifact.get("strategy_config_sha256", ""))):
        raise ValueError("Pin the reviewed strategy configuration and model/prompt versions")
    if not DIGEST.fullmatch(str(artifact.get("strategy_evidence_sha256", ""))):
        raise ValueError("Pin the reviewed strategy-linked episode evidence hash")
    for name in SINGLE_FILE_DIGESTS:
        if not DIGEST.fullmatch(str(artifact.get(name, ""))):
            raise ValueError(
                f"Pin the retained {name.removesuffix('_sha256').replace('_', ' ')} digest"
            )
    claimed = _claimed_evidence(artifact)
    if evidence_root is None:
        raise ValueError("Strategy review must be evaluated against the retained evidence directory")
    resolved = _resolve_evidence(artifact, evidence_root)
    ledger = evidence_root / resolved["ledger_evidence_sha256"][0]
    register = evidence_root / resolved["trial_register_sha256"][0]
    evidence = derive_strategy_evidence(ledger, tenant_id=tenant)
    trials = register_summary(register)
    if trials["runs"] < 1:
        raise ValueError("The retained trial register records no research trials")
    decision = evaluate_promotion(evidence)
    if not decision.approved:
        raise ValueError("Strategy policy rejected: " + ",".join(decision.reasons))
    disagreements = [
        field for field in COUNT_FIELDS
        if getattr(claimed, field) != getattr(evidence, field)
    ] + [
        field for field in AMOUNT_FIELDS
        if not _same(getattr(claimed, field), getattr(evidence, field))
    ]
    if disagreements:
        raise ValueError(
            "Reviewed figures disagree with the retained ledger: " + ",".join(sorted(disagreements))
        )
    return {
        "source": "retained_ledger_and_trial_register",
        "tenant_id": tenant,
        "sample_trades": evidence.sample_trades,
        "expectancy": str(evidence.expectancy),
        "max_drawdown": str(evidence.max_drawdown),
        "profit_factor": str(evidence.profit_factor),
        "profitable_regimes": evidence.profitable_regimes,
        "paper_days": evidence.paper_days,
        "registered_runs": trials["runs"],
        "candidate_trials": trials["candidate_trials"],
        "multiple_testing_correction": "none",
        "evidence_files": resolved,
    }


def review(artifact: dict, gate: str, *, tenant: str, revision: str,
           evidence_root: Path | None = None) -> dict:
    """Evaluate a review artifact. Returns the machine-derived findings; raises to refuse."""
    if artifact.get("tenant_id") != tenant or artifact.get("release_revision") != revision:
        raise ValueError("Artifact must match tenant and exact release revision")
    derived: dict = {}
    if gate == "strategy":
        derived = _review_strategy(artifact, tenant=tenant, evidence_root=evidence_root)
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
    return derived


def sign_review(artifact_path: Path, *, gate: str, tenant: str, revision: str,
                reviewer: str, secret: str, now: datetime | None = None) -> dict:
    if len(secret) < 32 or not reviewer.strip() or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Strong review key, reviewer name and full git revision are required")
    if artifact_path.stat().st_size > 100000:
        raise ValueError("Review artifact exceeds 100 KB")
    data = artifact_path.read_bytes()
    artifact = json.loads(data)
    # Evidence paths are resolved beside the review document, the same convention the
    # external gate preflight uses, so a signature always covers files that exist.
    derived = review(artifact, gate, tenant=tenant, revision=revision,
                     evidence_root=artifact_path.resolve().parent)
    now = now or datetime.now(timezone.utc)
    payload = json.dumps({"schema": "pramana.pilot.acceptance.v1", "scope": "private-paper-pilot",
                          "gate": gate, "tenant_id": tenant, "release_revision": revision,
                          "reviewer": reviewer.strip(), "reviewed_at": now.isoformat(),
                          "expires_at": (now + timedelta(days=7)).isoformat(),
                          "artifact": artifact, "derived": derived,
                          "artifact_sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True)
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
