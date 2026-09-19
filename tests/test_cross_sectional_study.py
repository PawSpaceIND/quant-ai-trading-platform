"""The cross-sectional study: what it holds, what it refuses to hold, and what it charges.

A study that returns nothing is only reassuring once you have watched it find something, so
the first test builds a universe with an effect planted in it and insists the ranking picks
that effect up. The rest are the refusals - the tick-grid floor, the turnover floor, the
delisting exit and the trial charge - each of which is a way a backtest flatters itself.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.point_in_time import Listing, PointInTimeUniverse
from quant_ai.research.cross_sectional import (
    CLOSE_SIGNALS,
    FORMATION_SESSIONS,
    MINIMUM_NAMES,
    MINIMUM_PRICE,
    MINIMUM_TURNOVER,
    CrossSectionalSignal,
    Series,
    _eligible_at,
    backtest_signal,
)

START = date(2016, 1, 4)
SESSIONS = 900
VOLUME = Decimal(500_000)


def _instrument(symbol: str) -> Instrument:
    return Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def _days(count: int = SESSIONS) -> list[date]:
    """Weekday-only dates; the study never assumes a calendar, it reads the bars it is given."""
    out, cursor = [], START
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _series(symbol: str, closes: list[float], *, days: list[date] | None = None,
            volume: Decimal = VOLUME) -> Series:
    instrument = _instrument(symbol)
    stamps = days or _days(len(closes))
    bars = []
    for day, close in zip(stamps, closes):
        price = Decimal(str(round(close, 2)))
        bars.append(Candle(
            instrument,
            datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
            price, price * Decimal("1.01"), price * Decimal("0.99"), price, volume,
        ))
    return Series(
        symbol,
        tuple(bar.timestamp.date() for bar in bars),
        tuple(float(bar.close) for bar in bars),
        tuple(float(bar.volume) for bar in bars),
        tuple(bars),
    )


def _drifting(rate: float, *, base: float = 100.0, count: int = SESSIONS) -> list[float]:
    """A deterministic compounding path. No randomness: the test must not be able to flake."""
    return [base * (1.0 + rate) ** index for index in range(count)]


def _universe(symbols: list[str], *, ceased: dict[str, date] | None = None) -> PointInTimeUniverse:
    stopped = ceased or {}
    days = _days()
    return PointInTimeUniverse(
        [Listing(symbol, "INDIA", START, stopped.get(symbol)) for symbol in symbols],
        source="synthetic",
        coverage_from=START,
        coverage_to=days[-1],
    )


CONSTANT = CrossSectionalSignal(
    "constant", 1, lambda closes, volumes: 1.0, "ranks nothing; a control")


def test_the_ranking_finds_an_effect_that_is_really_there():
    """Twelve names compound upward and twelve drift down, and momentum is told which is
    which by nothing but their past. If the top decile does not out-earn the book, the
    machinery is not measuring what it claims and every null result it reports is worthless."""
    winners = [f"UP{index}" for index in range(12)]
    losers = [f"DOWN{index}" for index in range(12)]
    series = (
        [_series(name, _drifting(0.0008)) for name in winners]
        + [_series(name, _drifting(-0.0004)) for name in losers]
    )
    universe = _universe(winners + losers)
    signal = next(item for item in CLOSE_SIGNALS if item.name == "momentum_12_1")

    result = backtest_signal(series, universe, signal, 21, _days())

    assert result["observations"] > 10
    earned = statistics.fmean(result["portfolio_returns"])
    assert earned > 0
    # The book is the rising names, so it must beat the losing cohort's drift over the period.
    assert earned > (1.0 - 0.0004) ** 21 - 1.0


def test_a_sub_rupee_security_never_enters_the_book():
    """BIRLACOT closed at five paise for months, where one tick is a 100% move. Every
    return-ranked signal puts names like that at the top, and the edge is the tick grid."""
    penny = [f"PENNY{index}" for index in range(MINIMUM_NAMES + 2)]
    series = [_series(name, _drifting(0.002, base=0.05)) for name in penny]
    universe = _universe(penny)

    result = backtest_signal(series, universe, CONSTANT, 21, _days())

    assert float(MINIMUM_PRICE) > 0.05
    # Nothing clears the floor, so no rebalance ever reaches the minimum book width.
    assert result["observations"] == 0


def test_a_name_nobody_trades_never_enters_the_book():
    """A lakh-sized book cannot be most of a day's turnover in a name that trades a few
    thousand rupees, and a backtest that pretends otherwise is uninvestable."""
    thin = [f"THIN{index}" for index in range(MINIMUM_NAMES + 2)]
    # 100 x 20 = 2,000 a day against a floor of a million.
    series = [_series(name, _drifting(0.001), volume=Decimal(20)) for name in thin]
    universe = _universe(thin)

    result = backtest_signal(series, universe, CONSTANT, 21, _days())

    assert Decimal(2_000) < MINIMUM_TURNOVER
    assert result["observations"] == 0


def test_a_delisted_holding_is_exited_at_the_last_price_the_archive_holds():
    """A position whose series ends inside the holding window must not quietly pay its entry
    price back. That is the survivorship this whole stack exists to refuse."""
    days = _days()
    names = [f"NAME{index}" for index in range(MINIMUM_NAMES + 4)]
    series = [_series(name, _drifting(0.0005)) for name in names]
    # One name stops halfway through, mid-holding-window rather than on a rebalance.
    short = _series("STOPS", _drifting(-0.002, count=400), days=days[:400])
    universe = _universe([*names, "STOPS"], ceased={"STOPS": days[399]})

    # Three overlapping tranches, so a holding formed before the stop is still open after
    # it. The stopping name leads the list because a constant signal ranks by insertion, and
    # a name outside the top decile would never be held to test anything.
    result = backtest_signal([short, *series], universe, CONSTANT, 63, days)

    assert result["observations"] > 10
    assert result["positions_exited_on_delisting"] >= 1


def test_a_book_too_narrow_to_be_a_portfolio_is_skipped_rather_than_held():
    few = [f"ONLY{index}" for index in range(MINIMUM_NAMES - 4)]
    series = [_series(name, _drifting(0.001)) for name in few]
    universe = _universe(few)

    result = backtest_signal(series, universe, CONSTANT, 21, _days())

    assert result["observations"] == 0


def test_an_instrument_outside_the_manifest_coverage_is_unknown_not_tradeable():
    """was_tradeable raises outside coverage. Treating that as 'yes' would hold a position in
    a period the manifest makes no claim about."""
    names = [f"NAME{index}" for index in range(MINIMUM_NAMES + 2)]
    series = [_series(name, _drifting(0.001)) for name in names]
    days = _days()
    narrow = PointInTimeUniverse(
        [Listing(name, "INDIA", START) for name in names],
        source="synthetic",
        coverage_from=START,
        # Coverage stops long before the bars do.
        coverage_to=days[300],
    )

    result = backtest_signal(series, narrow, CONSTANT, 21, days)

    assert all(item <= days[300].isoformat() for item in result["observation_dates"])


@pytest.mark.parametrize("horizon", [63, 126])
def test_an_observation_is_one_month_whatever_the_holding_period(horizon):
    """Tranches overlap, so a year-held strategy still reports monthly. Without this a
    ten-year archive yields nine annual observations and grades nothing at all."""
    names = [f"NAME{index}" for index in range(MINIMUM_NAMES + 2)]
    rate = 0.001
    series = [_series(name, _drifting(rate)) for name in names]
    universe = _universe(names)

    result = backtest_signal(series, universe, CONSTANT, horizon, _days())

    monthly = (1.0 + rate) ** FORMATION_SESSIONS - 1.0
    assert result["portfolio_returns"]
    # Net of the one tranche in ``tranches`` that turns over this month, so a little under.
    assert 0 < result["portfolio_returns"][0] <= monthly
    assert result["overlapping_tranches"] == horizon // FORMATION_SESSIONS


def test_a_longer_hold_pays_less_cost_for_the_same_book():
    """The real economic argument for holding longer, and the reason cost must be charged on
    the tranche that turns rather than on the whole book every month."""
    names = [f"NAME{index}" for index in range(MINIMUM_NAMES + 2)]
    series = [_series(name, _drifting(0.001)) for name in names]
    universe = _universe(names)

    quarterly = backtest_signal(series, universe, CONSTANT, 63, _days())
    yearly = backtest_signal(series, universe, CONSTANT, 252, _days())

    assert yearly["portfolio_returns"][0] > quarterly["portfolio_returns"][0]


def test_ranking_nothing_earns_nothing_over_the_universe():
    """The control that makes the benchmark meaningful. A signal that ranks every name the
    same holds a slice of the universe, so its excess over the universe must be about zero -
    and a study measuring against zero instead would report the market's decade as an edge."""
    names = [f"NAME{index}" for index in range(40)]
    series = [_series(name, _drifting(0.0008)) for name in names]
    universe = _universe(names)

    result = backtest_signal(series, universe, CONSTANT, 63, _days())

    assert result["universe_returns"]
    assert statistics.fmean(result["universe_returns"]) > 0
    # Costs make it slightly negative; the point is that none of the market's return leaks in.
    assert abs(statistics.fmean(result["returns"])) < 0.002


# --- End to end: what the register is charged ---------------------------------------------

def _dataset(directory, symbol: str, rate: float, count: int = 900) -> None:
    import json
    from datetime import timedelta

    base = datetime(2016, 1, 4, tzinfo=timezone.utc)
    bars = []
    for index in range(count):
        close = round(100.0 * (1.0 + rate) ** index, 4)
        span = round(close * 0.008, 4)
        bars.append({
            "timestamp": (base + timedelta(days=index)).isoformat(),
            "open": str(close), "high": str(round(close + span, 4)),
            "low": str(round(close - span, 4)), "close": str(close),
            "volume": "800000",
        })
    (directory / f"{symbol}.json").write_text(json.dumps({
        "bars": bars,
        "provenance": {"instrument": {
            "symbol": symbol, "market": "INDIA", "asset_class": "EQUITY",
            "currency": "INR", "exchange": "NSE",
        }},
    }))


def _manifest(path, symbols: list[str], last: str):
    import json

    from quant_ai.marketdata.universe_manifest import SCHEMA

    path.write_text(json.dumps({
        "schema": SCHEMA, "source": "synthetic cross-sectional fixture",
        "coverage_from": "2016-01-04", "coverage_to": last,
        "listings": [
            {"symbol": symbol, "market": "INDIA", "listed_on": "2016-01-04"}
            for symbol in symbols
        ],
    }))
    return path


def test_the_search_is_charged_per_signal_not_per_instrument(tmp_path):
    """The whole reason this study exists. The per-instrument runner charges
    features x instruments - roughly 75,000 trials on the real archive - and the deflated
    Sharpe bar rises with the logarithm of that. Ranking charges one trial per signal and
    horizon however many names it ranks, which is what gives a real effect room to show."""
    from quant_ai.research.cross_sectional import run_cross_sectional_study

    datasets = tmp_path / "datasets"
    datasets.mkdir()
    names = [f"NAME{index}" for index in range(30)]
    for index, symbol in enumerate(names):
        _dataset(datasets, symbol, 0.0003 + index * 0.00002)
    last = (datetime(2016, 1, 4, tzinfo=timezone.utc) + timedelta(days=899)).date().isoformat()

    report = run_cross_sectional_study(
        datasets,
        _manifest(tmp_path / "universe.json", names, last),
        register=tmp_path / "register.json",
        horizons=(63, 126),
    )

    assert report["instruments_loaded"] == 30
    # Three signals at two horizons. Not 30 x anything.
    assert report["trial_register"]["candidate_trials"] == len(CLOSE_SIGNALS) * 2
    assert report["promotion_approved"] is False


def test_running_it_again_is_charged_for_looking_again(tmp_path):
    """Registered before the verdict and charged cumulatively: a second look at the same bars
    is a larger search, and the bar has to rise with it."""
    from quant_ai.research.cross_sectional import run_cross_sectional_study

    datasets = tmp_path / "datasets"
    datasets.mkdir()
    names = [f"NAME{index}" for index in range(15)]
    for index, symbol in enumerate(names):
        _dataset(datasets, symbol, 0.0003 + index * 0.00003)
    last = (datetime(2016, 1, 4, tzinfo=timezone.utc) + timedelta(days=899)).date().isoformat()
    manifest = _manifest(tmp_path / "universe.json", names, last)
    register = tmp_path / "register.json"

    first = run_cross_sectional_study(datasets, manifest, register=register, horizons=(63,))
    second = run_cross_sectional_study(datasets, manifest, register=register, horizons=(63,))

    assert second["trial_register"]["candidate_trials"] > first["trial_register"]["candidate_trials"]


def test_the_shared_cache_changes_speed_and_nothing_else():
    """Eligibility and friction depend on the month and the name, never on which candidate is
    asking - so memoising them across eighteen candidates must be invisible in the result. A
    cache that changes an answer is worse than no cache, because it changes it quietly."""
    names = [f"NAME{index}" for index in range(30)]
    series = [_series(name, _drifting(0.0003 + index * 0.00002)) for index, name in enumerate(names)]
    universe = _universe(names)
    signal = next(item for item in CLOSE_SIGNALS if item.name == "momentum_12_1")

    uncached = backtest_signal(series, universe, signal, 63, _days())
    shared: dict = {}
    first = backtest_signal(series, universe, signal, 63, _days(), shared)
    # A second candidate reading a warm cache must also agree.
    second = backtest_signal(series, universe, signal, 63, _days(), shared)

    assert first["returns"] == uncached["returns"] == second["returns"]
    assert first["universe_returns"] == uncached["universe_returns"]
    assert first["positions_exited_on_delisting"] == uncached["positions_exited_on_delisting"]


def test_a_cached_month_still_respects_each_caller_s_own_lookback():
    """The cached set is every eligible name on that day, and each caller filters it by the
    history its own signal needs. Tested on _eligible_at directly because the shipped signals
    all guard their own lookback too - going through them would pass whatever this did."""
    names = [f"NAME{index}" for index in range(20)]
    series = [_series(name, _drifting(0.0005)) for name in names]
    universe = _universe(names)
    day = _days()[300]
    shared: dict = {}

    # Cold, by a caller that needs little history: fills the cache with every eligible name.
    shallow = _eligible_at(series, universe, day, 60, shared)
    # Warm, by a caller that needs more history than any of these names have on that day.
    deep = _eligible_at(series, universe, day, 500, shared)

    assert len(shallow) == 20
    assert deep == []
