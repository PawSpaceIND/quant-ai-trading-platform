"""The floors and the engine on one sheet, scored by identical rules.

``baselines`` produces the number an active strategy has to beat. Nothing put a strategy
next to it: a reader had to run two commands, read two documents and compare a Sharpe from
one against a Sharpe from the other, trusting that both were computed the same way over the
same bars. This module does that comparison in one place and refuses to do it when the two
sides are not comparable.

**No row here is the traded AI, and one is not missing by accident.** The pilot decides
through ``LLM_CONSENSUS``. :data:`~quant_ai.backtesting.replay.TRADED_CONFIGURATION_DIFFERENCES`
records why that cannot be replayed over a past window - a model's training data may
already contain the outcome, and no look-ahead assertion in this repository can detect
that - so ``HistoricalReplayHarness`` refuses any decision maker but the deterministic
consensus. The replayed entrant is therefore the rule engine underneath the swarm, and it
is labelled ``REPLAYED_RULE`` rather than named after the product.

What that means for the question this was built to answer: **whether the AI beats the
floor cannot be settled here.** It can only be settled forward, out of the live paper
record, against these same floors, once that record is long enough to carry a ratio. What
this sheet settles is the prior question - whether the deterministic rules the AI is built
on beat owning the asset - and that is worth knowing before anyone asks for capital.

Comparability is enforced, not assumed. Both sides must have scored the same bars, in the
same order, from the same starting capital, against the same annualisation. A mismatch
raises :class:`IncomparableEntrants` rather than printing two numbers side by side and
leaving the reader to discover they mean different things.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from quant_ai.analytics.metrics import (
    MINIMUM_RATIO_OBSERVATIONS,
    mean_return_significance,
    summarize_performance,
)
from quant_ai.backtesting.baselines import (
    SIGNIFICANCE_NOTE,
    BaselineEvaluator,
    BaselineReport,
    daily_annualisation_periods,
    default_baselines,
)
from quant_ai.backtesting.replay import (
    DETERMINISTIC_CONSENSUS,
    TRADED_CONFIGURATION_DIFFERENCES,
    HistoricalReplayResult,
)
from quant_ai.domain.models import Instrument
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.models import Candle

FLOOR = "FLOOR"
REPLAYED_RULE = "REPLAYED_RULE"

# Printed above every sheet. The sentence exists because the obvious misreading of this
# table - that the replayed row is the product - is also the expensive one.
NOT_THE_AI = (
    "No entrant below is the traded AI. The pilot decides through an LLM consensus, which "
    "cannot be replayed over a past window without its training data possibly containing "
    "the outcome, so the replayed entrant is the deterministic rule engine beneath it. "
    "Whether the AI beats these floors is a forward question, answerable only from the "
    "live paper record."
)


class IncomparableEntrants(ValueError):
    """Two results that cannot be put on one sheet without misleading the reader."""


@dataclass(frozen=True)
class ContestRow:
    """One entrant, carrying only what both sides can compute the same way.

    Trade-level figures - expectancy, hit rate, round trips - are deliberately absent.
    A baseline's round trips and a replayed engine's orders are not the same unit, and a
    column that silently mixes them is worse than no column.
    """

    entrant_id: str
    label: str
    kind: str
    net_total_return: Decimal
    sharpe: Decimal | None
    mean_return_t_statistic: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    observations: int
    periods_per_year: int
    fills: int
    notes: tuple[str, ...] = ()

    @property
    def is_the_traded_ai(self) -> bool:
        """Always false. Kept as a named property so the claim is checkable, not implied."""
        return False

    def to_dict(self) -> dict:
        return {
            "entrantId": self.entrant_id,
            "label": self.label,
            "kind": self.kind,
            "isTheTradedAi": self.is_the_traded_ai,
            "netTotalReturn": str(self.net_total_return),
            "sharpe": None if self.sharpe is None else str(self.sharpe),
            "meanReturnTStatistic": (
                None if self.mean_return_t_statistic is None
                else str(self.mean_return_t_statistic)
            ),
            "sortino": None if self.sortino is None else str(self.sortino),
            "maxDrawdown": str(self.max_drawdown),
            "observations": self.observations,
            "annualisationPeriodsPerYear": self.periods_per_year,
            "fills": self.fills,
            "notes": list(self.notes),
        }


def row_from_baseline(report: BaselineReport) -> ContestRow:
    """A floor's row, taken from the report rather than recomputed."""
    return ContestRow(
        entrant_id=report.baseline_id,
        label=report.label,
        kind=FLOOR,
        net_total_return=report.net_total_return,
        sharpe=report.sharpe,
        mean_return_t_statistic=report.mean_return_t_statistic,
        sortino=report.sortino,
        max_drawdown=report.max_drawdown,
        observations=report.observations,
        periods_per_year=report.periods_per_year,
        fills=report.trades,
        notes=report.notes,
    )


def row_from_replay(
    result: HistoricalReplayResult,
    broker: PaperBrokerService,
    *,
    periods: int,
    tenant_id: str = "backtest",
    label: str = "Deterministic consensus (stand-in, not the AI)",
) -> ContestRow:
    """The replayed engine's row, computed the way a floor's is.

    Same return series, same ``summarize_performance``, same annualisation, and the same
    refusal: a Sharpe is withheld wherever the t-statistic behind it is, because the two
    are one measurement at two scales and the flattering one alone is how a coin flip gets
    promoted.
    """
    if result.decision_maker != DETERMINISTIC_CONSENSUS:
        raise IncomparableEntrants(
            f"replayed decision maker is {result.decision_maker}; only "
            f"{DETERMINISTIC_CONSENSUS} produces a curve this sheet can carry"
        )
    curve = result.equity_curve
    if len(curve) < 2:
        raise IncomparableEntrants("a replayed curve needs at least two marks")
    starting = broker.get_margin(tenant_id).starting_capital
    returns = tuple(
        (after - before) / before for before, after in zip(curve, curve[1:]) if before > 0
    )
    metrics = summarize_performance(returns, curve, (), periods=periods)
    significance = mean_return_significance(returns)
    if significance is None:
        metrics = replace(metrics, sharpe=None, sortino=None)
    notes: list[str] = []
    if metrics.sharpe is None:
        notes.append(
            f"Sharpe and Sortino withheld: {len(returns)} observations, below the "
            f"{MINIMUM_RATIO_OBSERVATIONS} a ratio needs to mean anything."
            if len(returns) < MINIMUM_RATIO_OBSERVATIONS
            else "Sharpe and Sortino withheld: the return series has no dispersion."
        )
    notes.append(
        "This curve is the deterministic consensus, not the traded LLM consensus. See "
        "TRADED_CONFIGURATION_DIFFERENCES for every knob that differs from the engine "
        "that trades."
    )
    return ContestRow(
        entrant_id=f"replay.{result.decision_maker}",
        label=label,
        kind=REPLAYED_RULE,
        net_total_return=(
            (curve[-1] - starting) / starting if starting > 0 else Decimal(0)
        ),
        sharpe=metrics.sharpe,
        mean_return_t_statistic=None if significance is None else significance.t_statistic,
        sortino=metrics.sortino,
        max_drawdown=metrics.max_drawdown,
        observations=len(returns),
        periods_per_year=periods,
        fills=len(result.order_ids),
        notes=tuple(notes),
    )


def _assert_comparable(
    bars: tuple[Candle, ...],
    result: HistoricalReplayResult,
    rows: tuple[ContestRow, ...],
) -> None:
    """Refuse a sheet whose entrants did not face the same thing.

    Two curves are only comparable if they were drawn over the same bars in the same order
    and annualised against the same basis. Each of these has gone wrong in this repository
    at least once, and each fails silently: the numbers still print, they just answer
    different questions.
    """
    stamps = tuple(bar.timestamp for bar in bars)
    if result.timestamps != stamps:
        raise IncomparableEntrants(
            "the replay and the floors did not score the same bars: "
            f"{len(result.timestamps)} replayed against {len(stamps)} offered"
            + (
                ""
                if not result.timestamps or not stamps
                else f", {result.timestamps[0].isoformat()} vs {stamps[0].isoformat()}"
            )
        )
    bases = {row.periods_per_year for row in rows}
    if len(bases) > 1:
        raise IncomparableEntrants(f"entrants annualised against different bases: {bases}")
    counts = {row.observations for row in rows}
    if len(counts) > 1:
        raise IncomparableEntrants(f"entrants scored different observation counts: {counts}")


def contest(
    bars: tuple[Candle, ...],
    *,
    instrument: Instrument,
    replay_result: HistoricalReplayResult,
    broker: PaperBrokerService,
    tenant_id: str = "backtest",
    evaluator: BaselineEvaluator | None = None,
) -> tuple[ContestRow, ...]:
    """Every floor and the replayed engine over one series, cheapest floor first."""
    scorer = evaluator or BaselineEvaluator(instrument=instrument)
    periods = daily_annualisation_periods(instrument.market, instrument.exchange)
    rows = [row_from_baseline(report) for report in scorer.evaluate(bars, default_baselines())]
    rows.append(row_from_replay(replay_result, broker, periods=periods, tenant_id=tenant_id))
    ordered = tuple(rows)
    _assert_comparable(bars, replay_result, ordered)
    return ordered


def _cell(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def format_contest(rows: tuple[ContestRow, ...]) -> str:
    """The sheet, with the disclaimer above it rather than in a footnote."""
    header = (
        f"{'entrant':<34}{'kind':>15}{'net_ret':>10}{'sharpe':>9}{'t_stat':>9}"
        f"{'sortino':>9}{'max_dd':>9}{'fills':>7}{'obs':>6}"
    )
    lines = [NOT_THE_AI, "", header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.entrant_id:<34}{row.kind:>15}{_cell(row.net_total_return):>10}"
            f"{_cell(row.sharpe):>9}{_cell(row.mean_return_t_statistic):>9}"
            f"{_cell(row.sortino):>9}{_cell(row.max_drawdown):>9}"
            f"{row.fills:>7}{row.observations:>6}"
        )
    for row in rows:
        for note in row.notes:
            lines.append(f"  {row.entrant_id}: {note}")
    lines.append("")
    lines.append(SIGNIFICANCE_NOTE)
    for difference in TRADED_CONFIGURATION_DIFFERENCES:
        lines.append(
            f"  differs from the traded engine: {difference['knob']} "
            f"traded={difference['traded']} replayed={difference['replayed']}"
        )
    return "\n".join(lines)
