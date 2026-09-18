"""Paired sabotage checks for MCX pilot admission and existing execution barriers."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_mcx_pilot_admission.py::"

CASES = [
    {
        "id": "verified_fee_required",
        "path": "src/quant_ai/governance/pilot.py",
        "old": "if derivative_fee_schedule is None or not derivative_fee_schedule.verified:",
        "new": "if derivative_fee_schedule is None:",
        "test": TEST + "test_mcx_fee_schedule_must_be_reconciled[fee1]",
    },
    {
        "id": "live_contract_required",
        "path": "src/quant_ai/governance/pilot.py",
        "old": "assert_contract_tradable(item, Side.BUY, now)",
        "new": "None",
        "test": TEST + "test_mcx_entry_contract_must_be_live[expiry0-contract_expired]",
    },
    {
        "id": "fee_classification_required",
        "path": "src/quant_ai/governance/pilot.py",
        "old": "derivative_fee_schedule.assert_supported_order(probe)",
        "new": "None",
        "test": TEST + (
            "test_mcx_commodity_requires_supported_fee_classification"
            "[SOYBEAN-agricultural-mcx_agricultural_fee_schedule_not_configured]"
        ),
    },
    {
        "id": "margin_source_required",
        "path": "src/quant_ai/governance/pilot.py",
        "old": '''if margin_source is None:
        raise ValueError(f"pilot_mcx_margin_source_required:{item.symbol}")''',
        "new": "if margin_source is None:\n        return",
        "test": TEST + "test_mcx_margin_source_is_required",
    },
    {
        "id": "margin_contract_lot_binding",
        "path": "src/quant_ai/governance/pilot.py",
        "old": "if requirement.lot_size != item.lot_size:",
        "new": "if False:",
        "test": TEST + "test_mcx_margin_must_match_contract_lot",
    },
    {
        "id": "complete_contract_identity",
        "path": "src/quant_ai/governance/pilot.py",
        "old": '''or not isinstance(item.underlying, str)
        or not item.underlying.strip()''',
        "new": "or False",
        "test": TEST + "test_mcx_requires_complete_contract_identity[item0]",
    },
    {
        "id": "mcx_exchange_only",
        "path": "src/quant_ai/governance/pilot.py",
        "old": 'and item.exchange == "MCX"',
        "new": "and True",
        "test": TEST + "test_mcx_admission_does_not_widen_currency_or_exchange_scope[item1]",
    },
    {
        "id": "inr_only",
        "path": "src/quant_ai/governance/pilot.py",
        "old": 'and item.currency == "INR"\n        and item.exchange == "MCX"',
        "new": 'and True\n        and item.exchange == "MCX"',
        "test": TEST + "test_mcx_admission_does_not_widen_currency_or_exchange_scope[item0]",
    },
    {
        "id": "bound_runtime_required",
        "path": "src/quant_ai/execution/paper_ledger.py",
        "old": 'if any(identity.get("exchange") == "MCX" for identity in identities.values()):',
        "new": "if False:",
        "test": TEST + "test_mcx_pilot_requires_bound_runtime_before_broker_initialization",
    },
    {
        "id": "legacy_derivative_position_not_adopted",
        "path": "src/quant_ai/execution/paper_ledger.py",
        "old": 'if configured.get("exchange") == "MCX" and raw is None:',
        "new": "if False:",
        "test": TEST + "test_pilot_cannot_adopt_unbound_existing_derivative_position",
    },
    {
        "id": "configure_uses_verified_fee",
        "path": "src/quant_ai/execution/paper_ledger.py",
        "old": """derivative_fee_schedule=getattr(
                self.friction_model, "derivative_fee_schedule", None
            ),""",
        "new": "derivative_fee_schedule=None,",
        "test": TEST + "test_bound_mcx_pilot_fill_persists_exact_contract_identity",
    },
    {
        "id": "configure_uses_margin_source",
        "path": "src/quant_ai/execution/paper_ledger.py",
        "old": "margin_source=self.margin_source,",
        "new": "margin_source=None,",
        "test": TEST + "test_bound_mcx_pilot_fill_persists_exact_contract_identity",
    },
    {
        "id": "contract_lot_check",
        "path": "src/quant_ai/instruments/contract.py",
        "old": "if lot is not None and quantity % lot != 0:",
        "new": "if False:",
        "test": TEST + "test_corrupted_nonlot_bound_order_is_refused_by_ledger_contract_check",
    },
    {
        "id": "ledger_contract_expiry_check",
        "path": "src/quant_ai/execution/paper_ledger.py",
        "old": "assert_contract_tradable(instrument, order.side, now)",
        "new": "None",
        "test": TEST + "test_contract_expiring_after_scope_configuration_cannot_fill",
    },
]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_mcx_guard_requires_passing_control_and_named_assertion_failure(
    tmp_path, case
):
    run_guard(tmp_path, case)
    failures = [
        (row.get("name"), failure.get("message", ""))
        for row in ET.parse(tmp_path / "mutant.xml").iter("testcase")
        for failure in row.findall("failure")
    ]
    assert failures
    assert all(
        name
        and message.startswith(("assert ", "AssertionError:", "Failed:"))
        for name, message in failures
    ), failures
