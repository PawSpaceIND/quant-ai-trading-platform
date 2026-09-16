import pytest

from quant_ai.governance.super_platform import (
    CAPABILITIES,
    assert_engineering_closed,
    assert_launch_closed,
    closure_report,
    evaluate_super_platform,
    validate_register,
)


def complete_evidence():
    keys = {
        key
        for capability in CAPABILITIES
        for key in (*capability.engineering_evidence, *capability.external_evidence)
    }
    return {key: "verified-evidence" for key in keys}


def test_register_is_unique_and_all_dependencies_exist():
    validate_register()
    assert len(CAPABILITIES) >= 30
    assert len({item.capability_id for item in CAPABILITIES}) == len(CAPABILITIES)


def test_complete_evidence_closes_every_capability():
    evidence = complete_evidence()
    statuses = evaluate_super_platform(evidence)
    assert all(item.engineering_complete for item in statuses)
    assert all(item.launch_complete for item in statuses)
    assert_engineering_closed(evidence)
    assert_launch_closed(evidence)


def test_one_missing_engineering_proof_keeps_the_platform_open():
    evidence = complete_evidence()
    evidence.pop("durable_order_journal")
    status = next(item for item in evaluate_super_platform(evidence) if item.capability_id == "E02")
    assert not status.engineering_complete
    assert status.missing_engineering == ("durable_order_journal",)
    with pytest.raises(ValueError, match="E02:durable_order_journal"):
        assert_engineering_closed(evidence)


def test_external_evidence_is_separate_from_engineering_completion():
    evidence = complete_evidence()
    evidence.pop("target_host_burn_in")
    status = next(item for item in evaluate_super_platform(evidence) if item.capability_id == "O03")
    assert status.engineering_complete
    assert not status.external_complete
    assert status.missing_external == ("target_host_burn_in",)
    assert_engineering_closed(evidence)
    with pytest.raises(ValueError, match="O03:engineering=-:external=target_host_burn_in"):
        assert_launch_closed(evidence)


def test_empty_false_and_none_evidence_never_counts_as_verified():
    evidence = complete_evidence()
    evidence["ai_source_manifest"] = ""
    evidence["calibration_tests"] = False
    evidence["balanced_journal_tests"] = None
    report = closure_report(evidence)
    assert {"A01", "A04", "C01"} <= set(report["engineeringGaps"])
    assert report["launchComplete"] < report["capabilities"]
