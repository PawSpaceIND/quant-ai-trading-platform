"""One individually removed guard, one named regression, restored source.

Fixtures are synthetic. This verifies rejection behavior, not market performance.
The inventory counts each _require call site independently, even repeated reasons.
It additionally covers the rejection dispatcher, both explicit rejection handlers,
and the two delegated daily-input checks. It is not mutation coverage of every
boolean subexpression, arithmetic operator, library internals or possible fault.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_research_publisher import (
    INSTRUMENT,
    NOW,
    STUDY,
    producer_dataset,
    seeded_register,
    series,
)

from quant_ai.backtesting import research_publisher as pub
from quant_ai.domain.models import Instrument
from quant_ai.validation.trial_register import record_trials

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path("src/quant_ai/backtesting/research_publisher.py")
THIS_TEST = "tests/test_research_publisher_guard_inventory.py"
D = Decimal
# ID, containing function, zero-based _require site in that function, reason prefix.
SITES = [
    ("number_decimal", "_number", 0, "research_metric_not_finite"),
    ("number_float", "_number", 1, "research_metric_not_finite"),
    ("split_empty", "_assert_split", 0, "research_empty_split"),
    ("split_partition", "_assert_split", 1, "research_split_not_partition"),
    ("split_dates", "_assert_split", 2, "research_holdout_overlaps_training"),
    ("run_observations", "_assert_evidence_floor", 0, "research_observations_below_minimum"),
    ("run_trades", "_assert_evidence_floor", 1, "research_round_trips_below_minimum"),
    ("run_shape", "_assert_run", 0, "research_run_does_not_match_scored_bars"),
    ("run_equity", "_assert_run", 1, "research_invalid_equity_path"),
    ("run_returns", "_assert_run", 2, "research_return_path_mismatch"),
    ("stress_short", "path_stress", 0, "research_stress_too_short"),
    ("stress_returns", "path_stress", 1, "research_stress_invalid_returns"),
    ("ui_schema", "validate_report", 0, "research_ui_schema_invalid"),
    ("ui_section", "validate_report", 1, "research_ui_section_missing"),
    ("ui_metric", "validate_report", 2, "research_ui_metric_invalid"),
    ("ui_label", "validate_report", 3, "research_ui_label_missing"),
    ("ui_clock", "validate_report", 4, "research_timestamp_not_aware"),
    ("ui_limitations", "validate_report", 5, "research_limitations_empty"),
    ("ui_trials", "validate_report", 6, "research_trial_count_invalid"),
    ("ui_observations", "validate_report", 7, "research_observations_below_minimum"),
    ("ui_trades", "validate_report", 8, "research_round_trips_below_minimum"),
    ("ui_size", "validate_report", 9, "research_report_too_large"),
    ("write_count", "_atomic_write", 0, "research_partial_write"),
    ("write_readback", "_atomic_write", 1, "research_partial_write"),
    ("register_file", "_exclusive_register", 0, "research_existing_trial_register_required"),
    ("prior_order", "_assert_unseen", 0, "research_prior_trial_window_invalid"),
    ("prior_overlap", "_assert_unseen", 1, "research_holdout_previously_observed"),
    ("snapshot_drift", "_register_snapshot", 0, "research_trial_register_changed_during_read"),
    ("publish_paper", "_publish_research", 0, "research_requires_paper_only"),
    ("publish_clock", "_publish_research", 1, "research_timestamp_not_aware"),
    ("publish_support", "_publish_research", 2, "research_instrument_not_supported"),
    ("publish_identity", "_publish_research", 3, "research_instrument_mismatch"),
    ("publish_finite", "_publish_research", 4, "research_nonfinite_bar"),
    ("publish_closed", "_publish_research", 5, "research_unclosed_daily_bar"),
    ("publish_train", "_publish_research", 6, "research_training_too_short"),
    ("publish_holdout", "_publish_research", 7, "research_holdout_too_short"),
    ("publish_alias", "_publish_research", 8, "research_output_overwrites_register"),
    ("publish_study", "_publish_research", 9, "research_existing_study_required"),
    ("publish_fitted", "_publish_research", 10, "research_no_training_candidate_meets_floor"),
    ("publish_size", "_publish_research", 11, "research_report_too_large"),
    ("publish_drift", "_publish_research", 12, "research_trial_register_changed_during_publication"),
    ("json_duplicate", "_unique_pairs", 0, "research_duplicate_json_key"),
    ("cli_data", "publish_from_args", 0, "publish-research requires --data"),
    ("cli_suffix", "publish_from_args", 1, "research_declared_json_dataset_required"),
    ("cli_prices", "publish_from_args", 2, "research_raw_daily_price_provenance_required"),
    ("cli_identity", "publish_from_args", 3, "research_declared_instrument_required"),
    ("cli_market", "publish_from_args", 4, "research_market_contradicts_dataset"),
    ("cli_dates", "publish_from_args", 5, "research_date_range_reversed"),
    ("cli_window", "publish_from_args", 6, "research_no_bars_in_range"),
    ("cli_output", "publish_from_args", 7, "research_output_overwrites_dataset"),
    ("cli_register", "publish_from_args", 8, "research_register_overwrites_dataset"),
    ("cli_source", "publish_from_args", 9, "research_source_declaration_required"),
]
EXTRA_REJECTIONS = {
    "rejection_dispatch": "synthetic_rejection",
    "prior_unknown": "research_prior_trial_window_unknown_or_invalid",
    "lock_contention": "research_publisher_already_running",
}
IDS = [item[0] for item in SITES] + list(EXTRA_REJECTIONS)
REASONS = {item[0]: item[3] for item in SITES} | EXTRA_REJECTIONS
# _assert_unseen deliberately normalizes its invalid-window refusal.
REASONS["prior_order"] = "research_prior_trial_window_unknown_or_invalid"


@pytest.fixture(autouse=True)
def paper_only(monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")


def _sites(tree):
    result = {}
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        calls = sorted((node for node in ast.walk(function)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "_require"), key=lambda node: node.lineno)
        for index, call in enumerate(calls):
            result[function.name, index] = call
    return result


def test_every_require_site_has_individual_sabotage():
    actual = _sites(ast.parse((ROOT / MODULE).read_text()))
    expected = {(function, index) for _, function, index, _ in SITES}
    assert len(expected) == len(SITES) == 52
    assert set(actual) == expected, "A guard was added/removed without updating individual evidence"
    for _, function, index, reason in SITES:
        assert reason in ast.unparse(actual[function, index].args[1])


def _valid_report():
    return {
        "schema": "pramana.research.v1", "strategy": "synthetic validation fixture",
        "scope": "not market evidence", "created_at": NOW.isoformat(),
        "data_sha256": "a" * 64, "candidate_trials": 2,
        "trial_register": {"candidate_trials": 2},
        "holdout": {"net_return": 0, "max_drawdown": 0, "observations": 30, "round_trips": 20},
        "buy_and_hold": {"net_return": 0}, "path_stress": {"max_drawdown_p95": 0},
        "limitations": ["Synthetic shape fixture, not computed market returns."],
    }


def _ui_probe(name):
    report = _valid_report()
    changes = {
        "ui_schema": (report, "schema", "wrong"),
        "ui_section": (report, "buy_and_hold", []),
        "ui_metric": (report["holdout"], "net_return", float("nan")),
        "ui_label": (report, "scope", ""),
        "ui_clock": (report, "created_at", NOW.replace(tzinfo=None).isoformat()),
        "ui_limitations": (report, "limitations", []),
        "ui_trials": (report, "candidate_trials", 1),
        "ui_observations": (report["holdout"], "observations", 29),
        "ui_trades": (report["holdout"], "round_trips", 19),
        "ui_size": (report, "strategy", "x" * (pub.MAX_REPORT_BYTES + 1)),
    }
    target, key, value = changes[name]
    target[key] = value
    pub.validate_report(report)


def _write_probe(name, folder, monkeypatch):
    path = folder / "report.json"
    path.write_text("previous synthetic report")
    original = pub.os.fdopen

    class ContractBreakingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def write(self, data):
            if name == "write_count":
                # Complete bytes but an incorrect count isolates the first guard.
                self.stream.write(data)
                return len(data) - 1
            # A lying full count isolates readback independently of the count guard.
            self.stream.write(data[:len(data) // 2])
            return len(data)

    monkeypatch.setattr(pub.os, "fdopen", lambda *a, **kw: ContractBreakingWriter(original(*a, **kw)))
    pub._atomic_write(path, b"replacement synthetic report")


def _cli_probe(name, folder, monkeypatch):
    source = producer_dataset(folder)
    payload = json.loads(source.read_text())
    args = SimpleNamespace(data=str(source), market="india", start=None, end=None)
    output, register = folder / "report.json", folder / "trial-register.jsonl"
    if name == "cli_data":
        args.data = None
    elif name == "cli_suffix":
        source = source.with_suffix(".txt")
        args.data = str(source)
    elif name == "cli_prices":
        payload["provenance"]["adjusted_close_used"] = True
    elif name == "cli_identity":
        payload["provenance"]["instrument"] = None
    elif name == "cli_market":
        args.market = "us"
    elif name == "cli_dates":
        args.start, args.end = "2012-01-01", "2011-01-01"
    elif name == "cli_window":
        args.start = "2030-01-01"
    elif name == "cli_output":
        output = source
    elif name == "cli_register":
        register = source
    elif name == "cli_source":
        payload["provenance"]["source"] = ""
    else:
        raise AssertionError(name)
    source.write_text(json.dumps(payload))
    monkeypatch.setenv("PRAMANA_RESEARCH_REPORT", str(output))
    monkeypatch.setattr(pub.paths, "trial_register", lambda *a: register)
    monkeypatch.setattr(pub, "publish_research", lambda *a, **kw: {
        "report_sha256": "a" * 64, "candidate_trials": 23,
    })
    pub.publish_from_args(args)


def _publish_probe(name, folder, monkeypatch):
    bars = series()
    register = seeded_register(folder, bars)
    arguments = {"instrument": INSTRUMENT, "register": register,
                 "output": folder / "report.json", "now": NOW}
    if name == "publish_paper":
        monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    elif name == "publish_clock":
        arguments["now"] = NOW.replace(tzinfo=None)
    elif name == "publish_support":
        arguments["instrument"] = replace(INSTRUMENT, exchange="BSE")
    elif name == "publish_identity":
        arguments["instrument"] = Instrument("TCS", INSTRUMENT.market, INSTRUMENT.asset_class,
                                             INSTRUMENT.currency, INSTRUMENT.exchange)
    elif name == "publish_finite":
        bars = (replace(bars[0], volume=D("Infinity")),) + bars[1:]
    elif name == "publish_closed":
        arguments["now"] = bars[-1].timestamp + timedelta(hours=6)
    elif name == "publish_train":
        bars = bars[:40]
    elif name == "publish_holdout":
        bars = bars[:80]
    elif name == "publish_alias":
        arguments["output"] = register
    elif name == "publish_study":
        other = folder / "other-study.jsonl"
        record_trials(other, study="replay:TCS:INDIA", candidate_trials=1,
                      configuration={}, data_sha256="a" * 64, now=NOW)
        arguments["register"] = other
    elif name == "publish_fitted":
        bars = tuple(replace(bar, open=D(100), high=D(101), low=D(99), close=D(100)) for bar in bars)
    elif name == "publish_size":
        original = pub.validate_report

        def set_exact_pre_hash_cap(report):
            original(report)
            # An unsigned report fits; adding report_sha256 crosses the final cap.
            monkeypatch.setattr(pub, "MAX_REPORT_BYTES", len(pub._canonical(report)))

        monkeypatch.setattr(pub, "validate_report", set_exact_pre_hash_cap)
    elif name == "publish_drift":
        original = pub.path_stress

        def changed(returns):
            result = original(returns)
            with register.open("ab") as stream:
                stream.write(b"\n")
            return result

        monkeypatch.setattr(pub, "path_stress", changed)
    else:
        raise AssertionError(name)
    pub.publish_research(bars, **arguments)


def _probe(name, folder, monkeypatch):
    if name.startswith("ui_"):
        return _ui_probe(name)
    if name.startswith("write_"):
        return _write_probe(name, folder, monkeypatch)
    if name.startswith("cli_"):
        return _cli_probe(name, folder, monkeypatch)
    if name.startswith("publish_"):
        return _publish_probe(name, folder, monkeypatch)
    if name == "number_decimal":
        return pub._number(D("sNaN"))
    if name == "number_float":
        return pub._number(D("1e1000"))
    if name.startswith("split_"):
        bars = series(3)
        if name == "split_empty":
            return pub._assert_split(bars, (), bars)
        if name == "split_partition":
            return pub._assert_split(bars, bars[:1], bars)
        held = replace(bars[1], timestamp=bars[0].timestamp + timedelta(hours=1))
        return pub._assert_split((bars[0], held), (bars[0],), (held,))
    if name in {"run_observations", "run_trades"}:
        run = SimpleNamespace(returns=(D(0),) * (29 if name == "run_observations" else 30),
                              round_trips=(None,) * (19 if name == "run_trades" else 20))
        return pub._assert_evidence_floor(run)
    if name in {"run_shape", "run_equity", "run_returns"}:
        curve = (D(99999), D(100000)) if name == "run_equity" else (D(100000), D(101000))
        returns = (D(".02"),) if name == "run_returns" else ((curve[1] - curve[0]) / curve[0],)
        return pub._assert_run(SimpleNamespace(equity_curve=curve, returns=returns),
                               series(3 if name == "run_shape" else 2))
    if name == "stress_short":
        return pub.path_stress((D(0),) * 29)
    if name == "stress_returns":
        return pub.path_stress((D(-1),) * 30)
    if name == "json_duplicate":
        return pub._unique_pairs([("source", "first"), ("source", "second")])
    if name == "rejection_dispatch":
        return pub._require(False, "synthetic_rejection")
    if name == "register_file":
        with pub._exclusive_register(folder / "absent.jsonl"):
            return None
    bars = series()
    register = seeded_register(folder, bars)
    if name == "lock_contention":
        with pub._exclusive_register(register), pub._exclusive_register(register):
            return None
    if name == "snapshot_drift":
        original = pub.read_records

        def changed(path):
            result = original(path)
            with register.open("ab") as stream:
                stream.write(b"\n")
            return result

        monkeypatch.setattr(pub, "read_records", changed)
        return pub._register_snapshot(register, STUDY)
    if name in {"prior_order", "prior_overlap", "prior_unknown"}:
        config = {} if name == "prior_unknown" else {
            "start": bars[20 if name == "prior_order" else 840].timestamp.isoformat(),
            "end": bars[10 if name == "prior_order" else -1].timestamp.isoformat(),
        }
        record_trials(register, study=STUDY, candidate_trials=1, configuration=config,
                      data_sha256="a" * 64, now=NOW)
        return pub._assert_unseen(register, STUDY, bars[840:], INSTRUMENT)
    raise AssertionError(f"missing probe: {name}")


@pytest.mark.parametrize("name", IDS, ids=IDS)
def test_guard_rejection(tmp_path, monkeypatch, name):
    error = SystemExit if name.startswith("cli_") else pub.ResearchPublicationRefused
    with pytest.raises(error, match=re.escape(REASONS[name])):
        _probe(name, tmp_path, monkeypatch)


def test_daily_publisher_rejects_before_recording_trial(tmp_path):
    bars = series()
    register = seeded_register(tmp_path, bars)
    before = register.read_bytes()
    bad = (bars[0], replace(bars[1], timestamp=bars[0].timestamp + timedelta(hours=1))) + bars[2:]
    with pytest.raises(ValueError, match="daily bars"):
        pub.publish_research(bad, instrument=INSTRUMENT, register=register,
                             output=tmp_path / "report.json", now=NOW)
    assert register.read_bytes() == before, "Invalid input must not consume research history"
    assert not (tmp_path / "report.json").exists()


def test_daily_cli_rejects_before_publisher(tmp_path, monkeypatch):
    source = producer_dataset(tmp_path)
    payload = json.loads(source.read_text())
    payload["bars"][1]["timestamp"] = (series()[0].timestamp + timedelta(hours=1)).isoformat()
    source.write_text(json.dumps(payload))
    received = []

    def capture(*args, **kwargs):
        received.append(1)
        return {"report_sha256": "a" * 64, "candidate_trials": 23}

    monkeypatch.setattr(pub, "publish_research", capture)
    monkeypatch.setenv("PRAMANA_RESEARCH_REPORT", str(tmp_path / "report.json"))
    monkeypatch.setattr(pub.paths, "trial_register", lambda *a: tmp_path / "trials.jsonl")
    with pytest.raises(SystemExit, match="research publication refused: ValueError"):
        pub.publish_from_args(SimpleNamespace(data=str(source), market="india", start=None, end=None))
    assert not received


MUTATIONS = [(name, f"test_guard_rejection[{name}]") for name in IDS] + [
    ("daily_publisher", "test_daily_publisher_rejects_before_recording_trial"),
    ("daily_cli", "test_daily_cli_rejects_before_publisher"),
]


def _mutate(source, name):
    tree = ast.parse(source)
    positions = {identifier: (function, index) for identifier, function, index, _ in SITES}
    if name in positions:
        _sites(tree)[positions[name]].args[0] = ast.Constant(True)
    else:
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        if name == "rejection_dispatch":
            functions["_require"].body = [ast.Pass()]
        elif name in {"prior_unknown", "lock_contention"}:
            function = functions["_assert_unseen" if name == "prior_unknown" else "_exclusive_register"]
            handlers = [node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)]
            assert len(handlers) == 1
            handlers[0].body = [ast.Continue() if name == "prior_unknown" else ast.Pass()]
        else:
            function = functions["_publish_research" if name == "daily_publisher" else "publish_from_args"]
            calls = [node for node in ast.walk(function)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == "_assert_daily_bars"]
            assert len(calls) == 1
            calls[0].func = ast.Name(id="tuple", ctx=ast.Load())
            calls[0].args = []
    result = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    compile(result, str(MODULE), "exec")
    assert result != source
    return result


@pytest.mark.parametrize("name,test", MUTATIONS, ids=[name for name, _ in MUTATIONS])
def test_individual_guard_sabotage(tmp_path, name, test):
    original = (ROOT / MODULE).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    isolated = tmp_path / "isolated"
    shutil.copytree(ROOT / "src", isolated / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (isolated / "tests").mkdir()
    for filename in (Path(THIS_TEST).name, "test_research_publisher.py"):
        shutil.copy2(ROOT / "tests" / filename, isolated / "tests" / filename)
    env = {**os.environ, "PYTHONPATH": str(isolated / "src"),
           "PYTHONDONTWRITEBYTECODE": "1", "TRADING_LIVE_MONEY_ACTIVE": "false"}
    env.pop("PYTEST_ADDOPTS", None)

    def execute(label):
        xml = tmp_path / f"{label}.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", f"{THIS_TEST}::{test}", f"--junitxml={xml}"],
            cwd=isolated, env=env, capture_output=True, text=True, timeout=120, check=False,
        )
        log = result.stdout + result.stderr
        (tmp_path / f"{label}.log").write_text(log)
        assert xml.is_file(), log
        suites = list(ET.parse(xml).getroot().iter("testsuite"))
        assert sum(int(item.get("errors", "0")) for item in suites) == 0, log
        assert sum(int(item.get("skipped", "0")) for item in suites) == 0, log
        failures = sum(int(item.get("failures", "0")) for item in suites)
        return result, failures, log

    try:
        control, failures, log = execute("control")
        assert control.returncode == 0 and failures == 0, log
        (isolated / MODULE).write_text(_mutate(original.decode(), name))
        mutant, failures, log = execute("mutant")
        assert mutant.returncode == 1 and failures > 0, f"SURVIVING/INVALID MUTANT {name}\n{log}"
    finally:
        (isolated / MODULE).write_bytes(original)
        assert hashlib.sha256((ROOT / MODULE).read_bytes()).hexdigest() == digest
        assert (isolated / MODULE).read_bytes() == original
