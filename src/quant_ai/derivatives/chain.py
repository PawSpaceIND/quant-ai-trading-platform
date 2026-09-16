"""Point-in-time option-chain snapshot with exact contract identity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.derivatives.options import OptionContract


@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    bid: Decimal
    ask: Decimal
    last: Decimal | None
    volume: int
    open_interest: int
    provider_contract_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.provider_contract_id.strip():
            raise ValueError("option_provider_contract_id_required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("option_quote_time_must_be_timezone_aware")
        if not self.bid.is_finite() or not self.ask.is_finite() or self.bid < 0 or self.ask < self.bid:
            raise ValueError("option_quote_bid_ask_invalid")
        if self.last is not None and (not self.last.is_finite() or self.last < 0):
            raise ValueError("option_quote_last_invalid")
        if type(self.volume) is not int or type(self.open_interest) is not int:
            raise TypeError("option_quote_volume_oi_must_be_integer")
        if self.volume < 0 or self.open_interest < 0:
            raise ValueError("option_quote_volume_oi_must_be_nonnegative")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying: str
    spot: Decimal
    observed_at: datetime
    source: str
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        if not self.underlying.strip() or not self.source.strip() or not self.quotes:
            raise ValueError("option_chain_identity_source_quotes_required")
        if not self.spot.is_finite() or self.spot <= 0:
            raise ValueError("option_chain_spot_must_be_positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("option_chain_time_must_be_timezone_aware")
        identities: set[tuple] = set()
        provider_ids: set[str] = set()
        chain_time = self.observed_at.astimezone(timezone.utc)
        for quote in self.quotes:
            contract = quote.contract
            if contract.underlying.strip().upper() != self.underlying.strip().upper():
                raise ValueError("option_chain_underlying_mismatch")
            if quote.observed_at.astimezone(timezone.utc) > chain_time:
                raise ValueError("option_quote_newer_than_chain_snapshot")
            identity = (
                contract.exchange, contract.symbol, contract.expiry,
                contract.option_type.value, str(contract.strike), str(contract.multiplier),
            )
            if identity in identities:
                raise ValueError("duplicate_option_contract_identity")
            if quote.provider_contract_id in provider_ids:
                raise ValueError("duplicate_option_provider_contract_id")
            identities.add(identity)
            provider_ids.add(quote.provider_contract_id)

    def assert_fresh(self, now: datetime, max_age_seconds: int) -> None:
        if now.tzinfo is None or now.utcoffset() is None or max_age_seconds <= 0:
            raise ValueError("option_chain_freshness_inputs_invalid")
        age = (now.astimezone(timezone.utc) - self.observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            raise ValueError("option_chain_from_future")
        if age > max_age_seconds:
            raise ValueError("option_chain_stale")
