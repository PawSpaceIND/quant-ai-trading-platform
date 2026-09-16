"""Canonical decision-time identity; no fill-time instrument registry lookup."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from quant_ai.domain.models import AssetClass, Instrument, Market

SCHEMA = "pramana.instrument_identity.v1"
MAX_METADATA_ITEMS = 32
MAX_METADATA_TOKEN = 256
MAX_IDENTITY_BYTES = 32768


class _FrozenMetadata(dict[str, str]):
    """JSON/asdict-compatible defensive copy with no ordinary mutation operations."""

    def _immutable(self, *_args, **_kwargs):
        raise TypeError("bound_instrument_metadata_is_immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self):
        return type(self)(self)

    def __deepcopy__(self, _memo):
        return type(self)(self)


def _text(value: object, field: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_METADATA_TOKEN or any(ord(char) < 32 for char in value)):
        raise ValueError(f"instrument_identity_{field}_invalid")
    return value


def _metadata(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("instrument_identity_metadata_invalid")
    if len(value) > MAX_METADATA_ITEMS:
        raise ValueError("instrument_identity_metadata_too_large")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not isinstance(item, str)
                or not key or len(key) > MAX_METADATA_TOKEN or len(item) > MAX_METADATA_TOKEN
                or any(ord(char) < 32 for char in key + item)):
            raise ValueError("instrument_identity_metadata_invalid")
        result[key] = item
    return dict(sorted(result.items()))


def instrument_identity_payload(instrument: Instrument) -> dict[str, Any]:
    if not isinstance(instrument, Instrument) or instrument.tradable is not True:
        raise ValueError("instrument_identity_requires_tradable_instrument")
    symbol = _text(instrument.symbol, "symbol")
    exchange = _text(instrument.exchange, "exchange")
    currency = _text(instrument.currency, "currency")
    if (len(currency) != 3 or not currency.isascii()
            or not currency.isalpha() or not currency.isupper()):
        raise ValueError("instrument_identity_currency_invalid")
    if not isinstance(instrument.market, Market) or not isinstance(instrument.asset_class, AssetClass):
        raise TypeError("instrument_identity_classification_invalid")
    expiry, lot, tick = instrument.expiry, instrument.lot_size, instrument.tick_size
    if expiry is not None and type(expiry) is not date:
        raise ValueError("instrument_identity_expiry_invalid")
    if lot is not None and (type(lot) is not int or not 0 < lot <= 2**53 - 1):
        raise ValueError("instrument_identity_lot_size_invalid")
    if tick is not None and (not isinstance(tick, Decimal) or not tick.is_finite() or tick <= 0):
        raise ValueError("instrument_identity_tick_size_invalid")
    if instrument.is_dated_contract and (expiry is None or lot is None or tick is None):
        raise ValueError("instrument_identity_dated_contract_fields_required")
    underlying = instrument.underlying
    if underlying is not None:
        _text(underlying, "underlying")
    # Equivalent Decimal ticks have one encoding, without context-dependent rounding.
    tick_text = None if tick is None else str(tick)
    if tick_text is not None:
        if len(tick_text) > MAX_METADATA_TOKEN or abs(tick.adjusted()) > 100:
            raise ValueError("instrument_identity_tick_size_invalid")
        tick_text = format(tick, "f")
        if "." in tick_text:
            tick_text = tick_text.rstrip("0").rstrip(".")
    return {
        "schema": SCHEMA, "symbol": symbol, "market": instrument.market.value,
        "assetClass": instrument.asset_class.value, "currency": currency,
        "exchange": exchange, "expiry": None if expiry is None else expiry.isoformat(),
        "lotSize": lot, "tickSize": tick_text, "underlying": underlying,
        "metadata": _metadata(instrument.metadata),
    }


def canonical_instrument_identity(instrument: Instrument) -> str:
    return json.dumps(instrument_identity_payload(instrument), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def instrument_identity_sha256(instrument: Instrument) -> str:
    return hashlib.sha256(canonical_instrument_identity(instrument).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("instrument_identity_duplicate_key")
        result[key] = value
    return result


def instrument_from_identity(raw: str | dict[str, Any]) -> Instrument:
    try:
        if isinstance(raw, str):
            if len(raw.encode()) > MAX_IDENTITY_BYTES:
                raise ValueError("instrument_identity_too_large")
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        else:
            payload = raw
        if not isinstance(payload, dict):
            raise TypeError("instrument_identity_object_required")
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("instrument_identity_invalid_json") from error
    expected = {"schema", "symbol", "market", "assetClass", "currency", "exchange",
                "expiry", "lotSize", "tickSize", "underlying", "metadata"}
    if set(payload) != expected or payload.get("schema") != SCHEMA:
        raise ValueError("instrument_identity_schema_mismatch")
    expiry_raw, tick_raw = payload["expiry"], payload["tickSize"]
    try:
        expiry = None if expiry_raw is None else date.fromisoformat(_text(expiry_raw, "expiry"))
        tick = None if tick_raw is None else Decimal(_text(tick_raw, "tick_size"))
        instrument = Instrument(
            _text(payload["symbol"], "symbol"), Market(payload["market"]),
            AssetClass(payload["assetClass"]), _text(payload["currency"], "currency"),
            _text(payload["exchange"], "exchange"), True, _metadata(payload["metadata"]),
            expiry, payload["lotSize"], tick, payload["underlying"],
        )
        instrument_identity_payload(instrument)
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("instrument_identity_instrument_invalid") from error
    return replace(instrument, metadata=_FrozenMetadata(instrument.metadata))


def immutable_instrument_snapshot(instrument: Instrument) -> Instrument:
    """Copy and freeze metadata as well as the frozen dataclass's scalar fields."""
    return instrument_from_identity(canonical_instrument_identity(instrument))


def stored_identity(row) -> str | None:
    """Validate an optional historical snapshot against its owning ledger/position row."""
    raw = dict(row).get("instrument_identity")
    if raw is None:
        return None
    instrument = instrument_from_identity(raw)
    if (instrument.symbol, instrument.market.value, instrument.asset_class.value) != (
        row["symbol"], row["market"], row["asset_class"]
    ):
        raise ValueError("stored_instrument_identity_mismatch")
    return canonical_instrument_identity(instrument)
