"""Find the corporate actions hiding in raw bhavcopy prices, and say which are unexplained.

A bhavcopy prints what actually traded. It does not adjust history. So a 1:10 split reads
as a 90% overnight collapse, a 1:1 bonus as a halving, and every trend, reversal and
volatility feature computed across that boundary is measuring an accounting event rather
than a price. Left alone this is the single largest source of nonsense in a study built on
exchange archives — larger than survivorship, because it corrupts the series itself rather
than the choice of series.

**The exchange tells you, if you read the right column.** On an ex-date the exchange
restates ``PREVCLOSE`` on the *adjusted* basis, so it no longer equals the close it
actually printed the session before. That disagreement is the exchange's own announcement
that an action took effect, and the ratio between the two recovers the factor it applied::

    close[t-1] = 1000.00     (what actually printed)
    prevclose[t] = 100.00    (restated for a 1:10 split)
    implied factor = 10

This is the primary detector. It is worth being clear that it rests on an assumption about
venue behaviour rather than a documented guarantee, which is why a second, independent
detector looks for large overnight moves with no such signal. When real corporate-action
records are supplied, :func:`reconcile` reports whether the ``PREVCLOSE`` detector actually
found them — so the assumption is validated against data instead of trusted.

**The number this exists to produce.** :attr:`ReconciliationReport.unexplained` is how many
price discontinuities the archive contains that nothing accounts for. It is the input to a
purchasing decision: a low count means the free exchange archive is sufficient and no
corporate-actions vendor is needed; a high count sizes the problem before anyone pays to
solve it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

SCHEMA = "pramana.action_reconciliation.v1"

#: Below this relative disagreement between the stated and actual previous close, the
#: difference is rounding on a two-decimal price rather than an action. Small cash
#: dividends fall under it and go undetected; their effect on the price series is beneath
#: the noise floor of every feature in the library, though a total-return study would need
#: them from a dividend record rather than from here.
DEFAULT_TOLERANCE = Decimal("0.001")

#: Indian equities trade under circuit limits of 2, 5, 10 or 20 percent, so a large
#: overnight move is more often a corporate action than a price move. The threshold sits
#: *above* the widest band rather than on it, which matters more than it looks: a stock
#: that closes at its 20% circuit has moved exactly 0.20, so a detector firing at ``>= 0.20``
#: reports every circuit day in the market as an unexplained corporate action. Measured on
#: a real NSE archive that mistake accounted for thousands of false positives and would
#: have argued for buying vendor data nobody needs.
#:
#: The cost of the wider band is small. Unsignalled actions worth correcting are large by
#: nature - a 1:10 split moves -90%, a 1:1 bonus -50% - and anything under 25% is already
#: beneath what would change a study's conclusion.
DEFAULT_GAP_THRESHOLD = Decimal("0.25")

#: An action whose implied factor exceeds this is not credible as a split or bonus and
#: points at a data error — a misplaced decimal, or two securities merged under one key.
IMPLAUSIBLE_FACTOR = Decimal(1000)

#: Calendar days between two bars beyond which they are not consecutive sessions. A long
#: weekend with holidays either side reaches about five days; past a week the security did
#: not trade in between.
#:
#: This gates the gap detector, and it matters. An instrument's bars are the days it traded,
#: not the days the market was open, so a security suspended for two years has adjacent bars
#: two years apart, and an "overnight move" measured across that hole is a multi-year
#: return. On a real NSE archive every one of the ten largest such readings was a penny
#: stock resuming from suspension - VISESHINFO at +2000% on a previous close of 0.05 - each
#: reported as an unexplained corporate action, and each carrying no such information.
MAX_CONSECUTIVE_SESSION_DAYS = 7


@dataclass(frozen=True)
class CorporateActionRecord:
    """A known action, from an exchange circular or a vendor file.

    ``factor`` is the price divisor the action implies: 10 for a 1:10 split, 2 for a 1:1
    bonus, 1 for a pure cash dividend (whose effect is carried by ``cash`` instead).
    """

    key: str
    ex_date: date
    kind: str
    factor: Decimal = Decimal(1)
    cash: Decimal = Decimal(0)
    purpose: str = ""

    def __post_init__(self) -> None:
        if self.factor <= 0:
            raise ValueError(f"{self.key}: an action factor must be positive")
        if self.cash < 0:
            raise ValueError(f"{self.key}: a cash amount cannot be negative")
        if not self.kind.strip():
            raise ValueError(f"{self.key}: an action must say what kind it is")


@dataclass(frozen=True)
class PriceDiscontinuity:
    """A break in a price series, and what the archive says about it."""

    key: str
    symbol: str
    ex_date: date
    actual_previous_close: Decimal
    stated_previous_close: Decimal
    implied_factor: Decimal
    overnight_move: Decimal
    detector: str
    explained_by: str = ""

    @property
    def explained(self) -> bool:
        return bool(self.explained_by)

    def as_evidence(self) -> dict:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "actual_previous_close": str(self.actual_previous_close),
            "stated_previous_close": str(self.stated_previous_close),
            "implied_factor": str(self.implied_factor),
            "overnight_move": str(self.overnight_move.quantize(Decimal("0.0001"))),
            "detector": self.detector,
            "explained_by": self.explained_by,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    instruments: int
    sessions: int
    discontinuities: tuple
    records_supplied: int
    records_matched: int
    verdict: str
    notes: tuple

    @property
    def detected(self) -> int:
        return len(self.discontinuities)

    @property
    def explained(self) -> int:
        return sum(1 for item in self.discontinuities if item.explained)

    @property
    def unexplained(self) -> int:
        """Detected discontinuities that matched no supplied corporate-action record.

        With no records supplied this is simply the whole detected set, which is why it is
        not on its own a measure of data quality: see :attr:`unsignalled_gaps`.
        """
        return self.detected - self.explained

    @property
    def self_adjustable(self) -> int:
        """Discontinuities the exchange itself signalled, which need no vendor to fix."""
        return sum(1 for item in self.discontinuities
                   if item.detector == "exchange-prevclose")

    @property
    def unsignalled_gaps(self) -> int:
        """The number that decides whether corporate-action data has to be bought.

        A break the venue never announced cannot be adjusted from the archive. Every one of
        these is either an action with no signal or a data error, and both need a record
        from somewhere else before a study can run across them.
        """
        return sum(1 for item in self.discontinuities
                   if item.detector == "unexplained-gap" and not item.explained)

    @property
    def usable_for_research(self) -> bool:
        return self.verdict in {"clean", "self_adjusting"}

    def worst_unexplained(self, limit: int = 10) -> tuple:
        pending = [item for item in self.discontinuities if not item.explained]
        pending.sort(key=lambda item: abs(item.overnight_move), reverse=True)
        return tuple(pending[:limit])

    def as_evidence(self) -> dict:
        by_detector: dict = {}
        for item in self.discontinuities:
            by_detector[item.detector] = by_detector.get(item.detector, 0) + 1
        return {
            "schema": SCHEMA,
            "instruments": self.instruments,
            "sessions": self.sessions,
            "detected": self.detected,
            "explained": self.explained,
            "unexplained": self.unexplained,
            "self_adjustable": self.self_adjustable,
            "unsignalled_gaps": self.unsignalled_gaps,
            "by_detector": by_detector,
            "records_supplied": self.records_supplied,
            "records_matched": self.records_matched,
            "verdict": self.verdict,
            "usable_for_research": self.usable_for_research,
            "worst_unexplained": [item.as_evidence() for item in self.worst_unexplained()],
            "notes": list(self.notes),
            "limitation": (
                "Detection finds breaks in the price series. It does not identify the action "
                "that caused one, and cash dividends below the tolerance are invisible here."
            ),
        }


def _discontinuities_for(
    bars: Sequence,
    *,
    tolerance: Decimal,
    gap_threshold: Decimal,
) -> list:
    found: list = []
    for index in range(1, len(bars)):
        previous, current = bars[index - 1], bars[index]
        actual = previous.close
        stated = current.previous_close
        if actual <= 0 or stated <= 0:
            continue
        disagreement = abs(stated - actual) / actual
        move = (current.close - actual) / actual
        # Bars are the days this security traded, not the days the market was open.
        span = (current.trading_day - previous.trading_day).days
        consecutive = span <= MAX_CONSECUTIVE_SESSION_DAYS

        if disagreement > tolerance:
            # The exchange restated the previous close: it is telling us an action applied.
            factor = actual / stated
            found.append(
                PriceDiscontinuity(
                    key=current.key,
                    symbol=current.symbol,
                    ex_date=current.trading_day,
                    actual_previous_close=actual,
                    stated_previous_close=stated,
                    implied_factor=factor,
                    overnight_move=move,
                    detector="exchange-prevclose",
                )
            )
        elif consecutive and abs(move) >= gap_threshold:
            # No restatement, but a move past every circuit limit. Either an action the
            # venue did not signal, or a data error. Both need a human before a study runs.
            found.append(
                PriceDiscontinuity(
                    key=current.key,
                    symbol=current.symbol,
                    ex_date=current.trading_day,
                    actual_previous_close=actual,
                    stated_previous_close=stated,
                    implied_factor=Decimal(1),
                    overnight_move=move,
                    detector="unexplained-gap",
                )
            )
    return found


def reconcile(
    histories: Iterable,
    *,
    records: Sequence | None = None,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    gap_threshold: Decimal = DEFAULT_GAP_THRESHOLD,
) -> ReconciliationReport:
    """Detect every price discontinuity across the archive and match it against known actions.

    ``histories`` are :class:`~quant_ai.marketdata.listing_reconstruction.InstrumentHistory`
    values. ``records`` are known corporate actions; supply none and every discontinuity is
    reported as unmatched, which is the honest starting state rather than a failure.
    """
    known: dict = {}
    for record in records or ():
        known.setdefault((record.key, record.ex_date), []).append(record)

    discontinuities: list = []
    instruments = 0
    sessions = 0
    for history in histories:
        instruments += 1
        sessions += len(history.bars)
        discontinuities.extend(
            _discontinuities_for(
                history.bars, tolerance=tolerance, gap_threshold=gap_threshold
            )
        )

    matched: set = set()
    resolved: list = []
    for item in discontinuities:
        candidates = known.get((item.key, item.ex_date), ())
        if candidates:
            record = candidates[0]
            matched.add((record.key, record.ex_date))
            resolved.append(
                PriceDiscontinuity(
                    key=item.key,
                    symbol=item.symbol,
                    ex_date=item.ex_date,
                    actual_previous_close=item.actual_previous_close,
                    stated_previous_close=item.stated_previous_close,
                    implied_factor=item.implied_factor,
                    overnight_move=item.overnight_move,
                    detector=item.detector,
                    explained_by=f"{record.kind}:{record.purpose}" if record.purpose
                    else record.kind,
                )
            )
        else:
            resolved.append(item)

    signalled = sum(1 for item in resolved if item.detector == "exchange-prevclose")
    gaps = sum(1 for item in resolved if item.detector == "unexplained-gap")
    implausible = [item for item in resolved if item.implied_factor > IMPLAUSIBLE_FACTOR]

    notes: list = []
    supplied = len(records or ())
    unexplained = sum(1 for item in resolved if not item.explained)

    if supplied == 0:
        # Nothing to check against. The archive can still adjust itself wherever the venue
        # restated a previous close; what it cannot do is account for the silent gaps.
        verdict = "self_adjusting" if gaps == 0 else "unreconciled"
        notes.append(
            f"No corporate-action records were supplied. {signalled} discontinuities carry "
            "the exchange's own restated previous close and can be adjusted from the archive "
            f"alone; {gaps} are large moves the venue did not signal and cannot."
        )
    else:
        verdict = "clean" if unexplained == 0 else "needs_corporate_actions"
        notes.append(
            f"{unexplained} of {len(resolved)} detected discontinuities matched no supplied "
            "record."
        )

    # Whether the supplied records were actually found in the prices is the check that
    # validates the restated-previous-close detector itself, so it is reported on every
    # verdict rather than only on the failing ones.
    if supplied:
        missed = supplied - len(matched)
        if missed:
            notes.append(
                f"{len(matched)} of {supplied} supplied records were found in the prices; "
                f"{missed} were not, so either the ex-dates disagree or the "
                "restated-previous-close assumption does not hold for this venue."
            )
        else:
            notes.append(
                f"All {supplied} supplied records were found in the prices, so the "
                "restated-previous-close detector is confirmed against real records for "
                "this archive."
            )

    if implausible:
        notes.append(
            f"{len(implausible)} discontinuities imply a factor above {IMPLAUSIBLE_FACTOR}, "
            "which is not a credible split or bonus and points at a data error or two "
            "securities sharing one key."
        )

    return ReconciliationReport(
        instruments=instruments,
        sessions=sessions,
        discontinuities=tuple(resolved),
        records_supplied=supplied,
        records_matched=len(matched),
        verdict=verdict,
        notes=tuple(notes),
    )


def back_adjust(bars: Sequence, discontinuities: Sequence) -> tuple:
    """Return bars restated onto the most recent price basis.

    Each discontinuity divides everything before its ex-date by the implied factor, so the
    series is continuous and the newest bars keep the prices the market last saw. Volume is
    multiplied by the same factor, which keeps traded value intact across the boundary.

    Only discontinuities carrying an exchange-restated previous close are applied: an
    unexplained gap has no trustworthy factor, and inventing one would paper over exactly
    the problem :func:`reconcile` exists to surface. Adjusting for dividends as well as
    splits means the result is closer to a total-return series than to a price series,
    which is what a feature study wants and is not what a chart would show.
    """
    factors = [
        (item.ex_date, item.implied_factor)
        for item in discontinuities
        if item.detector == "exchange-prevclose" and item.implied_factor > 0
    ]
    if not factors:
        return tuple(bars)

    adjusted: list = []
    for bar in bars:
        divisor = Decimal(1)
        for ex_date, factor in factors:
            if bar.trading_day < ex_date:
                divisor *= factor
        if divisor == 1:
            adjusted.append(bar)
            continue
        adjusted.append(
            type(bar)(
                exchange=bar.exchange,
                trading_day=bar.trading_day,
                symbol=bar.symbol,
                isin=bar.isin,
                series=bar.series,
                open=bar.open / divisor,
                high=bar.high / divisor,
                low=bar.low / divisor,
                close=bar.close / divisor,
                previous_close=bar.previous_close / divisor,
                volume=bar.volume * divisor,
                turnover=bar.turnover,
                trades=bar.trades,
                security_name=bar.security_name,
            )
        )
    return tuple(adjusted)
