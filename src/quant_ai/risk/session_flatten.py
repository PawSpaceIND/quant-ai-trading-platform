"""Finish the day flat: enumerate what is held, cover it, and prove the book is empty.

An intraday mandate that cannot close its own positions is not an intraday mandate. The
overnight firewall in :mod:`quant_ai.risk.overnight` stops *new* exposure near the close
and caps what may be carried, but nothing in this repository ever closed what is already
open. A book that drifts into the night because no component owned the last step is
carrying gap risk that nobody decided to take.

**This is a deterministic, risk-reducing operation and it does not ask the AI.** Same
standing as a protective exit: a stop does not put its trigger to a vote, and neither does
this. An engine that can be talked out of going flat by a confident model has no intraday
guarantee at all.

**It plans; it does not execute.** The result is a set of ordinary ``OrderIntent`` values
for the caller to submit through the same governed OMS as everything else, so every
covering order still passes the risk firewall, idempotency and the ledger. Bypassing the
AI is the point; bypassing the order path would be how a bug becomes an unrecorded trade.

**Generating orders is not the same as being flat.** :func:`plan_session_flatten` says what
should be sent; only :func:`verify_flat` says what happened, and it refuses to certify a
flat book without both zero open positions and zero working orders. Treating submission as
completion is precisely how a position survives to the next morning while the log says the
day closed cleanly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Market, OrderIntent, PortfolioSnapshot, Side

SCHEMA = "pramana.session_flatten.v1"

#: How long before the regular close the flatten begins. It sits outside the overnight
#: firewall's ten-minute closing window on purpose: by the time covering orders go out,
#: exposure-adding entries are already being refused, so the book cannot be reopened behind
#: the flatten. Fifteen minutes also leaves room for a retry on an order that does not fill.
DEFAULT_CUTOFF = timedelta(minutes=15)

FLATTEN_STRATEGY_ID = "session-flatten"


@dataclass(frozen=True)
class FlattenPolicy:
    cutoff: timedelta = DEFAULT_CUTOFF

    def __post_init__(self) -> None:
        if self.cutoff <= timedelta(0):
            raise ValueError("the flatten cutoff must be a positive interval before the close")


@dataclass(frozen=True)
class FlattenIntent:
    """One covering order, and the position it exists to close."""

    order: OrderIntent
    held_quantity: int
    reason: str

    def __post_init__(self) -> None:
        if self.held_quantity == 0:
            raise ValueError("a flatten intent needs a position to close")
        # The safety property of this whole module, asserted where it is constructed rather
        # than left to the caller: a covering order is the exact opposite of what is held.
        # It can only ever reduce exposure to zero - never reverse it, never add to it.
        expected = Side.SELL if self.held_quantity > 0 else Side.BUY
        if self.order.side is not expected:
            raise ValueError(
                f"{self.order.symbol}: a flatten order must oppose the position it closes"
            )
        if self.order.quantity != abs(self.held_quantity):
            raise ValueError(
                f"{self.order.symbol}: a flatten order must cover the position exactly; "
                f"held {abs(self.held_quantity)}, ordered {self.order.quantity}"
            )


NO_INSTANT_REASON = (
    "no timezone-aware instant: where the book sits relative to the close is the whole "
    "question, and a guessed answer is wrong half the year"
)
NO_CALENDAR_REASON = (
    "no session calendar, so there is no close to flatten against. This is not a report "
    "that the book is fine; nothing was checked."
)


@dataclass(frozen=True)
class FlattenPlan:
    # ``None`` when no usable instant was supplied. A placeholder datetime here would be a
    # naive one, and a naive instant is exactly the thing this module refuses to reason from.
    as_of: datetime | None
    due: bool
    session_close: datetime | None
    intents: tuple
    unpriceable: tuple
    verdict: str
    reasons: tuple

    @property
    def orders(self) -> tuple:
        return tuple(item.order for item in self.intents)

    @property
    def covers_everything(self) -> bool:
        """Whether every open position has a covering order in this plan."""
        return not self.unpriceable and self.verdict in {"ready", "already_flat", "not_due"}

    def as_evidence(self) -> dict:
        return {
            "schema": SCHEMA,
            "as_of": None if self.as_of is None else self.as_of.isoformat(),
            "due": self.due,
            "session_close": None if self.session_close is None else self.session_close.isoformat(),
            "orders": [
                {
                    "symbol": item.order.symbol,
                    "side": item.order.side.value,
                    "quantity": item.order.quantity,
                    "reference_price": str(item.order.reference_price),
                    "held_quantity": item.held_quantity,
                    "reason": item.reason,
                }
                for item in self.intents
            ],
            "unpriceable": list(self.unpriceable),
            "covers_everything": self.covers_everything,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "limitation": (
                "A plan states what should be sent. Only verify_flat states what happened; "
                "submitting these orders is not evidence the book is flat."
            ),
        }


@dataclass(frozen=True)
class FlattenProof:
    """What the book actually looked like after the covering orders were worked."""

    checked_at: datetime
    open_positions: int
    open_orders: int
    residual: tuple
    verdict: str
    reasons: tuple

    @property
    def flat(self) -> bool:
        return self.verdict == "flat"

    @property
    def halt_required(self) -> bool:
        """Anything still open past the close is an unplanned overnight position."""
        return self.verdict == "not_flat"

    def as_evidence(self) -> dict:
        return {
            "schema": SCHEMA,
            "checked_at": self.checked_at.isoformat(),
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "residual": [
                {"symbol": symbol, "quantity": quantity} for symbol, quantity in self.residual
            ],
            "flat": self.flat,
            "halt_required": self.halt_required,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


def _held(portfolio: PortfolioSnapshot) -> list:
    return sorted(
        (symbol, quantity)
        for symbol, quantity in portfolio.symbol_quantity.items()
        if quantity != 0
    )


def plan_session_flatten(
    portfolio: PortfolioSnapshot,
    *,
    market: Market,
    now: datetime | None,
    calendar,
    marks: Mapping,
    policy: FlattenPolicy | None = None,
    tenant_id: str = "default",
    asset_class: AssetClass = AssetClass.EQUITY,
) -> FlattenPlan:
    """Covering orders for every open position, once the session is close enough to end.

    ``calendar`` is the operator's session calendar and is required. Without it there is no
    close to measure against, and this returns ``unavailable`` rather than deciding the book
    is fine - an unarmed control that reports nothing to do is indistinguishable from an
    armed one over an empty book, and only one of those is safe.

    ``marks`` are current prices. A position whose symbol has no mark cannot be given a
    reference price, so it is named in ``unpriceable`` and the plan refuses to call itself
    complete. Pricing it at a guess would be inventing the trade's own execution reference.
    """
    settings = policy or FlattenPolicy()
    rules: list = []

    if now is None or now.tzinfo is None or now.utcoffset() is None:
        return FlattenPlan(
            as_of=None, due=False, session_close=None, intents=(), unpriceable=(),
            verdict="unavailable", reasons=(NO_INSTANT_REASON,),
        )
    if calendar is None:
        return FlattenPlan(
            as_of=now, due=False, session_close=None, intents=(), unpriceable=(),
            verdict="unavailable", reasons=(NO_CALENDAR_REASON,),
        )

    held = _held(portfolio)
    try:
        session = calendar.session(market)
        zone = ZoneInfo(session.timezone)
        local = now.astimezone(zone)
        regular_close, _ = session.closes_on(local.date())
        close = datetime.combine(local.date(), regular_close, tzinfo=zone)
    except Exception as error:  # noqa: BLE001 - a calendar fault must not silently pass
        return FlattenPlan(
            as_of=now, due=False, session_close=None, intents=(), unpriceable=(),
            verdict="unavailable",
            reasons=(f"session close could not be determined: {type(error).__name__}",),
        )

    due = local >= close - settings.cutoff
    if not due:
        rules.append(
            f"{(close - settings.cutoff - local)} until the flatten window opens at "
            f"{(close - settings.cutoff).time()}; {len(held)} positions open."
        )
        return FlattenPlan(
            as_of=now, due=False, session_close=close, intents=(), unpriceable=(),
            verdict="not_due", reasons=tuple(rules),
        )

    if not held:
        return FlattenPlan(
            as_of=now, due=True, session_close=close, intents=(), unpriceable=(),
            verdict="already_flat",
            reasons=("no open positions at the flatten cutoff",),
        )

    intents: list = []
    unpriceable: list = []
    for symbol, quantity in held:
        mark = marks.get(symbol)
        if mark is None or Decimal(str(mark)) <= 0:
            unpriceable.append(symbol)
            continue
        side = Side.SELL if quantity > 0 else Side.BUY
        intents.append(
            FlattenIntent(
                order=OrderIntent(
                    symbol=symbol,
                    market=market,
                    side=side,
                    quantity=abs(quantity),
                    reference_price=Decimal(str(mark)),
                    strategy_id=FLATTEN_STRATEGY_ID,
                    asset_class=asset_class,
                    tenant_id=tenant_id,
                ),
                held_quantity=quantity,
                reason="session_flatten",
            )
        )

    if unpriceable:
        rules.append(
            f"{len(unpriceable)} positions have no current mark and cannot be given an "
            f"execution reference: {', '.join(unpriceable)}. They are not covered by this "
            "plan and the book will not be flat until they are."
        )
        verdict = "incomplete"
    else:
        rules.append(
            f"covering {len(intents)} positions before the {close.time()} close; every "
            "order opposes its position and matches its size exactly."
        )
        verdict = "ready"

    return FlattenPlan(
        as_of=now, due=True, session_close=close,
        intents=tuple(intents), unpriceable=tuple(unpriceable),
        verdict=verdict, reasons=tuple(rules),
    )


def verify_flat(
    portfolio: PortfolioSnapshot,
    *,
    working_orders: Sequence,
    checked_at: datetime,
) -> FlattenProof:
    """Whether the book is actually empty, which submission alone never establishes.

    Both conditions are required. Zero positions with a working order outstanding is a book
    that is about to have a position again, and calling that flat is how an unplanned
    overnight trade gets recorded as a clean close.
    """
    residual = tuple(_held(portfolio))
    open_orders = len(tuple(working_orders))
    reasons: list = []

    if not residual and open_orders == 0:
        verdict = "flat"
        reasons.append("no open positions and no working orders")
    else:
        verdict = "not_flat"
        if residual:
            reasons.append(
                "positions still open past the flatten: "
                + ", ".join(f"{symbol} {quantity:+d}" for symbol, quantity in residual)
            )
        if open_orders:
            reasons.append(
                f"{open_orders} working orders outstanding; the book can still acquire a "
                "position after this check"
            )
        reasons.append(
            "This is an unplanned overnight exposure. Halt and alert rather than recording "
            "the session as closed."
        )

    return FlattenProof(
        checked_at=checked_at,
        open_positions=len(residual),
        open_orders=open_orders,
        residual=residual,
        verdict=verdict,
        reasons=tuple(reasons),
    )
