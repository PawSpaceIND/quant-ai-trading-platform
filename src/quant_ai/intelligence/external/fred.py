from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import ResilientHttpClient


class FredMacroProvider:
    provider_id = "fred"
    endpoint = "https://api.stlouisfed.org/fred/series/observations"
    # Verified against the live FRED catalog on 20 September 2026 from the pilot host.
    # Two earlier mappings had been retired upstream and answered "Bad Request. The series
    # does not exist": IRLTLT01INM156N (the old OECD id for India's 10-year yield) and
    # GOLDAMGBD228NLBM (the LBMA gold fixing). FRED no longer carries any spot gold price;
    # GOLD reads the daily NASDAQ gold price index instead, which is only ever consumed as
    # a day-over-day fractional change, so its level and base do not matter. INDIA10Y
    # points at the OECD series that replaced the old id; it is monthly and no metric reads
    # it, so it is available on request but not part of MACRO_CORE_INDICATORS, where its
    # age would stale the whole snapshot.
    series: ClassVar[dict[str, str]] = {
        "US10Y": "DGS10",
        "INDIA10Y": "INDIRLTLT01STM",
        "BRENT": "DCOILBRENTEU",
        "GOLD": "NASDAQQGLDI",
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
                continue
            observations = payload.get("observations")
            if not isinstance(observations, list):
                continue
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
                values[indicator] = value
                observed.append(stamp)
                break
        # Change/version time and conservative freshness are distinct clocks.
        # Neither observation date authenticates release, receipt or vintage time.
        return MacroSnapshot(values, max(observed, default=now),
                             oldest_observed_at=min(observed, default=now))
