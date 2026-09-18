"""Reuse the existing isolated control/mutant harness for shadow-only boundaries."""
from __future__ import annotations

import pytest
from test_learning_guard_sensitivity import test_learning_guard_sensitivity as run_guard

MODULE = "quant_ai.learning.shadow"
TEST = "tests/test_shadow_forecast_lineage.py::"
CHECKS = [
    ("dataset_binding", "test_training_and_dataset_bindings_are_rechecked"),
    ("artifact_binding", "test_changed_artifact_bytes_refused_even_with_same_shape"),
    ("training_time", "test_future_trained_model_is_not_usable"),
    ("input_time", "test_input_not_available_at_decision_is_rejected"),
    ("logit_outside_supported_domain", "test_logit_domain_refuses_overflow_instead_of_saturating"),
    ("journal_tenant_binding", "test_wrong_tenant_is_refused_before_modifying_the_store"),
    ("forecast_replay_mismatch", "test_rehashed_forged_probability_fails_model_replay"),
    ("journal_path_replaced", "test_byte_valid_but_replaced_store_is_not_written_via_old_handle"),
]
CASES = [
    (name, MODULE, [("if not condition:", f"if not condition and code != {name!r}:")], TEST + target)
    for name, target in CHECKS
] + [
    ("append_only", MODULE, [('for verb in ("UPDATE", "DELETE"):', 'for verb in ():')],
     TEST + "test_lineage_records_are_append_only"),
    ("atomic_forecast_and_source", MODULE,
     [("            self.journal._insert_forecast(item)", "            self.journal._insert_forecast(item)\n            db.commit()")],
     TEST + "test_forecast_is_rolled_back_when_its_source_insert_fails"),
    ("stable_input_snapshot", MODULE,
     [("        bundle = ShadowModelBundle.from_payload(decode(raw_model.encode()))", ""),
      ("        snapshot = decode(_canonical(snapshot).encode())", "")],
     TEST + "test_clock_callback_cannot_substitute_supplied_model_or_features"),
    ("mandatory_monitor_lineage", "quant_ai.analytics.learning_monitor",
     [('if config.get("require_shadow_lineage") or has_shadow:', 'if has_shadow:')],
     TEST + "test_mandatory_lineage_monitor_cannot_fall_back_to_unscoped_journal"),
]


@pytest.mark.parametrize("name,module,replacements,target", CASES, ids=[case[0] for case in CASES])
def test_shadow_guard_sensitivity(tmp_path, name, module, replacements, target):
    run_guard(tmp_path, name, module, replacements, target)
