"""Read-only FRED observations adapter for the macro agent's five indicators.

Availability. Each indicator is fetched independently, so a series FRED has retired
costs the macro agent that one reading instead of the whole snapshot: a rejection,
timeout, open circuit or oversized payload abstains on that indicator alone and logs
one WARNING naming the cause. The API key never reaches a log line.

Integrity is deliberately not absorbed. A malformed observation - an unparseable or
non-finite value, a non-canonical or future-dated day - still raises out of ``fetch``
so the failover records a provider failure. Missing data and wrong data are different
answers, and only the first one is safe to shrug off.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import (
    CircuitOpenError,
    PayloadTooLargeError,
    ProviderHttpError,
    ResilientHttpClient,
)

LOGGER = logging.getLogger(__name__)


class FredMacroProvider:
    provider_id = "fred"
    endpoint = "https://api.stlouisfed.org/fred/series/observations"
    series: ClassVar[dict[str, str]] = {
        "US10Y": "DGS10",
        "INDIA10Y": "INDIRLTLT01STM",
        "BRENT": "DCOILBRENTEU",
        "GOLD": "GOLDAMGBD228NLBM",
        "USD_BROAD": "DTWEXBGS",
    }

    def __init__(self, client: ResilientHttpClient, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("fred_api_key_required")
        self.client = client
        self.api_key = api_key

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("fred_clock_must_be_aware")
        now = now.astimezone(timezone.utc)
        values: dict[str, Decimal] = {}
        observed: list[datetime] = []
        for indicator in indicators:
            series_id = self.series.get(indicator)
            if series_id is None:
                continue
            # One unavailable series must not decide the snapshot for the other four:
            # FRED retires series, and a retired id rejects every request alike. Only
            # availability is caught here; a malformed record still refuses upward.
            try:
                latest = self._latest(series_id, now)
            except (
                TimeoutError, OSError, ProviderHttpError, PayloadTooLargeError,
                CircuitOpenError,
            ) as exc:
                LOGGER.warning(
                    "fred macro abstain: indicator=%s series=%s reason=%s",
                    indicator, series_id, self._reason(exc),
                )
                continue
            if latest is None:
                continue
            value, stamp = latest
            values[indicator] = value
            observed.append(stamp)
        # Change/version time and conservative freshness are distinct clocks.
        # Neither observation date authenticates release, receipt or vintage time.
        return MacroSnapshot(values, max(observed, default=now),
                             oldest_observed_at=min(observed, default=now))

    def _reason(self, exc: BaseException) -> str:
        """Name the cause without ever rendering the key a transport may echo back."""
        detail = str(exc).replace(self.api_key, "***")
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

    def _latest(self, series_id: str, now: datetime) -> tuple[Decimal, datetime] | None:
        payload = self.client.get_json(
            self.endpoint,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": "10",
            },
        )
        if not isinstance(payload, dict):
            return None
        observations = payload.get("observations")
        if not isinstance(observations, list):
            return None
        for item in observations:
            if not isinstance(item, dict) or item.get("value") in {None, "."}:
                continue
            try:
                value = Decimal(str(item["value"]))
            except InvalidOperation:
                raise ValueError("fred_invalid_observation_value") from None
            if not value.is_finite():
                raise ValueError("fred_nonfinite_observation")
            try:
                raw_date = item["date"]
                day = date.fromisoformat(raw_date)
                if raw_date != day.isoformat():
                    raise ValueError("noncanonical observation date")
            except (KeyError, TypeError, ValueError):
                raise ValueError("fred_observation_date_invalid") from None
            stamp = datetime.combine(day, time.min, tzinfo=timezone.utc)
            if stamp > now:
                raise ValueError("fred_future_observation")
            return value, stamp
        return None
