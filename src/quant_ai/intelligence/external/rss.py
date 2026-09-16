from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from quant_ai.intelligence.headline_sentiment import keyword_sentiment
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.intelligence.resilience import ResilientHttpClient

# The subject that means "everything": world news carries no instrument ticker.
GEOPOLITICAL_SUBJECT = "GEOPOLITICAL"

SYMBOL_ALIASES_ENV = "PRAMANA_NEWS_SYMBOL_ALIASES_JSON"

# Bounds on operator-asserted aliases.
#
# The plain symbol filter reads news for INFY by asking whether "INFY" appears in the
# headline. Indian financial press writes "Infosys", "Gold ETFs" and "Silver prices", so
# three of the five pilot instruments matched nothing and their news agent published a
# number derived from no headlines at all. An alias fixes that by letting the operator
# say "INFOSYS means INFY" - but a careless alias widens the net in the other direction,
# and a false headline is worse than no headline because it is indistinguishable from
# evidence. These bounds are what make a careless alias safe to have written:
#
# * Word-boundary matching, not substring. This is the whole guard. "GOLD" as a substring
#   matches "Goldman Sachs" and "ITC" matches "SWITCH"; as a whole word neither does,
#   because a letter sits on the boundary in both cases. The symbol filter itself is left
#   as a plain substring so that an unconfigured install keeps the behaviour it has today,
#   character for character - the new rule applies only to the new, asserted matches.
# * One suffix is tolerated: a trailing "S" or "ES", so "Gold ETF" also matches
#   "Gold ETFs". English plurals are the one variation Indian headlines genuinely turn on
#   ("Gold ETF inflows" and "Gold ETFs see inflows" are the same story), and the closed
#   set cannot reach "Goldman" or "SWITCH", whose extra characters are not in it. A
#   possessive needs no rule: "Infosys's" already ends the word at the apostrophe.
# * At least three characters. A one- or two-letter whole word is ordinary prose or an
#   unrelated initialism - "IT", "AI", "US", "FY" all appear in market copy every day -
#   so an alias that short cannot assert anything specific enough to be checked.
# * At most 40 characters, the same ceiling the instrument master puts on a symbol.
# * It must contain a letter. A bare number is a date, a price or a scrip code in prose,
#   and matches all three.
# * At most 8 aliases per symbol and 32 symbols. Each alias is an unverified assertion
#   and each is applied to every item of every feed, so the map is kept to something an
#   operator can read in one screen and defend line by line.
# * An alias may not repeat its own symbol. The symbol already matches itself; accepting
#   it here would label an observed match as an asserted one and corrupt the provenance
#   this whole feature is supposed to preserve.
MAX_ALIAS_SYMBOLS = 32
MAX_ALIASES_PER_SYMBOL = 8
MIN_ALIAS_CHARS = 3
MAX_ALIAS_CHARS = 40
# Letters, digits and the separators real company names use, and it must begin and end on
# a letter or digit so that both ends of the match sit on a word boundary. ';' and '=' are
# excluded by construction, so an alias can never forge a field in the rendered evidence.
_ALIAS_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9 &.\-]*[A-Z0-9]$")


def _is_alias_list(value: object) -> bool:
    return not isinstance(value, str) and isinstance(value, Sequence)


def parse_symbol_aliases(payload: object) -> dict[str, tuple[str, ...]]:
    """Validate an operator's alias map, refusing loudly rather than dropping entries.

    ``None`` and an empty map mean no aliases, which is the unconfigured install. Anything
    else must be a mapping of symbol to a non-empty list of alias strings within the bounds
    above; a value that fails any of them raises, because an alias that is silently ignored
    looks exactly like an instrument with no news.
    """
    if payload is None or payload == "":
        return {}
    if not isinstance(payload, Mapping) or len(payload) > MAX_ALIAS_SYMBOLS:
        raise ValueError("news_symbol_aliases_invalid")
    parsed: dict[str, tuple[str, ...]] = {}
    for raw_symbol, raw_aliases in payload.items():
        symbol = raw_symbol.strip().upper() if isinstance(raw_symbol, str) else ""
        if not symbol or len(symbol) > MAX_ALIAS_CHARS or symbol == GEOPOLITICAL_SUBJECT:
            raise ValueError("news_alias_symbol_invalid")
        if symbol in parsed:
            raise ValueError("news_alias_symbol_duplicate")
        # A bare string is a sequence of characters, and accepting one would silently
        # declare an alias per letter, so it is refused rather than iterated.
        listed = tuple(raw_aliases) if _is_alias_list(raw_aliases) else ()
        if not listed or len(listed) > MAX_ALIASES_PER_SYMBOL:
            raise ValueError("news_alias_list_invalid")
        if any(not isinstance(item, str) for item in listed):
            raise ValueError("news_alias_invalid")
        aliases: list[str] = []
        for raw_alias in listed:
            alias = " ".join(raw_alias.upper().split())
            if not MIN_ALIAS_CHARS <= len(alias) <= MAX_ALIAS_CHARS:
                raise ValueError("news_alias_length_invalid")
            if not _ALIAS_SHAPE.match(alias):
                raise ValueError("news_alias_characters_invalid")
            if not any(char.isalpha() for char in alias):
                raise ValueError("news_alias_must_name_something")
            if alias == symbol:
                raise ValueError("news_alias_repeats_symbol")
            if alias in aliases:
                raise ValueError("news_alias_duplicate")
            aliases.append(alias)
        parsed[symbol] = tuple(aliases)
    return parsed


def symbol_aliases_from_env(raw: str | None = None) -> dict[str, tuple[str, ...]]:
    """Read ``PRAMANA_NEWS_SYMBOL_ALIASES_JSON``; unset or blank means no aliases."""
    text = (os.getenv(SYMBOL_ALIASES_ENV, "") if raw is None else raw).strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("news_symbol_aliases_invalid") from error
    return parse_symbol_aliases(payload)


def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(alias)}(?:E?S)?\b")


class RssNewsSentimentAdapter:
    provider_id = "rss-news"

    def __init__(
        self,
        client: ResilientHttpClient,
        feed_urls: tuple[str, ...],
        symbol_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if not feed_urls:
            raise ValueError("at least one RSS feed URL is required")
        self.client = client
        self.feed_urls = feed_urls
        # Validated here too, not only at the environment edge: an alias reaching the
        # matcher unbounded is the failure these bounds exist to prevent, whichever
        # caller built the map.
        self.symbol_aliases = parse_symbol_aliases(symbol_aliases)
        self._alias_patterns = {
            symbol: tuple((alias, _alias_pattern(alias)) for alias in aliases)
            for symbol, aliases in self.symbol_aliases.items()
        }

    def fetch(self, subject: str, now: datetime) -> tuple[NewsSignal, ...]:
        signals: list[NewsSignal] = []
        needle = subject.upper()
        patterns = self._alias_patterns.get(needle, ())
        for url in self.feed_urls:
            xml = self.client.get_text(url, headers={"User-Agent": "quant-ai-readonly/1.0"})
            root = ElementTree.fromstring(xml)
            for item in root.findall(".//item")[:50]:
                title = (item.findtext("title") or "").strip()
                description = (item.findtext("description") or "").strip()
                text = f"{title} {description}"
                matched_alias: str | None = None
                if needle != GEOPOLITICAL_SUBJECT and needle not in text.upper():
                    # The symbol itself is preferred wherever it appears, so a headline
                    # that names the instrument is never recorded as an asserted match.
                    matched_alias = self._matching_alias(patterns, text.upper())
                    if matched_alias is None:
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
                        matched_alias,
                    )
                )
        return tuple(sorted(signals, key=lambda item: item.published_at, reverse=True)[:50])

    @staticmethod
    def _matching_alias(
        patterns: tuple[tuple[str, re.Pattern[str]], ...], haystack: str
    ) -> str | None:
        """The first operator-declared alias this text names as a whole word, if any."""
        for alias, pattern in patterns:
            if pattern.search(haystack):
                return alias
        return None

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
