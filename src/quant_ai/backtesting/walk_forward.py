"""The replayed engine against the floors it has to beat, window after window.

``contest`` scores one window. One window answers whether the rules beat owning the asset
over those particular months, which is a question a single good quarter answers "yes" to
by accident. This scores a run of consecutive, non-overlapping test windows under
identical rules and counts how many each side won - so a verdict needs the rules to hold
up, not to get lucky once.

**No entrant is the traded AI.** The pilot decides through an LLM consensus, which cannot
be replayed over a past window without its training data possibly containing the outcome;
the replayed entrant is the deterministic rule engine beneath it. Since #235 the model may
only tighten that engine's decision, never add risk to it, so the replayed rule is the
live engine minus the model's vetoes: a faithful stand-in that takes slightly more risk,
where before #235 it was not a stand-in at all.

Three rules this module will not let a caller break.

**One rule for both sides, or none.** The live engine refuses an order whose mark has left
the bar the analysis read by more than ``ENTRY_PRICE_DRIFT_TOLERANCE``. Each window is
therefore scored twice: once with that rule applied to the engine AND every floor, once
with it applied to neither. The gated run calls the live gate's own code, so a change to
the live rule changes this one; it never reimplements it. On daily bars the rule spans the
overnight gap - a far wider window than live's seconds of analysis - so the gated run is
stricter than live and the ungated run looser. The live engine sits between them, and both
are printed rather than one quietly chosen.

**The embargo is structural.** Each fold names the bars a fitted component may see - every
bar before an embargo gap - and the test bars it is scored on. The rule engine fits
nothing, so today the embargo only holds the boundary open; it exists so that the first
fitted component to be scored here cannot see its own test window, and a test pins that.

**Losing is reported as losing.** Every window's result is a win, a loss or a tie against
each floor, net of the friction the paper ledger charges, and the summary says "lost to"
where the engine lost. A Sharpe never appears without its t-statistic; that rule lives in
``contest`` and is inherited here unchanged.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from quant_ai.backtesting.baselines import BaselineEvaluator
from quant_ai.backtesting.contest import FLOOR, NOT_THE_AI, REPLAYED_RULE, ContestRow, contest
from quant_ai.backtesting.replay import HistoricalReplayDataset, HistoricalReplayResult
from quant_ai.domain.models import Instrument
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.models import Candle

SCHEMA = "pramana.walk_forward.v1"
GATED, UNGATED = "live_drift_rule", "no_drift_rule"
WIN, LOSS, TIE = "beat", "lost_to", "tied"

# (bars, order gate or None) -> (the replayed engine's result, the broker it traded).
# Injected so the operator command replays with the one harness builder it already uses,
# and a test can stand in for it; the lab never wires a harness of its own.
ReplayRunner = Callable[[tuple[Candle, ...], "Callable[[Decimal, Decimal], bool] | None"],
                        tuple[HistoricalReplayResult, PaperBrokerService]]


class _Silent:
    """A logger for the live gate that records nothing: the lab calls it on every bar."""

    def log(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def live_drift_gate(reference: Decimal, mark: Decimal) -> bool:
    """True when the live entry drift rule would let an order through.

    Calls ``AutonomousTradingDaemon._entry_price_refusal`` itself rather than a copy of its
    arithmetic, so the tolerance, the boundary (a move of exactly the tolerance is allowed)
    and the handling of an unusable reference are the live engine's by construction.
    """
    from quant_ai.execution.daemon import AutonomousTradingDaemon

    runner = SimpleNamespace(_logger=_Silent())
    proposal = SimpleNamespace(reference_price=reference, symbol="walk-forward",
                               decision_id="walk-forward")
    return AutonomousTradingDaemon._entry_price_refusal(runner, proposal, mark) is None


@dataclass(frozen=True)
class Fold:
    """Three disjoint, consecutive ranges of bar indices."""

    index: int
    fit: range
    embargo: range
    test: range

    def to_dict(self, bars: Sequence[Candle]) -> dict[str, Any]:
        def span(indices: range) -> dict[str, Any]:
            if not indices:
                return {"bars": 0, "start": None, "end": None}
            return {"bars": len(indices), "start": bars[indices[0]].timestamp.isoformat(),
                    "end": bars[indices[-1]].timestamp.isoformat()}
        return {"index": self.index, "fit": span(self.fit), "embargo": span(self.embargo),
                "test": span(self.test)}


def folds(count: int, *, window: int, embargo: int, first_fit: int) -> tuple[Fold, ...]:
    """Consecutive test windows of ``window`` bars, each after an ``embargo``-bar gap.

    The fit range expands: a later fold may fit on an earlier fold's test bars, which is
    what walking forward means, and never on its own embargo or test bars.

    ``quant_ai.validation.walk_forward.walk_forward_splits`` already splits a series into
    train and test ranges, with the test starting on the bar the training ends. That
    touching boundary is the difference: a label that resolves after its decision - a
    60-minute return, a multi-day hold - can straddle it, so a fitted component would be
    scored on outcomes it was trained on. The embargo keeps that gap open.
    """
    if window < 2:
        raise ValueError("walk_forward_window_too_short: a window needs at least two bars")
    if embargo < 0 or first_fit < 1:
        raise ValueError("walk_forward_bounds_invalid")
    made: list[Fold] = []
    start = first_fit + embargo
    while start + window <= count:
        made.append(Fold(len(made), range(start - embargo), range(start - embargo, start),
                         range(start, start + window)))
        start += window
    if not made:
        raise ValueError(
            f"walk_forward_too_few_bars: {count} bars cannot hold {first_fit} fit, "
            f"{embargo} embargo and one {window}-bar window"
        )
    return tuple(made)


def windowed(dataset: HistoricalReplayDataset, bars: tuple[Candle, ...]) -> HistoricalReplayDataset:
    """The dataset as it stood at the end of ``bars``: nothing published after it survives.

    Evidence published before the window stays, because it was visible history at the
    first decision; anything after the last bar is removed, because it was not.
    """
    end = bars[-1].timestamp
    inside = {bar.timestamp for bar in bars[1:]}
    return HistoricalReplayDataset(
        bars,
        tuple(item for item in dataset.macro if item.observed_at <= end),
        tuple(item for item in dataset.news if item.published_at <= end),
        tuple(item for item in dataset.fundamentals if item.observed_at <= end),
        dataset.benchmark_closes,
        tuple(item for item in dataset.intrabar_windows if item.parent_timestamp in inside),
    )


def _score(rows: tuple[ContestRow, ...]) -> dict[str, str]:
    """Each floor's outcome for the replayed engine in one window, net of friction."""
    engine = next(row for row in rows if row.kind == REPLAYED_RULE)
    outcome = {}
    for row in rows:
        if row.kind != FLOOR:
            continue
        if engine.net_total_return > row.net_total_return:
            outcome[row.entrant_id] = WIN
        elif engine.net_total_return < row.net_total_return:
            outcome[row.entrant_id] = LOSS
        else:
            outcome[row.entrant_id] = TIE
    return outcome


def walk_forward(
    bars: tuple[Candle, ...],
    *,
    instrument: Instrument,
    run_replay: ReplayRunner,
    plan: tuple[Fold, ...],
    tenant_id: str = "backtest",
) -> dict[str, Any]:
    """Score every fold twice - under the live drift rule and under none - on one sheet each."""
    windows = []
    for fold in plan:
        scored = {}
        for run, gate in ((UNGATED, None), (GATED, live_drift_gate)):
            test_bars = bars[fold.test.start:fold.test.stop]
            result, broker = run_replay(test_bars, gate)
            rows = contest(
                test_bars, instrument=instrument, replay_result=result, broker=broker,
                tenant_id=tenant_id,
                evaluator=BaselineEvaluator(instrument=instrument, order_gate=gate),
            )
            scored[run] = {"entrants": [row.to_dict() for row in rows], "outcome": _score(rows)}
        windows.append({**fold.to_dict(bars), "runs": scored})
    return {
        "schema": SCHEMA,
        "noneOfTheseIsTheTradedAi": NOT_THE_AI,
        "instrument": {"symbol": instrument.symbol, "market": instrument.market.value},
        "windows": windows,
        "summary": summarize(windows),
    }


def summarize(windows: Sequence[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    """Per run and per floor, how many windows the engine beat, lost to and tied."""
    summary: dict[str, dict[str, dict[str, int]]] = {GATED: {}, UNGATED: {}}
    for window in windows:
        for run, scored in window["runs"].items():
            for floor, outcome in scored["outcome"].items():
                tally = summary[run].setdefault(floor, {WIN: 0, LOSS: 0, TIE: 0})
                tally[outcome] += 1
    return {run: dict(sorted(floors.items())) for run, floors in summary.items()}


def verdict_line(floor: str, tally: dict[str, int], run: str) -> str:
    """One sentence per floor, and the word for losing is "lost to"."""
    total = sum(tally.values())
    if tally[LOSS] > tally[WIN]:
        head = f"LOST TO {floor} in {tally[LOSS]} of {total} windows"
    elif tally[WIN] > tally[LOSS]:
        head = f"beat {floor} in {tally[WIN]} of {total} windows"
    else:
        head = f"no better than {floor}: beat {tally[WIN]}, lost {tally[LOSS]} of {total} windows"
    rule = "live drift rule on both sides" if run == GATED else "no drift rule on either side"
    return f"  {REPLAYED_RULE} {head} ({rule}{f', {tally[TIE]} tied' if tally[TIE] else ''})"


def render(report: dict[str, Any]) -> str:
    """Terminal text: the disclaimer, the verdict per floor, then the windows."""
    lines = [NOT_THE_AI, "",
             (f"Walk-forward  {report['instrument']['symbol']}  windows={len(report['windows'])}  "
              "(net of the friction the paper ledger charges)"), ""]
    for run, title in ((GATED, "Under the live drift rule (stricter than live on daily bars)"),
                       (UNGATED, "Under no drift rule (looser than live)")):
        lines.append(title)
        floors = report["summary"][run]
        lines += [verdict_line(floor, tally, run) for floor, tally in floors.items()] or ["  no floors"]
        lines.append("")
    lines.append("Windows (fit / embargo / test)")
    for window in report["windows"]:
        test = window["test"]
        lines.append(
            f"  {window['index']}: fit {window['fit']['bars']} bars, embargo {window['embargo']['bars']}, "
            f"test {test['start'][:10]} .. {test['end'][:10]}  "
            + "  ".join(f"{run}:{sum(1 for o in s['outcome'].values() if o == WIN)}"
                        f"/{len(s['outcome'])} floors beaten"
                        for run, s in window["runs"].items())
        )
    return "\n".join(lines)
