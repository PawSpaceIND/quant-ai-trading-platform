from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import ResilientHttpClient


class FredMacroProvider:
    provider_id = "fred"
    endpoint = "https://api.stlouisfed.org/fred/series/observations"
    series: ClassVar[dict[str, str]] = {
        "US10Y": "DGS10",
        "INDIA10Y": "IRLTLT01INM156N",
        "BRENT": "DCOILBRENTEU",
        "GOLD": "GOLDAMGBD228NLBM",
        "DXY": "DTWEXBGS",
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
        # The shared snapshot timestamp must not make an old indicator look as fresh
        # as the newest series. This is an observation-date floor, not a receipt,
        # release-time or point-in-time availability assertion.
        return MacroSnapshot(values, min(observed, default=now))
