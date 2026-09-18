from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class AgentDomain(str, Enum):
    TECHNICAL = "TECHNICAL"
    NEWS = "NEWS"
    MACRO = "MACRO"
    COUNTRY = "COUNTRY"
    DERIVATIVES = "DERIVATIVES"
    LIQUIDITY = "LIQUIDITY"
    RISK = "RISK"
    PORTFOLIO = "PORTFOLIO"


class Stance(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    AVOID = "AVOID"


@dataclass(frozen=True)
class AgentEvidence:
    agent_id: str
    domain: AgentDomain
    subject: str
    stance: Stance
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    rationale: tuple[str, ...]
    observed_at: datetime
    source_freshness_seconds: int

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("confidence must be between 0 and 1")
        if self.expected_risk < 0 or self.source_freshness_seconds < 0:
            raise ValueError("risk and freshness must be non-negative")


@dataclass(frozen=True)
class FounderEscalation:
    category: str
    reason: str
    required_decision: str
    urgency: str


@dataclass(frozen=True)
class AtlasDecision:
    cycle_id: str
    generated_at: datetime
    action: Stance
    subject: str
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    supporting_agents: tuple[str, ...]
    dissenting_agents: tuple[str, ...]
    rationale: tuple[str, ...]
    country_recommendations: tuple[str, ...]
    founder_escalations: tuple[FounderEscalation, ...]
    live_execution_allowed: bool = False
    provenance: dict | None = None

    def to_json(self) -> str:
        def normalize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, tuple):
                return [normalize(item) for item in value]
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            return value

        return json.dumps(normalize(asdict(self)), sort_keys=True, separators=(",", ":"))


# Bounds on the evidence handed to the consensus prompt. They keep every prompt a
# similar size and make prompt-injection surface (third-party headline text)
# small and easy to audit.
MAX_EVIDENCE_BARS = 20
MAX_EVIDENCE_HEADLINES = 8
MAX_HEADLINE_CHARS = 160
# Who produced a headline's sentiment number. ``keyword`` is the deterministic word-count
# scorer that cannot read negation or relevance; ``model`` is the bounded LLM scorer. Every
# headline says which one scored it, so a founder reading a proof can tell them apart.
KEYWORD_SCORER = "keyword"
MODEL_SCORER = "model"
HEADLINE_SCORERS = (MODEL_SCORER, KEYWORD_SCORER)
MAX_HEADLINE_RATIONALE_CHARS = 160
# An operator-asserted alias that attached a headline to an instrument, bounded to the
# same 40 characters the alias configuration allows.
MAX_HEADLINE_ALIAS_CHARS = 40
# Higher-timeframe context: a few closed bars per timeframe, so the consensus can see
# the trend it is trading inside without a second minute-by-minute bar list.
MAX_TIMEFRAMES = 4
MAX_TIMEFRAME_BARS = {"15m": 8, "1d": 10}
DEFAULT_MAX_TIMEFRAME_BARS = 8
# Operator-approved lessons from past sessions. They are data, never instructions.
MAX_LESSONS = 8
MAX_LESSON_CHARS = 200


@dataclass(frozen=True)
class EvidenceBar:
    """One closed OHLCV bar, already rendered: ISO-8601 timestamp and Decimals."""

    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class EvidenceHeadline:
    """One third-party headline. The text is untrusted data, never an instruction.

    ``scorer`` names who produced ``sentiment`` and ``rationale`` says why in one short
    line. A model rationale is derived from untrusted headline text, so it is bounded and
    single-line and carries no ``;`` of its own: it can never forge another evidence field.

    ``matched_alias`` is empty when the headline named this subject itself. When it is set,
    the headline reached this instrument through an operator-asserted alias rather than an
    observed mention, and the proof says which alias made the claim.
    """

    subject: str
    headline: str
    sentiment: Decimal
    published_at: str
    provider: str
    scorer: str = KEYWORD_SCORER
    rationale: str = ""
    matched_alias: str = ""

    def __post_init__(self) -> None:
        if len(self.headline) > MAX_HEADLINE_CHARS:
            raise ValueError(f"headline must be at most {MAX_HEADLINE_CHARS} characters")
        if any(char in self.headline for char in "\r\n"):
            raise ValueError("headline must be a single line")
        if self.scorer not in HEADLINE_SCORERS:
            raise ValueError(f"headline scorer must be one of {HEADLINE_SCORERS}")
        if len(self.rationale) > MAX_HEADLINE_RATIONALE_CHARS:
            raise ValueError(
                f"headline rationale must be at most {MAX_HEADLINE_RATIONALE_CHARS} characters"
            )
        if any(char in self.rationale for char in "\r\n;"):
            raise ValueError("headline rationale must be a single line without ';'")
        if len(self.matched_alias) > MAX_HEADLINE_ALIAS_CHARS:
            raise ValueError(
                f"matched alias must be at most {MAX_HEADLINE_ALIAS_CHARS} characters"
            )
        if any(char in self.matched_alias for char in "\r\n;="):
            raise ValueError("matched alias must be a single line without ';' or '='")


@dataclass(frozen=True)
class EvidenceContext:
    """Bounded, pre-rendered market evidence for the LLM trading consensus.

    Every field is already a string or Decimal so the prompt renderer only joins
    text and never computes. Metric maps are ordered ``(name, value)`` pairs;
    ``freshness`` pairs a data category with its rendered state. ``None``
    observation times and empty tuples render as ``unavailable``.

    ``timeframes`` holds closed higher-timeframe bars per timeframe name (``15m``,
    ``1d``), oldest first. ``regime`` holds the deterministic regime label and its
    metrics, ``label`` and ``timeframe`` first. ``lessons`` are operator-approved
    single-line notes from past sessions; the renderer labels them as data.
    """

    bars: tuple[EvidenceBar, ...] = ()
    technical: tuple[tuple[str, Decimal], ...] = ()
    headlines: tuple[EvidenceHeadline, ...] = ()
    macro: tuple[tuple[str, Decimal], ...] = ()
    macro_observed_at: str | None = None
    fundamentals: tuple[tuple[str, Decimal], ...] = ()
    fundamentals_observed_at: str | None = None
    freshness: tuple[tuple[str, str], ...] = ()
    timeframes: tuple[tuple[str, tuple[EvidenceBar, ...]], ...] = ()
    regime: tuple[tuple[str, Decimal | str], ...] = ()
    lessons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.bars) > MAX_EVIDENCE_BARS:
            raise ValueError(f"evidence context holds at most {MAX_EVIDENCE_BARS} bars")
        if len(self.headlines) > MAX_EVIDENCE_HEADLINES:
            raise ValueError(f"evidence context holds at most {MAX_EVIDENCE_HEADLINES} headlines")
        if len(self.timeframes) > MAX_TIMEFRAMES:
            raise ValueError(f"evidence context holds at most {MAX_TIMEFRAMES} timeframes")
        seen: set[str] = set()
        for timeframe, bars in self.timeframes:
            if not timeframe or timeframe in seen or any(c.isspace() or c in ";=" for c in timeframe):
                raise ValueError("timeframe names must be unique single tokens")
            seen.add(timeframe)
            limit = MAX_TIMEFRAME_BARS.get(timeframe, DEFAULT_MAX_TIMEFRAME_BARS)
            if len(bars) > limit:
                raise ValueError(f"evidence context holds at most {limit} {timeframe} bars")
        for name, value in self.regime:
            if not name or any(c in name for c in "\r\n;="):
                raise ValueError("regime metric names must be single tokens")
            if isinstance(value, str) and any(c in value for c in "\r\n"):
                raise ValueError("regime values must be a single line")
        if len(self.lessons) > MAX_LESSONS:
            raise ValueError(f"evidence context holds at most {MAX_LESSONS} lessons")
        for lesson in self.lessons:
            if len(lesson) > MAX_LESSON_CHARS:
                raise ValueError(f"lessons must be at most {MAX_LESSON_CHARS} characters")
            if any(char in lesson for char in "\r\n"):
                raise ValueError("lessons must be a single line")
