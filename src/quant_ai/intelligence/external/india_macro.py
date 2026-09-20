"""India-specific macro context: India VIX from Yahoo and NSE's daily FII/DII flows.

Two small read-only providers and the composite that lets them sit beside FRED under the
one MACRO category. The failover registry treats the providers of a category as
alternatives, so a second macro adapter registered next to FRED would never be read; the
composite is the single registered adapter and merges its parts.

Each part serves a declared set of indicator names and fails closed on its own: a part that
cannot answer is logged and left out, and only when every part that was asked fails does
the composite raise, which the registry turns into an empty snapshot exactly as a FRED
failure always was. The core set (``MACRO_CORE_INDICATORS``) still decides whether macro
counts as present; the India series are context on top of it, so a specialist reading them
sees "not observed" as an absent metric, never as a zero.

Both parts hold one answer in memory: the sources move once a day (flows) or once a bar
(VIX), and the pipeline asks once per instrument per cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import ProviderHttpError, ResilientHttpClient

LOGGER = logging.getLogger("quant_ai.india_macro")
NSE_ZONE = ZoneInfo("Asia/Kolkata")
USER_AGENT = "quant-ai-readonly/1.0"

INDIA_VIX = "INDIA_VIX"
INDIA_VIX_PREV_CLOSE = "INDIA_VIX_PREV_CLOSE"
FII_NET_CRORE = "FII_NET_CRORE"
DII_NET_CRORE = "DII_NET_CRORE"

# What one part can do on its own without saying anything about the others: the guarded
# client's transport, HTTP, size and circuit errors, and a payload that does not parse.
PART_FAILURES: tuple[type[BaseException], ...] = (
    TimeoutError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


class MacroPartsUnavailable(RuntimeError):
    """Every part the request reached failed; nothing stood in for any of them."""


def _aware_utc(now: datetime, label: str) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(f"{label}_clock_must_be_aware")
    return now.astimezone(timezone.utc)


def _money(value: object, label: str) -> Decimal:
    """A finite two-decimal number from a JSON number or a string, commas tolerated."""
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{label}_not_a_number") from None
    if not number.is_finite():
        raise ValueError(f"{label}_not_finite")
    return number.quantize(Decimal("0.01"))


class _CachedPart:
    """One answer held for ``cache_ttl`` after a success and ``abstain_ttl`` after a failure.

    A failure is re-raised once, so the composite logs it, then held as an abstention so the
    source is not asked again every ten seconds while it is down.
    """

    provider_id: str
    serves: tuple[str, ...]

    def __init__(
        self,
        client: ResilientHttpClient,
        *,
        cache_ttl: timedelta,
        abstain_ttl: timedelta,
    ) -> None:
        if cache_ttl <= timedelta(0) or abstain_ttl <= timedelta(0):
            raise ValueError("cache_ttl and abstain_ttl must be positive")
        self.client = client
        self.cache_ttl = cache_ttl
        self.abstain_ttl = abstain_ttl
        self._cache: tuple[MacroSnapshot, datetime] | None = None

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        now = _aware_utc(now, self.provider_id.replace("-", "_"))
        wanted = tuple(name for name in indicators if name in self.serves)
        if not wanted:
            return MacroSnapshot({}, now)
        if self._cache is not None:
            snapshot, fetched_at = self._cache
            ttl = self.cache_ttl if snapshot.indicators else self.abstain_ttl
            if timedelta(0) <= now - fetched_at < ttl:
                return self._select(snapshot, wanted, now)
        try:
            snapshot = self._fetch_uncached(now)
        except PART_FAILURES:
            self._cache = (MacroSnapshot({}, now), now)
            raise
        self._cache = (snapshot, now)
        return self._select(snapshot, wanted, now)

    def _fetch_uncached(self, now: datetime) -> MacroSnapshot:
        raise NotImplementedError

    @staticmethod
    def _select(snapshot: MacroSnapshot, wanted: tuple[str, ...], now: datetime) -> MacroSnapshot:
        chosen = {name: snapshot.indicators[name] for name in wanted if name in snapshot.indicators}
        if not chosen:
            return MacroSnapshot({}, now)
        return MacroSnapshot(chosen, snapshot.observed_at, snapshot.oldest_observed_at)


class YahooIndiaVixProvider(_CachedPart):
    """India VIX and its previous close from Yahoo's ``^INDIAVIX`` daily bars.

    The last bar is the current session while NSE is open (Yahoo's daily bar carries the
    running value, delayed by its usual few minutes) and the last close otherwise; the bar
    before it is the previous close. The observation clock is the last trade time Yahoo
    reports, falling back to the bar's own stamp, so a Friday reading is Friday's close
    and never the bar's 09:15 opening stamp.
    """

    provider_id = "yahoo-india-vix"
    serves = (INDIA_VIX, INDIA_VIX_PREV_CLOSE)
    endpoint = "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX"

    def __init__(
        self,
        client: ResilientHttpClient,
        *,
        cache_ttl: timedelta = timedelta(minutes=5),
        abstain_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        super().__init__(client, cache_ttl=cache_ttl, abstain_ttl=abstain_ttl)

    def _fetch_uncached(self, now: datetime) -> MacroSnapshot:
        payload = self.client.get_json(
            self.endpoint,
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": USER_AGENT},
        )
        result = self._result(payload)
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        bars = [
            (int(stamp), close)
            for stamp, close in zip(timestamps, closes)
            if isinstance(stamp, (int, float)) and isinstance(close, (int, float))
        ]
        if len(bars) < 2:
            raise ValueError("india_vix_insufficient_bars")
        (_, previous_close), (last_stamp, last_close) = bars[-2], bars[-1]
        level = _money(last_close, "india_vix")
        previous = _money(previous_close, "india_vix_prev_close")
        if level <= 0 or previous <= 0:
            raise ValueError("india_vix_nonpositive")
        traded = (result.get("meta") or {}).get("regularMarketTime")
        observed_stamp = max(last_stamp, int(traded)) if isinstance(traded, (int, float)) else last_stamp
        observed = datetime.fromtimestamp(observed_stamp, tz=timezone.utc)
        if observed > now:
            raise ValueError("india_vix_future_observation")
        return MacroSnapshot({INDIA_VIX: level, INDIA_VIX_PREV_CLOSE: previous}, observed)

    @staticmethod
    def _result(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise TypeError("invalid_yahoo_payload")
        chart = payload.get("chart")
        if not isinstance(chart, dict) or chart.get("error"):
            raise ValueError("yahoo_chart_error")
        results = chart.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ValueError("yahoo_chart_empty")
        return results[0]


class NseInstitutionalFlowsProvider(_CachedPart):
    """NSE's provisional daily FII/FPI and DII net cash-market purchases, in rupees crore.

    The endpoint answers the platform's own identity but only with the session cookies the
    site sets, so the first fetch (and any 401/403 after it) loads the site root once and
    carries every cookie it set on the API call. Figures are the latest day NSE has
    published, stamped at that day's 15:30 IST close; NSE posts them in the evening, so
    during a session the row is the previous trading day's.
    """

    provider_id = "nse-fii-dii"
    serves = (FII_NET_CRORE, DII_NET_CRORE)
    cookie_url = "https://www.nseindia.com/"
    endpoint = "https://www.nseindia.com/api/fiidiiTradeReact"
    referer = "https://www.nseindia.com/reports/fii-dii"

    def __init__(
        self,
        client: ResilientHttpClient,
        *,
        cache_ttl: timedelta = timedelta(minutes=30),
        abstain_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        super().__init__(client, cache_ttl=cache_ttl, abstain_ttl=abstain_ttl)
        self._cookie: str | None = None

    def _fetch_uncached(self, now: datetime) -> MacroSnapshot:
        cookie = self._cookie or self._authenticate()
        try:
            response = self.client.get_response(self.endpoint, headers=self._headers(cookie))
        except ProviderHttpError as error:
            if error.status_code not in {401, 403}:
                raise
            response = self.client.get_response(
                self.endpoint, headers=self._headers(self._authenticate())
            )
        try:
            rows = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("nse_flows_payload_not_json") from None
        if not isinstance(rows, list):
            raise TypeError("nse_flows_payload_shape")
        indicators: dict[str, Decimal] = {}
        days: list[datetime] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            category = str(row.get("category", "")).strip().upper()
            if category.startswith("FII"):
                name = FII_NET_CRORE
            elif category.startswith("DII"):
                name = DII_NET_CRORE
            else:
                continue
            indicators[name] = _money(row.get("netValue"), name.lower())
            days.append(self._close_of(row.get("date")))
        if not indicators:
            raise ValueError("nse_flows_no_rows")
        observed, oldest = max(days), min(days)
        if observed > now:
            raise ValueError("nse_flows_future_observation")
        return MacroSnapshot(indicators, observed, None if oldest == observed else oldest)

    def _headers(self, cookie: str) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.referer,
            "Cookie": cookie,
        }

    def _authenticate(self) -> str:
        response = self.client.get_response(
            self.cookie_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        pairs = [item.split(";", 1)[0].strip() for item in response.set_cookies]
        pairs = [pair for pair in pairs if "=" in pair]
        if not pairs:
            raise ValueError("nse_session_cookie_missing")
        self._cookie = "; ".join(pairs)
        return self._cookie

    @staticmethod
    def _close_of(raw: object) -> datetime:
        """NSE writes ``18-Sep-2026``; the figure belongs to that day's 15:30 IST close."""
        if not isinstance(raw, str):
            raise TypeError("nse_flows_date_missing")
        parts = raw.strip().split("-")
        try:
            day, month, year = int(parts[0]), _MONTHS[parts[1].lower()], int(parts[2])
            return datetime(year, month, day, 15, 30, tzinfo=NSE_ZONE).astimezone(timezone.utc)
        except (IndexError, KeyError, ValueError):
            raise ValueError("nse_flows_date_invalid") from None


class CompositeMacroProvider:
    """The one MACRO adapter: FRED's core series and the India parts, merged fail-closed."""

    provider_id = "composite-macro"

    def __init__(self, parts: tuple[object, ...]) -> None:
        if not parts:
            raise ValueError("composite macro provider needs at least one part")
        self.parts = tuple(parts)

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        now = _aware_utc(now, "composite_macro")
        merged: dict[str, Decimal] = {}
        latest: datetime | None = None
        oldest: datetime | None = None
        asked: list[str] = []
        failed: list[str] = []
        for part in self.parts:
            serves = getattr(part, "serves", None)
            # A part without a declared set (FRED) is given every name and skips the ones it
            # does not map; a part with one is asked only for its own.
            wanted = indicators if serves is None else tuple(n for n in indicators if n in serves)
            if not wanted:
                continue
            label = str(getattr(part, "provider_id", type(part).__name__))
            asked.append(label)
            try:
                snapshot = part.fetch(wanted, now)
            except PART_FAILURES as exc:
                failed.append(label)
                LOGGER.warning("macro_part_unavailable provider=%s error=%s", label, type(exc).__name__)
                continue
            if not snapshot.indicators:
                continue
            merged.update(snapshot.indicators)
            latest = snapshot.observed_at if latest is None else max(latest, snapshot.observed_at)
            stamp = snapshot.freshness_observed_at
            oldest = stamp if oldest is None else min(oldest, stamp)
        if asked and len(failed) == len(asked):
            raise MacroPartsUnavailable("macro_parts_unavailable:" + ";".join(failed))
        if not merged or latest is None:
            return MacroSnapshot({}, now)
        return MacroSnapshot(merged, latest, None if oldest == latest else oldest)
