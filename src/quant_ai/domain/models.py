from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    INDIA = "INDIA"
    USA = "USA"
    GLOBAL = "GLOBAL"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FX = "FX"
    COMMODITY = "COMMODITY"
    METAL = "METAL"
    BOND = "BOND"
    DEBT = "DEBT"
    FUND = "FUND"
    REIT = "REIT"
    INVIT = "INVIT"
    SLB = "SLB"
    IPO = "IPO"
    SGB = "SGB"
    CRYPTO = "CRYPTO"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RiskMode(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


# Every Indian venue that lists dated contracts and nothing else: MCX and NCDEX are
# commodity derivatives, NFO and BFO equity derivatives, CDS and BCD currency derivatives,
# and the MSEI and IFSC segments the instrument master ingests are derivative segments too.
# None of them list a cash share. Membership here makes an instrument a dated contract.
DERIVATIVE_EXCHANGES = frozenset({"MCX", "NCDEX", "NFO", "BFO", "CDS", "BCD", "MSEI", "IFSC"})

# And the two asset classes that are dated wherever they happen to be listed.
DERIVATIVE_ASSET_CLASSES = frozenset({AssetClass.FUTURE, AssetClass.OPTION})


@dataclass(frozen=True)
class Instrument:
    """A tradable thing, and - when it is a contract - which contract.

    ``symbol``/``exchange`` identify a listing. The four fields after ``metadata`` identify
    a *contract*: "GOLD" is a listing on MCX and cannot be bought, while GOLD25DECFUT -
    expiring 5 December, 100 grams a lot, priced on a INR 1 tick - can. A share needs none
    of them and must not carry an expiry; a tradable derivative cannot do without them.

    The requirement is on ``tradable`` instruments only. A ``tradable=False`` row is a
    statement that a venue and an asset class exist - which is what the catalog is for -
    and naming a specific December contract there would be wrong the moment it expired.
    """

    symbol: str
    market: Market
    asset_class: AssetClass
    currency: str
    exchange: str
    tradable: bool = True
    metadata: dict[str, str] = field(default_factory=dict)
    # Contract identity. Declared after ``metadata`` so that every existing positional
    # construction of a cash instrument keeps working unchanged.
    expiry: date | None = None
    lot_size: int | None = None
    tick_size: Decimal | None = None
    underlying: str | None = None

    @property
    def is_dated_contract(self) -> bool:
        """Whether this instrument dies on a date rather than being held indefinitely."""
        return (
            self.exchange.upper() in DERIVATIVE_EXCHANGES
            or self.asset_class in DERIVATIVE_ASSET_CLASSES
        )

    def __post_init__(self) -> None:
        if self.lot_size is not None and self.lot_size < 1:
            raise ValueError(f"instrument_lot_size_must_be_positive:{self.symbol}")
        if self.tick_size is not None and self.tick_size <= 0:
            raise ValueError(f"instrument_tick_size_must_be_positive:{self.symbol}")
        if not self.tradable:
            # A venue listing, not a contract. Nothing below applies.
            return
        if self.is_dated_contract:
            # Refusing this is the whole point of the field. An MCX row with no expiry is
            # not a contract anyone can trade, and every layer downstream - pricing,
            # margin, rollover - has to be able to assume the date is there.
            missing = [
                name for name, value in
                (("expiry", self.expiry), ("lot_size", self.lot_size))
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"contract_identity_required:{self.symbol}:{self.exchange}:"
                    f"{'+'.join(missing)}"
                )
        elif self.expiry is not None:
            # A share does not expire. Carrying a date here would make every rollover and
            # lifecycle check downstream act on a contract that does not exist.
            raise ValueError(
                f"cash_instrument_cannot_expire:{self.symbol}:{self.exchange}:{self.expiry}"
            )


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    market: Market
    side: Side
    quantity: int
    reference_price: Decimal
    strategy_id: str
    asset_class: AssetClass = AssetClass.EQUITY
    tenant_id: str = "default"
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None


@dataclass(frozen=True)
class InstrumentBoundOrderIntent(OrderIntent):
    """Order whose executable instrument identity is immutable at decision time.

    This is opt-in so existing cash-order provenance/idempotency remains unchanged.
    Derivative/pilot expansion can require this type without making a fill-time registry
    lookup or retroactively changing every legacy OrderIntent.
    """

    instrument: Instrument | None = None

    def __post_init__(self) -> None:
        instrument = self.instrument
        if instrument is None:
            raise ValueError("instrument_bound_order_requires_instrument")
        if not isinstance(instrument, Instrument) or instrument.tradable is not True:
            raise ValueError("instrument_bound_order_requires_tradable_instrument")
        from quant_ai.instruments.identity import immutable_instrument_snapshot

        instrument = immutable_instrument_snapshot(instrument)
        object.__setattr__(self, "instrument", instrument)
        if (
            self.symbol != instrument.symbol
            or self.market is not instrument.market
            or self.asset_class is not instrument.asset_class
        ):
            raise ValueError("instrument_bound_order_identity_mismatch")
        if instrument.lot_size is not None and (
            type(self.quantity) is not int
            or self.quantity <= 0
            or self.quantity % instrument.lot_size
        ):
            raise ValueError(
                f"instrument_bound_order_not_whole_lots:{instrument.symbol}:"
                f"{instrument.lot_size}"
            )


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    daily_realized_pnl: Decimal
    gross_exposure: Decimal
    peak_equity: Decimal | None = None
    symbol_exposure: dict[str, Decimal] = field(default_factory=dict)
    asset_exposure: dict[AssetClass, Decimal] = field(default_factory=dict)
    # Held units per symbol. Lets the risk firewall tell an unwind apart from a
    # short open by quantity rather than by mark-vs-reference notional drift.
    symbol_quantity: dict[str, int] = field(default_factory=dict)
    # Mark-to-market change from the persisted start-of-day equity baseline.
    # This is the loss circuit breaker's authoritative intraday P&L input.
    daily_total_pnl: Decimal = Decimal(0)
    # Aggregate marked exposure by country, derived from current open positions.
    country_exposure: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    source: str
    score: Decimal
    confidence: Decimal
    horizon_minutes: int
    rationale: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Opportunity:
    instrument: Instrument
    side: Side
    probability: Decimal
    expected_return: Decimal
    expected_loss: Decimal
    expected_value: Decimal
    confidence: Decimal
    stop_distance: Decimal
    take_profit_distance: Decimal
    signals: tuple[Signal, ...]
