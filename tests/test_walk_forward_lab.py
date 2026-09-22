"""The replayed engine against the floors, window after window, under one rule each.

One window answers "did the rules beat owning it over those months", which a single good
quarter answers "yes" to by accident. These tests pin what makes a run of windows worth
more than one: the windows cannot see each other, both sides of every comparison obey the
same drift rule or neither does, that rule is the live engine's own code rather than a
copy, every run is counted before it happens, and a loss is printed as a loss.
"""
from __future__ import annotations

import ast
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

# Sibling test modules by name: pytest puts tests/ on sys.path, the repository root it
# does not (the console script CI runs), so a ``tests.`` package import fails there.
from test_contest import FIRST, INDIA, START, bars, broker_at, replay_over

from quant_ai.backtesting import walk_forward as lab
from quant_ai.backtesting.baselines import (
    BaselineEvaluator,
    CashBaseline,
    TimeSeriesMomentumBaseline,
)
from quant_ai.backtesting.contest import NOT_THE_AI
from quant_ai.backtesting.replay import (
    HistoricalReplayHarness,
    load_replay_dataset,
)
from quant_ai.cli import main as cli_main
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode, Side
from quant_ai.execution import daemon as daemon_module
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

ROOT = Path(__file__).resolve().parents[1]
AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def gappy(count: int = 80, *, gap_bps: int = 50, instrument: Instrument = INDIA) -> tuple[Candle, ...]:
    """A trending series whose every open sits ``gap_bps`` above the prior close.

    The fixture in test_contest gaps about five basis points overnight, which the live
    tolerance never refuses; this one gaps far past it, so the rule has something to do.
    """
    made, close = [], Decimal(1000)
    for index in range(count):
        opening = close * (Decimal(1) + Decimal(gap_bps) / Decimal(10000))
        close = opening + Decimal(1)
        made.append(Candle(instrument, FIRST + timedelta(days=index), opening,
                           max(opening, close) + Decimal(4), min(opening, close) - Decimal(4),
                           close, Decimal(100000)))
    return tuple(made)


# ------------------------------------------------------------------ the folds


def test_windows_are_consecutive_and_never_overlap() -> None:
    plan = lab.folds(200, window=40, embargo=5, first_fit=60)
    assert [(fold.test.start, fold.test.stop) for fold in plan] == [(65, 105), (105, 145), (145, 185)]
    for earlier, later in zip(plan, plan[1:]):
        assert earlier.test.stop == later.test.start


def test_a_fold_never_fits_on_its_own_embargo_or_test_bars() -> None:
    """The structural guarantee. The first fitted component scored here depends on it."""
    for fold in lab.folds(300, window=30, embargo=7, first_fit=45):
        assert not set(fold.fit) & set(fold.embargo)
        assert not set(fold.fit) & set(fold.test)
        assert fold.fit.stop == fold.embargo.start and fold.embargo.stop == fold.test.start
        assert len(fold.embargo) == 7


def test_the_fit_range_expands_over_earlier_test_windows() -> None:
    """Walking forward: a later fold may learn from an earlier fold's test window."""
    plan = lab.folds(200, window=40, embargo=5, first_fit=60)
    assert plan[1].fit.stop == plan[0].test.stop - 5
    assert set(plan[0].test[:-5]) <= set(plan[1].fit)


def test_too_little_history_or_nonsense_bounds_is_refused() -> None:
    with pytest.raises(ValueError, match="walk_forward_too_few_bars"):
        lab.folds(50, window=40, embargo=5, first_fit=60)
    for bad in ({"window": 1, "embargo": 5, "first_fit": 60},
                {"window": 40, "embargo": -1, "first_fit": 60},
                {"window": 40, "embargo": 5, "first_fit": 0}):
        with pytest.raises(ValueError):
            lab.folds(500, **bad)


# ------------------------------------------------------------------ the drift rule


def test_the_gate_is_the_live_rule_at_the_live_boundary() -> None:
    """Twenty basis points exactly is allowed and a hair past it is not, either way."""
    reference = Decimal(100)
    assert lab.live_drift_gate(reference, Decimal("100.20")) is True
    assert lab.live_drift_gate(reference, Decimal("99.80")) is True
    assert lab.live_drift_gate(reference, Decimal("100.21")) is False
    assert lab.live_drift_gate(reference, Decimal("99.79")) is False
    # No usable reference is refused, as live refuses it.
    assert lab.live_drift_gate(Decimal(0), Decimal(100)) is False


def test_the_gate_calls_the_live_code_rather_than_copying_it(monkeypatch) -> None:
    """Change the live tolerance and the lab's rule moves with it. A copy would not."""
    assert lab.live_drift_gate(Decimal(100), Decimal("100.30")) is False
    monkeypatch.setattr(daemon_module, "ENTRY_PRICE_DRIFT_TOLERANCE", Decimal("0.005"))
    assert lab.live_drift_gate(Decimal(100), Decimal("100.30")) is True


def test_the_live_tolerance_itself_is_untouched() -> None:
    assert daemon_module.ENTRY_PRICE_DRIFT_TOLERANCE == Decimal("0.002")


def test_a_floor_under_the_gate_does_not_fill_where_the_engine_would_be_refused() -> None:
    """One rule for both sides. A floor that filled freely would be scored against an
    engine that could not have."""
    series = gappy(90)
    open_ = BaselineEvaluator(instrument=INDIA).evaluate(series, (TimeSeriesMomentumBaseline(),))[0]
    gated = BaselineEvaluator(instrument=INDIA, order_gate=lab.live_drift_gate).evaluate(
        series, (TimeSeriesMomentumBaseline(),))[0]
    assert open_.to_dict()["trades"] > 0
    assert gated.to_dict()["trades"] == 0


def test_the_gate_holds_for_the_gross_curve_too() -> None:
    """Gross and net must still differ by friction alone, never by which orders filled."""
    report = BaselineEvaluator(instrument=INDIA, order_gate=lambda *_: False).evaluate(
        bars(90), (TimeSeriesMomentumBaseline(),))[0].to_dict()
    assert report["trades"] == 0
    assert Decimal(report["grossTotalReturn"]) == Decimal(report["netTotalReturn"]) == 0


def test_without_a_gate_a_floor_trades_exactly_as_before() -> None:
    """The hook is off by default; nothing that scored before scores differently now."""
    series = gappy(90)
    before = BaselineEvaluator(instrument=INDIA).evaluate(series, (TimeSeriesMomentumBaseline(),))
    after = BaselineEvaluator(instrument=INDIA, order_gate=None).evaluate(
        series, (TimeSeriesMomentumBaseline(),))
    assert before[0].to_dict() == after[0].to_dict()


def _harness(**kwargs) -> HistoricalReplayHarness:
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        START, Decimal("0.80"), Decimal("0.20"), expected_edge=Decimal("0.02"),
        requested_mode=RiskMode.BALANCED))
    return HistoricalReplayHarness(PaperBrokerService(":memory:", starting_capital=START), plan,
                                   quantity=None, **kwargs)


def test_the_replayed_engine_is_refused_with_the_live_reason() -> None:
    harness = _harness(order_gate=lab.live_drift_gate)
    harness._decision_close, harness._execution_open = Decimal(100), Decimal("100.50")
    proposal = SimpleNamespace(side=Side.BUY, symbol="AAPL")
    assert harness._drift_veto(proposal) == "pilot_price_moved_during_analysis"
    # Exits too, as live: the swarm's own SELL is subject to the same gate.
    assert harness._drift_veto(SimpleNamespace(side=Side.SELL, symbol="AAPL")) is not None
    harness._execution_open = Decimal("100.10")
    assert harness._drift_veto(proposal) is None


def test_a_gated_replay_says_it_was_gated_and_an_ordinary_one_does_not() -> None:
    """The run evidence records the difference from live, as it records every other."""
    gated = [item["knob"] for item in _harness(order_gate=lab.live_drift_gate).traded_configuration_differences]
    plain = [item["knob"] for item in _harness().traded_configuration_differences]
    assert "entryDriftRule" in gated and "entryDriftRule" not in plain
    # And an ordinary replay's runtime is built exactly as it was: no pre-submit hook at all.
    assert _harness().build_runtime().pre_submit_check is None


def _dataset_file(tmp_path: Path, series) -> Path:
    path = tmp_path / "daily.json"
    path.write_text(json.dumps({"bars": [
        {"timestamp": bar.timestamp.isoformat(), "open": str(bar.open), "high": str(bar.high),
         "low": str(bar.low), "close": str(bar.close), "volume": str(bar.volume)}
        for bar in series]}))
    return path


def test_a_replayed_decision_freezes_the_same_snapshot_paper_does(tmp_path) -> None:
    """The replay builds its runtime with the daemon's own builder, so Atlas freezes T0 here too."""
    series = bars(count=70, instrument=AAPL)
    dataset = load_replay_dataset(_dataset_file(tmp_path, series), AAPL)
    logger = XAITraceLogger()
    _harness(xai_logger=logger).run(dataset)
    snapshots = [trace.provenance.get("feature_snapshot") for trace in logger.traces()
                 if isinstance(trace.provenance, dict)]
    frozen = [item for item in snapshots if isinstance(item, dict)]
    assert frozen, "no replayed decision carried a snapshot"
    assert {item["schema"] for item in frozen} == {"pramana.feature_snapshot.v1"}


# ------------------------------------------------------------------ the report


def _stub_runner(curve_for):
    """Stands in for the harness: returns a chosen curve, and records the gate it was given."""
    seen = []

    def run(test_bars, gate):
        seen.append(gate)
        return replay_over(test_bars, curve=curve_for(len(test_bars))), broker_at()
    return run, seen


def test_every_window_is_scored_both_ways_and_the_gated_run_gates_the_floors_too() -> None:
    series = bars(160)
    plan = lab.folds(len(series), window=40, embargo=5, first_fit=40)
    runner, seen = _stub_runner(lambda n: tuple(START for _ in range(n)))
    report = lab.walk_forward(series, instrument=INDIA, run_replay=runner, plan=plan)
    assert seen == [None, lab.live_drift_gate] * len(plan)
    for window in report["windows"]:
        assert set(window["runs"]) == {lab.GATED, lab.UNGATED}
    assert report["noneOfTheseIsTheTradedAi"] == NOT_THE_AI


def test_a_losing_engine_is_reported_as_having_lost() -> None:
    """The line the brief asked for. An engine that bled every window says "LOST TO"."""
    series = bars(160)
    plan = lab.folds(len(series), window=40, embargo=5, first_fit=40)
    bleeding = lambda n: tuple(START - Decimal(500) * index for index in range(n))
    report = lab.walk_forward(series, instrument=INDIA, run_replay=_stub_runner(bleeding)[0], plan=plan)
    cash = report["summary"][lab.UNGATED][CashBaseline.baseline_id]
    assert cash[lab.LOSS] == len(plan) and cash[lab.WIN] == 0
    text = lab.render(report)
    assert f"LOST TO {CashBaseline.baseline_id} in {len(plan)} of {len(plan)} windows" in text
    assert text.startswith(NOT_THE_AI)


def test_a_winning_engine_is_reported_as_having_won_and_nothing_louder() -> None:
    series = bars(160)
    plan = lab.folds(len(series), window=40, embargo=5, first_fit=40)
    climbing = lambda n: tuple(START + Decimal(2000) * index for index in range(n))
    report = lab.walk_forward(series, instrument=INDIA, run_replay=_stub_runner(climbing)[0], plan=plan)
    text = lab.render(report)
    assert f"beat {CashBaseline.baseline_id} in {len(plan)} of {len(plan)} windows" in text
    assert "LOST TO" not in text


def test_a_split_record_is_called_no_better_rather_than_a_win() -> None:
    line = lab.verdict_line("baseline.cash", {lab.WIN: 2, lab.LOSS: 2, lab.TIE: 1}, lab.GATED)
    assert "no better than baseline.cash" in line and "1 tied" in line


# ------------------------------------------------------------------ the operator command


def _cli(tmp_path, monkeypatch, *args: str) -> list[str]:
    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    return list(args)


def test_the_operator_command_runs_every_window_and_leaves_the_evidence(tmp_path, monkeypatch, capsys) -> None:
    data = _dataset_file(tmp_path, bars(count=130, instrument=AAPL))
    argv = _cli(tmp_path, monkeypatch, "walk-forward", "--data", str(data), "--market", "us",
                "--window", "40", "--embargo", "5")
    assert cli_main(argv) == 0
    printed = capsys.readouterr().out
    assert printed.startswith(NOT_THE_AI)
    assert "Under the live drift rule" in printed and "Under no drift rule" in printed
    written = json.loads((tmp_path / "xai" / "latest-walk-forward.json").read_text())
    assert written["schema"] == lab.SCHEMA and len(written["windows"]) == 2
    knobs = {item["knob"] for item in written["tradedConfigurationDifferences"]}
    assert "decisionMaker" in knobs


def test_every_run_is_counted_against_the_same_study_as_backtest(tmp_path, monkeypatch, capsys) -> None:
    """Two runs per window, recorded before the first. A sweep is not one lucky result."""
    data = _dataset_file(tmp_path, bars(count=130, instrument=AAPL))
    base = _cli(tmp_path, monkeypatch)
    assert cli_main([*base, "backtest", "--data", str(data), "--market", "us"]) == 0
    capsys.readouterr()
    assert cli_main([*base, "walk-forward", "--data", str(data), "--market", "us",
                     "--window", "40", "--embargo", "5"]) == 0
    capsys.readouterr()
    written = json.loads((tmp_path / "xai" / "latest-walk-forward.json").read_text())
    studies = {entry["study"] for entry in written["registeredTrials"]["studies"]}
    assert studies == {"replay:AAPL:USA"}
    assert written["registeredTrials"]["runs"] == 2  # backtest once, walk-forward once
    # One candidate for the backtest, and two per window for the two walk-forward windows.
    assert written["registeredTrials"]["candidate_trials"] == 1 + 2 * 2


# ------------------------------------------------------------------ the training set


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_refused_then_rallied_rows_cannot_reach_anything_that_fits() -> None:
    """The missed-opportunity report is selected on the outcome: holds that then rallied.

    Training on it would teach a model the future by construction. Nothing that fits,
    evaluates or replays may import it or read the directory it writes, and a future
    import fails here the moment it is written.
    """
    # Every way in: the report module, the directory's environment variable, the constant
    # that names it, and the accessor that resolves it.
    doors = ("PRAMANA_MISSED_OPPORTUNITY_DIR", "MISSED_OPPORTUNITY_DIR_ENV", "missed_opportunity_directory")
    offenders = []
    for package in ("learning", "backtesting", "research"):
        for path in (ROOT / "src" / "quant_ai" / package).rglob("*.py"):
            text = path.read_text()
            if (any(name.startswith("quant_ai.analytics.missed_opportunities") for name in _imports(path))
                    or any(door in text for door in doors)):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    # And the scan looks for names that really are the way in, so it cannot pass for free.
    paths_module = (ROOT / "src" / "quant_ai" / "config" / "paths.py").read_text()
    assert all(door in paths_module for door in doors)


def test_in_the_gated_run_the_floors_are_refused_exactly_where_the_engine_would_be() -> None:
    """The guarantee that makes the gated sheet a comparison at all.

    Checking that the engine received the gate proves half of it. On a series that gaps
    fifty basis points every open, a gated floor cannot fill a single order - and if it
    does, it was scored under a looser rule than the engine it is being compared with.
    """
    series = gappy(160)
    plan = lab.folds(len(series), window=40, embargo=5, first_fit=40)
    runner, _ = _stub_runner(lambda n: tuple(START for _ in range(n)))
    report = lab.walk_forward(series, instrument=INDIA, run_replay=runner, plan=plan)
    momentum = TimeSeriesMomentumBaseline.baseline_id
    for window in report["windows"]:
        fills = {run: next(row["fills"] for row in scored["entrants"] if row["entrantId"] == momentum)
                 for run, scored in window["runs"].items()}
        assert fills[lab.UNGATED] > 0, "the floor must trade when no rule stops it"
        assert fills[lab.GATED] == 0, "a gated floor filled an order the engine would be refused"
