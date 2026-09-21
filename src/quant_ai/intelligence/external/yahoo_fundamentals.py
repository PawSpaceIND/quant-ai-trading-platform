from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import ClassVar

from quant_ai.domain.models import Market
from quant_ai.intelligence.external.yahoo import yahoo_symbol
from quant_ai.intelligence.providers import FundamentalSnapshot
from quant_ai.intelligence.resilience import ProviderHttpError, ResilientHttpClient

LOGGER = logging.getLogger("quant_ai.yahoo_fundamentals")


def _finite_number(value: object) -> int | float | None:
    """The value when it is a real, finite number (``bool`` excluded), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


class YahooFundamentalsProvider:
    """Read-only Yahoo ``quoteSummary`` adapter for the four valuation-agent ratios.

    Endpoint. ``v10/finance/quoteSummary`` is the only key-less Yahoo endpoint that carries
    margins and cash flow (``v7/finance/quote`` has P/E and market cap only; the ``v8`` chart
    endpoint has neither). It requires a crumb bound to a Yahoo session cookie:
    ``fc.yahoo.com`` answers 404 while setting the cookie, then ``v1/test/getcrumb`` returns
    the crumb for that cookie. Both are fetched lazily on the first ``fetch`` (constructing
    the provider performs no I/O) and refreshed once when Yahoo rejects the crumb (401/403).

    Honesty. Yahoo carries the four ratios unevenly: most NSE listings publish no
    ``freeCashflow``, a loss-making company has no trailing P/E, and a metal ETF has none of
    them. The provider therefore returns every ratio the payload supports and leaves the
    rest out; the valuation agents score only the ratios present, name the missing ones in
    their rationale, scale confidence by coverage and abstain below two. A payload with no
    ratio at all, an HTTP failure or an envelope error abstains with an empty snapshot,
    which the pipeline treats as MISSING, and logs one WARNING naming every field behind
    it; a partial snapshot logs the missing fields at INFO. Nothing is ever estimated:
    ``fcf_yield`` needs both ``freeCashflow`` and a positive ``marketCap``.

    Units. Yahoo reports ``debtToEquity`` as a percentage (``89.0`` means 0.89x) and
    ``operatingMargins`` as a fraction; ``fcf_yield`` is ``freeCashflow / marketCap``. Every
    value is ``Decimal(str(raw))``; there is no float arithmetic.

    Symbols. The pipeline hands providers a bare symbol, but Yahoo needs the listing (``INFY``
    on NYSE versus ``INFY.NS`` on NSE), so the constructor takes a symbol-to-market mapping;
    the daemon builds it from the founder watchlist or the target instrument. A subject
    outside that mapping abstains without a request rather than guessing a venue.

    Timestamps. ``observed_at`` is ``price.regularMarketTime``, the as-of time of the price
    behind trailing P/E and market cap, falling back to ``now`` when Yahoo omits it. Over a
    weekend the pipeline's 24-hour fundamental TTL therefore marks the snapshot STALE and
    haircuts valuation confidence; that reflects the data's age, not a defect.

    Cache. Fundamentals move slowly and the cadence is ten minutes, so each symbol's snapshot
    is held in memory for ``cache_ttl`` (default six hours). Abstentions are held for the
    shorter ``abstain_ttl`` (default thirty minutes) so an outage or a missing field neither
    hammers Yahoo every tick nor hides a recovery for six hours. The cache is per process; a
    restart refetches.
    """

    provider_id = "yahoo-fundamentals"
    quote_summary_url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary"
    cookie_url = "https://fc.yahoo.com/"
    crumb_url = "https://query2.finance.yahoo.com/v1/test/getcrumb"
    modules: ClassVar[tuple[str, ...]] = (
        "summaryDetail",
        "financialData",
        "defaultKeyStatistics",
        "price",
    )
    # Yahoo's edge rejects non-browser agents on the crumb endpoints; "compatible" keeps the
    # client identifiable while passing that check.
    user_agent = "Mozilla/5.0 (compatible; quant-ai-readonly/1.0)"

    def __init__(
        self,
        client: ResilientHttpClient,
        markets: Mapping[str, Market],
        *,
        cache_ttl: timedelta = timedelta(hours=6),
        abstain_ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        if cache_ttl <= timedelta(0) or abstain_ttl <= timedelta(0):
            raise ValueError("cache_ttl and abstain_ttl must be positive")
        self.client = client
        self.markets = {symbol.upper(): market for symbol, market in markets.items()}
        self.cache_ttl = cache_ttl
        self.abstain_ttl = abstain_ttl
        self._cache: dict[str, tuple[FundamentalSnapshot, datetime]] = {}
        self._session: tuple[str, str] | None = None

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot:
        key = subject.upper()
        cached = self._cache.get(key)
        if cached is not None:
            snapshot, fetched_at = cached
            ttl = self.cache_ttl if snapshot.metrics else self.abstain_ttl
            if timedelta(0) <= now - fetched_at < ttl:
                return snapshot
        market = self.markets.get(key)
        if market is None:
            LOGGER.warning(
                "yahoo fundamentals abstain: subject=%s reason=no_market_mapping", subject
            )
            snapshot = FundamentalSnapshot(subject, {}, now)
        else:
            snapshot = self._fetch_uncached(subject, yahoo_symbol(key, market), now)
        self._cache[key] = (snapshot, now)
        return snapshot

    def _fetch_uncached(self, subject: str, symbol: str, now: datetime) -> FundamentalSnapshot:
        try:
            result = self._result(self._quote_summary(symbol))
        except (TimeoutError, OSError, RuntimeError, ValueError, TypeError) as exc:
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            LOGGER.warning("yahoo fundamentals abstain: symbol=%s reason=%s", symbol, detail)
            return FundamentalSnapshot(subject, {}, now)
        metrics, missing = self._metrics(result)
        if not metrics:
            LOGGER.warning(
                "yahoo fundamentals abstain: symbol=%s reason=no_ratio_available: %s",
                symbol,
                ", ".join(missing),
            )
            return FundamentalSnapshot(subject, {}, now)
        if missing:
            LOGGER.info(
                "yahoo fundamentals partial: symbol=%s ratios=%s missing=%s",
                symbol,
                ",".join(metrics),
                ",".join(missing),
            )
        return FundamentalSnapshot(subject, metrics, self._observed_at(result, now))

    def _quote_summary(self, symbol: str) -> object:
        cookie, crumb = self._session or self._authenticate()
        try:
            return self._get_quote_summary(symbol, cookie, crumb)
        except ProviderHttpError as exc:
            if exc.status_code not in (401, 403):
                raise
            # Yahoo rotates crumbs with the cookie: re-authenticate once, then let it abstain.
            self._session = None
            cookie, crumb = self._authenticate()
            return self._get_quote_summary(symbol, cookie, crumb)

    def _get_quote_summary(self, symbol: str, cookie: str, crumb: str) -> object:
        return self.client.get_json(
            f"{self.quote_summary_url}/{symbol}",
            params={"modules": ",".join(self.modules), "crumb": crumb, "formatted": "false"},
            headers={"User-Agent": self.user_agent, "Cookie": cookie},
        )

    def _authenticate(self) -> tuple[str, str]:
        response = self.client.get_response(
            self.cookie_url,
            headers={"User-Agent": self.user_agent},
            accept_statuses=frozenset({404}),
        )
        cookie = response.headers.get("set-cookie", "").split(";", 1)[0].strip()
        if "=" not in cookie:
            raise ValueError("yahoo_session_cookie_missing")
        crumb = self.client.get_text(
            self.crumb_url, headers={"User-Agent": self.user_agent, "Cookie": cookie}
        ).strip()
        if not crumb or "<" in crumb:
            raise ValueError("yahoo_crumb_missing")
        self._session = (cookie, crumb)
        return self._session

    @staticmethod
    def _result(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise TypeError("invalid_yahoo_payload")
        summary = payload.get("quoteSummary")
        if not isinstance(summary, dict):
            raise TypeError("invalid_yahoo_payload")
        error = summary.get("error")
        if error:
            if isinstance(error, dict):
                error = error.get("description") or error.get("code")
            raise ValueError(f"yahoo_error: {error}")
        results = summary.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ValueError("invalid_yahoo_payload")
        return results[0]

    @classmethod
    def _metrics(cls, result: Mapping[str, object]) -> tuple[dict[str, Decimal], tuple[str, ...]]:
        """Every ratio the payload supports, and the ``module.field`` names behind the rest.

        A ratio is present only when every field it needs is numeric; nothing is filled in.
        The returned order is the canonical one (``pe``, ``debt_equity``,
        ``operating_margin``, ``fcf_yield``) so logs and proofs read the same way each time.
        """
        missing: list[str] = []

        def field(module: str, name: str) -> Decimal | None:
            value = cls._number(result, module, name)
            if value is None:
                missing.append(f"{module}.{name}")
            return value

        metrics: dict[str, Decimal] = {}
        pe = field("summaryDetail", "trailingPE")
        if pe is not None:
            metrics["pe"] = pe
        debt_equity = field("financialData", "debtToEquity")
        if debt_equity is not None:
            metrics["debt_equity"] = debt_equity / Decimal(100)
        operating_margin = field("financialData", "operatingMargins")
        if operating_margin is not None:
            metrics["operating_margin"] = operating_margin
        free_cashflow = field("financialData", "freeCashflow")
        market_cap = field("summaryDetail", "marketCap")
        if market_cap is not None and market_cap <= 0:
            missing.append("summaryDetail.marketCap:non_positive")
            market_cap = None
        if free_cashflow is not None and market_cap is not None:
            metrics["fcf_yield"] = free_cashflow / market_cap
        return metrics, tuple(missing)

    @staticmethod
    def _number(result: Mapping[str, object], module: str, field: str) -> Decimal | None:
        """The field as a Decimal, or None when the module or field is absent or not numeric."""
        section = result.get(module)
        if not isinstance(section, dict):
            return None
        value = section.get(field)
        if isinstance(value, dict):
            # ``formatted=true`` shape: {"raw": 24.5, "fmt": "24.50"}; absent fields are {}.
            value = value.get("raw")
        number = _finite_number(value)
        if number is None:
            return None
        return Decimal(str(number))

    @staticmethod
    def _observed_at(result: Mapping[str, object], now: datetime) -> datetime:
        price = result.get("price")
        value = price.get("regularMarketTime") if isinstance(price, dict) else None
        if isinstance(value, dict):
            value = value.get("raw")
        seconds = _finite_number(value)
        if seconds is None:
            return now
        try:
            observed = datetime.fromtimestamp(int(seconds), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return now
        return min(observed, now)
