"""How much exposure may be carried into a gap, and when it may be opened at all.

Every sizing control in this engine is an intraday control. ``CapitalPlan`` sizes a trade
so that the distance from entry to the ATR-derived stop costs exactly
``per_trade_risk_amount``; the loss breaker fires at 2% of equity on the day; the drawdown
stop at 10%. All of them assume the loss is bounded by a stop that can act, because
``ProtectiveExitEngine`` sweeps live ticks every second and liquidates the moment the
threshold is crossed.

Across a close, none of that is true. The stop cannot act on a price that never traded, so
the position takes the whole gap, and the per-trade risk amount the sizer solved for
describes nothing. The book's overnight exposure was therefore never a decision - it was
whatever the last session happened to leave open.

This makes it a decision, with one number and one window.

``max_overnight_gross`` (25% of equity)
    Derived, not chosen. ``AdversarialStressAgent.DEFAULT_SCENARIOS`` already declares the
    worst gap this engine models: ``CRISIS_GAP_DOWN_8PCT``, -8%. The same agent already
    declares what one modelled gap may cost the book: ``book_tolerance_fraction``, 2% of
    equity, which is itself ``RiskPolicy.max_daily_loss``, the intraday loss breaker. The
    book veto ties them together on the reasoning that the engine must not add exposure
    that one modelled crisis day would, on its own, turn into a halt. Overnight that
    reasoning gets stronger, not weaker, because no stop stands between the gap and the
    book - so the whole of the modelled gap lands. 2% / 8% = 25% of equity.

    The cross-check is the same one the book stress veto passes: five positions at the 5%
    ``RiskPolicy.max_single_trade_notional`` cap are exactly 25%, and five is the default
    ``max_open_positions``. The cap permits precisely the book the engine was built to
    hold overnight and refuses anything beyond it. It is strictly tighter than the 45%
    correlation-adjusted gross limit and the 60% notional gross cap, so it only ever
    subtracts from what those already allow.

``closing_window`` (one cadence interval, 10 minutes)
    An entry opened inside the last cadence interval of the regular session gets no
    further cadence tick before the close. The swarm sees it once and then it is an
    overnight position by default rather than by decision, with no opportunity for the
    engine to reconsider it and very little for the stop to act. Those are refused
    outright rather than sized down: a haircut still leaves an unmonitored position
    carrying a gap, and the honest answer to "we have no time to manage this" is not to
    open a smaller one.

Arming, and which way it fails. Opt-in by data, exactly like the cross-position gates in
``risk.policy``: without a ``MarketCalendar`` the firewall is unarmed and the entry path
behaves as it always did. Once armed, anything that stops the policy being evaluated is a
refusal and never a pass - no timestamp, a tz-naive timestamp, a market with no session
definition, non-positive equity, an arithmetic fault. An operator who arms an overnight
policy is asking the engine not to carry exposure it cannot reason about, so unable to
reason means do not add.

This never sees a risk-reducing order. ``RiskWarden`` returns on the risk-reducing verdict
before any of this runs, so an exit is never trapped by an overnight limit, a closing
window or a calendar that could not answer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.domain.models import OrderIntent, PortfolioSnapshot
from quant_ai.execution.session import (
    MarketCalendar,
    MarketState,
    SessionDefinition,
    default_holidays,
    venue_of,
)
from quant_ai.risk.policy import RiskDecision, exposure_delta

# The default cadence of ``AutonomousCadenceScheduler`` and of the ghost runner. An entry
# opened closer than this to the close cannot be reconsidered by another tick before it.
DEFAULT_CLOSING_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class OvernightRiskPolicy:
    """Thresholds for the overnight controls. See the module docstring for the derivation.

    ``max_overnight_gross`` (0.25)
        Gross exposure, as a fraction of equity, the book may carry into a gap:
        the book stress tolerance (2%) over the worst modelled gap (-8%).

    ``closing_window`` (10 minutes)
        How close to the venue's regular close an exposure-adding order is refused.

    ``refuse_outside_session`` (True)
        An exposure-adding order placed when the regular session is not open is carried
        through at least one open before any stop can act on it. There is no window in
        which that is an intraday trade, so it is refused rather than measured.
    """

    max_overnight_gross: Decimal = Decimal("0.25")
    closing_window: timedelta = DEFAULT_CLOSING_WINDOW
    refuse_outside_session: bool = True

    def __post_init__(self) -> None:
        if not Decimal(0) < self.max_overnight_gross <= Decimal(1):
            raise ValueError("max_overnight_gross must be in (0, 1]")
        if self.closing_window < timedelta(0):
            raise ValueError("closing_window cannot be negative")


class OvernightExposureFirewall:
    """Overnight gross and closing-window limits over the book an order would create.

    ``calendar`` is the operator's session calendar, including their holiday overrides.
    Supplying one arms the firewall; without one it is unarmed and approves everything,
    which is why attaching this module changes no existing caller.
    """

    def __init__(
        self,
        policy: OvernightRiskPolicy | None = None,
        *,
        calendar: MarketCalendar | None = None,
    ) -> None:
        self.policy = policy or OvernightRiskPolicy()
        self.calendar = calendar

    @property
    def armed(self) -> bool:
        """Whether the overnight limits have the session calendar they need to apply."""
        return self.calendar is not None

    def evaluate(
        self, order: OrderIntent, portfolio: PortfolioSnapshot, now: datetime | None
    ) -> RiskDecision:
        """Judge the overnight book this exposure-adding order would create.

        Never raises: a caller on the cadence path gets a decision, and a policy that
        cannot be evaluated is a refusal.
        """
        if not self.armed:
            return RiskDecision(True, "overnight_risk_not_armed")
        try:
            return self._evaluate(order, portfolio, now)
        except Exception as error:  # noqa: BLE001 - a policy fault must not pass a trade
            return RiskDecision(
                False, f"overnight_risk_unavailable:{type(error).__name__}"
            )

    def _evaluate(
        self, order: OrderIntent, portfolio: PortfolioSnapshot, now: datetime | None
    ) -> RiskDecision:
        assert self.calendar is not None
        if now is None or now.tzinfo is None or now.utcoffset() is None:
            # Where the order sits relative to the close is the whole question. Without an
            # unambiguous instant there is no answer, and a guessed one would be a wrong
            # answer half the year.
            return RiskDecision(False, "overnight_risk_unavailable:no_timestamp")
        if portfolio.equity <= 0:
            return RiskDecision(False, "overnight_risk_unavailable:invalid_equity")
        try:
            venue_of(order.market)
        except (ValueError, KeyError):
            return RiskDecision(False, "overnight_risk_unavailable:unknown_venue")
        # An order carries no exchange, so the session comes from the calendar's own
        # symbol map. Judging an MCX metal by NSE hours would refuse every entry in its
        # evening session as "outside session" and put the closing window eight hours
        # before the book actually closes.
        session = self.calendar.session(order.market, symbol=order.symbol)
        state = self.calendar.state(order.market, now, symbol=order.symbol)
        if state != MarketState.REGULAR_HOURS:
            if self.policy.refuse_outside_session:
                return RiskDecision(False, "overnight_entry_outside_session")
        elif self._within_closing_window(now, session):
            return RiskDecision(False, "overnight_closing_window")
        notional = order.reference_price * order.quantity
        current = portfolio.symbol_exposure.get(order.symbol, Decimal(0))
        reducing, adding = exposure_delta(order, portfolio, notional, current)
        projected = portfolio.gross_exposure - reducing + adding
        if projected > portfolio.equity * self.policy.max_overnight_gross:
            return RiskDecision(False, "overnight_gross_limit")
        return RiskDecision(True, "approved_overnight_risk")

    def _within_closing_window(self, now: datetime, session: SessionDefinition) -> bool:
        """Whether ``now`` falls inside the final ``closing_window`` of the session."""
        if self.policy.closing_window <= timedelta(0):
            return False
        zone = ZoneInfo(session.timezone)
        local = now.astimezone(zone)
        # The close in force on this date: MCX shuts 25 minutes later under US DST, and a
        # window measured from the wrong close either opens late or never opens at all.
        regular_close, _ = session.closes_on(local.date())
        close = datetime.combine(local.date(), regular_close, tzinfo=zone)
        return close - self.policy.closing_window <= local < close


def overnight_risk_from_env(
    calendar: MarketCalendar | None = None,
) -> OvernightExposureFirewall:
    """The overnight limits, armed only when the operator named a cap.

    Off unless ``PRAMANA_OVERNIGHT_GROSS_CAP`` names a fraction of equity; the documented
    value is ``0.25``, the book stress tolerance over the worst modelled gap. Arming it is
    a deliberate operator act for the same reason the book limits are: it fails closed, so
    an entry whose position in the session cannot be established stops rather than being
    carried into a gap nobody measured. Exits are never affected.

    Every runtime that trades reads the cap here, so the daemon and the historical replay
    cannot arm it differently. A replay that ignored a limit the daemon enforces would
    draw a curve for an engine nobody runs. Callers with an operator's holiday overrides
    pass their own calendar; the rest get the built-in one.

    A malformed value is a boot failure, not a silently unarmed control. An operator who
    typed a cap meant to have one, and starting without it is the one outcome they did
    not ask for.
    """
    raw = os.getenv("PRAMANA_OVERNIGHT_GROSS_CAP", "").strip()
    if not raw or raw.lower() == "none":
        return OvernightExposureFirewall()
    try:
        cap = Decimal(raw)
    except (ArithmeticError, ValueError) as error:
        raise RuntimeError(f"unsupported PRAMANA_OVERNIGHT_GROSS_CAP: {raw}") from error

    window_raw = os.getenv("PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES", "").strip()
    closing_window = DEFAULT_CLOSING_WINDOW
    if window_raw:
        try:
            minutes = int(window_raw)
        except ValueError as error:
            raise RuntimeError(
                f"unsupported PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES: {window_raw}"
            ) from error
        if minutes < 0 or str(minutes) != window_raw:
            raise RuntimeError(
                f"unsupported PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES: {window_raw}"
            )
        closing_window = timedelta(minutes=minutes)

    return OvernightExposureFirewall(
        OvernightRiskPolicy(max_overnight_gross=cap, closing_window=closing_window),
        calendar=calendar or MarketCalendar(holidays=default_holidays()),
    )
