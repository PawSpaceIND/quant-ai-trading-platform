"""Paired controls and deliberate removals for each new explicit ingestion guard."""
from __future__ import annotations

import ast
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = "src/quant_ai/learning/ingestion.py"
TEST = "tests/test_received_feature_ingestion.py::"
TARGETS = {
    "grant_payload_bound": "test_grant_payload_has_direct_size_bound",
    "receipt_byte_bound": "test_cumulative_receipt_byte_limit_precedes_large_reads_and_writes[True-receipt_byte_bound]",
    "new_receipt_byte_bound": "test_cumulative_receipt_byte_limit_precedes_large_reads_and_writes[False-new_receipt_byte_bound]",
    "batch_schema": "test_invalid_batch_has_specific_refusal_before_any_feature_write[schema-batch_schema]",
    "single_typed_grant": "test_typed_input_and_single_source_contract",
    "source_not_permitted": "test_source_permissions_refuse_before_feature_writes",
    "batch_row_bound": "test_invalid_batch_has_specific_refusal_before_any_feature_write[empty-batch_row_bound]",
    "record_schema": "test_invalid_batch_has_specific_refusal_before_any_feature_write[availability-record_schema]",
    "duplicate_source_record": "test_invalid_batch_has_specific_refusal_before_any_feature_write[duplicate-duplicate_source_record]",
    "category_not_permitted": "test_invalid_batch_has_specific_refusal_before_any_feature_write[category-category_not_permitted]",
    "future_observation": "test_invalid_batch_has_specific_refusal_before_any_feature_write[future-future_observation]",
    "tenant_binding": "test_wrong_tenant_refuses_existing_store",
    "read_transaction_required": "test_validator_requires_stable_read_transaction",
    "receipt_bound": "test_capacity_refusals_do_not_partially_write[True-MAX_RECEIPTS-receipt_bound]",
    "store_row_bound": "test_capacity_refusals_do_not_partially_write[True-MAX_STORE_ROWS-store_row_bound]",
    "receipt_time_order": "test_retained_receipt_times_cannot_go_backwards",
    "receipt_digest": "test_corrupted_saved_evidence_has_named_refusal[digest-receipt_digest]",
    "receipt_identity": "test_corrupted_saved_evidence_has_named_refusal[identity-receipt_identity]",
    "source_record_changed": "test_retained_same_id_cannot_describe_two_values",
    "source_coverage": "test_corrupted_saved_evidence_has_named_refusal[coverage-source_coverage]",
    "record_lineage_missing": "test_corrupted_saved_evidence_has_named_refusal[missing-record_lineage_missing]",
    "record_lineage_mismatch": "test_corrupted_saved_evidence_has_named_refusal[record-record_lineage_mismatch]",
    "private_directory": "test_output_parent_requires_private_directory",
    "selected_store_replaced": "test_replaced_store_refuses_on_existing_handle",
    "paper_only": "test_paper_only_receipt",
    "frozen_inputs": "test_typed_input_and_single_source_contract",
    "clock_regressed": "test_clock_cannot_move_behind_saved_receipt",
    "batch_identity_reused": "test_batch_identity_conflict_never_overwrites_receipt",
    "new_receipt_bound": "test_capacity_refusals_do_not_partially_write[False-MAX_RECEIPTS-new_receipt_bound]",
    "new_source_record_changed": "test_changed_record_in_later_batch_is_refused_atomically",
    "new_store_row_bound": "test_capacity_refusals_do_not_partially_write[False-MAX_STORE_ROWS-new_store_row_bound]",
}


def cases():
    source = (ROOT / MODULE).read_text()
    result = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_require":
            code = node.args[1].value
            assert code in TARGETS
            result.append({"id": code, "path": MODULE, "old": ast.get_source_segment(source, node),
                           "new": "None", "test": TEST + TARGETS[code]})
    assert {row["id"] for row in result} == set(TARGETS)
    result.extend([
        {"id": "first_known_receipt_time", "path": MODULE,
         "old": 'available, source, record["schema_id"])',
         "new": '_time(record["observed_at"]), source, record["schema_id"])',
         "test": TEST + "test_import_uses_actual_receipt_time_not_historical_observation_time"},
        {"id": "shared_insert_transaction", "path": MODULE, "old": 'self._insert_observation(item)',
         "new": 'PointInTimeFeatureStore.append(self, item)',
         "test": TEST + "test_write_failure_rolls_back_every_row[received_feature_records]"},
        {"id": "receipt_sql_immutability", "path": MODULE, 'old': 'for verb in ("UPDATE", "DELETE"):',
         "new": 'for verb in ():', "test": TEST + "test_receipt_tables_are_append_only"},
        {"id": "cli_paper_only", "path": "scripts/ingest_feature_observations.py",
         "old": 'os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false"', "new": 'False',
         "test": "tests/test_received_feature_integration.py::test_cli_refuses_live_mode_before_creating_database"},
    ])
    return result


CASES = cases()


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_receipt_guard_needs_passing_control_and_named_assertion_failure(tmp_path, case):
    protected = [ROOT / MODULE, ROOT / "src/quant_ai/features/store.py",
                 ROOT / "tests/test_received_feature_ingestion.py"]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    run_guard(tmp_path, case)
    failures = [(row.get("name"), failure.get("message", ""))
        for row in ET.parse(tmp_path / "mutant.xml").iter("testcase")
        for failure in row.findall("failure")]
    assert failures and all(name and message.startswith(("assert ", "AssertionError:", "Failed:"))
                            for name, message in failures), failures
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
