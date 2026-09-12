from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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
                values[indicator] = Decimal(str(item["value"]))
                observed.append(datetime.fromisoformat(str(item["date"])).replace(tzinfo=timezone.utc))
                break
        return MacroSnapshot(values, max(observed, default=now))
