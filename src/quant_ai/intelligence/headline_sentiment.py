"""Headline sentiment: a model scorer with the keyword counter as its floor.

The deterministic scorer counts six hopeful words against six frightening ones. It
cannot read negation ("no war expected" counts as a war), it cannot tell a story about
the instrument from a story that merely mentions it, and it cannot tell a conflict that
reaches Indian IT revenue from one that does not. It is kept, because a number the
cadence can always produce is worth more than a number that needs a provider to be up.

``HeadlineSentimentScorer`` puts the model in front of it and the keyword counter behind
it. Every headline comes back labelled with the scorer that produced it and a one-line
reason, and that label travels with the headline into the evidence block and onto the
proof, so a founder reading a decision can see which of the two scored each story.

Degradation is total and silent to the cadence: no API key, no configured client, a
spent daily budget, a provider fault, a timeout or a payload that fails the strict
schema all fall back to the keyword score for exactly the headlines that were not
scored. A model score is cached per headline so a story that has already been read is
not paid for again on the next ten-minute tick; a keyword fallback is never cached, so
the next tick retries the model once the budget or the provider recovers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from quant_ai.agents.contracts import (
    KEYWORD_SCORER,
    MAX_HEADLINE_CHARS,
    MAX_HEADLINE_RATIONALE_CHARS,
    MODEL_SCORER,
)
from quant_ai.llm.anthropic_client import MAX_SCORED_HEADLINES, AnthropicSwarmClient

LOGGER = logging.getLogger("quant_ai.headline_sentiment")

POSITIVE_WORDS = ("gain", "growth", "beat", "rally", "peace", "deal")
NEGATIVE_WORDS = ("war", "sanction", "miss", "fall", "crash", "conflict")
KEYWORD_REASON = "keyword_word_count"
# The newest headlines the model is asked about on one tick. Everything older keeps its
# keyword score: a bounded spend beats an unbounded one on a heavy news day, and the
# cache means a steady state usually scores only the stories that are actually new.
MAX_MODEL_SCORED = 16
MAX_CACHED_SCORES = 512


@dataclass(frozen=True)
class HeadlineScore:
    """One headline's sentiment, who produced it and why, in one bounded line."""

    sentiment: Decimal
    scorer: str
    rationale: str


def normalize_headline(text: str) -> str:
    """Collapse third-party text to the single bounded line the contracts allow."""
    return " ".join(text.split())[:MAX_HEADLINE_CHARS]


def normalize_rationale(text: str) -> str:
    """Bound a rationale to one line that cannot open another evidence field."""
    return " ".join(text.replace(";", ",").split())[:MAX_HEADLINE_RATIONALE_CHARS]


def keyword_sentiment(text: str) -> Decimal:
    """The deterministic floor: positive words against negative words, clamped to [-1, 1]."""
    lowered = text.lower()
    positive = sum(word in lowered for word in POSITIVE_WORDS)
    negative = sum(word in lowered for word in NEGATIVE_WORDS)
    score = Decimal(positive - negative) / Decimal(max(1, positive + negative))
    return max(Decimal(-1), min(Decimal(1), score))


def keyword_score(text: str, reason: str = KEYWORD_REASON) -> HeadlineScore:
    return HeadlineScore(keyword_sentiment(text), KEYWORD_SCORER, normalize_rationale(reason))


class HeadlineSentimentScorer:
    """Score headlines with the model when it is available, the keyword counter otherwise.

    ``client`` is the same structured Anthropic adapter the consensus uses; ``None``
    means keyword-only and performs no I/O at all. Nothing this class does can raise
    into the cadence.
    """

    def __init__(
        self,
        client: AnthropicSwarmClient | None = None,
        *,
        max_model_scored: int = MAX_MODEL_SCORED,
        max_batch: int = MAX_SCORED_HEADLINES,
        max_cached: int = MAX_CACHED_SCORES,
    ) -> None:
        if max_model_scored < 0 or max_batch < 1 or max_cached < 1:
            raise ValueError("headline scorer bounds must be positive")
        self.client = client
        self.max_model_scored = max_model_scored
        self.max_batch = min(max_batch, MAX_SCORED_HEADLINES)
        self.max_cached = max_cached
        self._cache: dict[str, HeadlineScore] = {}
        self.calls = 0
        self.cache_hits = 0

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def score(self, subject: str, headlines: Sequence[str]) -> tuple[HeadlineScore, ...]:
        """One score per supplied headline, in the supplied order. Never raises."""
        texts = [normalize_headline(item) for item in headlines]
        if not texts:
            return ()
        if self.client is None or not subject.strip():
            return tuple(keyword_score(text) for text in texts)
        results: list[HeadlineScore | None] = [None] * len(texts)
        pending: list[int] = []
        for index, text in enumerate(texts):
            cached = self._cache.get(self._key(subject, text))
            if cached is not None:
                results[index] = cached
                self.cache_hits += 1
            elif len(pending) < self.max_model_scored:
                pending.append(index)
            else:
                results[index] = keyword_score(text, "keyword_fallback:headline_scoring_limit")
        for start in range(0, len(pending), self.max_batch):
            chunk = pending[start:start + self.max_batch]
            scored = await self._score_batch(subject, [texts[index] for index in chunk])
            for index, score in zip(chunk, scored):
                results[index] = score
                if score.scorer == MODEL_SCORER:
                    self._remember(self._key(subject, texts[index]), score)
        return tuple(
            item if item is not None else keyword_score(texts[index])
            for index, item in enumerate(results)
        )

    async def _score_batch(self, subject: str, texts: list[str]) -> tuple[HeadlineScore, ...]:
        def degraded(reason: str) -> tuple[HeadlineScore, ...]:
            return tuple(keyword_score(text, f"keyword_fallback:{reason}") for text in texts)

        self.calls += 1
        try:
            payload = await self.client.score_headlines(subject, tuple(texts))
        except Exception as error:  # noqa: BLE001 - a scorer fault never breaks the cadence
            LOGGER.warning("headline_scoring_failed detail=%s", type(error).__name__)
            return degraded(type(error).__name__)
        provenance = getattr(payload, "provenance", None)
        status = provenance.get("status") if isinstance(provenance, Mapping) else None
        if status != "completed":
            return degraded(str(status or "unverified_inference"))
        try:
            parsed = self.client.parse_headline_scores(payload, len(texts))
        except Exception as error:  # noqa: BLE001 - a malformed payload is no opinion
            LOGGER.warning("headline_scoring_invalid_schema detail=%s", error)
            return degraded("invalid_schema")
        return tuple(
            HeadlineScore(sentiment, MODEL_SCORER, normalize_rationale(rationale))
            for sentiment, rationale in parsed
        )

    def _remember(self, key: str, score: HeadlineScore) -> None:
        if key in self._cache:
            return
        if len(self._cache) >= self.max_cached:
            # Oldest first; the newest stories are the ones a ten-minute tick repeats.
            del self._cache[next(iter(self._cache))]
        self._cache[key] = score

    @staticmethod
    def _key(subject: str, text: str) -> str:
        """Per subject and per headline: the same story about another name is another score."""
        return sha256(f"{subject.upper()}\x00{text}".encode()).hexdigest()


def scorer_for(client: AnthropicSwarmClient | None) -> HeadlineSentimentScorer:
    """Headline scoring follows the consensus client it is given.

    No client — which is what an unset ``ANTHROPIC_API_KEY`` produces, since the adapter
    refuses to build without one — means keyword-only and no I/O at all, not a boot
    failure: headline sentiment keeps working deterministically, exactly as it did before
    the model path existed. A client carries its daily budget in with it.
    """
    if client is None:
        LOGGER.info("headline_scoring_keyword_only reason=no_consensus_client")
        return HeadlineSentimentScorer()
    return HeadlineSentimentScorer(client)
