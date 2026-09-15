"""Cross-position risk for the live book: covariance, correlation, 95% VaR and ES.

This is the engine-side port of ``apps/pramana-ui/lib/historical-risk.ts``. The
dashboard already computed these numbers as a read-only diagnostic; the trading
path had no cross-position measure at all, so two positions in one industry read
as diversified to every notional bucket. The algorithms here are the same ones,
digit for digit, so the dashboard and the engine cannot disagree about the same
book. ``docs/HISTORICAL_PORTFOLIO_RISK.md`` documents the shared convention.

Differences from the browser module, all deliberate:

* ``Decimal`` throughout. No float arithmetic anywhere in this module, so a
  measure that gates an order is exactly reproducible from the stored closes.
* Inputs are already-aligned daily closes. The browser validates a provider
  envelope; here ``align_daily_closes`` does the alignment and says why it
  failed, and the caller decides what an unusable book means.
* Every failure is an explicit unavailable measure carrying a reason. Nothing is
  interpolated, forward filled, pairwise deleted or renormalized, and no number
  is invented to stand in for a missing one. What the caller does with an
  unavailable measure is the caller's decision; ``risk.policy.BookRiskFirewall``
  fails closed on it.

Convention (identical to the browser module and to the doc):

* ``r[i,t] = close[i,t] / close[i,t-1] - 1`` over the same session pairs for
  every instrument.
* Sample covariance of centred returns with an ``n-1`` denominator.
* Weights are marked position values over total equity, so uninvested cash
  dilutes the book exactly as it does on the dashboard.
* Portfolio variance is ``wT S w``; daily volatility is its square root.
* Correlation is ``cov[i][j] / sqrt(cov[i][i] * cov[j][j])`` clamped to
  ``[-1, 1]``, and is ``None`` (never an invented zero) when either instrument
  has no variance in the sample.
* Scenario P&L is ``sum(value[i] * r[i,t])``; signed loss is ``-pnl``, so a
  negative loss means the book gained in that historical sample.
* 95% VaR is the nearest-rank 95th percentile of the signed losses. Expected
  shortfall integrates the worst 5% of them, with fractional weight on the
  boundary observation.
* At least 60 return intervals are required for covariance and 100 for the tail
  measures (five observations in the 5% tail). These are engineering floors, not
  statistical sufficiency, and they are the same floors the dashboard uses.

Historical tails and correlations change. Nothing here is a forecast, a
guaranteed loss limit or a claim that a loss cannot exceed every observed
scenario.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

# Engineering floors shared with the dashboard diagnostic.
MIN_COVARIANCE_INTERVALS = 60
MIN_TAIL_INTERVALS = 100
TAIL_CONFIDENCE = Decimal("0.95")
TAIL_MASS_FRACTION = Decimal("0.05")

_ONE = Decimal(1)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class BookPosition:
    """One marked position and its aligned daily closes, oldest first.

    ``closes`` must sit on the same session axis for every position in the book;
    ``align_daily_closes`` produces that axis. ``market_value`` is the marked
    value of the holding in the book's currency.
    """

    symbol: str
    market_value: Decimal
    closes: tuple[Decimal, ...]


@dataclass(frozen=True)
class BookRiskMeasure:
    """Covariance-based risk for one book, or an explicit unavailable state.

    ``available`` is false whenever the covariance block could not be computed;
    ``reason`` then names why. The tail measures carry their own availability:
    a book with 60-99 intervals has covariance and correlation but ``None`` VaR
    and expected shortfall with ``tail_reason`` set.
    """

    available: bool
    reason: str
    intervals: int = 0
    equity: Decimal = _ZERO
    symbols: tuple[str, ...] = ()
    weights: tuple[Decimal, ...] = ()
    covariance: tuple[tuple[Decimal, ...], ...] = ()
    correlation: tuple[tuple[Decimal | None, ...], ...] = ()
    daily_volatility: Decimal | None = None
    diversification_ratio: Decimal | None = None
    value_at_risk_95: Decimal | None = None
    expected_shortfall_95: Decimal | None = None
    value_at_risk_fraction: Decimal | None = None
    expected_shortfall_fraction: Decimal | None = None
    tail_reason: str = ""
    _index: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    @property
    def tail_available(self) -> bool:
        return self.expected_shortfall_95 is not None

    def correlation_of(self, left: str, right: str) -> Decimal | None:
        """Sample correlation between two symbols, or ``None`` when undefined."""
        i = self._index.get(left)
        j = self._index.get(right)
        if i is None or j is None:
            return None
        return self.correlation[i][j]

    def weight_of(self, symbol: str) -> Decimal:
        index = self._index.get(symbol)
        return self.weights[index] if index is not None else _ZERO


def unavailable(reason: str, intervals: int = 0) -> BookRiskMeasure:
    """An explicit refusal to report a number, never a fabricated one."""
    return BookRiskMeasure(False, reason, intervals)


def align_daily_closes(
    series: Mapping[str, Sequence[tuple[str, Decimal]]],
) -> tuple[dict[str, tuple[Decimal, ...]], str]:
    """Put every symbol's daily closes on one shared session axis.

    ``series`` maps a symbol to ``(session key, close)`` pairs; the session key
    is any orderable label identifying the session (an ISO date, a bar close
    timestamp). The axis is every session that falls inside the span every
    symbol covers. A symbol missing one of those sessions withholds the whole
    book, exactly as the dashboard withholds aggregates rather than using
    pairwise deletion, forward filling or a renormalized subset.

    Returns ``({}, reason)`` when no honest common axis exists.
    """
    if not series:
        return {}, "no_history_supplied"
    observed: dict[str, dict[str, Decimal]] = {}
    for symbol, rows in series.items():
        values: dict[str, Decimal] = {}
        previous: str | None = None
        for session, close in rows:
            if session in values:
                return {}, f"duplicate_session_close:{symbol}"
            if previous is not None and session <= previous:
                return {}, f"unordered_session_closes:{symbol}"
            if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
                return {}, f"invalid_close:{symbol}"
            values[session] = close
            previous = session
        if not values:
            return {}, f"no_closes:{symbol}"
        observed[symbol] = values
    start = max(min(values) for values in observed.values())
    end = min(max(values) for values in observed.values())
    if start > end:
        return {}, "no_overlapping_sessions"
    axis = sorted(
        {session for values in observed.values() for session in values if start <= session <= end}
    )
    aligned: dict[str, tuple[Decimal, ...]] = {}
    for symbol, values in observed.items():
        missing = [session for session in axis if session not in values]
        if missing:
            return {}, f"missing_session_closes:{symbol}:{len(missing)}"
        aligned[symbol] = tuple(values[session] for session in axis)
    return aligned, ""


def daily_returns(closes: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Simple adjacent-session returns, oldest first."""
    return tuple(closes[t + 1] / closes[t] - _ONE for t in range(len(closes) - 1))


def sample_covariance(
    returns: Sequence[Sequence[Decimal]],
) -> tuple[tuple[Decimal, ...], ...]:
    """Sample covariance of centred returns with an ``n-1`` denominator."""
    intervals = len(returns[0])
    denominator = Decimal(intervals - 1)
    means = tuple(sum(row, _ZERO) / Decimal(intervals) for row in returns)
    centred = [
        tuple(value - means[i] for value in row) for i, row in enumerate(returns)
    ]
    return tuple(
        tuple(
            sum((centred[i][t] * centred[j][t] for t in range(intervals)), _ZERO) / denominator
            for j in range(len(returns))
        )
        for i in range(len(returns))
    )


def correlation_matrix(
    covariance: Sequence[Sequence[Decimal]],
) -> tuple[tuple[Decimal | None, ...], ...]:
    """Pearson correlation from a covariance matrix; ``None`` where undefined."""
    size = len(covariance)
    return tuple(
        tuple(
            _clamped_correlation(covariance[i][j], covariance[i][i], covariance[j][j])
            for j in range(size)
        )
        for i in range(size)
    )


def _clamped_correlation(
    pair: Decimal, left_variance: Decimal, right_variance: Decimal
) -> Decimal | None:
    if left_variance <= 0 or right_variance <= 0:
        return None
    value = pair / (left_variance * right_variance).sqrt()
    return max(Decimal(-1), min(_ONE, value))


def historical_loss(
    losses: Sequence[Decimal], intervals: int
) -> tuple[Decimal | None, Decimal | None, str]:
    """Nearest-rank 95% VaR and the expected shortfall of the worst 5%.

    ``losses`` are signed losses (``-pnl``) in any order. Returns
    ``(var, expected_shortfall, reason)``; both are ``None`` with a reason below
    the tail floor, because five tail observations is the fewest this convention
    will report on.
    """
    if intervals < MIN_TAIL_INTERVALS:
        return (
            None,
            None,
            f"insufficient_tail_history:{intervals}_of_{MIN_TAIL_INTERVALS}_intervals",
        )
    ascending = sorted(losses)
    rank = -((-Decimal(intervals) * TAIL_CONFIDENCE).to_integral_value(rounding=ROUND_FLOOR))
    value_at_risk = ascending[int(rank) - 1]
    tail_mass = Decimal(intervals) * TAIL_MASS_FRACTION
    whole = int(tail_mass.to_integral_value(rounding=ROUND_FLOOR))
    descending = ascending[::-1]
    boundary = descending[whole] if whole < len(descending) else _ZERO
    integrated = sum(descending[:whole], _ZERO) + (tail_mass - Decimal(whole)) * boundary
    return value_at_risk, integrated / tail_mass, ""


def measure_book_risk(
    positions: Sequence[BookPosition], equity: Decimal
) -> BookRiskMeasure:
    """Covariance, correlation, 95% VaR and expected shortfall for one book.

    Every rejection is explicit. This function never raises on malformed input:
    a caller on the trading path must be able to treat it as a measurement, not
    as an exception source.
    """
    if not isinstance(equity, Decimal) or not equity.is_finite() or equity <= 0:
        return unavailable("invalid_equity")
    if not positions:
        return unavailable("no_invested_positions")
    symbols = [position.symbol for position in positions]
    if len(set(symbols)) != len(symbols):
        return unavailable("duplicate_position_identity")
    length = len(positions[0].closes)
    for position in positions:
        value = position.market_value
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return unavailable(f"invalid_market_value:{position.symbol}")
        if len(position.closes) != length:
            return unavailable("misaligned_history")
        for close in position.closes:
            if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
                return unavailable(f"invalid_close:{position.symbol}")
    intervals = length - 1
    if intervals < MIN_COVARIANCE_INTERVALS:
        return unavailable(
            f"insufficient_history:{max(0, intervals)}_of_{MIN_COVARIANCE_INTERVALS}_intervals",
            max(0, intervals),
        )
    returns = [daily_returns(position.closes) for position in positions]
    covariance = sample_covariance(returns)
    weights = tuple(position.market_value / equity for position in positions)
    covariance_weights = tuple(
        sum((covariance[i][j] * weights[j] for j in range(len(weights))), _ZERO)
        for i in range(len(weights))
    )
    variance = sum(
        (weights[i] * covariance_weights[i] for i in range(len(weights))), _ZERO
    )
    if variance < 0:
        # Rounding can only push an exactly-zero variance a hair below zero; a
        # materially negative one means the aggregation is not trustworthy.
        if -variance > Decimal("1e-24"):
            return unavailable("invalid_covariance_aggregation", intervals)
        variance = _ZERO
    volatility = variance.sqrt()
    standalone = tuple(covariance[i][i].sqrt() for i in range(len(weights)))
    diversification = (
        sum((weights[i] * standalone[i] for i in range(len(weights))), _ZERO) / volatility
        if volatility > 0
        else None
    )
    scenario_losses = tuple(
        -sum(
            (positions[i].market_value * returns[i][t] for i in range(len(positions))),
            _ZERO,
        )
        for t in range(intervals)
    )
    value_at_risk, expected_shortfall, tail_reason = historical_loss(scenario_losses, intervals)
    return BookRiskMeasure(
        True,
        "available",
        intervals,
        equity,
        tuple(symbols),
        weights,
        covariance,
        correlation_matrix(covariance),
        volatility,
        diversification,
        value_at_risk,
        expected_shortfall,
        None if value_at_risk is None else value_at_risk / equity,
        None if expected_shortfall is None else expected_shortfall / equity,
        tail_reason,
        {symbol: index for index, symbol in enumerate(symbols)},
    )


def correlation_adjusted_gross(
    measure: BookRiskMeasure, *, correlation_floor: Decimal = _ZERO
) -> Decimal | None:
    """Gross exposure counted at the book's own correlation, as a fraction of equity.

    ``sqrt(wT R+ w)`` where ``R+`` is the correlation matrix with every entry
    floored at ``correlation_floor`` (0 by default) and weights are absolute
    equity weights. The measure has exactly the property the notional gross cap
    lacks:

    * perfectly correlated positions give ``sum(w)`` - one bet of their combined
      size, which is what they economically are;
    * uncorrelated positions give ``sqrt(sum(w^2))`` - the diversified
      equivalent, so genuine diversification is rewarded;
    * anything in between lands in between, monotonically in the correlations.

    Flooring at zero is deliberate: a negative sample correlation is the most
    fragile thing a short window can tell you, and letting it shrink the measure
    below the independent case would turn an estimation artefact into permission
    to add exposure. An undefined pairwise correlation (a constant price in the
    sample) is treated as perfectly correlated, the conservative reading.

    Returns ``None`` when the measure is unavailable.
    """
    if not measure.available:
        return None
    weights = tuple(abs(weight) for weight in measure.weights)
    total = _ZERO
    for i, left in enumerate(weights):
        for j, right in enumerate(weights):
            pair = measure.correlation[i][j]
            effective = _ONE if pair is None else max(correlation_floor, pair)
            total += left * right * effective
    if total < 0:
        return None
    return total.sqrt()
