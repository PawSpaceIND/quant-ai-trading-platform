"""One strategy ranked across the universe, charged for the search it actually is.

``study_runner.run_universe_study`` studies each instrument separately and reports the best,
so it charges ``features x instruments`` - on the NSE archive that is roughly 75,000 trials,
and the deflated Sharpe bar rises with the logarithm of that count. A real effect can be
buried by the size of the search rather than by its own weakness, and the first run of that
study cleared nothing at all.

A cross-sectional study asks a different question. Rather than "does any name have an edge",
it asks "does ranking the whole universe by this quantity earn anything". One signal at one
horizon is *one* candidate however many instruments it ranks, so a handful of signals over a
handful of horizons is a few dozen trials rather than tens of thousands. The same real effect
faces a bar three orders of magnitude lower, and a null result means something much stronger.

What is deliberately kept from the per-instrument study: the universe is audited before
anything is scored, trials are registered before the verdict is computed and charged
cumulatively, costs come from the platform's own friction model rather than an assumption,
and nothing here approves anything.

The first run of this study charged six trials and cleared nothing, but not by much: twelve-
month momentum held a quarter earned an annualised Sharpe of 0.79 against a deflated bar of
0.95, and every one of the three signals did better at 63 sessions than at 21 - three for
three in the same direction. Two things about that run were weak, and both are fixed here.
Its returns were measured against zero, which credits a long-only book for the market's own
decade; they are now measured as excess over an equal-weighted hold of the same eligible
names. And it rebalanced on a fixed clock, which caps a ten-year archive at nine annual
observations; tranches now overlap, so a year-held strategy still reports monthly.

Two floors exist because the archive taught us they must. A sub-rupee security moves 100% on
a single paise tick, so return-ranked signals fill their top decile with tick noise; and a
book of a lakh cannot be most of a day's turnover in a name that trades a few thousand rupees.
Both are exclusions at selection time, not adjustments afterwards.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.backtesting.datasets import dataset_instrument, load_replay_dataset
from quant_ai.marketdata.point_in_time import PointInTimeUniverse
from quant_ai.marketdata.universe_manifest import load_universe_manifest
from quant_ai.research.feature_study import round_trip_cost
from quant_ai.validation.deflated_sharpe import (
    deflated_sharpe_ratio,
    minimum_track_record_length,
    sharpe_ratio,
)
from quant_ai.validation.harness import trial_sharpe_variance
from quant_ai.validation.overfitting import probability_of_backtest_overfitting
from quant_ai.validation.trial_register import record_trials, register_summary

SCHEMA = "pramana.cross_sectional_study.v1"
STUDY = "cross_sectional_rank"

# A decile of four names is not a portfolio, it is four bets wearing a portfolio's clothes.
MINIMUM_NAMES = 10
SELECTION_FRACTION = 0.10
# BIRLACOT closed at five paise for months, where one tick is a 100% move. Every return-ranked
# signal puts names like that at the top of its book, and the "edge" is the tick grid.
MINIMUM_PRICE = Decimal(5)
# Average daily traded value. A lakh-sized book cannot be a large share of a name that turns
# over a few thousand rupees a day, and a backtest that pretends otherwise is uninvestable.
MINIMUM_TURNOVER = Decimal(1_000_000)
LIQUIDITY_WINDOW = 60
TRADING_SESSIONS_A_YEAR = 252
MINIMUM_OBSERVATIONS = 36
# A tranche is formed every month and held for the full horizon, so at any moment the book
# carries ``horizon / FORMATION_SESSIONS`` overlapping tranches. This is the Jegadeesh-Titman
# construction, and it exists because the alternative does not survive contact with ten years
# of data: rebalancing once a year over 2,469 sessions yields nine observations, which grades
# nothing. Overlapping tranches give a monthly observation of an annually-held strategy.
FORMATION_SESSIONS = 21
# Holding periods in sessions: quarterly, half-yearly, yearly. The first search found every
# signal did better at 63 than at 21, without exception, so this one looks further out rather
# than repeating the short end.
DEFAULT_HORIZONS: tuple[int, ...] = (63, 126, 252)


@dataclass(frozen=True)
class CrossSectionalSignal:
    """A ranking quantity, and how much history it needs before it may be computed."""

    name: str
    lookback: int
    score: Callable[[Sequence[float], Sequence[float]], float | None]
    rationale: str


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        later / earlier - 1.0
        for earlier, later in zip(closes, closes[1:])
        if earlier > 0
    ]


def _momentum_12_1(closes: Sequence[float], volumes: Sequence[float] = ()) -> float | None:
    """Twelve-month return, skipping the most recent month.

    The skip is not decoration. Raw twelve-month momentum contains last month's return, which
    reverses; including it mixes two effects with opposite signs and measures neither.
    """
    if len(closes) < 252:
        return None
    start, end = closes[-252], closes[-21]
    if start <= 0:
        return None
    return end / start - 1.0


def _low_volatility(closes: Sequence[float], volumes: Sequence[float] = ()) -> float | None:
    """Negated realised volatility, so that a higher score is a calmer name."""
    if len(closes) < 120:
        return None
    moves = _returns(closes[-120:])
    if len(moves) < 60:
        return None
    spread = statistics.pstdev(moves)
    if spread <= 0:
        return None
    return -spread


def _short_term_reversal(closes: Sequence[float], volumes: Sequence[float] = ()) -> float | None:
    """Negated one-month return: the classic counterpart to momentum, tested alongside it."""
    if len(closes) < 22:
        return None
    start, end = closes[-22], closes[-1]
    if start <= 0:
        return None
    return -(end / start - 1.0)


def _volatility_scaled_momentum(closes: Sequence[float], volumes: Sequence[float] = ()) -> float | None:
    """Momentum divided by the volatility it was earned through.

    Two names up thirty percent are not the same bet if one got there smoothly. Scaling by
    realised volatility is the standard correction, and it is a different hypothesis from
    either momentum or low volatility alone rather than a blend of the two.
    """
    if len(closes) < 252:
        return None
    raw = _momentum_12_1(closes, volumes)
    if raw is None:
        return None
    moves = _returns(closes[-252:-21])
    if len(moves) < 120:
        return None
    spread = statistics.pstdev(moves)
    if spread <= 0:
        return None
    return raw / spread


def _illiquidity(closes: Sequence[float], volumes: Sequence[float]) -> float | None:
    """Amihud: average absolute return per rupee traded. Higher means thinner.

    The illiquidity premium is one of the better-evidenced effects, and it is the one most
    likely to be an artefact of costs rather than a return - which is precisely why it is
    worth testing here, where costs are priced from each name's own bars and the turnover
    floor has already removed the untradeable tail.
    """
    window = 120
    if len(closes) < window + 1:
        return None
    prices, traded = closes[-window:], volumes[-window:]
    moves = _returns(prices)
    ratios = [
        abs(move) / (prices[index + 1] * traded[index + 1])
        for index, move in enumerate(moves)
        if prices[index + 1] * traded[index + 1] > 0
    ]
    if len(ratios) < 60:
        return None
    return statistics.fmean(ratios) * 1e9


def _long_term_reversal(closes: Sequence[float], volumes: Sequence[float] = ()) -> float | None:
    """Negated return from five years ago to one year ago: De Bondt and Thaler's horizon.

    Deliberately disjoint from momentum's window, so the two are testing different claims
    about the same prices rather than the same claim twice.
    """
    if len(closes) < 1260:
        return None
    start, end = closes[-1260], closes[-252]
    if start <= 0:
        return None
    return -(end / start - 1.0)


CLOSE_SIGNALS: tuple[CrossSectionalSignal, ...] = (
    CrossSectionalSignal(
        "momentum_12_1", 252, _momentum_12_1,
        "twelve-month return excluding the last month, the standard cross-sectional momentum",
    ),
    CrossSectionalSignal(
        "low_volatility", 120, _low_volatility,
        "negated realised volatility over six months; the low-volatility anomaly",
    ),
    CrossSectionalSignal(
        "short_term_reversal", 22, _short_term_reversal,
        "negated one-month return; tested because it is momentum's opposite over a short window",
    ),
    CrossSectionalSignal(
        "volatility_scaled_momentum", 252, _volatility_scaled_momentum,
        "twelve-month momentum divided by the volatility it was earned through",
    ),
    CrossSectionalSignal(
        "illiquidity", 121, _illiquidity,
        "Amihud average absolute return per rupee traded; the illiquidity premium",
    ),
    CrossSectionalSignal(
        "long_term_reversal", 1260, _long_term_reversal,
        "negated five-to-one-year return; De Bondt and Thaler's reversal horizon",
    ),
)


@dataclass(frozen=True)
class Series:
    """One instrument's closes and volumes, in date order, with dates indexed for bisect."""

    symbol: str
    days: tuple[date, ...]
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    bars: tuple[Any, ...]

    def upto(self, day: date) -> int:
        """How many observations exist on or before ``day``. Zero means nothing is known."""
        return bisect_right(self.days, day)

    def eligible(self, cut: int) -> bool:
        """Price and turnover floors, judged on what was known at the rebalance, never later."""
        if cut < LIQUIDITY_WINDOW:
            return False
        if Decimal(str(self.closes[cut - 1])) < MINIMUM_PRICE:
            return False
        window = range(max(0, cut - LIQUIDITY_WINDOW), cut)
        turnover = sum(self.closes[i] * self.volumes[i] for i in window) / len(window)
        return Decimal(str(turnover)) >= MINIMUM_TURNOVER

    def month_return(self, opening: date, closing: date) -> tuple[float, bool] | None:
        """Return across a window, and whether the series died inside it.

        Taken from dates rather than from index arithmetic, because the two disagree the
        moment a name stops trading: its cut stops advancing while the calendar does not.
        A holding whose bars end mid-window is exited at the last close the archive holds and
        reported as such. Paying the entry price back would be the survivorship this stack
        exists to refuse, and dropping the window silently would be the same thing wearing a
        different face.
        """
        entered = self.upto(opening)
        if entered < 1:
            return None
        entry = self.closes[entered - 1]
        if entry <= 0:
            return None
        exited = self.upto(closing)
        return self.closes[exited - 1] / entry - 1.0, self.days[-1] < closing


def _load(datasets: Path, universe: PointInTimeUniverse) -> tuple[list[Series], list[dict[str, str]], list[str]]:
    known = {item.symbol for item in universe.listings}
    loaded: list[Series] = []
    skipped: list[dict[str, str]] = []
    digests: list[str] = []
    for path in sorted(Path(datasets).glob("*.json")):
        instrument = dataset_instrument(path)
        if instrument is None:
            skipped.append({"dataset": path.name, "reason": "declares no instrument"})
            continue
        if instrument.symbol not in known:
            # The survivorship leak's back door: a dataset nobody put in the manifest.
            skipped.append({
                "dataset": path.name,
                "reason": f"{instrument.symbol} is not in the universe manifest",
            })
            continue
        bars = load_replay_dataset(path, instrument).bars
        if len(bars) < 2:
            skipped.append({"dataset": path.name, "reason": "holds no history"})
            continue
        loaded.append(Series(
            instrument.symbol,
            tuple(bar.timestamp.date() for bar in bars),
            tuple(float(bar.close) for bar in bars),
            tuple(float(bar.volume) for bar in bars),
            tuple(bars),
        ))
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return loaded, skipped, digests


def _calendar(series: Sequence[Series]) -> tuple[date, ...]:
    days: set[date] = set()
    for item in series:
        days.update(item.days)
    return tuple(sorted(days))


def _cost_fraction(bars: Sequence[Any]) -> float | None:
    try:
        return float(round_trip_cost(bars).round_trip_fraction)
    except (ValueError, ZeroDivisionError):
        # An instrument the friction model cannot price is one this study must not hold.
        return None


def _eligible_at(
    series: Sequence[Series], universe: PointInTimeUniverse, day: date, lookback: int
) -> list[tuple[Series, int]]:
    """Every name this study may rank on ``day``, judged only on what was known by then."""
    out: list[tuple[Series, int]] = []
    for item in series:
        cut = item.upto(day)
        if cut == 0 or cut < lookback:
            continue
        try:
            if not universe.was_tradeable(item.symbol, day):
                continue
        except ValueError:
            # Outside the manifest's coverage: unknown, not assumed tradeable.
            continue
        if not item.eligible(cut):
            continue
        out.append((item, cut))
    return out


def backtest_signal(
    series: Sequence[Series],
    universe: PointInTimeUniverse,
    signal: CrossSectionalSignal,
    horizon: int,
    calendar: Sequence[date],
) -> dict[str, Any]:
    """Overlapping tranches, measured as excess over the universe the names came from.

    Two things separate this from the naive construction. A tranche is formed every month and
    held for ``horizon``, so the book carries several overlapping tranches and a year-held
    strategy still produces a monthly observation - without this, ten years of data yields
    nine annual rebalances and grades nothing.

    And the return reported is *excess* over an equal-weighted hold of every eligible name.
    A long-only decile of Indian equities between 2015 and 2025 earns a positive Sharpe by
    being long, and a benchmark of zero would credit the signal for the market's own return.
    The question is whether ranking beat not ranking.

    Cost is charged on the fraction of the book that actually turns: one tranche in
    ``horizon / FORMATION_SESSIONS`` each month, which is the real economic advantage of
    holding longer and is why it must not be charged per-month in full.
    """
    tranches = max(1, horizon // FORMATION_SESSIONS)
    bounds = list(range(0, len(calendar), FORMATION_SESSIONS))
    selections: dict[int, tuple[list[tuple[Series, int]], float]] = {}
    delisting_exits = 0
    unpriceable = 0

    for month, index in enumerate(bounds[:-1]):
        picks = _eligible_at(series, universe, calendar[index], signal.lookback)
        ranked: list[tuple[float, Series, int]] = []
        for item, cut in picks:
            value = signal.score(item.closes[:cut], item.volumes[:cut])
            if value is None or not math.isfinite(value):
                continue
            ranked.append((value, item, cut))
        if len(ranked) < MINIMUM_NAMES:
            continue
        ranked.sort(key=lambda row: row[0], reverse=True)
        width = max(MINIMUM_NAMES, int(len(ranked) * SELECTION_FRACTION))
        held, costs = [], []
        for _, item, cut in ranked[:width]:
            cost = _cost_fraction(item.bars[:cut])
            if cost is None:
                unpriceable += 1
                continue
            held.append((item, cut))
            costs.append(cost)
        if len(held) < MINIMUM_NAMES:
            continue
        selections[month] = (held, statistics.fmean(costs))

    excess: list[float] = []
    portfolio: list[float] = []
    market: list[float] = []
    dates: list[str] = []
    widths: list[int] = []

    for month in range(1, len(bounds) - 1):
        active = [selections[m] for m in range(max(0, month - tranches + 1), month + 1)
                  if m in selections]
        if not active:
            continue
        opening, closing = bounds[month], bounds[month + 1]

        legs: list[float] = []
        for held, _ in active:
            names: list[float] = []
            for item, _cut in held:
                moved = item.month_return(calendar[opening], calendar[closing])
                if moved is None:
                    continue
                names.append(moved[0])
                delisting_exits += 1 if moved[1] else 0
            if names:
                legs.append(statistics.fmean(names))
        if not legs:
            continue

        # Every eligible name, equally weighted: the return of not ranking at all.
        benchmark: list[float] = []
        for item, _cut in _eligible_at(series, universe, calendar[opening], LIQUIDITY_WINDOW):
            moved = item.month_return(calendar[opening], calendar[closing])
            if moved is not None:
                benchmark.append(moved[0])
        if len(benchmark) < MINIMUM_NAMES:
            continue

        # One tranche in ``tranches`` turns over this month, and only that part pays.
        charge = statistics.fmean([cost for _, cost in active]) / tranches
        gross = statistics.fmean(legs)
        passive = statistics.fmean(benchmark)
        portfolio.append(gross - charge)
        market.append(passive)
        excess.append(gross - charge - passive)
        dates.append(calendar[opening].isoformat())
        widths.append(sum(len(held) for held, _ in active))

    return {
        "signal": signal.name,
        "rationale": signal.rationale,
        "horizon_sessions": horizon,
        "overlapping_tranches": tranches,
        "observations": len(excess),
        "returns": excess,
        "portfolio_returns": portfolio,
        "universe_returns": market,
        "observation_dates": dates,
        "median_book_width": statistics.median(widths) if widths else 0,
        "positions_exited_on_delisting": delisting_exits,
        "positions_dropped_unpriceable": unpriceable,
    }


def _grade(candidate: dict[str, Any], trials: int, variance: float) -> dict[str, Any]:
    returns = candidate["returns"]
    # Observations are monthly whatever the holding period, because tranches overlap.
    periods_a_year = TRADING_SESSIONS_A_YEAR / FORMATION_SESSIONS
    if len(returns) < MINIMUM_OBSERVATIONS:
        return {
            "gradeable": False,
            "reason": f"{len(returns)} observations is below the {MINIMUM_OBSERVATIONS} this grades",
        }
    observed = sharpe_ratio(returns)
    deflated = deflated_sharpe_ratio(returns, trials=trials, trial_sharpe_variance=variance)
    needed = minimum_track_record_length(returns)
    return {
        "gradeable": True,
        "sharpe": observed,
        "annualised_sharpe": observed * math.sqrt(periods_a_year),
        "deflated_sharpe": deflated,
        "minimum_track_record_length": None if math.isinf(needed) else needed,
        "periods_a_year": periods_a_year,
        "mean_excess_return": statistics.fmean(returns),
        "mean_portfolio_return": statistics.fmean(candidate["portfolio_returns"]),
        "mean_universe_return": statistics.fmean(candidate["universe_returns"]),
        "universe_sharpe": sharpe_ratio(candidate["universe_returns"]),
    }


def run_cross_sectional_study(
    datasets: Path,
    manifest: Path,
    *,
    register: Path,
    signals: Sequence[CrossSectionalSignal] = CLOSE_SIGNALS,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    minimum_deflated_sharpe: float = 0.95,
    maximum_overfitting_probability: float = 0.10,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rank the universe by each signal at each horizon, and charge for every pair tried."""
    if not signals or not horizons:
        raise ValueError("a search evaluates at least one signal at one horizon")
    if any(horizon < 1 for horizon in horizons):
        raise ValueError("a horizon is at least one session")

    universe = load_universe_manifest(manifest)
    audit = universe.audit().as_evidence()
    series, skipped, digests = _load(datasets, universe)
    if not series:
        raise ValueError("no dataset in this directory belongs to the universe manifest")
    calendar = _calendar(series)

    # Registered before the verdict, and charged cumulatively. Running this again is looking
    # at the same bars again, and the bar rises accordingly.
    record_trials(
        register,
        study=STUDY,
        candidate_trials=len(signals) * len(horizons),
        configuration={
            "instruments": len(series),
            "signals": [item.name for item in signals],
            "horizons": list(horizons),
            "selection_fraction": SELECTION_FRACTION,
            "minimum_price": str(MINIMUM_PRICE),
            "minimum_turnover": str(MINIMUM_TURNOVER),
            "universe_source": universe.source,
        },
        data_sha256=hashlib.sha256("".join(sorted(digests)).encode()).hexdigest(),
        now=now,
    )
    summary = register_summary(register, study=STUDY)
    charged = int(summary["candidate_trials"])

    candidates = [
        backtest_signal(series, universe, signal, horizon, calendar)
        for signal in signals
        for horizon in horizons
    ]

    graded = [item for item in candidates if len(item["returns"]) >= MINIMUM_OBSERVATIONS]
    spread = [sharpe_ratio(item["returns"]) for item in graded]
    variance = 0.0 if len(spread) < 2 else trial_sharpe_variance(spread)
    for item in candidates:
        item["study"] = _grade(item, charged, variance)

    scored = [item for item in candidates if item["study"]["gradeable"]]
    best = max(scored, key=lambda item: item["study"]["deflated_sharpe"]) if scored else None

    # Overfitting is a property of the selection, so it is measured across the candidates that
    # were compared, on the periods every one of them covers.
    # Overfitting is a property of the selection, so it is measured across the candidates that
    # were actually compared, on observations every one of them covers. Same holding period
    # only: return series of different lengths are not the same experiment. The first search
    # could put just three columns here, which is a thin basis for a probability; the wider
    # signal set exists partly so this number has something to stand on.
    overfitting: float | None = None
    comparable = [item for item in scored if item["horizon_sessions"] == (best or {}).get("horizon_sessions")]
    if best is not None and len(comparable) >= 2:
        width = min(len(item["returns"]) for item in comparable)
        if width >= 16:
            matrix = [[item["returns"][-width + row] for item in comparable] for row in range(width)]
            overfitting = probability_of_backtest_overfitting(matrix, chunks=8).probability

    blockers: list[str] = []
    if audit["verdict"] != "plausible":
        blockers.append(
            f"universe is not research-grade ({audit['verdict']}): a result measured on it "
            "cannot be trusted however the statistics read"
        )
    if best is None:
        blockers.append("no signal produced enough rebalances to grade")
    else:
        if best["study"]["deflated_sharpe"] < minimum_deflated_sharpe:
            blockers.append(
                f"deflated Sharpe {best['study']['deflated_sharpe']:.3f} is below "
                f"{minimum_deflated_sharpe}: the result has not cleared its own search"
            )
        if overfitting is not None and overfitting > maximum_overfitting_probability:
            blockers.append(
                f"probability of backtest overfitting {overfitting:.3f} is above "
                f"{maximum_overfitting_probability}"
            )

    return {
        "schema": SCHEMA,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "universe": audit,
        "universe_source": universe.source,
        "instruments_loaded": len(series),
        "instruments_skipped": skipped,
        "sessions": len(calendar),
        "trial_register": summary,
        "candidates": candidates,
        "best": None if best is None else f"{best['signal']}@{best['horizon_sessions']}",
        "overfitting_probability": overfitting,
        "clears_every_gate": not blockers,
        "blockers": blockers,
        "promotion_approved": False,
        "benchmark": (
            "Excess over an equal-weighted hold of every eligible name. The universe's own "
            "Sharpe is reported per candidate so the market's contribution is visible rather "
            "than absorbed."
        ),
        "limitation": (
            "Equal-weighted long-only top decile in monthly overlapping tranches, with a round "
            "trip charged on the one tranche that turns each month, priced from each name's own "
            "bars. It carries no short side, no capacity model beyond the turnover floor, no "
            "sector or factor neutrality and no financing cost. Excess over an equal-weighted "
            "universe is not the same as risk-adjusted alpha: the decile's beta is not "
            "estimated, so a signal that simply selects higher-beta names would show excess in "
            "a rising market. It never approves promotion or live trading."
        ),
    }


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--register", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    report = run_cross_sectional_study(args.datasets, args.manifest, register=args.register)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({
        "report": str(args.output),
        "instruments": report["instruments_loaded"],
        "cumulative_candidate_trials": report["trial_register"]["candidate_trials"],
        "best": report["best"],
        "overfitting_probability": report["overfitting_probability"],
        "clears_every_gate": report["clears_every_gate"],
        "blockers": report["blockers"],
        "promotion_approved": False,
    }))


if __name__ == "__main__":
    main()
