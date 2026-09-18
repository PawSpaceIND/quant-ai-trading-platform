"""Load a point-in-time universe from a file an operator writes.

`PointInTimeUniverse` can express a universe that remembers its failures. Nothing could
build one from anything except a Python literal, so in practice every study ran on a universe
of whatever instruments happened to have datasets on disk — which is today's survivors.

This is the seam through which real listing history enters. It is deliberately a plain file
rather than a vendor client: the delisting record is the scarce thing, and an operator who has
it from an exchange bhavcopy archive, a broker instrument master or a paid vendor can supply
it here without this repository taking a dependency on where it came from.

The file says what it is and where it came from, because a universe with no stated source is
indistinguishable from one somebody assembled from memory::

    {
      "schema": "pramana.universe_manifest.v1",
      "source": "NSE equity listing history, exchange archive 2015-2025",
      "coverage_from": "2015-01-01",
      "coverage_to": "2025-01-01",
      "listings": [
        {"symbol": "INFY", "market": "INDIA", "listed_on": "2015-01-01"},
        {"symbol": "XYZ", "market": "INDIA", "listed_on": "2015-01-01",
         "delisted_on": "2019-06-01", "delisting_reason": "insolvency"}
      ]
    }
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from quant_ai.marketdata.point_in_time import Listing, PointInTimeUniverse

SCHEMA = "pramana.universe_manifest.v1"


def _day(value: Any, field: str, symbol: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{symbol}: {field} is not an ISO date: {value!r}") from error


def parse_universe_manifest(payload: Any) -> PointInTimeUniverse:
    if not isinstance(payload, dict):
        raise TypeError("universe manifest must be a JSON object")
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"universe manifest schema must be {SCHEMA}")

    source = str(payload.get("source") or "").strip()
    if len(source) < 8:
        raise ValueError(
            "universe manifest must name where its listing history came from; a universe "
            "with no stated source cannot be told from one assembled from memory"
        )

    raw = payload.get("listings")
    if not isinstance(raw, list) or not raw:
        raise ValueError("universe manifest must list at least one instrument")

    listings: list[Listing] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("every listing must be an object")
        symbol = str(item.get("symbol") or "").strip()
        market = str(item.get("market") or "").strip()
        if not symbol or not market:
            raise ValueError("every listing needs a symbol and a market")
        if (symbol, market) in seen:
            raise ValueError(f"{symbol} is listed twice on {market}")
        seen.add((symbol, market))
        delisted = item.get("delisted_on")
        listings.append(Listing(
            symbol=symbol,
            market=market,
            listed_on=_day(item.get("listed_on"), "listed_on", symbol),
            delisted_on=None if delisted is None else _day(delisted, "delisted_on", symbol),
            delisting_reason=str(item.get("delisting_reason") or ""),
        ))

    return PointInTimeUniverse(
        listings,
        source=source,
        coverage_from=_day(payload.get("coverage_from"), "coverage_from", "manifest"),
        coverage_to=_day(payload.get("coverage_to"), "coverage_to", "manifest"),
    )


def load_universe_manifest(path: str | Path) -> PointInTimeUniverse:
    return parse_universe_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
