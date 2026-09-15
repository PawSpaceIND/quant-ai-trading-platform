"""What the ``analytics`` command claims to be measuring.

The command reports on the traded instrument's last hour of one-minute candles. It is run
by an operator who also runs ``portfolio``, which reports on the paper account, and the
two once printed the same bare key names. A ``sharpe=`` line sitting where an account
statistic would sit is not a small labelling matter: it is the difference between "this
stock moved up over the last hour" and "this system makes money", and only one of those
is true of an account that has never closed a trade.

Every assertion here is about that separation surviving future edits to the command.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import sin, sqrt
from types import SimpleNamespace

from quant_ai.cli import _analytics
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle

INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
NOW = datetime.now(timezone.utc)


def daemon_over(closes, attribution=()):
    """A daemon stub exposing only what ``_analytics`` reads."""
    candles = tuple(
        Candle(INFY, NOW - timedelta(minutes=len(closes) - index), Decimal(str(close)),
               Decimal(str(close)), Decimal(str(close)), Decimal(str(close)), Decimal(1))
        for index, close in enumerate(closes)
    )
    pipeline = SimpleNamespace(
        market_feed=SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: candles),
        runtime=SimpleNamespace(attribution=SimpleNamespace(attribution=lambda: attribution)),
    )
    return SimpleNamespace(instrument=INFY, scheduler=SimpleNamespace(pipeline=pipeline))


def output(capsys, closes, attribution=()):
    _analytics(daemon_over(closes, attribution))
    return dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line
    )


# A drifting-up hour: enough observations for every ratio, and a positive mean return.
RISING = [100 + index * 0.05 for index in range(61)]
# An hour that went nowhere, which is the ordinary case: bounded oscillation, mean return
# near zero, and no RNG so the figures below are the same on every machine and release.
QUIET_HOUR = [round(100 * (1 + 0.002 * sin(index)), 4) for index in range(61)]


def test_every_market_statistic_names_the_series_it_measures(capsys):
    printed = output(capsys, RISING)
    assert printed["series"] == "instrument_price:INFY:INDIA:1m:last_60_minutes"
    assert printed["series_measures"] == "instrument_price_not_account_performance"
    # The bare names are what an operator mistakes for account statistics. None may return.
    for orphan in ("sharpe", "sortino", "max_drawdown", "win_loss_ratio", "var_95"):
        assert orphan not in printed, f"{orphan}= reads as an account statistic"
    assert printed["instrument_sharpe"]
    assert printed["instrument_max_drawdown"] is not None
    # Up minutes over down minutes, which is not a win/loss record over trades.
    assert "instrument_up_down_minute_ratio" in printed


def test_alpha_and_beta_are_withheld_rather_than_measured_against_the_instrument_itself(capsys):
    """The tautology this command used to print.

    Passing the instrument's own returns as the benchmark makes the regression exact:
    alpha=0.0 and beta=1.0 every time, on any series, which reads as "tracking the market
    perfectly" and is instead a column regressed on itself. There is no independent index
    in scope here, so there is no alpha and no beta to report.
    """
    printed = output(capsys, RISING)
    assert printed["alpha"] == "unavailable:no_independent_benchmark"
    assert printed["beta"] == "unavailable:no_independent_benchmark"


def test_the_annualised_ratio_and_the_t_statistic_are_one_measurement_at_two_scales(capsys):
    """Why a large Sharpe here is not a second, corroborating finding.

    Both come from the same 60 minutes. The Sharpe multiplies the sample's signal-to-noise
    by sqrt(periods_per_year), which on a one-minute India series is sqrt(94500) - about
    307. Divided by the sqrt(n) already inside the t-statistic, the two differ by a fixed
    factor of about 40 on an hour of candles: a mean return statistically indistinguishable
    from zero still prints a Sharpe in the double digits, and an operator reading only the
    Sharpe learns the opposite of what the sample supports. Both stay on screen together,
    and this pins the arithmetic that makes them inseparable.
    """
    printed = output(capsys, QUIET_HOUR)
    periods, count = int(printed["annualisation_periods_per_year"]), int(printed["return_observations"])
    assert (periods, count) == (94500, 60)
    t_statistic = Decimal(printed["instrument_mean_return_t_statistic"])
    sharpe = Decimal(printed["instrument_sharpe"])
    # sqrt(periods / n), times sqrt(n / (n - 1)) for the population/sample stdev difference.
    expected = Decimal(str(sqrt(periods / count) * sqrt(count / (count - 1))))
    assert abs(sharpe / t_statistic - expected) < Decimal("0.0001")
    # The hour is a wash, and the honest number says so while the annualised one shouts.
    assert abs(t_statistic) < Decimal("0.1") < Decimal(1) < abs(sharpe)
    assert printed["instrument_mean_return_multiple_testing_correction"] == "none"


def test_a_sample_too_short_for_a_ratio_prints_no_ratio(capsys):
    printed = output(capsys, [100 + index * 0.05 for index in range(20)])
    assert printed["instrument_sharpe"].startswith("unavailable:")
    assert printed["instrument_mean_return_t_statistic"].startswith("unavailable:")
    # The withheld ratios must not take the account statistics down with them.
    assert printed["agent_attribution"] == "none"


def test_an_account_that_has_closed_no_trades_reports_no_learning_rather_than_none_needed(capsys):
    """``agent_attribution=none`` is the honest reading of an untraded account.

    Conviction weights are earned from closed trades only. Before the first one there is
    nothing to weight, and the command says so instead of printing a default 1.0 weight
    per agent, which would read as a measured result.
    """
    assert output(capsys, RISING)["agent_attribution"] == "none"


def test_a_scored_agent_is_reported_with_the_weight_it_earned(capsys):
    row = SimpleNamespace(agent_id="momentum", hit_rate=Decimal("0.6"),
                          pnl=Decimal(120), conviction_weight=Decimal("1.05"))
    printed = output(capsys, RISING, attribution=(row,))
    assert "agent_attribution" not in printed
    assert printed["agent"].startswith("momentum hit_rate=0.6")


def test_the_market_is_named_so_a_default_us_run_on_an_india_pilot_is_visible(capsys):
    """The failure this line exists to make visible.

    ``cli analytics`` builds a hardcoded AAPL instrument whatever the operator asks for,
    so on an India pilot every ratio describes a stock the watchlist does not contain. The
    only tell was an annualisation of 98280 rather than 94500 - a difference nobody reads.
    Naming the instrument and its market puts it in the first line of output instead, which
    is how that mismatch was found; refusing the flag outright is the separate guard in
    ``test_cli_runtime_reporting``. This one stays because the mismatch can return without
    the flag: any change to the instrument this runtime builds is silent otherwise.
    """
    aapl = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    daemon = daemon_over(RISING)
    daemon.instrument = aapl
    _analytics(daemon)
    printed = dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line
    )
    assert printed["series"] == "instrument_price:AAPL:USA:1m:last_60_minutes"
    assert int(printed["annualisation_periods_per_year"]) == 98280
