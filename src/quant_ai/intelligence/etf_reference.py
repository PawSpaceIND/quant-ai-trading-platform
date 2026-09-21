"""Read-only, point-in-time ETF indicative-value observations supplied by an operator.

This is not a quote provider or an arbitrage strategy. A source-labelled iNAV may
explain a premium/discount, but it is neither an executable price nor a forecast.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quant_ai.domain.models import AssetClass, Instrument
from quant_ai.llm.provenance import content_hash
from quant_ai.marketdata.ticker_stream import LiveTick


class ETFReferenceReader:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None

    def read(self, instrument: Instrument, now: datetime, tick: LiveTick | None):
        def empty(code):
            return {"etf_reference_status": code}

        if instrument.asset_class is not AssetClass.ETF:
            return {}
        if self.path is None:
            return empty("etf_reference_unconfigured")
        try:
            with self.path.open("rb") as source:
                raw = source.read(1_000_001)
            if len(raw) > 1_000_000:
                return empty("etf_reference_invalid")
            doc = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(doc, dict) or set(doc) != {"schema", "observations"}:
                return empty("etf_reference_invalid")
            rows = doc["observations"]
            if doc["schema"] != "pramana.etf_inav.v1" or not isinstance(rows, list) or len(rows) > 500:
                return empty("etf_reference_invalid")
            identity = (instrument.symbol, instrument.market.value, instrument.exchange, instrument.currency)
            matching = [row for row in rows if isinstance(row, dict) and
                        tuple(row.get(k) for k in ("symbol", "market", "exchange", "currency")) == identity]
            if not matching:
                return empty("etf_reference_missing")
            if len(matching) != 1:
                return empty("etf_reference_invalid")
            row = matching[0]
            fields = {"symbol", "market", "exchange", "currency", "kind", "value", "observed_at",
                      "received_at", "source"}
            if set(row) != fields or row["kind"] != "indicative_nav":
                return empty("etf_reference_invalid")
            if not isinstance(row["source"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", row["source"]):
                return empty("etf_reference_invalid")
            if not isinstance(row["value"], str) or len(row["value"]) > 32:
                return empty("etf_reference_invalid")
            value = Decimal(row["value"])
            if not value.is_finite() or not Decimal("0.000001") <= value <= Decimal(1000000000):
                return empty("etf_reference_invalid")
            observed, received = (_time(row[key]) for key in ("observed_at", "received_at"))
            if now.tzinfo is None or not observed <= received <= now:
                return empty("etf_reference_invalid")
            if (now - observed).total_seconds() > 60:
                return empty("etf_reference_stale")
            if (tick is None or tick.symbol != instrument.symbol or tick.observed_at.tzinfo is None
                    or not 0 <= (now - tick.observed_at).total_seconds() <= 60
                    or not tick.ltp.is_finite() or tick.ltp <= 0):
                return empty("etf_reference_quote_unavailable")
            premium_bps = ((tick.ltp / value - 1) * 10_000).quantize(Decimal("0.01"))
            return {"etf_reference_status": "etf_reference_observed", "etf_inav": value,
                    "etf_quote": tick.ltp, "etf_premium_bps": premium_bps,
                    "etf_reference_source": row["source"], "etf_reference_sha256": content_hash(row),
                    "etf_reference_observed_at": observed.isoformat(),
                    "etf_reference_received_at": received.isoformat(),
                    "etf_quote_observed_at": tick.observed_at.isoformat(),
                    "etf_reference_basis": "operator_supplied_inav_not_executable_or_a_forecast"}
        except FileNotFoundError:
            return empty("etf_reference_missing")
        except (OSError, ValueError, TypeError, KeyError, InvalidOperation, OverflowError, RecursionError):
            return empty("etf_reference_invalid")


def _time(raw):
    if not isinstance(raw, str) or len(raw) > 40:
        raise ValueError("invalid_time")
    result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result
