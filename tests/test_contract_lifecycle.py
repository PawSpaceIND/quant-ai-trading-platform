"""A dated contract, and the three answers its own calendar gives."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.governance.directives import FounderDirectives
from quant_ai.instruments.contract import (
    ContractError,
    ContractState,
    assert_contract_tradable,
    assert_order_fits_contract,
    contract_state,
    is_dated_contract,
)

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 12, 5)


def gold(**overrides) -> Instrument:
    fields = {
        "expiry": EXPIRY, "lot_size": 100, "tick_size": Decimal(1), "underlying": "GOLD",
    }
    fields.update(overrides)
    return Instrument("GOLD25DECFUT", Market.INDIA, AssetClass.METAL, "INR", "MCX", **fields)


INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def test_a_tradable_contract_must_say_which_contract_it_is() -> None:
    """The state this change exists to make unrepresentable.

    "GOLD" on MCX is a listing, not something anyone can buy. Every layer downstream -
    pricing, margin, rollover - has to be able to assume the expiry and the lot are there,
    and the only way to guarantee that is to refuse to build the object without them.
    A half-named contract is refused too: an expiry with no lot size is still not a
    contract, and the message says which half is missing.
    """
    for missing in ({"expiry": None}, {"lot_size": None}, {"expiry": None, "lot_size": None}):
        with pytest.raises(ValueError, match="contract_identity_required:GOLD25DECFUT:MCX"):
            gold(**missing)

    # Named in full, it builds - and knows what it is.
    assert is_dated_contract(gold())
    assert not is_dated_contract(INFY)

    # The requirement is on tradable rows only. The catalog's job is to say "MCX lists
    # metals", and pinning a December contract there would be wrong every January.
    listing = Instrument("GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX", tradable=False)
    assert listing.expiry is None and is_dated_contract(listing)

    # A share is the mirror image: it does not expire, and a date here would send every
    # lifecycle check below hunting a contract that does not exist.
    with pytest.raises(ValueError, match="cash_instrument_cannot_expire:INFY:NSE"):
        Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE", expiry=EXPIRY)

    # A lot or tick that is not a real quantity is refused wherever it appears.
    with pytest.raises(ValueError, match="instrument_lot_size_must_be_positive"):
        gold(lot_size=0)
    with pytest.raises(ValueError, match="instrument_tick_size_must_be_positive"):
        gold(tick_size=Decimal(0))


def test_expiry_is_judged_on_the_venues_own_day_not_utc() -> None:
    """MCX trades to 23:30 IST, so its day and the UTC day come apart every night.

    02:00 IST on 6 December is 20:30 UTC on the 5th. Read off the wrong clock, a contract
    that expired yesterday still looks live for five and a half hours - long enough for an
    overnight housekeeping tick to act on a position that settled.
    """
    after_expiry_ist = datetime(2026, 12, 6, 2, tzinfo=IST)
    assert after_expiry_ist.astimezone(timezone.utc).date() == date(2026, 12, 5)
    assert contract_state(gold(), after_expiry_ist) is ContractState.EXPIRED

    # And the reverse: late on expiry day itself the contract is still trading.
    assert contract_state(gold(), datetime(2026, 12, 5, 23, tzinfo=IST)) is ContractState.ROLLOVER

    # A naive timestamp has no day at all, and guessing one is how the bug above happens.
    with pytest.raises(ContractError, match="contract_clock_must_be_timezone_aware"):
        contract_state(gold(), datetime(2026, 12, 6, 2))  # noqa: DTZ001

    # A share has no calendar to consult, at any hour.
    assert contract_state(INFY, after_expiry_ist) is ContractState.LIVE


def test_the_three_states_and_what_each_one_refuses() -> None:
    """Expired refuses both sides; rollover refuses only new risk.

    The asymmetry is deliberate and matches the rule the risk firewall already applies to
    a halt: freeze risk-taking, never trap an exit. Expiry is the one case where even the
    exit is refused, because there is nothing left to exit - the contract settled at the
    exchange's price, and an order against it now would open a position in a contract that
    no longer lists. A position still on the book there is a reconciliation failure and has
    to surface as one.
    """
    live = datetime(2026, 11, 20, 12, tzinfo=IST)
    rolling = datetime(2026, 12, 3, 12, tzinfo=IST)
    gone = datetime(2026, 12, 8, 12, tzinfo=IST)
    contract = gold()

    assert contract_state(contract, live) is ContractState.LIVE
    for side in (Side.BUY, Side.SELL):
        assert assert_contract_tradable(contract, side, live) is ContractState.LIVE

    assert contract_state(contract, rolling) is ContractState.ROLLOVER
    with pytest.raises(ContractError, match="contract_in_rollover:GOLD25DECFUT:MCX:2026-12-05"):
        assert_contract_tradable(contract, Side.BUY, rolling)
    # The exit is never trapped.
    assert assert_contract_tradable(contract, Side.SELL, rolling) is ContractState.ROLLOVER

    assert contract_state(contract, gone) is ContractState.EXPIRED
    for side in (Side.BUY, Side.SELL):
        with pytest.raises(ContractError, match="contract_expired:GOLD25DECFUT:MCX:2026-12-05"):
            assert_contract_tradable(contract, side, gone)

    # The window is configurable, and a zero window still refuses on expiry day's close.
    assert contract_state(contract, rolling, rollover_sessions=0) is ContractState.LIVE
    assert contract_state(contract, live, rollover_sessions=30) is ContractState.ROLLOVER
    with pytest.raises(ContractError, match="rollover_sessions_cannot_be_negative"):
        contract_state(contract, live, rollover_sessions=-1)


def test_a_derivative_trades_in_whole_lots_on_its_own_tick() -> None:
    """A paper fill nobody could have made is worse than no fill.

    Seven grams of a hundred-gram contract is not a small order, it is not an order. A
    broker rejects it. If the paper ledger accepts it, the track record contains trades
    that could never have happened, which is the one thing a paper record exists to avoid.
    """
    contract = gold()
    assert_order_fits_contract(contract, 200, Decimal(75000))

    with pytest.raises(ContractError, match="contract_quantity_not_whole_lots:.*quantity=150:lot_size=100"):
        assert_order_fits_contract(contract, 150, Decimal(75000))
    with pytest.raises(ContractError, match="contract_price_off_tick:.*tick_size=1"):
        assert_order_fits_contract(contract, 100, Decimal("75000.50"))
    with pytest.raises(ContractError, match="contract_quantity_must_be_positive"):
        assert_order_fits_contract(contract, 0, Decimal(75000))
    with pytest.raises(ContractError, match="contract_price_must_be_positive"):
        assert_order_fits_contract(contract, 100, Decimal(0))

    # A share has neither constraint declared, so neither is invented for it.
    assert_order_fits_contract(INFY, 7, Decimal("1543.35"))


def test_a_directives_watchlist_can_name_a_contract_and_cannot_half_name_one() -> None:
    """The operator declares the watchlist in JSON, so the contract has to survive JSON.

    A file that carries an expiry this cannot read must be an error. Dropping it would turn
    a malformed contract into a cash instrument, which ``Instrument`` then accepts without
    complaint - the silent widening this whole change exists to prevent.
    """
    payload = {
        "watchlist": [{
            "symbol": "GOLD25DECFUT", "market": "INDIA", "asset_class": "METAL",
            "currency": "INR", "exchange": "MCX",
            "expiry": "2026-12-05", "lot_size": 100, "tick_size": "1", "underlying": "gold",
        }]
    }
    parsed = FounderDirectives.from_json(payload).watchlist[0]
    assert parsed.expiry == EXPIRY
    assert parsed.lot_size == 100
    assert parsed.tick_size == Decimal(1)
    assert parsed.underlying == "GOLD"

    for bad, message in (
        ({"expiry": "5th December"}, "watchlist_expiry_not_a_date"),
        ({"lot_size": "one hundred"}, "watchlist_lot_size_not_an_integer"),
        ({"tick_size": "a rupee"}, "watchlist_tick_size_not_a_number"),
    ):
        broken = {"watchlist": [{**payload["watchlist"][0], **bad}]}
        with pytest.raises(ValueError, match=message):
            FounderDirectives.from_json(broken)

    # And an MCX entry that names no contract at all is refused by the model beneath.
    bare = {"watchlist": [{"symbol": "GOLD", "market": "INDIA", "asset_class": "METAL",
                           "currency": "INR", "exchange": "MCX"}]}
    with pytest.raises(ValueError, match="contract_identity_required"):
        FounderDirectives.from_json(bare)
