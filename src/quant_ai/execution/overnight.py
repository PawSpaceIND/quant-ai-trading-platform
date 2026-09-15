"""The overnight gap: measuring it, and refusing to sit quietly on one nobody explained.

A stop is an intraday instrument. ``ProtectiveExitEngine`` sweeps live ticks every second
and liquidates the moment a stored threshold is crossed, which works for every price the
market actually trades through. It cannot work for a price that never traded. A position
carried through a close reopens wherever the next session decides, and the first mark of
that session can be on the far side of the stop with nothing in between. Nothing in this
engine handled that: no end-of-day squaring, no overnight cap, no statement of policy.

Two pieces answer it, and they answer different halves.

``OvernightGapMonitor`` here is the *after* half. It watches the step each protected
symbol makes across a session boundary, classifies it with
``quant_ai.marketdata.gap.classify_gap``, and - this is the whole point - keeps an
unresolved one in front of the operator. A discontinuity the engine cannot explain is an
open question about a protected position, and an open question that nobody is told about
becomes a position nobody is watching. So every unresolved step alerts immediately,
re-alerts on a bounded cadence so it cannot go quiet for a session, and after one full
session unresolved it hands the daemon a halt reason.

``quant_ai.risk.overnight.OvernightExposureFirewall`` is the *before* half: how much may
be carried into a gap at all.

Neither piece may ever suppress a protective exit. The monitor observes and reports; the
engine's decision to liquidate is taken from the same stored levels it always was. A new
control here may cause a halt, never a pass - that direction is the only one that is
safe, because the engine's existing guarantee is that a breached stop fires, and nothing
added for the gap is allowed to take that away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Market
from quant_ai.execution.session import SESSIONS, GlobalVenue, regular_session_length, venue_of
from quant_ai.marketdata.gap import GapAssessment, GapVerdict, classify_gap
from quant_ai.notifications.trading import (
    TradingAlertCode,
    TradingNotificationDispatcher,
)

LOGGER = logging.getLogger("quant_ai.overnight")

# How often an unresolved discontinuity is put back in front of the operator. The engine
# already has a number for how long one protective decision stays in force -
# ``ProtectiveExitEngine.re_entry_cooldown``, thirty minutes - so an unexplained step is
# repeated at least as often as the engine would allow itself to re-enter a stopped-out
# symbol. Against the shortest regular session in ``SESSIONS`` (NSE, six and a quarter
# hours) that is twelve alerts, so silence for a whole session is arithmetically
# impossible rather than merely unlikely.
DEFAULT_ESCALATION_INTERVAL = timedelta(minutes=30)

#: Declared ex-dates, if the operator keeps any: ``(symbol, moment) -> kind or None``.
#: A callable rather than a calendar type so an operator's own source plugs in without
#: this module owning a file format.
DeclaredActionLookup = Callable[[str, datetime], str | None]


@dataclass(frozen=True)
class GapObservation:
    """One classified step for one symbol, and when it was first seen.

    ``venue`` is carried so each symbol's deadline is its own market's session length: a
    mixed book must not give an NSE name the longer of the two clocks.
    """

    symbol: str
    venue: GlobalVenue
    assessment: GapAssessment
    previous_mark: Decimal
    current_mark: Decimal
    first_seen: datetime
    last_alert_at: datetime


def session_close_of(moment: datetime, venue: GlobalVenue) -> datetime:
    """When the venue's regular session on ``moment``'s local date closes, in UTC."""
    session = SESSIONS[venue]
    zone = ZoneInfo(session.timezone)
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), session.regular_close, tzinfo=zone).astimezone(
        timezone.utc
    )


def crossed_session_boundary(
    before: datetime, after: datetime, venue: GlobalVenue
) -> bool:
    """Whether a regular session closed between two observations.

    This is the one fact that genuinely separates a corporate action from an intraday
    collapse: an action re-bases at an open, so it can only ever appear across a
    boundary. Anything at or after the close of ``before``'s session counts, which reads
    two post-close marks on the same day as separated - conservative, and both sides of
    that call are unresolved and loud, so it changes what the operator is told and never
    what the engine does.
    """
    return session_close_of(before, venue) <= after


class OvernightGapMonitor:
    """Classifies each symbol's step across a session boundary and escalates the unclear.

    Observation is passive: ``observe`` records, classifies, alerts and returns. It never
    tells the caller to skip an exit, and ``ProtectiveExitEngine`` never asks it to.

    Resolution is an operator act. A step becomes resolved when the operator declares the
    ex-date behind it (``declared_action_lookup`` then names it on the next observation),
    or when the position leaves the book. Until then it is repeated every
    ``escalation_interval``, and once it has been unresolved for a full regular session of
    its own venue, ``halt_reason`` names it so the daemon can stop adding risk. Stopping
    new risk is the only lever that points the safe way: liquidating on a quote the engine
    has just admitted it cannot interpret would book whichever loss the ambiguity invented.
    """

    def __init__(
        self,
        *,
        dispatcher: TradingNotificationDispatcher | None = None,
        tenant_id: str = "default",
        declared_action_lookup: DeclaredActionLookup | None = None,
        escalation_interval: timedelta = DEFAULT_ESCALATION_INTERVAL,
        resolve_deadline: timedelta | None = None,
    ) -> None:
        if escalation_interval <= timedelta(0):
            raise ValueError("escalation interval must be positive")
        self.dispatcher = dispatcher or TradingNotificationDispatcher()
        self.tenant_id = tenant_id
        self.declared_action_lookup = declared_action_lookup
        self.escalation_interval = escalation_interval
        # None means "one regular session of the symbol's own venue", which is the point
        # at which a position has been carried unexplained for as long as the market was
        # open to explain it. An explicit value overrides that for every venue.
        self.resolve_deadline = resolve_deadline
        self._marks: dict[str, tuple[Decimal, datetime]] = {}
        self._open: dict[str, GapObservation] = {}

    @property
    def unresolved(self) -> tuple[str, ...]:
        """Symbols carrying a discontinuity the engine could not explain, sorted."""
        return tuple(sorted(self._open))

    def observe(
        self, symbol: str, market: Market, mark: Decimal, now: datetime
    ) -> GapAssessment | None:
        """Record a mark, classify the step from the previous one, and alert if needed.

        Returns the assessment, or ``None`` when the observation could not be judged at
        all - a tz-naive clock, a venue with no session definition, an unusable price.
        Never raises: this runs inside the one-second protection sweep, and a monitor that
        raised would cost the sweep every stop it was about to enforce.
        """
        try:
            return self._observe(symbol, market, mark, now)
        except Exception as error:  # noqa: BLE001 - reporting must never break protection
            LOGGER.warning(
                "overnight_gap_observation_failed symbol=%s error=%s",
                symbol, type(error).__name__,
            )
            return None

    def _observe(
        self, symbol: str, market: Market, mark: Decimal, now: datetime
    ) -> GapAssessment | None:
        if now.tzinfo is None or now.utcoffset() is None:
            LOGGER.warning("overnight_gap_unjudgeable symbol=%s reason=naive_clock", symbol)
            return None
        try:
            venue = venue_of(market)
        except (ValueError, KeyError):
            # A market with no session definition has no boundary to measure against, so
            # there is no honest answer here. The exposure gate refuses such a market
            # outright; this half declines to invent a verdict for it.
            LOGGER.warning("overnight_gap_unjudgeable symbol=%s reason=unknown_venue", symbol)
            return None
        if mark is None or mark <= 0:
            return None
        key = symbol.upper()
        previous = self._marks.get(key)
        self._marks[key] = (mark, now)
        declared = self._declared(symbol, now)
        assessment = classify_gap(
            previous[0] if previous else None,
            mark,
            crossed_session_boundary=(
                crossed_session_boundary(previous[1], now, venue) if previous else False
            ),
            declared_action=declared,
        )
        self._record(key, assessment, previous[0] if previous else mark, mark, now, venue)
        return assessment

    def _declared(self, symbol: str, now: datetime) -> str | None:
        if self.declared_action_lookup is None:
            return None
        try:
            declared = self.declared_action_lookup(symbol, now)
        except Exception as error:  # noqa: BLE001 - an operator's source is not a dependency
            LOGGER.warning(
                "overnight_declared_action_unavailable symbol=%s error=%s",
                symbol, type(error).__name__,
            )
            return None
        return str(declared) if declared else None

    def _record(
        self,
        key: str,
        assessment: GapAssessment,
        previous: Decimal,
        current: Decimal,
        now: datetime,
        venue: GlobalVenue,
    ) -> None:
        """Open, re-alert or close this symbol's unresolved discontinuity.

        Only an operator's declaration closes one. A later step back inside the band does
        not: the gap still happened, and a quiet tick a second afterwards is not an
        explanation of it. Left the other way round, every unexplained step would clear
        itself on the next ordinary sweep and the escalation would never reach anybody.
        """
        existing = self._open.get(key)
        if assessment.verdict == GapVerdict.DECLARED_ACTION:
            if existing is not None:
                # The operator named the ex-date behind it. Close the loop with them
                # rather than going silent on an alert they were paged about.
                del self._open[key]
                self._alert(key, assessment, existing.previous_mark, current, now, venue,
                            resolved=True, detail=assessment.detail)
            return
        if existing is None:
            if assessment.resolved:
                return
            self._open[key] = GapObservation(
                key, venue, assessment, previous, current, now, now
            )
            self._alert(key, assessment, previous, current, now, venue, resolved=False)
            return
        if now - existing.last_alert_at >= self.escalation_interval:
            self._open[key] = GapObservation(
                key, existing.venue, existing.assessment, existing.previous_mark, current,
                existing.first_seen, now,
            )
            self._alert(
                key, existing.assessment, existing.previous_mark, current, now, venue,
                resolved=False, detail=f"unresolved_for={now - existing.first_seen}",
            )

    def unresolved_state(self) -> tuple[dict[str, object], ...]:
        """Each open discontinuity as plain JSON values, for an operator-facing surface.

        The alert path already puts these in front of whoever reads notifications. A page
        is the other half of that: an operator who was not watching a dispatcher when the
        step appeared still has to be able to see that a protected position is carrying
        one, and for how much longer before it stops new risk.

        Every value here was recorded when the step was classified. Nothing is estimated:
        a deadline the venue's session length cannot answer is reported as absent rather
        than guessed, and a symbol with no deadline still appears, because it is still
        unresolved. Never raises - this is read from the telemetry publish that runs
        inside the protection sweep.
        """
        rows: list[dict[str, object]] = []
        for key in sorted(self._open):
            observation = self._open[key]
            try:
                deadline = self.resolve_deadline or regular_session_length(observation.venue)
            except Exception as error:  # noqa: BLE001 - reporting never breaks the sweep
                LOGGER.warning(
                    "overnight_gap_deadline_unavailable symbol=%s error=%s",
                    key, type(error).__name__,
                )
                deadline = None
            rows.append({
                "symbol": key,
                "verdict": observation.assessment.verdict.value,
                "venue": observation.venue.value,
                # Exact text, the way the decision journal stores money: a mark that
                # travels through a float stops being the number the engine compared.
                "previousMark": str(observation.previous_mark),
                "currentMark": str(observation.current_mark),
                "stepFraction": str(observation.assessment.step_fraction),
                "nearestAction": observation.assessment.nearest_action,
                "firstSeenAt": observation.first_seen.isoformat(),
                "lastAlertAt": observation.last_alert_at.isoformat(),
                # When new risk stops unless an operator resolves this. Absent means the
                # venue could not be asked, which is not the same as "no deadline".
                "haltsAt": (
                    (observation.first_seen + deadline).isoformat()
                    if deadline is not None else None
                ),
            })
        return tuple(rows)

    def halt_reason(self, now: datetime) -> str | None:
        """The first discontinuity left unresolved past its deadline, if any.

        Each symbol is judged against one regular session of its own venue - the point at
        which it has been carried unexplained for as long as its market was open to
        explain it. An explicit ``resolve_deadline`` overrides that for every venue.
        """
        try:
            for key in sorted(self._open):
                observation = self._open[key]
                deadline = self.resolve_deadline or regular_session_length(observation.venue)
                if now - observation.first_seen >= deadline:
                    return f"overnight_gap_unresolved:{key}"
            return None
        except Exception as error:  # noqa: BLE001 - never raise into the cadence
            LOGGER.warning("overnight_halt_check_failed error=%s", type(error).__name__)
            return None

    def _alert(
        self,
        symbol: str,
        assessment: GapAssessment,
        previous: Decimal,
        current: Decimal,
        now: datetime,
        venue: GlobalVenue,
        *,
        resolved: bool,
        detail: str = "",
    ) -> None:
        """Put a discontinuity in front of the operator through the existing dispatcher.

        A failed send is logged and swallowed. These alerts are raised while something is
        already unclear about a protected position; a sink that raised would abort the
        sweep that is trying to report it.
        """
        verb = "resolved" if resolved else "UNEXPLAINED"
        message = (
            f"Overnight price discontinuity {verb} on {symbol}: {previous} -> {current} "
            f"({assessment.describe()}). The stored stop and cost basis may not refer to "
            f"the same unit as this quote; protective exits remain armed."
        )
        try:
            self.dispatcher.dispatch(
                TradingAlertCode.OVERNIGHT_GAP_UNEXPLAINED,
                message,
                tenant_id=self.tenant_id,
                metadata={
                    "symbol": symbol,
                    "verdict": assessment.verdict.value,
                    "resolved": str(resolved),
                    "previous_mark": str(previous),
                    "current_mark": str(current),
                    "step_fraction": str(assessment.step_fraction),
                    "ratio": str(assessment.ratio),
                    "nearest_action": assessment.nearest_action,
                    "venue": venue.value,
                    "detail": detail or assessment.detail,
                },
            )
        except Exception as error:  # noqa: BLE001 - an alert sink may not break the sweep
            LOGGER.warning(
                "overnight_gap_alert_failed symbol=%s error=%s", symbol, type(error).__name__
            )
        LOGGER.warning(
            "overnight_gap symbol=%s verdict=%s resolved=%s previous=%s current=%s",
            symbol, assessment.verdict.value, resolved, previous, current,
        )

    def forget(self, symbols: tuple[str, ...]) -> None:
        """Drop state for symbols no longer held, so a closed position stops escalating."""
        held = {symbol.upper() for symbol in symbols}
        for key in [item for item in self._open if item not in held]:
            del self._open[key]
        for key in [item for item in self._marks if item not in held]:
            del self._marks[key]


__all__ = [
    "DEFAULT_ESCALATION_INTERVAL",
    "GapObservation",
    "GapVerdict",
    "OvernightGapMonitor",
    "crossed_session_boundary",
    "session_close_of",
]
