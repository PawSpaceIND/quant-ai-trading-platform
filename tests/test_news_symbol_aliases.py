"""Indian headlines do not use tickers, and a fix for that can quietly invent evidence.

Per-instrument news was filtered by the ticker as a bare substring. Of the five pilot
instruments only TCS and RELIANCE ever matched: the press writes "Infosys", "Gold ETFs"
and "Silver prices", so INFY, GOLDBEES and SILVERBEES read zero headlines and the agents
that depend on them published a number derived from nothing.

Letting an operator declare aliases fixes that and opens a worse hole in the same motion.
Substring matching cuts both ways: an alias of GOLD would take "Goldman Sachs raises
target" as a gold story and ITC would take "SWITCH". A headline wrongly attached to an
instrument is not a missing input - it is a fabricated one, and it reaches a decision
looking exactly like evidence.

The tests here are about three things: the real Indian phrasing now matches; the bounds
that keep a careless alias from matching prose actually hold; and a headline that arrived
through an operator's assertion stays marked as asserted all the way to the proof, so it
is never read as an observed mention of the instrument.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.atlas import _headline_line, _headline_provenance
from quant_ai.agents.contracts import EvidenceContext, EvidenceHeadline
from quant_ai.daemon import _env_intelligence_providers
from quant_ai.intelligence.external.rss import (
    SYMBOL_ALIASES_ENV,
    RssNewsSentimentAdapter,
    parse_symbol_aliases,
    symbol_aliases_from_env,
)
from quant_ai.intelligence.failover import ProviderCategory
from quant_ai.intelligence.freshness import FreshnessResult, FreshnessState
from quant_ai.intelligence.headline_sentiment import HeadlineScore
from quant_ai.intelligence.pipeline import PipelineFreshness, SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import FundamentalSnapshot, MacroSnapshot, NewsSignal

NOW = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
PUBLISHED = "Mon, 14 Sep 2026 09:00:00 GMT"

# Real phrasing from the Indian markets desks the operator's three feeds carry, plus the
# false-positive probes that a careless alias would swallow.
HEADLINES = (
    "Infosys Q2 results beat estimates",
    "Gold ETFs see record inflows",
    "Silver prices hit a fresh high",
    "ITC Q2 profit rises",
    "Goldman Sachs raises target",
    "Bharat Forge to SWITCH suppliers",
    "TCS wins a large deal",
)


class Feed:
    """One RSS document holding every headline above, with valid publication times."""

    def __init__(self, headlines=HEADLINES):
        self.headlines = headlines

    def get_text(self, *args, **kwargs):
        items = "".join(
            f"<item><title>{title}</title><pubDate>{PUBLISHED}</pubDate></item>"
            for title in self.headlines
        )
        return f"<rss><channel>{items}</channel></rss>"


def fetch(subject, aliases=None, headlines=HEADLINES):
    adapter = RssNewsSentimentAdapter(Feed(headlines), ("https://example.test/rss",), aliases)
    return adapter.fetch(subject, NOW)


def test_an_unconfigured_install_matches_exactly_what_it_matched_before():
    """No aliases means no behaviour change at all - the reason this is safe to deploy.

    Both spellings of "absent" are covered: the argument omitted entirely and an empty
    map passed explicitly. TCS still matches its own headline, INFY still matches nothing,
    and nothing at all is recorded as alias-matched.

    The last assertion pins today's behaviour including its flaw: the ticker is still a
    bare substring, so ITC still reads "SWITCH". The new word-boundary rule deliberately
    governs only the aliases, because tightening the ticker filter would change what a
    running install sees, and this change is meant to add news, not remove any.
    """
    for aliases in (None, {}):
        assert [row.headline for row in fetch("TCS", aliases)] == ["TCS wins a large deal"]
        assert fetch("INFY", aliases) == ()
        assert fetch("GOLDBEES", aliases) == ()
        assert fetch("SILVERBEES", aliases) == ()
        assert all(row.matched_alias is None for row in fetch("TCS", aliases))
        assert [row.headline for row in fetch("ITC", aliases)] == [
            "ITC Q2 profit rises",
            "Bharat Forge to SWITCH suppliers",
        ]


def test_an_empty_environment_variable_is_the_same_as_no_environment_variable(monkeypatch):
    """A blank or whitespace value in .env must not be read as a malformed map."""
    monkeypatch.delenv(SYMBOL_ALIASES_ENV, raising=False)
    assert symbol_aliases_from_env() == {}
    for blank in ("", "   ", "\n"):
        monkeypatch.setenv(SYMBOL_ALIASES_ENV, blank)
        assert symbol_aliases_from_env() == {}
    # An empty JSON object is a configured map that declares nothing, which is also no-op.
    monkeypatch.setenv(SYMBOL_ALIASES_ENV, "{}")
    assert symbol_aliases_from_env() == {}


def test_the_three_dead_instruments_now_read_the_headlines_that_are_about_them():
    """The failure that motivated this: INFY, GOLDBEES and SILVERBEES saw nothing.

    "Infosys Q2 results beat estimates" is the ordinary way an INFY story is written and
    INFY is not a substring of INFOSYS. "Gold ETFs" needs the plural to be tolerated, and
    "Silver prices" the same. Each one is asserted by the alias it came in on.
    """
    aliases = {
        "INFY": ["Infosys"],
        "GOLDBEES": ["Gold ETF", "Gold BeES", "Gold price"],
        "SILVERBEES": ["Silver ETF", "Silver BeES", "Silver price"],
    }
    infy = fetch("INFY", aliases)
    assert [row.headline for row in infy] == ["Infosys Q2 results beat estimates"]
    assert infy[0].matched_alias == "INFOSYS"

    gold = fetch("GOLDBEES", aliases)
    assert [row.headline for row in gold] == ["Gold ETFs see record inflows"]
    assert gold[0].matched_alias == "GOLD ETF"

    silver = fetch("SILVERBEES", aliases)
    assert [row.headline for row in silver] == ["Silver prices hit a fresh high"]
    assert silver[0].matched_alias == "SILVER PRICE"


def test_a_careless_alias_does_not_swallow_the_word_it_is_buried_inside():
    """GOLD must not read "Goldman Sachs", ITC must not read "SWITCH".

    These are the two ways a substring match goes wrong - extra letters after the alias,
    and extra letters before it - and they are the reason the net was not simply widened.
    An operator who writes the broadest alias they can think of still gets only whole
    words, so a careless alias costs coverage rather than inventing a story.
    """
    gold = [row.headline for row in fetch("GOLDBEES", {"GOLDBEES": ["Gold"]})]
    assert gold == ["Gold ETFs see record inflows"]  # and not "Goldman Sachs raises target"

    itc = [row.headline for row in fetch("ITCLTD", {"ITCLTD": ["ITC"]})]
    assert itc == ["ITC Q2 profit rises"]  # and not "Bharat Forge to SWITCH suppliers"


def test_a_headline_that_names_the_symbol_is_never_recorded_as_an_assertion():
    """An observed mention outranks an asserted one, even when both are present.

    TCS appears in its own headline. If an alias were allowed to claim that match, the
    proof would say an operator vouched for a link the platform saw for itself, and the
    provenance would be weaker than the truth.
    """
    rows = fetch("TCS", {"TCS": ["wins a large deal"]})
    assert [row.headline for row in rows] == ["TCS wins a large deal"]
    assert rows[0].matched_alias is None


def test_a_symbols_own_aliases_do_not_leak_into_another_symbols_news():
    """Aliases are declared per symbol; one instrument's assertion is not another's."""
    aliases = {"INFY": ["Infosys"]}
    assert fetch("INFY", aliases) != ()
    assert fetch("WIPRO", aliases) == ()
    assert fetch("GEOPOLITICAL", aliases) != ()


@pytest.mark.parametrize(
    "payload",
    [
        {"INFY": ["IT"]},                      # two letters: ordinary prose, not a name
        {"INFY": ["a" * 41]},                  # longer than the instrument master allows
        {"INFY": ["500325"]},                  # a bare number is a date or a price too
        # Nine distinct aliases: only the cap can reject this, not the duplicate check.
        {"INFY": [f"Infosys {i}" for i in range(9)]},
        {f"SYM{i}": ["Something"] for i in range(33)},  # more symbols than the map allows
        {"INFY": []},                          # declares nothing; almost certainly a typo
        {"INFY": "Infosys"},                   # a bare string would match letter by letter
        {"INFY": ["Infosys", "INFOSYS"]},      # the same assertion twice
        {"INFY": ["INFY"]},                    # the symbol already matches itself
        {"INFY": ["Infosys;sentiment=1"]},     # would forge a field in the evidence block
        {"INFY": ["Infosys*"]},                # not a name; regex metacharacters excluded
        {"GEOPOLITICAL": ["War"]},             # the catch-all subject matches everything
        {"INFY": [7]},                         # not a string at all
        {"": ["Infosys"]},                     # no symbol to attach the assertion to
        ["INFY", "Infosys"],                   # not a mapping
    ],
)
def test_an_alias_outside_the_bounds_is_refused_and_not_quietly_dropped(payload):
    """Every rejected shape, refused loudly, because silence looks like "no news".

    A dropped alias and an instrument with nothing written about it produce the same
    empty result, and the operator has no way to tell which one they are looking at. The
    same validation runs at the adapter, so a caller that skips the environment parser
    cannot hand the matcher something unbounded either.
    """
    with pytest.raises(ValueError):
        parse_symbol_aliases(payload)
    with pytest.raises(ValueError):
        RssNewsSentimentAdapter(Feed(), ("https://example.test/rss",), payload)


def test_a_malformed_environment_value_fails_instead_of_parsing_to_nothing(monkeypatch):
    """Invalid JSON in .env must stop the boot, not degrade to the old broken filter."""
    monkeypatch.setenv(SYMBOL_ALIASES_ENV, "{not json")
    with pytest.raises(ValueError):
        symbol_aliases_from_env()
    monkeypatch.setenv(SYMBOL_ALIASES_ENV, '{"INFY": ["IT"]}')
    with pytest.raises(ValueError):
        symbol_aliases_from_env()


def test_the_stored_alias_is_the_normalised_one_the_matcher_actually_used():
    """The proof must show what matched, not what was typed.

    Case and spacing are normalised before matching, so the recorded alias is normalised
    too; a reader comparing the proof against the configuration sees the same string the
    matcher compared.
    """
    assert parse_symbol_aliases({" infy ": ["  Gold   ETF "]}) == {"INFY": ("GOLD ETF",)}
    rows = fetch("GOLDBEES", {"goldbees": ["  gold   etf  "]})
    assert rows[0].matched_alias == "GOLD ETF"


def test_the_alias_survives_the_hand_off_from_the_provider_to_the_evidence():
    """The adapter recording it is worth nothing if the pipeline drops it on the way.

    Between the RSS adapter and the proof the signal is re-scored and re-rendered into the
    bounded evidence contract. That is where a provenance field is easiest to lose, and
    losing it would leave an asserted headline looking like an observed one.
    """
    fresh = FreshnessResult(FreshnessState.FRESH, 0, 900, Decimal(1))
    score = HeadlineScore(Decimal("0.3"), "keyword", "counted")
    asserted = NewsSignal("INFY", "Infosys Q2 results beat estimates", Decimal("0.3"),
                          "rss-news", NOW, "INFOSYS")
    observed = NewsSignal("TCS", "TCS wins a large deal", Decimal("0.3"), "rss-news", NOW)
    context = SwarmMarketAnalysisPipeline._evidence_context(
        (), {}, ((asserted, score), (observed, score)),
        MacroSnapshot({}, NOW), FundamentalSnapshot("INFY", {}, NOW),
        PipelineFreshness(fresh, fresh, fresh, fresh),
    )
    assert [item.matched_alias for item in context.headlines] == ["INFOSYS", ""]


def test_an_alias_match_is_visible_on_the_proof_and_a_symbol_match_reads_as_before():
    """A proof reader has to be able to tell an asserted link from an observed one.

    The alias travels from the signal into the rendered evidence line and into the
    headline provenance block on the proof. A headline that named its instrument carries
    no such field, so the proof of an install with no aliases is byte-for-byte what it
    was and the presence of the field always means somebody asserted something.
    """
    asserted = EvidenceHeadline("INFY", "Infosys Q2 results beat estimates", Decimal("0.3"),
                                NOW.isoformat(), "rss-news", "keyword", "counted", "INFOSYS")
    observed = EvidenceHeadline("TCS", "TCS wins a large deal", Decimal("0.3"),
                                NOW.isoformat(), "rss-news", "keyword", "counted")

    assert "matched_alias=INFOSYS;" in _headline_line(asserted)
    assert "matched_alias" not in _headline_line(observed)

    proof = _headline_provenance(EvidenceContext(headlines=(asserted, observed)))
    assert proof["headlines"][0]["matched_alias"] == "INFOSYS"
    assert "matched_alias" not in proof["headlines"][1]


def test_an_alias_cannot_forge_another_field_of_the_evidence_it_appears_in():
    """The rendered evidence is ';'-delimited 'key=value', so the alias must hold neither.

    The configuration parser already refuses those characters; the contract refuses them
    again at the point of rendering, because that is where a forged 'sentiment=' would be
    read by the consensus as a field the platform computed.
    """
    for forged in ("INFOSYS;sentiment=1", "INFOSYS=1", "INFOSYS\nsentiment=1", "A" * 41):
        with pytest.raises(ValueError):
            EvidenceHeadline("INFY", "text", Decimal("0.3"), NOW.isoformat(), "rss-news",
                             "keyword", "counted", forged)


def test_the_daemon_hands_the_configured_aliases_to_the_adapter_it_registers(monkeypatch):
    """The feature is only real if the boot path reads the variable and passes it on.

    An alias map that parses but never reaches the adapter is the same silent nothing the
    operator was already getting, so the wiring is asserted on the registered provider.
    """
    monkeypatch.setenv("PRAMANA_NEWS_RSS_URLS", "https://example.test/rss")
    monkeypatch.setenv(SYMBOL_ALIASES_ENV, '{"INFY": ["Infosys"]}')
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    news, _, _ = _env_intelligence_providers()
    registered = news.registry._providers[ProviderCategory.NEWS]
    assert [item.symbol_aliases for item in registered] == [{"INFY": ("INFOSYS",)}]


def test_a_bad_alias_map_stops_the_boot_even_with_no_feed_configured(monkeypatch):
    """Validation runs before the feed check, so the mistake surfaces where it was made.

    Deferring it until a feed exists would let an operator configure aliases, see no error
    and get no news, with nothing anywhere saying the map was never read.
    """
    monkeypatch.delenv("PRAMANA_NEWS_RSS_URLS", raising=False)
    monkeypatch.setenv("PRAMANA_FUNDAMENTALS_PROVIDER", "none")
    monkeypatch.setenv(SYMBOL_ALIASES_ENV, '{"INFY": ["IT"]}')
    with pytest.raises(ValueError):
        _env_intelligence_providers()


def test_an_aliased_headline_still_has_to_be_real_news():
    """Aliases widen who a story is about; they do not relax anything else.

    A story with no publication time, an unparseable one or a time in the future is not
    fresh evidence, and reaching the instrument through an alias must not be a way around
    the check that establishes that.
    """
    feed = (
        "<rss><channel>"
        "<item><title>Infosys undated</title></item>"
        "<item><title>Infosys bad date</title><pubDate>garbage</pubDate></item>"
        "<item><title>Infosys tomorrow</title>"
        "<pubDate>Wed, 16 Sep 2026 09:00:00 GMT</pubDate></item>"
        "<item><title>Infosys Q2 results beat estimates</title>"
        f"<pubDate>{PUBLISHED}</pubDate></item>"
        "</channel></rss>"
    )

    class OneDocument:
        def get_text(self, *args, **kwargs):
            return feed

    adapter = RssNewsSentimentAdapter(
        OneDocument(), ("https://example.test/rss",), {"INFY": ["Infosys"]}
    )
    rows = adapter.fetch("INFY", NOW)
    assert [row.headline for row in rows] == ["Infosys Q2 results beat estimates"]
