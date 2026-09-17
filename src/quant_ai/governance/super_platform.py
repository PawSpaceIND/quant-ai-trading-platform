"""Fail-closed capability register for the full trading-platform objective.

The private paper pilot has a deliberately narrower acceptance contract.  This register is
for the larger platform: a capability is not "done" because adjacent code exists, and an
engineering-complete module is not launch-complete until the required external evidence is
attached.  Unknown or empty evidence is always a gap.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

SCHEMA = "pramana.super_platform_closure.v1"


class CapabilityDomain(str, Enum):
    DATA = "DATA"
    RESEARCH = "RESEARCH"
    AI = "AI"
    PORTFOLIO = "PORTFOLIO"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    ACCOUNTING = "ACCOUNTING"
    DERIVATIVES = "DERIVATIVES"
    OPERATIONS = "OPERATIONS"
    SECURITY = "SECURITY"


@dataclass(frozen=True)
class CapabilityRequirement:
    capability_id: str
    domain: CapabilityDomain
    title: str
    engineering_evidence: tuple[str, ...]
    external_evidence: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


# The register is intentionally explicit.  Adding a subsystem to the architecture requires
# adding its closure evidence here; deleting a capability is therefore a reviewable diff.
CAPABILITIES = (
    CapabilityRequirement("D01", CapabilityDomain.DATA, "live market-data truth layer", ("market_data_integrity_tests", "source_timestamp_contract"), ("open_session_feed_capture",)),
    CapabilityRequirement("D02", CapabilityDomain.DATA, "point-in-time historical data", ("historical_data_contract", "lookahead_guards"), ("licensed_point_in_time_source",)),
    CapabilityRequirement("D03", CapabilityDomain.DATA, "corporate actions and total-return adjustments", ("corporate_action_pipeline", "adjustment_provenance"), ("qualified_adjustment_source",)),
    CapabilityRequirement("D04", CapabilityDomain.DATA, "canonical instrument and contract master", ("instrument_identity_tests", "broker_instrument_mapping"), ("broker_master_capture",)),
    CapabilityRequirement("R01", CapabilityDomain.RESEARCH, "feature and evidence lineage", ("feature_schema", "feature_provenance", "availability_time_guards"), depends_on=("D01", "D02")),
    CapabilityRequirement("R02", CapabilityDomain.RESEARCH, "regime detection and regime evidence", ("regime_tests", "regime_provenance"), depends_on=("R01",)),
    CapabilityRequirement("R03", CapabilityDomain.RESEARCH, "causal backtest and replay fidelity", ("backtest_fidelity_tests", "intrabar_replay_tests", "friction_model_tests"), ("real_source_replay",), ("D02", "R01")),
    CapabilityRequirement("R04", CapabilityDomain.RESEARCH, "experiment and trial registry", ("trial_register_chain", "walk_forward_tests", "holdout_separation"), depends_on=("R03",)),
    CapabilityRequirement("A01", CapabilityDomain.AI, "grounded AI knowledge-access plane", ("ai_source_manifest", "ai_evidence_cutoff_tests", "ai_prompt_provenance"), ("qualified_ai_sources",), ("R01",)),
    CapabilityRequirement("A02", CapabilityDomain.AI, "specialist swarm and adversarial review", ("specialist_coverage_tests", "consensus_schema_tests", "stress_veto_tests"), depends_on=("A01",)),
    CapabilityRequirement("A03", CapabilityDomain.AI, "reproducible model-training registry", ("training_dataset_manifest", "training_run_manifest", "artifact_digest_registry"), depends_on=("R04",)),
    CapabilityRequirement("A04", CapabilityDomain.AI, "probability calibration and drift monitoring", ("calibration_tests", "drift_tests", "net_return_label_contract"), ("forward_calibration_evidence",), ("A03",)),
    CapabilityRequirement("A05", CapabilityDomain.AI, "champion/challenger shadow evaluation", ("shadow_candidate_isolation", "candidate_comparison_contract", "promotion_review_binding"), ("forward_challenger_observation",), ("A04",)),
    CapabilityRequirement("A06", CapabilityDomain.AI, "bounded learning from realised outcomes", ("learning_feedback_tests", "regime_sample_floor", "weight_bounds"), ("forward_learning_review",), ("A04",)),
    CapabilityRequirement("P01", CapabilityDomain.PORTFOLIO, "strategy portfolio and capital allocator", ("strategy_budget_contract", "cross_strategy_exposure_tests"), depends_on=("R03",)),
    CapabilityRequirement("P02", CapabilityDomain.PORTFOLIO, "portfolio optimizer and rebalance engine", ("optimizer_constraints_tests", "turnover_penalty_tests", "rebalance_tests"), depends_on=("P01",)),
    CapabilityRequirement("P03", CapabilityDomain.PORTFOLIO, "position lifecycle manager", ("scale_in_out_tests", "time_stop_tests", "protective_exit_tests"), depends_on=("P02",)),
    CapabilityRequirement("K01", CapabilityDomain.RISK, "portfolio risk firewall", ("risk_firewall_tests", "book_risk_tests", "drawdown_halt_tests"), depends_on=("P01",)),
    CapabilityRequirement("K02", CapabilityDomain.RISK, "factor, liquidity and scenario risk", ("factor_risk_contract", "liquidity_risk_tests", "scenario_stress_tests"), ("forward_risk_calibration",), ("K01",)),
    CapabilityRequirement("E01", CapabilityDomain.EXECUTION, "execution planner and market-impact controls", ("execution_plan_contract", "participation_limit_tests", "latency_impact_stress"), depends_on=("K01",)),
    CapabilityRequirement("E02", CapabilityDomain.EXECUTION, "durable order management system", ("durable_order_journal", "partial_fill_state_tests", "cancel_replace_recovery_tests"), depends_on=("D04", "E01")),
    CapabilityRequirement("E03", CapabilityDomain.EXECUTION, "broker lifecycle adapter", ("broker_ack_contract", "idempotent_client_order_ids", "uncertain_submission_recovery"), ("real_broker_lifecycle_capture",), ("E02",)),
    CapabilityRequirement("E04", CapabilityDomain.EXECUTION, "continuous broker reconciliation", ("order_reconciliation_tests", "position_reconciliation_tests", "cash_reconciliation_tests"), ("real_account_reconciliation",), ("E03",)),
    CapabilityRequirement("C01", CapabilityDomain.ACCOUNTING, "double-entry trading subledger", ("balanced_journal_tests", "fee_tax_posting_tests", "pnl_posting_tests"), depends_on=("E02",)),
    CapabilityRequirement("C02", CapabilityDomain.ACCOUNTING, "settlement and collateral engine", ("settlement_state_tests", "collateral_tests", "unsettled_cash_tests"), ("broker_settlement_reconciliation",), ("C01",)),
    CapabilityRequirement("C03", CapabilityDomain.ACCOUNTING, "multi-currency cashbook and FX translation", ("currency_cashbook_tests", "fx_translation_tests"), ("qualified_fx_source",), ("C01",)),
    CapabilityRequirement("F01", CapabilityDomain.DERIVATIVES, "futures lifecycle, margin and roll", ("futures_contract_tests", "margin_source_tests", "roll_tests", "futures_fee_tests"), ("real_contract_note_reconciliation", "real_margin_capture"), ("D04", "C02")),
    CapabilityRequirement("F02", CapabilityDomain.DERIVATIVES, "options valuation, Greeks and lifecycle", ("option_chain_contract", "iv_greeks_tests", "exercise_assignment_tests", "spread_margin_tests"), ("qualified_option_chain_source",), ("D04", "C02", "K02")),
    CapabilityRequirement("O01", CapabilityDomain.OPERATIONS, "health, telemetry and independent alerting", ("heartbeat_tests", "alert_delivery_tests", "clock_guard_tests"), ("target_host_alert_receipt",)),
    CapabilityRequirement("O02", CapabilityDomain.OPERATIONS, "backup, restore and disaster recovery", ("recovery_bundle_tests", "restore_replay_tests"), ("off_host_restore_drill",)),
    CapabilityRequirement("O03", CapabilityDomain.OPERATIONS, "deployment, rollback and sustained burn-in", ("deployment_image_tests", "rollback_contract"), ("target_host_burn_in",), ("O01", "O02")),
    CapabilityRequirement("S01", CapabilityDomain.SECURITY, "authentication, authorization and tenant isolation", ("auth_tests", "authorization_tests", "tenant_isolation_tests", "secret_handling_tests"), ("external_security_review",)),
    CapabilityRequirement("S02", CapabilityDomain.SECURITY, "operator controls and execution authority", ("kill_switch_tests", "human_approval_boundary", "live_money_fail_closed"), ("operator_acceptance",), ("S01", "E02")),
)


@dataclass(frozen=True)
class CapabilityStatus:
    capability_id: str
    engineering_complete: bool
    external_complete: bool
    missing_engineering: tuple[str, ...]
    missing_external: tuple[str, ...]
    blocked_engineering_dependencies: tuple[str, ...] = ()
    blocked_launch_dependencies: tuple[str, ...] = ()

    @property
    def launch_complete(self) -> bool:
        return (self.engineering_complete and self.external_complete
                and not self.blocked_launch_dependencies)


def _present(evidence: Mapping[str, object], key: str) -> bool:
    value = evidence.get(key)
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        return bool(value)
    # A flag, number (including NaN), or arbitrary object is not an evidence reference.
    return False


def evaluate_super_platform(evidence: Mapping[str, object]) -> tuple[CapabilityStatus, ...]:
    """Check references and transitive prerequisites; this does not authenticate artifacts."""
    validate_register()
    requirements = {item.capability_id: item for item in CAPABILITIES}
    statuses: dict[str, CapabilityStatus] = {}

    def evaluate(capability_id: str) -> CapabilityStatus:
        if capability_id in statuses:
            return statuses[capability_id]
        capability = requirements[capability_id]
        dependencies = tuple(evaluate(key) for key in capability.depends_on)
        missing_engineering = tuple(
            key for key in capability.engineering_evidence if not _present(evidence, key)
        )
        missing_external = tuple(
            key for key in capability.external_evidence if not _present(evidence, key)
        )
        blocked_engineering = tuple(
            item.capability_id for item in dependencies if not item.engineering_complete
        )
        blocked_launch = tuple(
            item.capability_id for item in dependencies if not item.launch_complete
        )
        status = CapabilityStatus(
            capability_id,
            not missing_engineering and not blocked_engineering,
            not missing_external,
            missing_engineering,
            missing_external,
            blocked_engineering,
            blocked_launch,
        )
        statuses[capability_id] = status
        return status

    return tuple(evaluate(item.capability_id) for item in CAPABILITIES)


def closure_report(evidence: Mapping[str, object]) -> dict[str, object]:
    statuses = evaluate_super_platform(evidence)
    return {
        "schema": SCHEMA,
        "capabilities": len(statuses),
        "engineeringComplete": sum(item.engineering_complete for item in statuses),
        "launchComplete": sum(item.launch_complete for item in statuses),
        "engineeringGaps": [item.capability_id for item in statuses if not item.engineering_complete],
        "externalGaps": [item.capability_id for item in statuses if not item.external_complete],
        "launchGaps": [item.capability_id for item in statuses if not item.launch_complete],
        "evidenceQualification": "reference_presence_and_dependencies_only",
        "rows": [
            {
                "id": item.capability_id,
                "engineeringComplete": item.engineering_complete,
                "externalComplete": item.external_complete,
                "launchComplete": item.launch_complete,
                "missingEngineering": list(item.missing_engineering),
                "missingExternal": list(item.missing_external),
                "blockedEngineeringDependencies": list(item.blocked_engineering_dependencies),
                "blockedLaunchDependencies": list(item.blocked_launch_dependencies),
            }
            for item in statuses
        ],
    }


def assert_engineering_closed(evidence: Mapping[str, object]) -> None:
    gaps = [item for item in evaluate_super_platform(evidence) if not item.engineering_complete]
    if gaps:
        detail = ";".join(
            f"{item.capability_id}:{','.join(item.missing_engineering) or '-'}:"
            f"dependencies={','.join(item.blocked_engineering_dependencies) or '-'}"
            for item in gaps
        )
        raise ValueError(f"super_platform_engineering_incomplete:{detail}")


def assert_launch_closed(evidence: Mapping[str, object]) -> None:
    statuses = evaluate_super_platform(evidence)
    gaps = [item for item in statuses if not item.launch_complete]
    if gaps:
        detail = ";".join(
            f"{item.capability_id}:engineering={','.join(item.missing_engineering) or '-'}:"
            f"external={','.join(item.missing_external) or '-'}:"
            f"dependencies={','.join(item.blocked_launch_dependencies) or '-'}"
            for item in gaps
        )
        raise ValueError(f"super_platform_launch_incomplete:{detail}")


def validate_register() -> None:
    if not CAPABILITIES:
        raise ValueError("super_platform_register_empty")
    ids = [item.capability_id for item in CAPABILITIES]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_super_platform_capability")
    known = set(ids)
    for item in CAPABILITIES:
        if not item.engineering_evidence or any(not key.strip() for key in item.engineering_evidence):
            raise ValueError(f"super_platform_engineering_evidence_required:{item.capability_id}")
        if len(item.depends_on) != len(set(item.depends_on)):
            raise ValueError(f"duplicate_super_platform_dependency:{item.capability_id}")
        missing = set(item.depends_on) - known
        if missing:
            raise ValueError(
                f"unknown_super_platform_dependency:{item.capability_id}:{','.join(sorted(missing))}"
            )

    by_id = {item.capability_id: item for item in CAPABILITIES}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(capability_id: str) -> None:
        if capability_id in visiting:
            raise ValueError(f"super_platform_dependency_cycle:{capability_id}")
        if capability_id in visited:
            return
        visiting.add(capability_id)
        for dependency in by_id[capability_id].depends_on:
            visit(dependency)
        visiting.remove(capability_id)
        visited.add(capability_id)

    for capability_id in ids:
        visit(capability_id)
