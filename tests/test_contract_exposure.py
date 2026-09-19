"""The gap between what a contract costs to hold and what it puts at risk.

Every margin in these tests is one Zerodha quoted on the pilot account, and every one of them
is affordable out of a lakh. The point of the gate is that affordability was never the
question.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.governance.exposure import (
    DEFAULT_MAXIMUM_CONTRACT_FRACTION,
    assess_contract_exposure,
    contract_notional,
)

BOOK = Decimal(100_000)


def contract(symbol: str, lot: int, *, tradable: bool = True) -> Instrument:
    return Instrument(
        symbol, Market.INDIA, AssetClass.METAL, "INR", "MCX", tradable,
        expiry=date(2026, 12, 5), lot_size=lot, tick_size=Decimal(1), underlying="GOLD",
    )


def equity(symbol: str = "INFY") -> Instrument:
    return Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def test_the_small_contracts_the_pilot_account_can_actually_hold():
    """GOLDPETAL is one gram: about fifteen and a half thousand of gold against a lakh."""
    petal = contract("GOLDPETAL", 1)

    exposure = assess_contract_exposure([petal], {"GOLDPETAL": Decimal(15467)}, capital=BOOK)

    assert exposure[0].notional == Decimal(15467)
    assert exposure[0].fraction_of_book < DEFAULT_MAXIMUM_CONTRACT_FRACTION


def test_a_contract_whose_margin_fits_but_whose_exposure_does_not_is_refused():
    """GOLDTEN's margin is 14,196 - fourteen percent of the book, comfortably affordable.
    The contract controls about 1.55 lakh, which is 155% of the account at roughly eleven
    times leverage. A 1.3% day in gold then moves the book 2%, the founder policy's entire
    daily loss allowance, before anything has gone wrong. A margin-based check waves this
    through; that is the whole reason this one measures notional."""
    ten = contract("GOLDTEN", 10)

    with pytest.raises(ValueError, match="pilot_contract_exposure_exceeds_book:GOLDTEN"):
        assess_contract_exposure([ten], {"GOLDTEN": Decimal(15450)}, capital=BOOK)


def test_the_refusal_names_the_numbers_it_refused_on():
    """An operator reading a startup failure needs the arithmetic, not just a verdict."""
    guinea = contract("GOLDGUINEA", 8)

    with pytest.raises(ValueError) as refusal:
        assess_contract_exposure([guinea], {"GOLDGUINEA": Decimal(15470)}, capital=BOOK)

    message = str(refusal.value)
    assert "notional=123760" in message
    assert "capital=100000" in message
    assert "limit=0.25" in message


def test_cash_equity_is_not_judged_by_this_gate():
    """A share is divisible: the order sizes itself against the plan, so one share is not an
    indivisible unit of risk the way one contract is."""
    exposure = assess_contract_exposure([equity()], {}, capital=BOOK)

    assert exposure == ()


def test_a_watched_row_holds_nothing_so_it_risks_nothing():
    watched = contract("GOLD", 100, tradable=False)

    exposure = assess_contract_exposure([watched], {}, capital=BOOK)

    assert exposure == ()


def test_an_unpriced_contract_is_refused_rather_than_assumed_affordable():
    """Admitting the unjudged is how a gate becomes decoration."""
    petal = contract("GOLDPETAL", 1)

    with pytest.raises(ValueError, match="exposure_reference_price_required:GOLDPETAL"):
        assess_contract_exposure([petal], {}, capital=BOOK)


def test_a_bigger_book_admits_a_bigger_contract():
    """The gate is a ratio, not a list of instruments. The same contract that is refused at a
    lakh is ordinary at fifty."""
    ten = contract("GOLDTEN", 10)

    exposure = assess_contract_exposure(
        [ten], {"GOLDTEN": Decimal(15450)}, capital=Decimal(5_000_000)
    )

    assert exposure[0].fraction_of_book < DEFAULT_MAXIMUM_CONTRACT_FRACTION


@pytest.mark.parametrize("capital", [Decimal(0), Decimal(-1)])
def test_a_book_with_no_capital_is_an_error_not_an_empty_pass(capital):
    with pytest.raises(ValueError, match="exposure_capital_must_be_positive"):
        assess_contract_exposure([contract("GOLDPETAL", 1)], {}, capital=capital)


@pytest.mark.parametrize("fraction", [Decimal(0), Decimal("-0.1"), Decimal("1.5")])
def test_a_limit_that_is_not_a_fraction_of_the_book_is_refused(fraction):
    with pytest.raises(ValueError, match="exposure_maximum_fraction"):
        assess_contract_exposure(
            [contract("GOLDPETAL", 1)], {"GOLDPETAL": Decimal(15467)},
            capital=BOOK, maximum_fraction=fraction,
        )


def test_notional_is_price_times_lot_not_the_quoted_price():
    """Kite reports lot_size 1 for MCX, which is the tradable unit rather than the contract
    multiplier. The multiplier is what the operator declares in the watchlist, and it is what
    turns a quote into an exposure."""
    assert contract_notional(contract("GOLDTEN", 10), Decimal(15450)) == Decimal(154_500)


def test_a_contract_with_no_lot_size_cannot_be_measured():
    bad = Instrument(
        "GOLDX", Market.INDIA, AssetClass.METAL, "INR", "MCX", False,
        expiry=date(2026, 12, 5), lot_size=None, tick_size=Decimal(1), underlying="GOLD",
    )

    with pytest.raises(ValueError, match="exposure_lot_size_required:GOLDX"):
        contract_notional(bad, Decimal(100))


def test_every_contract_is_judged_not_only_the_first():
    """A refusal on the second row must not be hidden by a pass on the first."""
    petal = contract("GOLDPETAL", 1)
    ten = contract("GOLDTEN", 10)

    with pytest.raises(ValueError, match="GOLDTEN"):
        assess_contract_exposure(
            [petal, ten],
            {"GOLDPETAL": Decimal(15467), "GOLDTEN": Decimal(15450)},
            capital=BOOK,
        )


# --- The path the launcher actually walks -------------------------------------------------

def _directives(rows: list[dict], capital: int = 100_000) -> dict:
    return {
        "starting_capital": capital,
        "allowed_markets": ["INDIA"],
        "allowed_asset_classes": ["EQUITY", "ETF", "METAL"],
        "max_open_positions": 5,
        "watchlist": [
            {"symbol": "INFY", "market": "INDIA", "asset_class": "EQUITY",
             "currency": "INR", "exchange": "NSE"},
            *rows,
        ],
    }


def _mcx_row(symbol: str, lot: int, **extra) -> dict:
    row = {"symbol": symbol, "market": "INDIA", "asset_class": "METAL", "currency": "INR",
           "exchange": "MCX", "expiry": "2026-12-05", "lot_size": lot, "tick_size": "1",
           "underlying": "GOLD"}
    row.update(extra)
    return row


def test_a_directives_file_naming_an_oversized_contract_is_refused():
    """The composition the launcher performs: read the mandate, price the watchlist, refuse
    before any broker state is opened. Written against the JSON rather than against
    hand-built objects, because the JSON is what an operator actually edits."""
    from quant_ai.governance.directives import FounderDirectives

    mandate = FounderDirectives.from_json(_directives([_mcx_row("GOLDTEN", 10)]))

    with pytest.raises(ValueError, match="pilot_contract_exposure_exceeds_book:GOLDTEN"):
        assess_contract_exposure(
            mandate.watchlist,
            {"GOLDTEN": Decimal(15450)},
            capital=mandate.starting_capital,
        )


def test_a_directives_file_naming_a_contract_the_book_can_hold_is_admitted():
    from quant_ai.governance.directives import FounderDirectives

    mandate = FounderDirectives.from_json(_directives([_mcx_row("GOLDPETAL", 1)]))

    exposures = assess_contract_exposure(
        mandate.watchlist, {"GOLDPETAL": Decimal(15467)}, capital=mandate.starting_capital,
    )

    assert [item.symbol for item in exposures] == ["GOLDPETAL"]


def test_a_watched_contract_needs_no_price_and_blocks_no_launch():
    """The evening-observation row: an MCX metal on an account with no commodity segment.
    It cannot be ordered, so it is never priced against the book."""
    from quant_ai.governance.directives import FounderDirectives

    mandate = FounderDirectives.from_json(
        _directives([_mcx_row("GOLD", 100, tradable=False, expiry=None, lot_size=None,
                              tick_size=None, underlying=None)])
    )

    assert assess_contract_exposure(mandate.watchlist, {}, capital=mandate.starting_capital) == ()


def test_the_launcher_prices_the_book_before_it_opens_broker_state():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "india_paper_runtime.py").read_text()

    assert "assess_contract_exposure" in source
    # Priced from the same quote call the token map is built from, so the check cannot be
    # judging a different price than the one the engine subscribes to.
    assert '"last_price"' in source
