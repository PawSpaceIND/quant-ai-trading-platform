"""Corrections and restarts cannot silently change an accepted weekly learning policy."""
import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from test_governed_learning import NOW, TENANT, broker_for, build_daemon, seed_week

from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import specialist_skill as skill


def seeded(tmp_path):
    broker = broker_for(tmp_path)
    seed_week(broker)
    return broker


def correct_outcome(broker):
    assert journal.update_decision(broker, "d-14-0", forward_return_60m="-0.01")


def test_hash_binds_outcome_values_and_sample_policy_not_only_ids(tmp_path):
    broker = seeded(tmp_path)
    before = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    correct_outcome(broker)
    after = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    assert before["basis_sha256"] != after["basis_sha256"]
    assert skill.weights_of(before) != skill.weights_of(after)
    strict = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW, minimum_sample=41)
    assert strict["basis_sha256"] != after["basis_sha256"]
    assert strict["basis_schema"] == skill.BASIS_SCHEMA


def test_restart_keeps_accepted_weights_after_historical_correction(tmp_path):
    daemon, broker, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seed_week(broker)
    accepted = daemon._refresh_specialist_skill(NOW)
    original = daemon.scheduler.pipeline.runtime.attribution.skill_weights.copy()
    archive = skill.accepted_path(skill.report_path(daemon.decision_quality_report_path), TENANT, NOW)
    archive_bytes = archive.read_bytes()
    correct_outcome(broker)
    # The display file may be missing or replaced by another reporting command.
    skill.report_path(daemon.decision_quality_report_path).unlink()
    restarted, _, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None, broker=broker)
    restored = restarted._refresh_specialist_skill(NOW + timedelta(days=2))
    assert restored == accepted
    assert restarted.scheduler.pipeline.runtime.attribution.skill_weights == original
    assert archive.read_bytes() == archive_bytes
    next_report = restarted._refresh_specialist_skill(NOW + timedelta(days=7))
    assert next_report["basis_sha256"] != accepted["basis_sha256"]
    assert archive.read_bytes() == archive_bytes


def test_restarted_first_decision_uses_the_accepted_policy(tmp_path, monkeypatch):
    daemon, broker, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seed_week(broker)
    original = daemon._refresh_specialist_skill(NOW)
    correct_outcome(broker)
    restarted, _, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None, broker=broker)
    tick = restarted.scheduler.run_tick
    observed = []

    def observe(*args, **kwargs):
        engine = restarted.scheduler.pipeline.runtime.attribution
        observed.append((engine.skill_basis, engine.skill_weights.copy()))
        return tick(*args, **kwargs)

    monkeypatch.setattr(restarted.scheduler, "run_tick", observe)
    asyncio.run(restarted.run_once(NOW))
    assert observed == [(original["basis_sha256"], skill.weights_of(original))]


def test_upgrade_preserves_existing_week_and_labels_legacy_basis(tmp_path):
    broker = seeded(tmp_path)
    path = tmp_path / "specialist-skill.json"
    legacy = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    legacy.pop("basis_schema")
    skill.write_skill_report(path, legacy)
    correct_outcome(broker)
    accepted = skill.accepted_weekly_report(path, broker, tenant_id=TENANT, now=NOW)
    assert skill.weights_of(accepted) == skill.weights_of(legacy)
    assert accepted["basis_sha256"] == legacy["basis_sha256"]
    assert accepted["basis_schema"] == "legacy_decision_ids_only"


@pytest.mark.parametrize("mutation", [
    lambda envelope: envelope["report"].update(tenant_id="other"),
    lambda envelope: envelope["report"].update(week_start="2026-09-14T00:00:00+05:30"),
    lambda envelope: envelope["report"]["agents"][0].update(weight="1.24"),
    lambda envelope: envelope.update(report_sha256="0" * 64),
])
def test_corrupt_archive_refuses_recomputation_and_clears_old_weights(tmp_path, mutation):
    daemon, broker, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seed_week(broker)
    daemon._refresh_specialist_skill(NOW)
    path = skill.accepted_path(skill.report_path(daemon.decision_quality_report_path), TENANT, NOW)
    envelope = json.loads(path.read_text())
    mutation(envelope)
    path.write_text(json.dumps(envelope))
    damaged = path.read_bytes()
    daemon._skill_week = None  # exercise reload while the engine still holds prior weights
    with pytest.raises(ValueError):
        daemon._refresh_specialist_skill(NOW)
    assert daemon.scheduler.pipeline.runtime.attribution.skill_weights == {}
    assert path.read_bytes() == damaged


def test_invalid_legacy_weights_are_not_silently_partially_applied(tmp_path):
    broker = seeded(tmp_path)
    report = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    report["agents"][0]["weight"] = "NaN"
    path = tmp_path / "specialist-skill.json"
    skill.write_skill_report(path, report)
    with pytest.raises(ValueError, match="weight_invalid"):
        skill.accepted_weekly_report(path, broker, tenant_id=TENANT, now=NOW)
    assert not skill.accepted_path(path, TENANT, NOW).exists()


def test_research_sample_floor_cannot_replace_the_daemons_accepted_policy(tmp_path):
    broker = seeded(tmp_path)
    report = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW, minimum_sample=1)
    path = tmp_path / "specialist-skill.json"
    skill.write_skill_report(path, report)
    with pytest.raises(ValueError, match="policy_invalid"):
        skill.accepted_weekly_report(path, broker, tenant_id=TENANT, now=NOW)


def test_competing_initializers_accept_one_complete_report(tmp_path, monkeypatch):
    broker = seeded(tmp_path)
    first = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    correct_outcome(broker)
    second = skill.build_skill_report(broker, tenant_id=TENANT, now=NOW)
    candidates = iter([first, second])
    barrier = threading.Barrier(2)

    def build(*args, **kwargs):
        candidate = copy.deepcopy(next(candidates))
        barrier.wait(timeout=5)
        return candidate

    monkeypatch.setattr(skill, "build_skill_report", build)
    path = tmp_path / "specialist-skill.json"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(skill.accepted_weekly_report, path, broker, tenant_id=TENANT, now=NOW) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert results[0] in (first, second)
    assert not list(skill.accepted_path(path, TENANT, NOW).parent.glob(".skill-*"))


def test_other_tenant_cannot_reuse_the_accepted_policy(tmp_path):
    broker = seeded(tmp_path)
    path = tmp_path / "specialist-skill.json"
    first = skill.accepted_weekly_report(path, broker, tenant_id=TENANT, now=NOW)
    other = skill.accepted_weekly_report(path, broker, tenant_id="../other", now=NOW)
    assert first["applied"] == 3 and other["applied"] == 0
    assert skill.accepted_path(path, TENANT, NOW) != skill.accepted_path(path, "../other", NOW)


def test_corrupt_archive_is_reported_as_unavailable_without_stale_weights(tmp_path):
    daemon, broker, _ = build_daemon(tmp_path, lambda: NOW, post_mortem_dir=None)
    seed_week(broker)
    daemon._refresh_specialist_skill(NOW)
    path = skill.accepted_path(skill.report_path(daemon.decision_quality_report_path), TENANT, NOW)
    path.write_text("{")
    daemon._skill_week = None
    daemon._write_decision_quality(NOW)
    report = json.loads(daemon.decision_quality_report_path.read_text())
    assert report["specialist_skill"] == {"state": "unavailable", "applied_to_engine": False}
    assert daemon.scheduler.pipeline.runtime.attribution.skill_weights == {}
