from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from quant_ai.intelligence.headline_sentiment import keyword_sentiment
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.intelligence.resilience import ResilientHttpClient


class RssNewsSentimentAdapter:
    provider_id = "rss-news"

    def __init__(self, client: ResilientHttpClient, feed_urls: tuple[str, ...]) -> None:
        if not feed_urls:
            raise ValueError("at least one RSS feed URL is required")
        self.client = client
        self.feed_urls = feed_urls

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]:
        signals: list[NewsSignal] = []
        needle = subject.upper()
        for url in self.feed_urls:
            xml = self.client.get_text(url, headers={"User-Agent": "quant-ai-readonly/1.0"})
            root = ElementTree.fromstring(xml)
            for item in root.findall(".//item")[:50]:
                title = (item.findtext("title") or "").strip()
                description = (item.findtext("description") or "").strip()
                text = f"{title} {description}"
                if needle != "GEOPOLITICAL" and needle not in text.upper():
                    continue
                try:
                    published = self._published_at(item.findtext("pubDate"), now)
                except (ValueError, TypeError, OverflowError):
                    continue
                if published > now:
                    continue
                signals.append(
                    NewsSignal(
                        subject,
                        title or "untitled",
                        self._sentiment(text),
                        url,
                        published,
                    )
                )
        return tuple(sorted(signals, key=lambda item: item.published_at, reverse=True)[:50])

    @staticmethod
    def _published_at(raw: str | None, fallback: datetime) -> datetime:
        if not raw:
            raise ValueError("RSS publication time is missing")
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _sentiment(text: str) -> Decimal:
        """The deterministic floor every signal carries until a scorer improves on it.

        The adapter is synchronous and runs wherever news is fetched, so it never calls a
        provider of its own. ``SwarmMarketAnalysisPipeline`` re-scores the headlines that
        reach a decision, with the model when one is configured, and records which scorer
        produced the number it used.
        """
        return keyword_sentiment(text)
