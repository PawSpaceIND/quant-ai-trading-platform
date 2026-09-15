from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import Instrument, Market
from quant_ai.intelligence.resilience import ResilientHttpClient
from quant_ai.marketdata.feed import MarketDataFeed, MarketTick
from quant_ai.marketdata.models import Candle


def yahoo_symbol(symbol: str, market: Market) -> str:
    """Yahoo ticker for a canonical symbol: NSE listings carry the ``.NS`` suffix."""
    if market == Market.INDIA and not symbol.endswith(".NS"):
        return f"{symbol}.NS"
    return symbol


class YahooFinanceMarketDataAdapter(MarketDataFeed):
    """Read-only Yahoo chart adapter with canonical Candle/MarketTick normalization."""

    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    @staticmethod
    def _provider_symbol(instrument: Instrument) -> str:
        return yahoo_symbol(instrument.symbol, instrument.market)

    def fetch_ohlcv(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str = "1m",
    ) -> tuple[Candle, ...]:
        if end <= start:
            raise ValueError("end must be after start")
        payload = self.client.get_json(
            f"{self.base_url}/{self._provider_symbol(instrument)}",
            params={
                "period1": str(int(start.timestamp())),
                "period2": str(int(end.timestamp())),
                "interval": timeframe,
                "events": "history",
            },
        )
        result = self._result(payload)
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        candles: list[Candle] = []
        for index, raw_ts in enumerate(timestamps):
            values = [quote.get(name, []) for name in ("open", "high", "low", "close", "volume")]
            if any(index >= len(items) or items[index] is None for items in values):
                continue
            open_, high, low, close, volume = (Decimal(str(items[index])) for items in values)
            candles.append(
                Candle(
                    instrument,
                    datetime.fromtimestamp(int(raw_ts), timezone.utc),
                    open_,
                    high,
                    low,
                    close,
                    volume,
                )
            )
        return tuple(candles)

    def latest_tick(self, instrument: Instrument) -> MarketTick:
        payload = self.client.get_json(
            f"{self.base_url}/{self._provider_symbol(instrument)}",
            params={"range": "1d", "interval": "1m"},
        )
        result = self._result(payload)
        meta = result.get("meta") or {}
        price = Decimal(str(meta["regularMarketPrice"]))
        timestamp = datetime.fromtimestamp(int(meta.get("regularMarketTime", 0)), timezone.utc)
        bid = Decimal(str(meta.get("bid") or price))
        ask = Decimal(str(meta.get("ask") or price))
        if bid > ask:
            bid = ask = price
        return MarketTick(instrument, timestamp, price, bid, ask, Decimal(0))

    @staticmethod
    def _result(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise TypeError("invalid_yahoo_payload")
        chart = payload.get("chart")
        if not isinstance(chart, dict) or chart.get("error"):
            raise ValueError("invalid_yahoo_payload")
        results = chart.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ValueError("invalid_yahoo_payload")
        return results[0]
