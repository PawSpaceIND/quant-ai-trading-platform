"""Friction inputs taken from real market observations, with an honest fallback.

The paper broker prices every fill from a :class:`FrictionContext`. Historical replay
builds one from the bars it can see; until now the live path built none, so live fills were
priced from a fabricated context whose half spread was a constant 5 bps at every price and
every order size. This module supplies the live path with the same kind of context replay
uses - ATR and average daily volume from closed bars, and the half spread from the tick's
own bid/ask when the tick carries one.

Two rules hold everywhere in here:

* **Observed beats modelled.** A genuine two-sided quote prices the spread directly.
* **Assumed is never cheaper than observed.** When the bars are missing, too thin, or the
  tick carries no depth, the context falls back to a deliberately wide assumption and
  records that it did, so a proof shows which fills were priced off real inputs. A
  conservative fallback overstates cost; the opposite flatters the pilot.

Nothing in here raises into a cadence tick: every market lookup is guarded and a failure
degrades to the assumption.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from quant_ai.domain.models import Instrument, Market, OrderIntent
from quant_ai.execution.friction import (
    ASSUMED,
    OBSERVED_BARS,
    OBSERVED_BARS_ASSUMED_SPREAD,
    OBSERVED_QUOTE,
    OBSERVED_QUOTE_AND_BARS,
    FrictionContext,
)

LOGGER = logging.getLogger(__name__)

# Closed one-minute bars behind the ATR and average-volume estimates, and the number of
# such bars in a full session: NSE cash trades 09:15-15:30 (375 minutes), US listed equities
# 09:30-16:00 (390 minutes). Scaling average bar volume by the session length turns a short
# observed window into an average-daily-volume estimate.
LOOKBACK_BARS = 15
DEFAULT_SESSION_MINUTES = 390
SESSION_MINUTES: dict[Market, int] = {Market.INDIA: 375, Market.USA: 390}


@dataclass(frozen=True)
class AssumedFrictionInputs:
    """What the engine assumes when it cannot see the market.

    Every number here is deliberately worse than a normal liquid NSE cash counter, and all
    of them are worse than the fabricated context they replace (a flat 5.0 bps half spread
    with impact saturating at 0.5 bps). A cold start, a thin history or a quote-less feed
    must cost more in paper than the real market would, never less.
    """

    # 25 bps per side. Wide for a large-cap, plausible for the thin end of NSE cash, and
    # five times the 5 bps the fabricated context charged at every price and size.
    half_spread_fraction: Decimal = Decimal("0.0025")
    # 2% of price as a daily true range: a volatile, not a calm, session.
    atr_fraction: Decimal = Decimal("0.02")
    # A thin counter. Fixed rather than scaled by order size, so a bigger order in the dark
    # is charged more impact, not less - the opposite of the context this replaces.
    average_daily_volume: Decimal = Decimal(25000)
    # Below 1 so the modelled spread is penalised too, wherever it is still used.
    liquidity_score: Decimal = Decimal("0.60")

    def __post_init__(self) -> None:
        if self.half_spread_fraction < 0 or self.atr_fraction < 0:
            raise ValueError("assumed friction fractions cannot be negative")
        if self.average_daily_volume <= 0:
            raise ValueError("assumed average_daily_volume must be positive")
        if not Decimal(0) < self.liquidity_score <= Decimal(1):
            raise ValueError("assumed liquidity_score must be in (0,1]")


DEFAULT_ASSUMPTIONS = AssumedFrictionInputs()


def session_minutes(market: Market) -> int:
    return SESSION_MINUTES.get(market, DEFAULT_SESSION_MINUTES)


def friction_context_from_bars(
    bars: Sequence,
    *,
    liquidity_score: Decimal,
    delivery: bool = True,
    bars_per_session: int = DEFAULT_SESSION_MINUTES,
    observed_half_spread_fraction: Decimal | None = None,
    assumed_half_spread_floor: Decimal = Decimal(0),
    input_source: str | None = None,
) -> FrictionContext:
    """ATR and ADV from the closed bars a caller can legitimately see.

    ATR is the mean high-low range of the most recent :data:`LOOKBACK_BARS` bars; average
    daily volume is the mean bar volume scaled to a full session. Historical replay and the
    live path share this so neither can drift into its own cost model.
    """
    recent = tuple(bars)[-LOOKBACK_BARS:]
    ranges = [item.high - item.low for item in recent]
    atr = sum(ranges, Decimal(0)) / Decimal(len(ranges)) if ranges else Decimal(0)
    average_bar_volume = (
        sum((item.volume for item in recent), Decimal(0)) / Decimal(len(recent))
        if recent
        else Decimal(1)
    )
    adv = max(Decimal(1), average_bar_volume * Decimal(bars_per_session))
    if input_source is None:
        if observed_half_spread_fraction is not None:
            input_source = OBSERVED_QUOTE_AND_BARS
        elif assumed_half_spread_floor > 0:
            input_source = OBSERVED_BARS_ASSUMED_SPREAD
        else:
            input_source = OBSERVED_BARS
    return FrictionContext(
        atr,
        adv,
        liquidity_score,
        delivery,
        observed_half_spread_fraction,
        assumed_half_spread_floor,
        input_source,
    )


def assumed_friction_context(
    order: OrderIntent,
    assumptions: AssumedFrictionInputs = DEFAULT_ASSUMPTIONS,
    *,
    observed_half_spread_fraction: Decimal | None = None,
    delivery: bool = True,
) -> FrictionContext:
    """The conservative context for an order whose market could not be observed."""
    reference = order.reference_price if order.reference_price > 0 else Decimal(0)
    return FrictionContext(
        reference * assumptions.atr_fraction,
        assumptions.average_daily_volume,
        assumptions.liquidity_score,
        delivery,
        observed_half_spread_fraction,
        Decimal(0)
        if observed_half_spread_fraction is not None
        else assumptions.half_spread_fraction,
        OBSERVED_QUOTE if observed_half_spread_fraction is not None else ASSUMED,
    )


class LiveFrictionContextProvider:
    """Builds a :class:`FrictionContext` for a live order. Never raises.

    ``feed`` supplies closed bars for ATR and volume; ``buffer`` supplies the newest tick,
    whose bid/ask prices the spread when it carries one. Instruments are resolved by symbol
    from the configured watchlist, because a feed needs a whole instrument and an order
    carries only a symbol. Anything missing, stale or malformed degrades to
    :func:`assumed_friction_context` with the reason recorded on the context.
    """

    def __init__(
        self,
        feed: object | None = None,
        buffer: object | None = None,
        instruments: Iterable[Instrument] = (),
        *,
        clock: Callable[[], datetime] | None = None,
        # An hour of one-minute bars, and at least five closed ones before ATR and volume
        # are worth trusting. A quote older than the cadence reader's own tolerance is not
        # the current market. Live liquidity is scored no better than replay's 0.90.
        lookback: timedelta = timedelta(minutes=60),
        min_bars: int = 5,
        max_quote_age: timedelta = timedelta(minutes=2),
        liquidity_score: Decimal = Decimal("0.85"),
        assumptions: AssumedFrictionInputs = DEFAULT_ASSUMPTIONS,
        delivery: bool = True,
    ) -> None:
        if min_bars < 1:
            raise ValueError("min_bars must be positive")
        if lookback <= timedelta(0) or max_quote_age <= timedelta(0):
            raise ValueError("lookback and max_quote_age must be positive")
        self.feed = feed
        self.buffer = buffer
        self.instruments = {item.symbol: item for item in instruments}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lookback = lookback
        self.min_bars = min_bars
        self.max_quote_age = max_quote_age
        self.liquidity_score = liquidity_score
        self.assumptions = assumptions
        self.delivery = delivery

    def __call__(self, order: OrderIntent) -> FrictionContext:
        try:
            return self._context(order)
        except Exception:  # a cadence tick is never lost to a market-data surprise
            LOGGER.warning(
                "friction_inputs_unavailable symbol=%s; pricing from assumed inputs",
                order.symbol,
                exc_info=True,
            )
            return assumed_friction_context(order, self.assumptions, delivery=self.delivery)

    def _context(self, order: OrderIntent) -> FrictionContext:
        half_spread = self._observed_half_spread(order.symbol)
        bars = self._bars(order)
        if len(bars) < self.min_bars:
            return assumed_friction_context(
                order,
                self.assumptions,
                observed_half_spread_fraction=half_spread,
                delivery=self.delivery,
            )
        return friction_context_from_bars(
            bars,
            liquidity_score=self.liquidity_score,
            delivery=self.delivery,
            bars_per_session=session_minutes(order.market),
            observed_half_spread_fraction=half_spread,
            assumed_half_spread_floor=(
                Decimal(0) if half_spread is not None else self.assumptions.half_spread_fraction
            ),
        )

    def _bars(self, order: OrderIntent) -> tuple:
        instrument = self.instruments.get(order.symbol)
        if self.feed is None or instrument is None:
            return ()
        now = self._now()
        try:
            bars = self.feed.fetch_ohlcv(instrument, now - self.lookback, now, "1m")
        except Exception:
            LOGGER.warning("friction_bars_unavailable symbol=%s", order.symbol, exc_info=True)
            return ()
        return tuple(bars or ())

    def _observed_half_spread(self, symbol: str) -> Decimal | None:
        """Half of the tick's own bid/ask as a fraction of the mid, when it is genuine."""
        if self.buffer is None:
            return None
        try:
            tick = self.buffer.latest(symbol)
        except Exception:
            LOGGER.warning("friction_quote_unavailable symbol=%s", symbol, exc_info=True)
            return None
        return observed_half_spread_fraction(tick, self._now(), self.max_quote_age)

    def _now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)


def observed_half_spread_fraction(
    tick: object | None, now: datetime, max_age: timedelta
) -> Decimal | None:
    """Half the quoted spread over the mid, or ``None`` when the quote is not usable.

    A quote is usable only when both sides are present and positive, the ask is not below
    the bid, and the tick is recent. Anything else returns ``None`` so the caller assumes
    rather than invents a tight market.
    """
    if tick is None:
        return None
    bid, ask = getattr(tick, "bid", None), getattr(tick, "ask", None)
    if not isinstance(bid, Decimal) or not isinstance(ask, Decimal):
        return None
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or ask < bid:
        return None
    observed_at = getattr(tick, "observed_at", None)
    if not isinstance(observed_at, datetime):
        return None
    observed = (
        observed_at.replace(tzinfo=timezone.utc)
        if observed_at.tzinfo is None
        else observed_at.astimezone(timezone.utc)
    )
    if observed > now or now - observed > max_age:
        return None
    mid = (bid + ask) / Decimal(2)
    if mid <= 0:
        return None
    return (ask - bid) / Decimal(2) / mid
