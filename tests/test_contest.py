"""The floors and the engine on one sheet, and the ways that sheet can lie.

Putting two curves in one table asserts they are comparable. Every test here is about
that assertion holding, or the sheet refusing to print: the numbers are the easy part and
the claim underneath them is what a reader acts on.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.backtesting.baselines import baseline_instrument, default_baselines
from quant_ai.backtesting.contest import (
    FLOOR,
    NOT_THE_AI,
    REPLAYED_RULE,
    ContestRow,
    IncomparableEntrants,
    contest,
    format_contest,
    row_from_replay,
)
from quant_ai.backtesting.replay import (
    DETERMINISTIC_CONSENSUS,
    LLM_CONSENSUS,
    TRADED_CONFIGURATION_DIFFERENCES,
    HistoricalReplayResult,
)
from quant_ai.cli import main as cli_main
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.models import Candle

IST = timezone(timedelta(hours=5, minutes=30))
INDIA = baseline_instrument(Market.INDIA)
FIRST = datetime(2024, 1, 1, 15, 30, tzinfo=IST)
START = Decimal(100000)


def bars(count: int = 80, *, instrument: Instrument = INDIA) -> tuple[Candle, ...]:
    """A gently trending daily series, one bar per trading date."""
    made = []
    for index in range(count):
        price = Decimal(1000) + Decimal(index) * Decimal("1.5")
        made.append(
            Candle(instrument, FIRST + timedelta(days=index), price, price + Decimal(6),
                   price - Decimal(6), price + Decimal(1), Decimal(100000))
        )
    return tuple(made)


def broker_at(capital: Decimal = START) -> PaperBrokerService:
    return PaperBrokerService(starting_capital=capital, slippage_bps=Decimal(0))


def replay_over(
    series: tuple[Candle, ...],
    *,
    curve: tuple[Decimal, ...] | None = None,
    decision_maker: str = DETERMINISTIC_CONSENSUS,
) -> HistoricalReplayResult:
    """A replay result shaped exactly as the harness builds one: a mark per bar."""
    marks = curve or tuple(
        START + Decimal(index) * Decimal(40) for index in range(len(series))
    )
    return HistoricalReplayResult(
        equity_curve=marks,
        benchmark_returns=(),
        timestamps=tuple(bar.timestamp for bar in series),
        order_ids=tuple(f"order-{index}" for index in range(4)),
        final_snapshot=None,
        decision_maker=decision_maker,
    )


# ------------------------------------------------- the claim the sheet makes


def test_no_entrant_is_the_traded_ai_and_the_sheet_says_so_before_the_numbers():
    """The expensive misreading, refused in the one place a reader cannot skip.

    The pilot decides through an LLM consensus that cannot be replayed over a past window:
    its training data may already contain the outcome, and no look-ahead assertion in this
    repository can detect that. The replayed entrant is therefore the deterministic rule
    engine underneath the swarm, and a reader who takes it for the product will conclude
    the AI beat buy-and-hold on evidence that says nothing of the kind.
    """
    series = bars()
    rows = contest(series, instrument=INDIA, replay_result=replay_over(series),
                   broker=broker_at(), tenant_id="backtest")

    assert not any(row.is_the_traded_ai for row in rows)
    assert {row.kind for row in rows} == {FLOOR, REPLAYED_RULE}
    replayed = [row for row in rows if row.kind == REPLAYED_RULE]
    assert len(replayed) == 1
    assert "not the AI" in replayed[0].label

    rendered = format_contest(rows)
    # Above the table, not in a footnote under it.
    assert rendered.index(NOT_THE_AI) < rendered.index("entrant")
    # And every knob that differs from the traded engine is named, not summarised.
    for difference in TRADED_CONFIGURATION_DIFFERENCES:
        assert difference["knob"] in rendered


def test_a_replayed_llm_curve_is_refused_rather_than_scored():
    """The harness refuses to produce one; this refuses to carry one that appeared anyway.

    Two refusals rather than one, because the reason is not that the number is hard to
    compute - it is that the number would be unfalsifiable, and a second guard costs
    nothing next to publishing it once.
    """
    series = bars()
    with pytest.raises(IncomparableEntrants, match=LLM_CONSENSUS):
        row_from_replay(
            replay_over(series, decision_maker=LLM_CONSENSUS),
            broker_at(),
            periods=252,
        )


# ------------------------------------------------- comparability, enforced


def test_a_replay_over_different_bars_is_refused_rather_than_tabulated():
    """Two curves over two windows, printed side by side, answer different questions.

    Nothing in the numbers reveals it: both look like returns, both have a Sharpe, and the
    reader compares them. The bars have to match exactly, in order, or there is no sheet.
    """
    series = bars()
    shifted = replay_over(bars(count=len(series) - 1))

    with pytest.raises(IncomparableEntrants, match="did not score the same bars"):
        contest(series, instrument=INDIA, replay_result=shifted, broker=broker_at())


def test_a_replay_whose_bars_are_reordered_is_refused_even_at_the_same_length():
    """Same count, same instants, different order - and a curve that means nothing.

    Length is the check that is easy to write and the one that misses this.
    """
    series = bars()
    scrambled = replay_over(series)
    stamps = list(scrambled.timestamps)
    stamps[10], stamps[11] = stamps[11], stamps[10]
    scrambled = replace(scrambled, timestamps=tuple(stamps))

    with pytest.raises(IncomparableEntrants, match="did not score the same bars"):
        contest(series, instrument=INDIA, replay_result=scrambled, broker=broker_at())


def test_every_entrant_is_annualised_against_one_basis_and_one_observation_count():
    """A Sharpe annualised against 252 next to one annualised against 98,280.

    Both print four decimals and neither says which basis it used. The sheet checks that
    every entrant agrees before it renders, so the columns can be read down.
    """
    series = bars()
    rows = contest(series, instrument=INDIA, replay_result=replay_over(series),
                   broker=broker_at())

    assert len({row.periods_per_year for row in rows}) == 1
    assert len({row.observations for row in rows}) == 1
    assert rows[0].observations == len(series) - 1


# ------------------------------------------------- the same refusals as a floor


def test_the_replayed_row_withholds_a_sharpe_wherever_a_floor_would():
    """One measurement at two scales, and the flattering one never travels alone.

    ``BaselineReport`` enforces this in its constructor. A row built from a replay reaches
    the same table by a different path, and the pairing has to survive that path too.
    """
    short = bars(count=12)  # below MINIMUM_RATIO_OBSERVATIONS
    row = row_from_replay(replay_over(short), broker_at(), periods=252)

    assert row.sharpe is None
    assert row.mean_return_t_statistic is None
    assert any("withheld" in note for note in row.notes)


def test_a_flat_replayed_curve_withholds_both_rather_than_reporting_a_zero_ratio():
    """No dispersion is no risk, and 0/0 is not a Sharpe of zero.

    A flat curve would otherwise be the only row on the sheet carrying a ratio with no
    t-statistic beside it - which is the exact shape this table exists to refuse.
    """
    series = bars(count=60)
    flat = replay_over(series, curve=tuple(START for _ in series))

    row = row_from_replay(flat, broker_at(), periods=252)

    assert row.sharpe is None and row.sortino is None
    assert row.mean_return_t_statistic is None
    assert row.net_total_return == Decimal(0)


def test_the_replayed_return_is_measured_against_the_brokers_own_starting_capital():
    """Not against the curve's first mark, which is the same number until it is not.

    A broker opened with different capital than the floors were given makes every return
    on the sheet incomparable, and reading the denominator off the curve would hide it.
    """
    series = bars(count=40)
    curve = tuple(Decimal(200000) + Decimal(index) * Decimal(100) for index in range(len(series)))

    row = row_from_replay(replay_over(series, curve=curve), broker_at(START), periods=252)

    assert row.net_total_return == (curve[-1] - START) / START


# ------------------------------------------------------------------ the command


def test_the_operator_command_prints_the_sheet_and_leaves_the_evidence(tmp_path, monkeypatch, capsys):
    """End to end through the real CLI, over the same file ``backtest`` reads."""
    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    series = bars(count=60, instrument=Instrument("AAPL", Market.USA, AssetClass.EQUITY,
                                                  "USD", "NASDAQ"))
    dataset = tmp_path / "daily.json"
    dataset.write_text(json.dumps({"bars": [
        {"timestamp": bar.timestamp.isoformat(), "open": str(bar.open),
         "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
         "volume": str(bar.volume)} for bar in series
    ]}))

    assert cli_main(["contest", "--data", str(dataset), "--market", "us"]) == 0

    printed = capsys.readouterr().out
    assert NOT_THE_AI in printed
    payload = json.loads(printed.splitlines()[-1])
    assert payload["schema"] == "pramana.contest.v1"
    assert payload["noneOfTheseIsTheTradedAi"] == NOT_THE_AI
    assert not any(row["isTheTradedAi"] for row in payload["entrants"])
    ids = [row["entrantId"] for row in payload["entrants"]]
    assert [item.baseline_id for item in default_baselines()] == ids[:-1]
    assert ids[-1] == f"replay.{DETERMINISTIC_CONSENSUS}"
    written = json.loads((tmp_path / "xai" / "latest-contest.json").read_text())
    assert written == payload


def test_contest_and_backtest_count_against_one_study_rather_than_two(tmp_path, monkeypatch, capsys):
    """Both are looks at the same data through the same engine.

    Keying the trial register on the command name would split them, and the running total
    that exists to make a sweep of windows visible would restart at one for each. The
    study name is therefore deliberately not the command.
    """
    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    series = bars(count=60, instrument=Instrument("AAPL", Market.USA, AssetClass.EQUITY,
                                                  "USD", "NASDAQ"))
    dataset = tmp_path / "daily.json"
    dataset.write_text(json.dumps({"bars": [
        {"timestamp": bar.timestamp.isoformat(), "open": str(bar.open),
         "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
         "volume": str(bar.volume)} for bar in series
    ]}))

    assert cli_main(["backtest", "--data", str(dataset), "--market", "us"]) == 0
    capsys.readouterr()
    assert cli_main(["contest", "--data", str(dataset), "--market", "us"]) == 0

    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    studies = {entry["study"] for entry in payload["registeredTrials"]["studies"]}
    assert studies == {"replay:AAPL:USA"}
    assert payload["registeredTrials"]["runs"] == 2


def test_a_row_cannot_declare_itself_the_traded_ai():
    """``is_the_traded_ai`` is a property, not a field a future caller can set true.

    The sheet's whole claim is that nothing on it is the product. A settable flag would
    make that claim a convention, and conventions are what this module refuses to rely on.
    """
    row = ContestRow(
        entrant_id="x", label="x", kind=FLOOR, net_total_return=Decimal(0), sharpe=None,
        mean_return_t_statistic=None, sortino=None, max_drawdown=Decimal(0),
        observations=0, periods_per_year=252, fills=0,
    )
    assert row.is_the_traded_ai is False
    with pytest.raises(AttributeError):
        row.is_the_traded_ai = True  # type: ignore[misc]
