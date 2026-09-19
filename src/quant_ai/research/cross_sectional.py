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

Two floors exist because the archive taught us they must. A sub-rupee security moves 100% on
a single paise tick, so return-ranked signals fill their top decile with tick noise; and a
book of a lakh cannot be most of a day's turnover in a name that trades a few thousand rupees.
Both are exclusions at selection time, not adjustments afterwards.
"""

from __future__ import annotations

import bisect
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
MINIMUM_REBALANCES = 24
# Rebalance frequencies in sessions: monthly and quarterly. Daily bars do not support
# anything faster honestly - the close-to-close return is all this archive holds.
DEFAULT_HORIZONS: tuple[int, ...] = (21, 63)


@dataclass(frozen=True)
class CrossSectionalSignal:
    """A ranking quantity, and how much history it needs before it may be computed."""

    name: str
    lookback: int
    score: Callable[[Sequence[Any]], float | None]
    rationale: str


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        later / earlier - 1.0
        for earlier, later in zip(closes, closes[1:])
        if earlier > 0
    ]


def _momentum_12_1(closes: Sequence[float]) -> float | None:
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


def _low_volatility(closes: Sequence[float]) -> float | None:
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


def _short_term_reversal(closes: Sequence[float]) -> float | None:
    """Negated one-month return: the classic counterpart to momentum, tested alongside it."""
    if len(closes) < 22:
        return None
    start, end = closes[-22], closes[-1]
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

    def forward(self, cut: int, horizon: int) -> tuple[float, bool]:
        """Return from the close at ``cut - 1`` to ``horizon`` sessions later.

        When the series ends inside the window the position is exited at the last close the
        archive holds and the caller is told, because a delisting that silently pays the
        entry price back is the survivorship this whole stack exists to refuse.
        """
        entry = self.closes[cut - 1]
        target = cut - 1 + horizon
        if entry <= 0:
            return 0.0, False
        if target < len(self.closes):
            return self.closes[target] / entry - 1.0, False
        return self.closes[-1] / entry - 1.0, True


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


def backtest_signal(
    series: Sequence[Series],
    universe: PointInTimeUniverse,
    signal: CrossSectionalSignal,
    horizon: int,
    calendar: Sequence[date],
) -> dict[str, Any]:
    """Equal-weight the top decile by ``signal``, rebalanced every ``horizon`` sessions.

    Returns are net of a round trip priced by the platform's own friction model on each held
    name's own bars, charged once per rebalance because the book turns over once per period.
    """
    periods: list[float] = []
    gross: list[float] = []
    dates: list[str] = []
    widths: list[int] = []
    delisting_exits = 0
    unpriceable = 0

    start = bisect.bisect_left(calendar, calendar[0])
    for index in range(start, len(calendar) - 1, horizon):
        day = calendar[index]
        ranked: list[tuple[float, Series, int]] = []
        for item in series:
            cut = item.upto(day)
            if cut == 0 or cut < signal.lookback:
                continue
            try:
                if not universe.was_tradeable(item.symbol, day):
                    continue
            except ValueError:
                # Outside the manifest's coverage: unknown, not assumed tradeable.
                continue
            if not item.eligible(cut):
                continue
            value = signal.score(item.closes[:cut])
            if value is None or not math.isfinite(value):
                continue
            ranked.append((value, item, cut))

        if len(ranked) < MINIMUM_NAMES:
            continue
        ranked.sort(key=lambda row: row[0], reverse=True)
        width = max(MINIMUM_NAMES, int(len(ranked) * SELECTION_FRACTION))
        held = ranked[:width]

        realised: list[float] = []
        costs: list[float] = []
        for _, item, cut in held:
            move, exited = item.forward(cut, horizon)
            cost = _cost_fraction(item.bars[:cut])
            if cost is None:
                unpriceable += 1
                continue
            realised.append(move)
            costs.append(cost)
            delisting_exits += 1 if exited else 0
        if len(realised) < MINIMUM_NAMES:
            continue

        raw = statistics.fmean(realised)
        charge = statistics.fmean(costs)
        gross.append(raw)
        periods.append(raw - charge)
        dates.append(day.isoformat())
        widths.append(len(realised))

    return {
        "signal": signal.name,
        "rationale": signal.rationale,
        "horizon_sessions": horizon,
        "rebalances": len(periods),
        "returns": periods,
        "gross_returns": gross,
        "rebalance_dates": dates,
        "median_book_width": statistics.median(widths) if widths else 0,
        "positions_exited_on_delisting": delisting_exits,
        "positions_dropped_unpriceable": unpriceable,
    }


def _grade(candidate: dict[str, Any], trials: int, variance: float) -> dict[str, Any]:
    returns = candidate["returns"]
    periods_a_year = TRADING_SESSIONS_A_YEAR / candidate["horizon_sessions"]
    if len(returns) < MINIMUM_REBALANCES:
        return {
            "gradeable": False,
            "reason": f"{len(returns)} rebalances is below the {MINIMUM_REBALANCES} this grades",
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
        "mean_net_return": statistics.fmean(returns),
        "mean_gross_return": statistics.fmean(candidate["gross_returns"]),
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

    graded = [item for item in candidates if len(item["returns"]) >= MINIMUM_REBALANCES]
    spread = [sharpe_ratio(item["returns"]) for item in graded]
    variance = 0.0 if len(spread) < 2 else trial_sharpe_variance(spread)
    for item in candidates:
        item["study"] = _grade(item, charged, variance)

    scored = [item for item in candidates if item["study"]["gradeable"]]
    best = max(scored, key=lambda item: item["study"]["deflated_sharpe"]) if scored else None

    # Overfitting is a property of the selection, so it is measured across the candidates that
    # were compared, on the periods every one of them covers.
    overfitting: float | None = None
    comparable = [item for item in scored if item["horizon_sessions"] == (best or {}).get("horizon_sessions")]
    if best is not None and len(comparable) >= 2:
        width = min(len(item["returns"]) for item in comparable)
        if width >= 8:
            matrix = [[item["returns"][row] for item in comparable] for row in range(width)]
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
        "limitation": (
            "Equal-weighted long-only top decile, rebalanced on a fixed session count, with a "
            "round trip charged per rebalance from each name's own bars. It carries no short "
            "side, no capacity model beyond the turnover floor, no sector or factor neutrality "
            "and no financing cost, and it never approves promotion or live trading."
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
