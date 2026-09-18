"""A catalogue of signal hypotheses, each with a written economic reason.

Before this the whole feature surface was three functions: a return, an ATR and a rolling
volatility. Three functions cannot express a hypothesis worth testing, so a search over them
is not research, it is a parameter sweep over one idea.

Two rules make this a library rather than a pile of formulas.

**Every feature states why it might work, in economics, without mentioning results.** A
rationale that says "this performed well in the backtest" is circular: it explains the
feature by the thing the feature is supposed to predict. The constructor refuses one. Writing
the reason first is also the cheapest defence against fitting noise, because a signal you
cannot justify beforehand is one you found by looking.

**Insufficient history returns None, never zero.** The existing technical.py returns
``Decimal(0)`` when the window is short, which is a silent lie: zero volatility and zero
return are meaningful values, and anything downstream consumes them as real. A feature that
cannot be computed says so.

The library also counts itself. Every feature evaluated in a study is a hypothesis tested,
and ``hypothesis_count`` is what belongs in the trial register so the deflated Sharpe charges
for the whole search rather than the one candidate that survived it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from quant_ai.marketdata.models import Candle

SCHEMA = "pramana.feature_library.v1"

TREND = "trend"
REVERSAL = "reversal"
VOLATILITY = "volatility"
LIQUIDITY = "liquidity"
STRUCTURE = "structure"
SEASONALITY = "seasonality"

FAMILIES = (TREND, REVERSAL, VOLATILITY, LIQUIDITY, STRUCTURE, SEASONALITY)

#: A rationale is an economic argument, not a result. These words mean the author explained
#: the feature by its performance, which is the circularity this library exists to refuse.
_RESULT_LANGUAGE = re.compile(
    r"\b(backtest(ed|ing)?|sharpe|out ?perform(s|ed|ance)?|profitab(le|ility)|"
    r"in.?sample|p.?value|win.?rate|edge was|made money)\b",
    re.IGNORECASE,
)

Computation = Callable[[Sequence[Candle]], "Decimal | None"]


@dataclass(frozen=True)
class Feature:
    name: str
    family: str
    lookback: int
    """Minimum number of candles before the feature can be computed at all."""
    compute: Computation
    rationale: str
    """Why the market would behave this way. Passed by keyword so it cannot be skipped."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,48}", self.name):
            raise ValueError(f"feature name is not a lowercase identifier: {self.name!r}")
        if self.family not in FAMILIES:
            raise ValueError(f"{self.name}: unknown family {self.family!r}")
        if self.lookback < 1:
            raise ValueError(f"{self.name}: lookback must be positive")
        if len(self.rationale.strip()) < 40:
            raise ValueError(f"{self.name}: rationale must be a real economic argument")
        if _RESULT_LANGUAGE.search(self.rationale):
            raise ValueError(
                f"{self.name}: rationale explains the feature by its results. Say why the "
                "market would behave this way, not that it did."
            )

    def evaluate(self, history: Sequence[Candle]) -> Decimal | None:
        if len(history) < self.lookback:
            return None
        try:
            return self.compute(history)
        except (InvalidOperation, ZeroDivisionError):
            # A degenerate window (a flat series, a zero-volume session) is missing data,
            # not a value. Returning None keeps it out of the study instead of injecting a
            # number nobody computed.
            return None


class FeatureLibrary:
    def __init__(self, features: Iterable[Feature]) -> None:
        catalogue = tuple(features)
        if not catalogue:
            raise ValueError("a feature library needs at least one feature")
        names = [item.name for item in catalogue]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate feature names: {', '.join(duplicates)}")
        self.features = catalogue

    def __len__(self) -> int:
        return len(self.features)

    @property
    def hypothesis_count(self) -> int:
        """What belongs in the trial register: evaluating the library tests this many ideas."""
        return len(self.features)

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.features)

    def by_family(self, family: str) -> tuple[Feature, ...]:
        if family not in FAMILIES:
            raise ValueError(f"unknown family {family!r}")
        return tuple(item for item in self.features if item.family == family)

    def evaluate(self, history: Sequence[Candle]) -> dict[str, Decimal | None]:
        """Every feature, including the ones that could not be computed. None is a result."""
        return {item.name: item.evaluate(history) for item in self.features}

    def as_evidence(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "features": len(self.features),
            "families": {family: len(self.by_family(family)) for family in FAMILIES},
            "limitation": (
                "A catalogue of hypotheses, not of edges. Evaluating all of them is a search of "
                "this size and must be registered as such before any result is believed."
            ),
        }


# --------------------------------------------------------------------------- helpers


def _closes(history: Sequence[Candle], count: int) -> list[Decimal]:
    return [candle.close for candle in history[-count:]]


def _returns(history: Sequence[Candle], count: int) -> list[Decimal]:
    closes = _closes(history, count + 1)
    return [(b - a) / a for a, b in zip(closes, closes[1:])]


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _stdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise InvalidOperation("stdev needs two observations")
    average = _mean(values)
    variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(len(values) - 1)
    return variance.sqrt()


def _true_ranges(history: Sequence[Candle], count: int) -> list[Decimal]:
    window = history[-(count + 1):]
    return [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(window, window[1:])
    ]


def _momentum(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        old = history[-1 - lookback].close
        return (history[-1].close - old) / old
    return compute


def _realised_volatility(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        return _stdev(_returns(history, lookback))
    return compute


def _distance_from_average(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        average = _mean(_closes(history, lookback))
        return (history[-1].close - average) / average
    return compute


def _range_position(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        window = history[-lookback:]
        highest = max(candle.high for candle in window)
        lowest = min(candle.low for candle in window)
        span = highest - lowest
        if span == 0:
            raise InvalidOperation("flat window has no range")
        return (history[-1].close - lowest) / span
    return compute


def _volume_z(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        volumes = [candle.volume for candle in history[-lookback:]]
        spread = _stdev(volumes)
        if spread == 0:
            raise InvalidOperation("flat volume has no dispersion")
        return (history[-1].volume - _mean(volumes)) / spread
    return compute


def _amihud(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        window = history[-lookback:]
        ratios: list[Decimal] = []
        for previous, current in zip(window, window[1:]):
            turnover = current.close * current.volume
            if turnover <= 0:
                continue
            ratios.append(abs((current.close - previous.close) / previous.close) / turnover)
        if not ratios:
            raise InvalidOperation("no traded sessions in window")
        return _mean(ratios)
    return compute


def _atr_ratio(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        return _mean(_true_ranges(history, lookback)) / history[-1].close
    return compute


def _parkinson(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        spans = [
            (candle.high - candle.low) / candle.close
            for candle in history[-lookback:]
        ]
        return _mean(spans)
    return compute


def _volatility_ratio(fast: int, slow: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        recent = _stdev(_returns(history, fast))
        baseline = _stdev(_returns(history, slow))
        if baseline == 0:
            raise InvalidOperation("no baseline volatility")
        return recent / baseline
    return compute


def _up_session_share(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        moves = _returns(history, lookback)
        return Decimal(sum(1 for value in moves if value > 0)) / Decimal(len(moves))
    return compute


def _body_share(history: Sequence[Candle]) -> Decimal | None:
    candle = history[-1]
    span = candle.high - candle.low
    if span == 0:
        raise InvalidOperation("candle has no range")
    return (candle.close - candle.open) / span


def _overnight_gap(history: Sequence[Candle]) -> Decimal | None:
    previous, current = history[-2], history[-1]
    return (current.open - previous.close) / previous.close


def _close_to_open(history: Sequence[Candle]) -> Decimal | None:
    candle = history[-1]
    return (candle.close - candle.open) / candle.open


def _weekday(history: Sequence[Candle]) -> Decimal | None:
    return Decimal(history[-1].timestamp.weekday())


def _month_position(history: Sequence[Candle]) -> Decimal | None:
    return Decimal(history[-1].timestamp.day) / Decimal(31)


def _drawdown_from_peak(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        peak = max(candle.high for candle in history[-lookback:])
        return (history[-1].close - peak) / peak
    return compute


def _turnover(lookback: int) -> Computation:
    def compute(history: Sequence[Candle]) -> Decimal | None:
        window = history[-lookback:]
        return _mean([candle.close * candle.volume for candle in window])
    return compute


# ------------------------------------------------------------------- the catalogue
#
# Every rationale below is an argument about why market participants would behave this way.
# None of them appeals to a result, because a feature justified by its own performance is
# indistinguishable from one found by looking.

CORE_FEATURES: tuple[Feature, ...] = (
    # ---- trend -------------------------------------------------------------------
    Feature("momentum_5", TREND, 6, _momentum(5),
            rationale="Investors update slowly on new information and institutions accumulate "
                      "positions over days rather than instantly, so a move that has begun tends "
                      "to continue while the order flow behind it is still being worked."),
    Feature("momentum_21", TREND, 22, _momentum(21),
            rationale="Monthly horizons capture the rebalancing and mandate-driven flows of funds "
                      "that review positions on a calendar cycle rather than continuously."),
    Feature("momentum_63", TREND, 64, _momentum(63),
            rationale="A quarter spans one earnings cycle, so this reflects sustained revision of "
                      "expectations rather than a single surprise or a liquidity event."),
    Feature("distance_from_ma_20", TREND, 20, _distance_from_average(20),
            rationale="Distance from a month of average price measures how stretched the current "
                      "quote is against the level most recent participants transacted at, which "
                      "anchors their sense of a fair entry."),
    Feature("distance_from_ma_50", TREND, 50, _distance_from_average(50),
            rationale="A longer anchor captures positioning by participants who entered over a "
                      "full quarter and whose unrealised profit or loss shapes their willingness "
                      "to hold through volatility."),
    Feature("up_session_share_21", TREND, 22, _up_session_share(21),
            rationale="The share of advancing sessions separates a grind driven by persistent "
                      "accumulation from the same total move delivered by one large gap, which "
                      "are different flows with different persistence."),

    # ---- reversal ----------------------------------------------------------------
    Feature("reversal_1", REVERSAL, 2, _momentum(1),
            rationale="A single session's move is dominated by liquidity demand, and the dealers "
                      "who absorbed it need compensation to carry the inventory, which they "
                      "recover as the pressure abates."),
    Feature("reversal_5", REVERSAL, 6, _momentum(5),
            rationale="A week of one-directional pressure often reflects a single participant "
                      "working an order rather than a change in fundamentals, and the price "
                      "concession paid to complete it is temporary."),
    Feature("range_position_21", REVERSAL, 21, _range_position(21),
            rationale="Where the price sits inside its recent range tells you whether holders are "
                      "sitting on gains they may take or losses they are reluctant to realise, "
                      "and the disposition effect makes those two behave differently."),
    Feature("range_position_63", REVERSAL, 63, _range_position(63),
            rationale="A quarterly range positions the quote against the levels that anchored "
                      "longer-horizon entries, which is where resting supply and demand tend to "
                      "have been placed."),
    Feature("drawdown_from_peak_63", REVERSAL, 63, _drawdown_from_peak(63),
            rationale="Distance below a recent peak measures unrealised loss across the holder "
                      "base, and loss-averse holders behave differently from ones sitting on a "
                      "gain when deciding whether to sell into strength."),

    # ---- volatility --------------------------------------------------------------
    Feature("realised_volatility_21", VOLATILITY, 22, _realised_volatility(21),
            rationale="Volatility clusters because information and the liquidity provision that "
                      "responds to it both arrive in bursts, so recent dispersion is informative "
                      "about the dispersion to come."),
    Feature("realised_volatility_63", VOLATILITY, 64, _realised_volatility(63),
            rationale="A quarterly window estimates the regime a name is trading in rather than "
                      "its reaction to one event, which is what position sizing needs."),
    Feature("volatility_ratio_5_63", VOLATILITY, 64, _volatility_ratio(5, 63),
            rationale="Short volatility against long volatility detects a regime turning, which "
                      "matters because the same position size carries different risk before and "
                      "after that turn."),
    Feature("atr_ratio_14", VOLATILITY, 15, _atr_ratio(14),
            rationale="True range includes overnight gaps that close-to-close dispersion misses, "
                      "and a stop placed without accounting for gap risk is not the protection it "
                      "appears to be."),
    Feature("parkinson_range_21", VOLATILITY, 21, _parkinson(21),
            rationale="The high-low span uses the whole session's path rather than two points, so "
                      "it estimates the same underlying dispersion from strictly more of the "
                      "information the session produced."),

    # ---- liquidity ---------------------------------------------------------------
    Feature("turnover_21", LIQUIDITY, 21, _turnover(21),
            rationale="Traded value is the capacity constraint on any position: a signal that "
                      "cannot be entered and exited without moving the price is not tradeable at "
                      "size however well it predicts."),
    Feature("volume_zscore_21", LIQUIDITY, 21, _volume_z(21),
            rationale="Unusual volume marks the arrival of a participant who is not normally "
                      "present, and their reason for trading is more likely to be information "
                      "than routine rebalancing."),
    Feature("amihud_illiquidity_21", LIQUIDITY, 22, _amihud(21),
            rationale="Price impact per unit of traded value measures how thin the book really "
                      "is, which sets the cost of the trade rather than the merit of it."),

    # ---- structure ---------------------------------------------------------------
    Feature("body_share", STRUCTURE, 1, _body_share,
            rationale="How much of a session's range the open-to-close move retained separates a "
                      "directional session that held its gains from one that was rejected, which "
                      "are different statements about who was in control."),
    Feature("overnight_gap", STRUCTURE, 2, _overnight_gap,
            rationale="The gap is the repricing that happened while nobody could trade, so it "
                      "isolates information released outside the session from the intraday flow "
                      "that follows it."),
    Feature("close_to_open", STRUCTURE, 1, _close_to_open,
            rationale="The intraday leg strips out the overnight gap, separating what the session "
                      "itself did from what it inherited at the opening auction."),

    # ---- seasonality -------------------------------------------------------------
    Feature("weekday", SEASONALITY, 1, _weekday,
            rationale="Settlement cycles, weekly derivative expiries and the timing of scheduled "
                      "releases all fall on fixed weekdays, so the composition of who is trading "
                      "is not constant across the week."),
    Feature("month_position", SEASONALITY, 1, _month_position,
            rationale="Fund flows, salary credits, monthly expiries and mandate reviews cluster "
                      "at the start and end of a calendar month, which changes the mix of price "
                      "sensitive and price insensitive participants."),
)

CORE_LIBRARY = FeatureLibrary(CORE_FEATURES)
