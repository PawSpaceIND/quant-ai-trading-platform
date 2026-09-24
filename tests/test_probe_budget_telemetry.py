"""The probe budget as the running engine applies it, published so "off" reads as off.

The exploration budget is operator-set (PRAMANA_EXPLORATION_*) and defaults to a cap of
0. A book with the cap at 0 and a book whose budget found nothing to probe are equally
silent, so the engine publishes the budget it is actually running - from the policy
object, not re-read from the environment - and the dashboard says which one it is.
Nothing here raises a cap, lowers a bar or probes a hard hold.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_pilot_closure import publish_tick
from test_pilot_closure import runner_for as pilot_runner_for

NOW = datetime.now(timezone.utc)
SETTINGS = ("PRAMANA_EXPLORATION_MAX_PER_DAY", "PRAMANA_EXPLORATION_MIN_CONFIDENCE",
            "PRAMANA_EXPLORATION_NOTIONAL_FRACTION", "PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE",
            "PRAMANA_REGIME_PLAYBOOKS")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


def published(tmp_path, monkeypatch, **environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    runner = pilot_runner_for(tmp_path)
    runner.daemon.clock = lambda: NOW
    publish_tick(runner, "100", NOW)
    return runner


def exploration(runner):
    runner.daemon.protection_tick(NOW)
    row = runner.daemon.tracker.broker._connection.execute("SELECT payload FROM pilot_runtime").fetchone()
    return json.loads(row[0])["exploration"]


def atlas(runner):
    return runner.daemon.scheduler.pipeline.runtime.cio.atlas


def test_a_cap_of_zero_is_published_as_off_with_the_setting_that_arms_it(tmp_path, monkeypatch) -> None:
    block = exploration(published(tmp_path, monkeypatch))
    assert block == {
        "schema": "pramana.exploration.v1", "tenantId": "pilot", "checkedAt": NOW.isoformat(),
        "setting": "PRAMANA_EXPLORATION_MAX_PER_DAY", "armed": False, "maxPerDay": 0,
        "minWeightedScore": 0.45, "minConfidence": 0.40, "notionalFraction": 0.01,
        "withheldInRegimes": ["high_volatility", "trending_down"],
    }


def test_the_published_budget_is_the_one_the_engine_booted_with(tmp_path, monkeypatch) -> None:
    runner = published(tmp_path, monkeypatch, PRAMANA_EXPLORATION_MAX_PER_DAY="3",
                       PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE="0.35",
                       PRAMANA_EXPLORATION_NOTIONAL_FRACTION="0.02")
    block = exploration(runner)
    assert (block["armed"], block["maxPerDay"], block["minWeightedScore"], block["notionalFraction"]) == (
        True, 3, 0.35, 0.02)
    # Read from the running policy, not from the environment: a setting changed after boot
    # is not what the engine applies until it restarts, and the page must not claim it is.
    monkeypatch.setenv("PRAMANA_EXPLORATION_MAX_PER_DAY", "5")
    assert exploration(runner)["maxPerDay"] == 3
    assert atlas(runner).policy.exploration_max_per_day == 3, "publishing never changes the budget"


def test_with_routing_off_no_regime_withholds_probes(tmp_path, monkeypatch) -> None:
    block = exploration(published(tmp_path, monkeypatch, PRAMANA_REGIME_PLAYBOOKS="off"))
    assert block["withheldInRegimes"] == []


def test_an_engine_with_no_atlas_policy_reports_nothing_rather_than_off(tmp_path, monkeypatch) -> None:
    runner = published(tmp_path, monkeypatch)
    runner.daemon.scheduler.pipeline.runtime.cio.atlas = SimpleNamespace()
    assert exploration(runner) is None


def test_an_unreadable_policy_value_withholds_the_block_and_keeps_the_heartbeat(tmp_path, monkeypatch) -> None:
    runner = published(tmp_path, monkeypatch)
    agent = atlas(runner)
    # Bypass the dataclass bounds the way a corrupted object would: the heartbeat must
    # still publish, with the budget withheld rather than guessed.
    broken = replace(agent.policy)
    object.__setattr__(broken, "exploration_max_per_day", "three")
    agent.policy = broken
    assert exploration(runner) is None


def test_the_cap_ships_at_zero_in_code_and_in_compose() -> None:
    """The cap is the operator's to raise on the host; the build never raises it."""
    from decimal import Decimal
    from pathlib import Path

    import yaml

    from quant_ai.agents.atlas import AtlasPolicy

    assert AtlasPolicy().exploration_max_per_day == 0
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy/docker-compose.yml").read_text())
    ghost = compose["services"]["pramana-ghost"]["environment"]
    assert ghost["PRAMANA_EXPLORATION_MAX_PER_DAY"] == "${PRAMANA_EXPLORATION_MAX_PER_DAY:-0}"
    assert ghost["PRAMANA_EXPLORATION_NOTIONAL_FRACTION"] == "${PRAMANA_EXPLORATION_NOTIONAL_FRACTION:-0.01}"
    assert AtlasPolicy().exploration_notional_fraction == Decimal("0.01")
