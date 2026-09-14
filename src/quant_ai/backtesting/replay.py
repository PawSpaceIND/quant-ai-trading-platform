from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import Instrument, PortfolioSnapshot
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.friction import FrictionContext
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import FundamentalSnapshot, MacroSnapshot, NewsSignal
from quant_ai.marketdata.feed import MarketDataFeed, MarketTick
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalPlan


@dataclass(frozen=True)
class HistoricalMacroEvent:
    observed_at: datetime
    indicators: dict[str, Decimal]


@dataclass(frozen=True)
class HistoricalFundamentalEvent:
    observed_at: datetime
    subject: str
    metrics: dict[str, Decimal]


@dataclass(frozen=True)
class HistoricalReplayDataset:
    bars: tuple[Candle, ...]
    macro: tuple[HistoricalMacroEvent, ...] = ()
    news: tuple[NewsSignal, ...] = ()
    fundamentals: tuple[HistoricalFundamentalEvent, ...] = ()
    benchmark_closes: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class HistoricalReplayResult:
    equity_curve: tuple[Decimal, ...]
    benchmark_returns: tuple[Decimal, ...]
    timestamps: tuple[datetime, ...]
    order_ids: tuple[str, ...]
    final_snapshot: PortfolioSnapshot


class HistoricalMarketDataFeed(MarketDataFeed):
    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self.bars = bars
        self.current_time = bars[0].timestamp

    def set_time(self, timestamp: datetime) -> None:
        self.current_time = timestamp

    def visible(self) -> tuple[Candle, ...]:
        return tuple(item for item in self.bars if item.timestamp <= self.current_time)

    def fetch_ohlcv(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str = "1m",
    ) -> tuple[Candle, ...]:
        del timeframe
        return tuple(
            item
            for item in self.visible()
            if item.instrument == instrument and start <= item.timestamp <= end
        )

    def latest_tick(self, instrument: Instrument) -> MarketTick:
        visible = [item for item in self.visible() if item.instrument == instrument]
        if not visible:
            raise ValueError("no_visible_historical_price")
        bar = visible[-1]
        return MarketTick(instrument, bar.timestamp, bar.close, bar.close, bar.close, bar.volume)


class HistoricalNewsProvider:
    provider_id = "historical-news"

    def __init__(self, events: tuple[NewsSignal, ...]) -> None:
        self.events = events

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]:
        return tuple(
            item
            for item in self.events
            if item.published_at <= now and item.subject == subject
        )


class HistoricalMacroProvider:
    provider_id = "historical-macro"

    def __init__(self, events: tuple[HistoricalMacroEvent, ...]) -> None:
        self.events = events

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        visible = [item for item in self.events if item.observed_at <= now]
        if not visible:
            return MacroSnapshot({}, now)
        latest = visible[-1]
        return MacroSnapshot(
            {key: value for key, value in latest.indicators.items() if key in indicators},
            latest.observed_at,
        )


class HistoricalFundamentalProvider:
    provider_id = "historical-fundamentals"

    def __init__(self, events: tuple[HistoricalFundamentalEvent, ...]) -> None:
        self.events = events

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot:
        visible = [
            item for item in self.events if item.subject == subject and item.observed_at <= now
        ]
        if not visible:
            return FundamentalSnapshot(subject, {}, now)
        latest = visible[-1]
        return FundamentalSnapshot(subject, latest.metrics, latest.observed_at)


class HistoricalReplayHarness:
    """Point-in-time replay with next-bar execution for close-derived decisions."""

    def __init__(
        self,
        broker: PaperBrokerService,
        plan: CapitalPlan,
        *,
        quantity: int | None = 1,
        country: str = "USA",
        tenant_id: str = "backtest",
        xai_logger: XAITraceLogger | None = None,
    ) -> None:
        self.broker = broker
        self.plan = plan
        self.quantity = quantity
        self.country = country
        self.tenant_id = tenant_id
        self.xai_logger = xai_logger

    def run(self, dataset: HistoricalReplayDataset) -> HistoricalReplayResult:
        self._validate(dataset)
        feed = HistoricalMarketDataFeed(dataset.bars)
        runtime = SwarmPaperTradingService(broker=self.broker, xai_logger=self.xai_logger)
        pipeline = SwarmMarketAnalysisPipeline(
            feed,
            HistoricalNewsProvider(dataset.news),
            HistoricalFundamentalProvider(dataset.fundamentals),
            HistoricalMacroProvider(dataset.macro),
            runtime=runtime,
        )
        instrument = dataset.bars[0].instrument
        tracker = PortfolioTracker(self.broker, feed, tenant_id=self.tenant_id)
        curve: list[Decimal] = []
        timestamps: list[datetime] = []
        order_ids: list[str] = []

        first = dataset.bars[0]
        feed.set_time(first.timestamp)
        self._assert_no_lookahead(feed, dataset, instrument, first.timestamp)
        first_snapshot = tracker.get_snapshot(first.timestamp)
        self._record_valuation(tracker, first.timestamp)
        curve.append(first_snapshot.equity)
        timestamps.append(first.timestamp)

        for index in range(1, len(dataset.bars)):
            decision_bar = dataset.bars[index - 1]
            execution_bar = dataset.bars[index]
            feed.set_time(decision_bar.timestamp)
            visible = feed.visible()
            context = self._friction_context(visible)
            self.broker.set_friction_context(
                context,
                execution_time=execution_bar.timestamp,
            )
            before = tracker.get_snapshot(decision_bar.timestamp)
            self._assert_no_lookahead(
                feed,
                dataset,
                instrument,
                decision_bar.timestamp,
            )
            result = pipeline.run(
                instrument,
                decision_bar.timestamp,
                self.plan,
                before,
                quantity=self.quantity,
                country=self.country,
                tenant_id=self.tenant_id,
                country_exposure={self.country: before.gross_exposure},
                reference_price_override=execution_bar.open,
            )
            if result.execution.fill is not None:
                order_ids.append(result.execution.fill.order_id)

            feed.set_time(execution_bar.timestamp)
            after = tracker.get_snapshot(execution_bar.timestamp)
            self._record_valuation(tracker, execution_bar.timestamp)
            curve.append(after.equity)
            timestamps.append(execution_bar.timestamp)

        self.broker.set_friction_context(None)
        return HistoricalReplayResult(
            tuple(curve),
            self._benchmark_returns(dataset),
            tuple(timestamps),
            tuple(order_ids),
            tracker.get_snapshot(dataset.bars[-1].timestamp),
        )

    def _record_valuation(self, tracker: PortfolioTracker, timestamp: datetime) -> None:
        metrics = tracker.metrics(timestamp)
        payload = {
            "status": "ok",
            "tenantId": self.tenant_id,
            "markMode": "historical_replay",
            "markDisclaimer": "Historical/synthetic replay valuations; not live market prices.",
            "cash": float(metrics.cash_balance),
            "totalEquity": float(metrics.total_equity),
            "startingCapital": float(
                self.broker.get_margin(self.tenant_id).starting_capital
            ),
            "realizedPnl": float(metrics.realized_pnl),
            "unrealizedPnl": float(metrics.unrealized_pnl),
            "highWaterMark": float(metrics.high_water_mark),
            "drawdown": float(metrics.drawdown_fraction),
            "holdings": [
                {
                    "symbol": position.symbol,
                    "market": position.market.value,
                    "assetClass": position.asset_class.value,
                    "quantity": position.quantity,
                    "averageEntry": float(position.average_entry_price),
                    "markPrice": float(position.current_price),
                    "markSource": "replay_bar_close",
                    "marketValue": float(position.market_value),
                    "unrealizedPnl": float(position.unrealized_pnl),
                }
                for position in metrics.positions
            ],
            "updatedAt": timestamp.isoformat(),
        }
        self.broker.record_replay_valuation(
            timestamp.isoformat(),
            json.dumps(payload, allow_nan=False),
            self.tenant_id,
        )

    @staticmethod
    def _assert_no_lookahead(
        feed: HistoricalMarketDataFeed,
        dataset: HistoricalReplayDataset,
        instrument: Instrument,
        now: datetime,
    ) -> None:
        window = feed.fetch_ohlcv(instrument, now - timedelta(minutes=60), now, "1m")
        if window and window[-1].timestamp > now:
            raise RuntimeError("lookahead_violation: future bar served to the pipeline")
        news_at = max(
            (
                item.published_at
                for item in HistoricalNewsProvider(dataset.news).fetch(instrument.symbol, now)
            ),
            default=now,
        )
        macro_at = HistoricalMacroProvider(dataset.macro).fetch(("US10Y",), now).observed_at
        fundamentals_at = HistoricalFundamentalProvider(dataset.fundamentals).fetch(
            instrument.symbol, now
        ).observed_at
        if max(news_at, macro_at, fundamentals_at) > now:
            raise RuntimeError("lookahead_violation: future provider event served to the pipeline")

    @staticmethod
    def _friction_context(visible: tuple[Candle, ...]) -> FrictionContext:
        recent = visible[-15:]
        ranges = [item.high - item.low for item in recent]
        atr = sum(ranges, Decimal(0)) / Decimal(len(ranges)) if ranges else Decimal(0)
        average_bar_volume = (
            sum((item.volume for item in recent), Decimal(0)) / Decimal(len(recent))
            if recent
            else Decimal(1)
        )
        adv = max(Decimal(1), average_bar_volume * Decimal(390))
        return FrictionContext(atr, adv, Decimal("0.90"), True)

    @staticmethod
    def _benchmark_returns(dataset: HistoricalReplayDataset) -> tuple[Decimal, ...]:
        closes = dataset.benchmark_closes
        return tuple(
            (after - before) / before
            for before, after in zip(closes, closes[1:])
            if before > 0
        )

    @staticmethod
    def _validate(dataset: HistoricalReplayDataset) -> None:
        if not dataset.bars:
            raise ValueError("historical bars are required")
        if any(
            item.timestamp.tzinfo is None or item.timestamp.utcoffset() is None
            for item in dataset.bars
        ):
            raise ValueError("historical timestamps must be timezone-aware")
        if any(
            current.timestamp <= previous.timestamp
            for previous, current in zip(dataset.bars, dataset.bars[1:])
        ):
            raise ValueError("historical bars must be strictly ordered")
        end = dataset.bars[-1].timestamp
        for timestamps, label in (
            ((item.observed_at for item in dataset.macro), "macro"),
            ((item.published_at for item in dataset.news), "news"),
            ((item.observed_at for item in dataset.fundamentals), "fundamentals"),
        ):
            values = tuple(timestamps)
            if any(b <= a for a, b in zip(values, values[1:])):
                raise ValueError(f"{label} events must be strictly ordered")
            if any(item > end for item in values):
                raise ValueError(f"future-dated {label} event")
            if any(item.tzinfo is None or item.utcoffset() is None for item in values):
                raise ValueError("historical timestamps must be timezone-aware")


def load_replay_dataset(path: str | Path, instrument: Instrument) -> HistoricalReplayDataset:
    source = Path(path)
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text())
        bars = tuple(_bar_from_mapping(item, instrument) for item in payload.get("bars", ()))
        macro = tuple(
            HistoricalMacroEvent(
                datetime.fromisoformat(item["observed_at"]),
                {key: Decimal(str(value)) for key, value in item["indicators"].items()},
            )
            for item in payload.get("macro", ())
        )
        news = tuple(
            NewsSignal(
                item["subject"],
                item["headline"],
                Decimal(str(item["sentiment"])),
                item.get("source", "historical"),
                datetime.fromisoformat(item["published_at"]),
            )
            for item in payload.get("news", ())
        )
        fundamentals = tuple(
            HistoricalFundamentalEvent(
                datetime.fromisoformat(item["observed_at"]),
                item["subject"],
                {key: Decimal(str(value)) for key, value in item["metrics"].items()},
            )
            for item in payload.get("fundamentals", ())
        )
        benchmark = tuple(Decimal(str(item)) for item in payload.get("benchmark_closes", ()))
        return HistoricalReplayDataset(bars, macro, news, fundamentals, benchmark)
    with source.open(newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    return HistoricalReplayDataset(tuple(_bar_from_mapping(item, instrument) for item in rows))


def _bar_from_mapping(item: dict[str, object], instrument: Instrument) -> Candle:
    return Candle(
        instrument,
        datetime.fromisoformat(str(item["timestamp"])),
        Decimal(str(item["open"])),
        Decimal(str(item["high"])),
        Decimal(str(item["low"])),
        Decimal(str(item["close"])),
        Decimal(str(item["volume"])),
    )
