"""Readiness reporting must not certify a subsystem over missing prerequisites."""
from dataclasses import replace
from decimal import Decimal

import pytest
from test_super_platform_closure import complete_evidence

from quant_ai.governance import super_platform as module


def test_missing_data_engineering_blocks_downstream_ai_and_execution():
    evidence = complete_evidence()
    evidence.pop("market_data_integrity_tests")
    rows = {row.capability_id: row for row in module.evaluate_super_platform(evidence)}
    assert not rows["R01"].engineering_complete
    assert not rows["A01"].engineering_complete
    assert not rows["E04"].engineering_complete


def test_missing_broker_capture_blocks_dependent_launch_not_its_unit_tests():
    evidence = complete_evidence()
    evidence.pop("real_broker_lifecycle_capture")
    rows = {row.capability_id: row for row in module.evaluate_super_platform(evidence)}
    assert rows["E04"].engineering_complete
    assert rows["E04"].external_complete  # its own external checklist is present
    assert not rows["E04"].launch_complete


def test_cycles_are_rejected_before_evaluating_evidence(monkeypatch):
    first, second = module.CAPABILITIES[:2]
    monkeypatch.setattr(module, "CAPABILITIES", (
        replace(first, depends_on=(second.capability_id,)),
        replace(second, depends_on=(first.capability_id,)),
    ))
    with pytest.raises(ValueError, match="super_platform_dependency_cycle"):
        module.evaluate_super_platform(complete_evidence())


@pytest.mark.parametrize("value", [0, -1, float("nan"), Decimal("Infinity"), True, object()])
def test_scalar_flags_and_nonfinite_numbers_are_not_evidence_references(value):
    evidence = complete_evidence()
    evidence["durable_order_journal"] = value
    row = next(row for row in module.evaluate_super_platform(evidence) if row.capability_id == "E02")
    assert not row.engineering_complete


def test_dependency_evaluation_is_independent_of_register_order(monkeypatch):
    evidence = complete_evidence()
    evidence.pop("market_data_integrity_tests")
    expected = {row.capability_id: row for row in module.evaluate_super_platform(evidence)}
    monkeypatch.setattr(module, "CAPABILITIES", tuple(reversed(module.CAPABILITIES)))
    actual = {row.capability_id: row for row in module.evaluate_super_platform(evidence)}
    assert actual == expected


def test_report_names_the_blocked_launch_dependency():
    evidence = complete_evidence()
    evidence.pop("real_broker_lifecycle_capture")
    report = module.closure_report(evidence)
    row = next(row for row in report["rows"] if row["id"] == "E04")
    assert row["blockedLaunchDependencies"] == ["E03"]
    assert "E04" in report["launchGaps"]
    assert "E04" not in report["engineeringGaps"]
    assert report["evidenceQualification"] == "reference_presence_and_dependencies_only"


@pytest.mark.parametrize("change", ["empty", "unknown", "duplicate", "unproved"])
def test_invalid_registers_refuse_instead_of_vacuously_closing(monkeypatch, change):
    rows = module.CAPABILITIES
    if change == "empty":
        rows = ()
    elif change == "unknown":
        rows = (replace(rows[0], depends_on=("MISSING",)),)
    elif change == "duplicate":
        rows = (rows[0], rows[0])
    else:
        rows = (replace(rows[0], engineering_evidence=()),)
    monkeypatch.setattr(module, "CAPABILITIES", rows)
    with pytest.raises(ValueError):
        module.assert_launch_closed(complete_evidence())
