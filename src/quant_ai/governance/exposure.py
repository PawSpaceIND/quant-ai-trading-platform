"""What one contract puts at risk, measured against the book rather than against its margin.

A derivative is admitted on margin and lost on notional. Zerodha quoted these on the pilot
account, against a book of a lakh::

    GOLDPETAL   margin     1,422    notional     ~15,500     15% of the book
    SILVER100   margin     2,996    notional     ~24,000     24%
    GOLDGUINEA  margin    11,385    notional    ~123,800    124%
    GOLDTEN     margin    14,196    notional    ~154,500    155%
    GOLD        margin 1,416,538    notional  ~1.54 crore  1,540%

Every one of those margins is affordable out of a lakh, and the middle rows are where a
margin-based check quietly fails. GOLDTEN asks for fourteen thousand and controls one and a
half times the whole account: an ordinary 1.3% day in gold moves the book 2%, which is the
founder policy's entire daily loss allowance, before anything has gone wrong.

So the gate is on notional exposure. It is not a substitute for position sizing, a stop, or
the margin the broker will actually demand - it is the prior question of whether a single
contract of this thing belongs in a book this size at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Instrument

# A quarter of the book in one contract. With max_open_positions at five, a book of four such
# positions is fully committed, and the fifth cannot be opened - which is the intended shape.
# It admits GOLDPETAL and SILVER100 and refuses GOLDGUINEA and GOLDTEN, which is exactly where
# the pilot account's own numbers put the line.
DEFAULT_MAXIMUM_CONTRACT_FRACTION = Decimal("0.25")


@dataclass(frozen=True)
class ContractExposure:
    """One contract's notional, and what share of the book it would be."""

    symbol: str
    reference_price: Decimal
    lot_size: int
    notional: Decimal
    fraction_of_book: Decimal

    def as_evidence(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "referencePrice": str(self.reference_price),
            "lotSize": str(self.lot_size),
            "notional": str(self.notional),
            "fractionOfBook": str(self.fraction_of_book),
        }


def contract_notional(instrument: Instrument, reference_price: Decimal) -> Decimal:
    """Price times lot size: what the holder is long of, not what they posted to hold it."""
    if reference_price <= 0:
        raise ValueError(f"exposure_reference_price_must_be_positive:{instrument.symbol}")
    if instrument.lot_size is None or instrument.lot_size < 1:
        raise ValueError(f"exposure_lot_size_required:{instrument.symbol}")
    return reference_price * Decimal(instrument.lot_size)


def assess_contract_exposure(
    instruments: Sequence[Instrument],
    prices: Mapping[str, Decimal],
    *,
    capital: Decimal,
    maximum_fraction: Decimal = DEFAULT_MAXIMUM_CONTRACT_FRACTION,
) -> tuple[ContractExposure, ...]:
    """Refuse any dated contract whose single-lot notional is too large for this book.

    Cash equity is exempt because it is bought in shares: the order sizes itself against the
    plan, and one share is not an indivisible unit of risk. A dated contract is - you hold one
    or none - so the question is settled before any sizing logic runs.

    A watched, untradeable row is exempt too. It holds nothing, so it risks nothing.
    """
    if capital <= 0:
        raise ValueError("exposure_capital_must_be_positive")
    if not Decimal(0) < maximum_fraction <= Decimal(1):
        raise ValueError("exposure_maximum_fraction_must_be_a_fraction_of_the_book")

    assessed: list[ContractExposure] = []
    for item in instruments:
        if not item.tradable or not item.is_dated_contract:
            continue
        price = prices.get(item.symbol)
        if price is None:
            # An unpriced contract cannot be judged, and admitting the unjudged is how a
            # gate becomes decoration. This is the same reflex as the rest of the stack:
            # refuse rather than assume the missing value was fine.
            raise ValueError(f"exposure_reference_price_required:{item.symbol}")
        notional = contract_notional(item, price)
        fraction = notional / capital
        assessed.append(ContractExposure(
            item.symbol, price, int(item.lot_size or 0), notional, fraction,
        ))
        if fraction > maximum_fraction:
            raise ValueError(
                f"pilot_contract_exposure_exceeds_book:{item.symbol}:"
                f"notional={notional}:capital={capital}:"
                f"fraction={fraction.quantize(Decimal('0.0001'))}:"
                f"limit={maximum_fraction}"
            )
    return tuple(assessed)
