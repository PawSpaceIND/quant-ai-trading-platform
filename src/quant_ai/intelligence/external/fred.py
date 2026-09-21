from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import ClassVar

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import ResilientHttpClient

LOGGER = logging.getLogger("quant_ai.fred_macro")

# The failures a fetch can hold a last good snapshot through: the transport, the guard
# (timeouts, an open circuit, an HTTP or payload error) and FRED's own observation checks
# below. The composite treats the same set as a part failure, so nothing escapes it here
# that the composite would not already log and drop.
FRED_FAILURES: tuple[type[BaseException], ...] = (
    TimeoutError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


class FredUnavailable(RuntimeError):
    """Raised while a failure is being held and nothing good is young enough to serve."""


class FredMacroProvider:
    """Daily FRED series behind the core macro indicators, held between fetches.

    FRED is asked once per instrument per cycle (twelve times every ten minutes on the pilot
    host) for series that change once a day, and one request that outruns the client's
    five-second bound fails the whole fetch: on 21 September 2026 the host saw three timeouts
    and three open-circuit refusals in two hours, each blanking the core indicators for that
    decision. A successful snapshot is therefore held for ``cache_ttl`` and served without a
    request; a failure is held for ``abstain_ttl`` so a slow source is probed once, not on
    every instrument; and while the last good snapshot is younger than ``stale_grace`` it is
    served through a failure, carrying its own observation stamps, so a slow request never
    takes a still-current daily reading away. A failure with nothing young enough to hold is
    raised as before, and the composite logs it. Nothing here changes what a snapshot says
    about its age: the pipeline's freshness reads the observation dates, not the fetch time.
    """

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

    def __init__(
        self,
        client: ResilientHttpClient,
        api_key: str,
        *,
        cache_ttl: timedelta = timedelta(minutes=15),
        abstain_ttl: timedelta = timedelta(minutes=2),
        stale_grace: timedelta = timedelta(hours=6),
    ) -> None:
        if not api_key.strip():
            raise ValueError("fred_api_key_required")
        if min(cache_ttl, abstain_ttl, stale_grace) <= timedelta(0):
            raise ValueError("fred_ttls_must_be_positive")
        if stale_grace < cache_ttl:
            raise ValueError("fred_stale_grace_must_cover_cache_ttl")
        self.client = client
        self.api_key = api_key
        self.cache_ttl = cache_ttl
        self.abstain_ttl = abstain_ttl
        self.stale_grace = stale_grace
        # Last good snapshot and its fetch time, per set of indicators asked.
        self._held: dict[tuple[str, ...], tuple[MacroSnapshot, datetime]] = {}
        self._failed_at: dict[tuple[str, ...], datetime] = {}

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("fred_clock_must_be_aware")
        now = now.astimezone(timezone.utc)
        key = tuple(sorted(set(indicators)))
        held = self._held.get(key)
        if held is not None and timedelta(0) <= now - held[1] < self.cache_ttl:
            return held[0]
        failed_at = self._failed_at.get(key)
        if failed_at is not None and timedelta(0) <= now - failed_at < self.abstain_ttl:
            return self._hold_through(key, held, now, FredUnavailable("fred_backing_off"))
        try:
            snapshot = self._fetch_uncached(indicators, now)
        except FRED_FAILURES as exc:
            self._failed_at[key] = now
            return self._hold_through(key, held, now, exc)
        self._failed_at.pop(key, None)
        self._held[key] = (snapshot, now)
        return snapshot

    def _hold_through(
        self,
        key: tuple[str, ...],
        held: tuple[MacroSnapshot, datetime] | None,
        now: datetime,
        failure: BaseException,
    ) -> MacroSnapshot:
        """Serve the last good snapshot while it is inside the grace; otherwise re-raise."""
        if held is not None and timedelta(0) <= now - held[1] < self.stale_grace:
            LOGGER.warning(
                "fred_unavailable error=%s held_age_seconds=%d",
                type(failure).__name__,
                int((now - held[1]).total_seconds()),
            )
            return held[0]
        self._held.pop(key, None)
        raise failure

    def _fetch_uncached(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
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
