"""Founder directives: the one place the founder tells the machine what it may do.

Everything the autonomous runtime needs from a human is expressed here - how
much capital it manages, the risk posture, which markets and asset classes are
in scope, the watchlist it evaluates every cadence tick, how many positions it
may hold at once, and free-text instructions that are handed to the Atlas
consensus prompt and recorded on every XAI proof. The risk firewall still has
the final word: directives can narrow what the swarm may do, never widen the
baseline limits.

Supplied as inline JSON (``PRAMANA_FOUNDER_DIRECTIVES_JSON``) or a file
(``PRAMANA_FOUNDER_DIRECTIVES_FILE``)::

    {
      "starting_capital": 100000,
      "risk_mode": "BALANCED",
      "allowed_markets": ["INDIA", "USA"],
      "allowed_asset_classes": ["EQUITY", "INDEX", "METAL", "FX", "COMMODITY"],
      "max_open_positions": 5,
      "sector_map": {"TCS": "IT_SERVICES", "INFY": "IT_SERVICES"},
      "watchlist": [
        {"symbol": "NIFTY", "market": "INDIA", "asset_class": "INDEX", "currency": "INR", "exchange": "NSE"},
        {"symbol": "GOLD", "market": "INDIA", "asset_class": "METAL", "currency": "INR", "exchange": "MCX"},
        {"symbol": "USDINR", "market": "INDIA", "asset_class": "FX", "currency": "INR", "exchange": "CDS"},
        {"symbol": "AAPL", "market": "USA", "asset_class": "EQUITY", "currency": "USD", "exchange": "NASDAQ"}
      ],
      "instructions": "Preserve capital first. Prefer liquid, large instruments."
    }

``sector_map`` is the operator's symbol-to-group mapping for the warden's group
concentration limit. There is no security master here and no defensible way to
infer one, so an absent mapping means no grouping rather than a guessed one. It
can also be supplied through ``PRAMANA_SECTOR_MAP_JSON`` / ``PRAMANA_SECTOR_MAP_FILE``
(see ``quant_ai.risk.book_history``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.planning.capital import CapitalPlanRequest
from quant_ai.risk.book_history import normalize_sector_map

DIRECTIVES_JSON_ENV = "PRAMANA_FOUNDER_DIRECTIVES_JSON"
DIRECTIVES_FILE_ENV = "PRAMANA_FOUNDER_DIRECTIVES_FILE"
MAX_INSTRUCTION_CHARS = 2000

COUNTRY_BY_MARKET = {Market.INDIA: "India", Market.USA: "USA", Market.GLOBAL: "Global"}


def country_for(instrument: Instrument) -> str:
    """Country used by the warden's allocation cap; explicit metadata wins."""
    return instrument.metadata.get("country") or COUNTRY_BY_MARKET[instrument.market]


@dataclass(frozen=True)
class FounderDirectives:
    starting_capital: Decimal = Decimal(100000)
    risk_mode: RiskMode | None = RiskMode.BALANCED
    confidence: Decimal = Decimal("0.80")
    annualized_volatility: Decimal = Decimal("0.20")
    expected_edge: Decimal = Decimal("0.02")
    allowed_markets: frozenset[Market] = frozenset({Market.INDIA, Market.USA})
    allowed_asset_classes: frozenset[AssetClass] = frozenset(AssetClass)
    max_open_positions: int = 5
    watchlist: tuple[Instrument, ...] = field(default_factory=tuple)
    instructions: str = ""
    # Operator-supplied symbol-to-group mapping for the warden's group limit.
    # Empty means ungrouped; nothing is inferred from a symbol or an exchange.
    sector_map: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be at least one")
        if not self.allowed_markets or not self.allowed_asset_classes:
            raise ValueError("at least one market and one asset class must be allowed")
        if len(self.instructions) > MAX_INSTRUCTION_CHARS:
            raise ValueError(f"instructions exceed {MAX_INSTRUCTION_CHARS} characters")
        seen: set[str] = set()
        for instrument in self.watchlist:
            if instrument.market not in self.allowed_markets:
                raise ValueError(f"{instrument.symbol}: market {instrument.market.value} is not allowed")
            if instrument.asset_class not in self.allowed_asset_classes:
                raise ValueError(
                    f"{instrument.symbol}: asset class {instrument.asset_class.value} is not allowed"
                )
            if instrument.symbol in seen:
                # Tick buffers, exposure and stop controls are keyed by symbol.
                # Keeping one symbol per pilot watchlist avoids cross-venue collisions.
                raise ValueError(f"{instrument.symbol}: duplicate watchlist symbol")
            seen.add(instrument.symbol)

    def capital_plan_request(self) -> CapitalPlanRequest:
        return CapitalPlanRequest(
            self.starting_capital,
            self.confidence,
            self.annualized_volatility,
            expected_edge=self.expected_edge,
            requested_mode=self.risk_mode,
        )

    def blocked_asset_classes(self) -> tuple[AssetClass, ...]:
        return tuple(item for item in AssetClass if item not in self.allowed_asset_classes)

    def instruments_or(self, fallback: Instrument) -> tuple[Instrument, ...]:
        return self.watchlist or (fallback,)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> FounderDirectives:
        def decimal(key: str, default: Decimal) -> Decimal:
            return Decimal(str(payload[key])) if key in payload else default

        mode_raw = payload.get("risk_mode", RiskMode.BALANCED.value)
        risk_mode = RiskMode(str(mode_raw).upper()) if mode_raw is not None else None
        markets = frozenset(Market(str(item).upper()) for item in payload.get("allowed_markets", ("INDIA", "USA")))
        classes = frozenset(
            AssetClass(str(item).upper())
            for item in payload.get("allowed_asset_classes", [item.value for item in AssetClass])
        )
        watchlist = tuple(_instrument_from_json(item) for item in payload.get("watchlist", ()))
        return cls(
            starting_capital=decimal("starting_capital", Decimal(100000)),
            risk_mode=risk_mode,
            confidence=decimal("confidence", Decimal("0.80")),
            annualized_volatility=decimal("annualized_volatility", Decimal("0.20")),
            expected_edge=decimal("expected_edge", Decimal("0.02")),
            allowed_markets=markets,
            allowed_asset_classes=classes,
            max_open_positions=int(payload.get("max_open_positions", 5)),
            watchlist=watchlist,
            instructions=str(payload.get("instructions", "")).strip(),
            sector_map=normalize_sector_map(payload.get("sector_map") or {}),
        )

    @classmethod
    def from_env(cls) -> FounderDirectives | None:
        """Directives from the environment, or ``None`` when the founder set none."""
        inline = os.getenv(DIRECTIVES_JSON_ENV, "").strip()
        if inline:
            return cls.from_json(json.loads(inline))
        location = os.getenv(DIRECTIVES_FILE_ENV, "").strip()
        if location:
            return cls.from_json(json.loads(Path(location).expanduser().read_text(encoding="utf-8")))
        return None


def _instrument_from_json(item: dict[str, Any]) -> Instrument:
    metadata = {str(key): str(value) for key, value in dict(item.get("metadata", {})).items()}
    if item.get("country"):
        metadata["country"] = str(item["country"])
    return Instrument(
        str(item["symbol"]).strip().upper(),
        Market(str(item["market"]).upper()),
        AssetClass(str(item["asset_class"]).upper()),
        str(item.get("currency", "USD")).upper(),
        str(item.get("exchange", "")).upper(),
        metadata=metadata,
        # Contract identity, for a watchlist entry that names a dated contract rather than
        # a share. Absent for every cash instrument, and `Instrument` refuses a derivative
        # that leaves them out - so a directives file cannot declare a half-named contract.
        expiry=_contract_date(item.get("expiry")),
        lot_size=_contract_int(item.get("lot_size")),
        tick_size=_contract_decimal(item.get("tick_size")),
        underlying=(str(item["underlying"]).strip().upper() or None) if item.get("underlying") else None,
    )


def _contract_date(value: Any) -> date | None:
    """An ISO expiry, or nothing. A date this cannot read is an error, never a silent None.

    Dropping an unparseable expiry would turn a malformed contract into a cash instrument,
    which `Instrument` would then accept without complaint - the exact silent widening this
    whole change exists to prevent.
    """
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as error:
        raise ValueError(f"watchlist_expiry_not_a_date:{value}") from error


def _contract_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except ValueError as error:
        raise ValueError(f"watchlist_lot_size_not_an_integer:{value}") from error


def _contract_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as error:
        raise ValueError(f"watchlist_tick_size_not_a_number:{value}") from error
