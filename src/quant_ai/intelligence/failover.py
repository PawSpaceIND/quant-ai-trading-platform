from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from quant_ai.domain.models import Instrument
from quant_ai.intelligence.providers import FundamentalSnapshot, MacroSnapshot
from quant_ai.marketdata.feed import MarketTick


class ProviderCategory(str, Enum):
    PRICE = "PRICE"
    NEWS = "NEWS"
    FUNDAMENTALS = "FUNDAMENTALS"
    MACRO = "MACRO"


class AllProvidersFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderAttempt:
    provider_id: str
    error: str


class ProviderFailoverRegistry:
    def __init__(self) -> None:
        self._providers: dict[ProviderCategory, tuple[object, ...]] = {}
        self.last_attempts: tuple[ProviderAttempt, ...] = ()

    def register(self, category: ProviderCategory, *providers: object) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers[category] = tuple(providers)

    def call(self, category: ProviderCategory, method: str, *args: object, **kwargs: object) -> Any:
        attempts: list[ProviderAttempt] = []
        for provider in self._providers.get(category, ()):
            provider_id = str(getattr(provider, "provider_id", provider.__class__.__name__))
            try:
                result = getattr(provider, method)(*args, **kwargs)
                self.last_attempts = tuple(attempts)
                return result
            except (TimeoutError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                attempts.append(ProviderAttempt(provider_id, type(exc).__name__))
        self.last_attempts = tuple(attempts)
        raise AllProvidersFailed(f"all providers failed for {category.value}:{method}")


class FailoverMarketDataFeed:
    def __init__(self, registry: ProviderFailoverRegistry) -> None:
        self.registry = registry

    def fetch_ohlcv(self, instrument: Instrument, start: datetime, end: datetime, timeframe: str = "1m"):
        try:
            return self.registry.call(
                ProviderCategory.PRICE, "fetch_ohlcv", instrument, start, end, timeframe
            )
        except AllProvidersFailed:
            return ()

    def latest_tick(self, instrument: Instrument) -> MarketTick:
        return self.registry.call(ProviderCategory.PRICE, "latest_tick", instrument)


class FailoverNewsProvider:
    provider_id = "failover-news"

    def __init__(self, registry: ProviderFailoverRegistry) -> None:
        self.registry = registry

    def fetch(self, subject: str, now: datetime):
        try:
            return self.registry.call(ProviderCategory.NEWS, "fetch", subject, now)
        except AllProvidersFailed:
            return ()


class FailoverFundamentalProvider:
    provider_id = "failover-fundamentals"

    def __init__(self, registry: ProviderFailoverRegistry) -> None:
        self.registry = registry

    def fetch(self, subject: str, now: datetime) -> FundamentalSnapshot:
        try:
            return self.registry.call(ProviderCategory.FUNDAMENTALS, "fetch", subject, now)
        except AllProvidersFailed:
            return FundamentalSnapshot(subject, {}, now)


class FailoverMacroProvider:
    provider_id = "failover-macro"

    def __init__(self, registry: ProviderFailoverRegistry) -> None:
        self.registry = registry

    def fetch(self, indicators: tuple[str, ...], now: datetime) -> MacroSnapshot:
        try:
            return self.registry.call(ProviderCategory.MACRO, "fetch", indicators, now)
        except AllProvidersFailed:
            return MacroSnapshot({}, now)
