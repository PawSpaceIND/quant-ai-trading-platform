"""What a dated contract is, and when it stops being one.

A share does not expire. You can hold INFY for nineteen years, which is exactly what the
baseline runs do. Everything on MCX, NCDEX, NFO, BFO and the currency segments is the
other kind of instrument: a contract with a death date, a fixed lot, and a tick it must
be priced on. "GOLD" is not tradable. ``GOLD25DECFUT``, expiring 5 December, lot 100
grams, tick INR 1, is.

The platform had no way to say that. ``Instrument`` carried a symbol and an exchange and
nothing that distinguishes a contract from a listing, so an MCX row and an NSE row looked
alike to every layer above. That is the gap this module closes, and it has to close before
anything prices or margins a future: you cannot compute a cost or a margin for a contract
you cannot identify, and a position held past expiry is not a position, it is a settlement
that already happened without you.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from quant_ai.domain.models import (
    DERIVATIVE_ASSET_CLASSES,
    DERIVATIVE_EXCHANGES,
    Instrument,
    Market,
    Side,
)

IST = ZoneInfo("Asia/Kolkata")

__all__ = [
    "DEFAULT_ROLLOVER_SESSIONS",
    "DERIVATIVE_ASSET_CLASSES",
    "DERIVATIVE_EXCHANGES",
    "ContractError",
    "ContractState",
    "assert_contract_tradable",
    "assert_order_fits_contract",
    "contract_state",
    "is_dated_contract",
    "local_date",
]

# How long before expiry a contract stops accepting new risk. Three sessions is the window
# in which liquidity moves to the next month and the front month's spread widens; opening a
# position there is buying into the exit. Closing one is always allowed, and must be.
DEFAULT_ROLLOVER_SESSIONS = 3


class ContractState(str, Enum):
    LIVE = "LIVE"
    # Near expiry: close or roll, but take no new risk.
    ROLLOVER = "ROLLOVER"
    # Past its last trading day. It settled. There is nothing left to trade.
    EXPIRED = "EXPIRED"


class ContractError(ValueError):
    """An instrument that cannot be traded as the contract it claims to be."""


def is_dated_contract(instrument: Instrument) -> bool:
    """Whether this instrument dies on a date, rather than being held indefinitely."""
    return instrument.is_dated_contract


def local_date(moment: datetime, instrument: Instrument) -> date:
    """The trading date a moment falls on, in the venue's own day.

    Read off the venue's clock, never UTC. MCX trades to 23:30 IST, which is 18:00 UTC of
    the same day - but an evening bar and the next morning's share a UTC day while
    belonging to two Indian ones, and an expiry judged in UTC would kill a contract at
    18:00 on the day it is still trading.
    """
    if moment.tzinfo is None:
        raise ContractError("contract_clock_must_be_timezone_aware")
    zone = IST if instrument.market == Market.INDIA else moment.tzinfo
    return moment.astimezone(zone).date()


def contract_state(
    instrument: Instrument,
    now: datetime,
    rollover_sessions: int = DEFAULT_ROLLOVER_SESSIONS,
) -> ContractState:
    """LIVE, ROLLOVER or EXPIRED for a dated contract; always LIVE for a share.

    ``rollover_sessions`` is counted in calendar days, which is deliberately conservative:
    a weekend inside the window shortens the real trading runway, so counting calendar days
    opens the window earlier than counting sessions would, never later.
    """
    if rollover_sessions < 0:
        raise ContractError("rollover_sessions_cannot_be_negative")
    expiry = instrument.expiry
    if expiry is None:
        # A share, an ETF, an index. Nothing to expire, so nothing to check.
        return ContractState.LIVE
    today = local_date(now, instrument)
    if today > expiry:
        return ContractState.EXPIRED
    if today >= expiry - timedelta(days=rollover_sessions):
        return ContractState.ROLLOVER
    return ContractState.LIVE


def assert_contract_tradable(
    instrument: Instrument,
    side: Side,
    now: datetime,
    rollover_sessions: int = DEFAULT_ROLLOVER_SESSIONS,
) -> ContractState:
    """Refuse an order the contract's own calendar has already answered.

    An expired contract refuses both sides. That is not symmetry for its own sake: the
    position is gone, settled at the exchange's price on expiry day, and an order to
    "close" it would be opening a new one in a contract that no longer lists. A position
    still on the book against an expired contract is a reconciliation failure, and it has
    to surface as one rather than be quietly traded away here.

    The rollover window refuses only the buy. Exits are never trapped - the same rule the
    risk firewall applies to a halt, for the same reason.
    """
    state = contract_state(instrument, now, rollover_sessions)
    if state is ContractState.EXPIRED:
        raise ContractError(
            f"contract_expired:{instrument.symbol}:{instrument.exchange}:"
            f"{instrument.expiry}:settled_reconcile_do_not_trade"
        )
    if state is ContractState.ROLLOVER and side == Side.BUY:
        raise ContractError(
            f"contract_in_rollover:{instrument.symbol}:{instrument.exchange}:"
            f"{instrument.expiry}:roll_or_close_no_new_risk"
        )
    return state


def assert_order_fits_contract(
    instrument: Instrument, quantity: int, price: Decimal
) -> None:
    """A derivative trades in whole lots, at prices on its tick. Both or neither.

    An order for 7 lots of a 100-gram gold contract is 700 grams; an order for 7 *grams*
    is not an order at all, and a broker would reject it. The same is true of a price
    between two ticks. Both are refused here rather than discovered at the exchange,
    because a paper fill that could not have happened is worse than no fill: it produces a
    track record of trades nobody could have made.
    """
    if quantity <= 0:
        raise ContractError(f"contract_quantity_must_be_positive:{instrument.symbol}")
    lot = instrument.lot_size
    if lot is not None and quantity % lot != 0:
        raise ContractError(
            f"contract_quantity_not_whole_lots:{instrument.symbol}:"
            f"quantity={quantity}:lot_size={lot}"
        )
    tick = instrument.tick_size
    if price <= 0:
        raise ContractError(f"contract_price_must_be_positive:{instrument.symbol}")
    if tick is not None and (price % tick) != 0:
        raise ContractError(
            f"contract_price_off_tick:{instrument.symbol}:price={price}:tick_size={tick}"
        )
