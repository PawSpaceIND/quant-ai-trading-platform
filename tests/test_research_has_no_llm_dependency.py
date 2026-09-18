"""Research must import without the agent stack or its LLM SDK behind it."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

PROBE = """
import sys
sys.path.insert(0, {src!r})
import {module}
loaded = sorted(name for name in sys.modules if name.split('.')[0] in {{'anthropic', 'kiteconnect', 'ib_async'}})
print(','.join(loaded))
"""


def modules_pulled_in(module: str) -> set:
    # A subprocess, because sys.modules in this process already carries whatever the rest of
    # the suite imported. The question is what this module drags in on its own.
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(src=str(SRC), module=module)],
        capture_output=True, text=True, check=True,
    )
    return {name for name in result.stdout.strip().split(",") if name}


def test_the_study_runner_does_not_need_an_llm_sdk_installed():
    # It reached anthropic through study_runner -> backtesting.replay -> agents.swarm ->
    # agents.atlas -> llm.anthropic_client, all for one type annotation on one method. A
    # feature study never calls a model, and a research run should not fail on a machine
    # that has no model SDK on it.
    assert modules_pulled_in("quant_ai.research.study_runner") == set()


def test_the_feature_study_and_library_are_equally_free_of_it():
    assert modules_pulled_in("quant_ai.research.feature_study") == set()
    assert modules_pulled_in("quant_ai.features.library") == set()


def test_the_dataset_loader_is_free_of_it_while_the_replay_engine_may_not_be():
    # The split is the point. Reading a JSON file of bars needs no agent stack; the replay
    # engine runs the swarm and genuinely does, so requiring it to be clean would be asking
    # the wrong thing.
    assert modules_pulled_in("quant_ai.backtesting.datasets") == set()
    assert modules_pulled_in("quant_ai.backtesting.replay") != set()


def test_the_archive_and_validation_path_is_free_of_it():
    for module in (
        "quant_ai.marketdata.bhavcopy",
        "quant_ai.marketdata.listing_reconstruction",
        "quant_ai.marketdata.action_reconciliation",
        "quant_ai.validation.deflated_sharpe",
        "quant_ai.validation.track_record",
        "quant_ai.execution.cost_calibration",
        "quant_ai.risk.session_flatten",
    ):
        assert modules_pulled_in(module) == set(), module
