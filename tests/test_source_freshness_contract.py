from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, IndianEquitiesAgent
from quant_ai.domain.models import AssetClass, Market
from quant_ai.intelligence.freshness import DataCategory, FreshnessState, FreshnessValidator
from quant_ai.intelligence.pipeline import PipelineFreshness
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline as Pipeline

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def states(*, price=10, news=10, fundamentals=10):
    validator = FreshnessValidator()
    def result(category, seconds):
        stamp = None if seconds is None else NOW - timedelta(seconds=seconds)
        return validator.validate(category, stamp, NOW)
    return PipelineFreshness(
        result(DataCategory.PRICE, price), result(DataCategory.NEWS, news),
        result(DataCategory.MACRO, 10), result(DataCategory.FUNDAMENTAL, fundamentals),
    )


def vote(source_states):
    return IndianEquitiesAgent().analyze(AgentAnalysisRequest(
        'TEST', Market.INDIA, AssetClass.EQUITY, NOW,
        {'pe': Decimal(12), 'fcf_yield': Decimal('0.05'), 'debt_equity': Decimal('0.2'),
         'revenue_growth': Decimal('0.2'), 'operating_margin': Decimal('0.2'),
         'equity_news_sentiment': Decimal('0.8'),
         'freshness_multiplier': Pipeline._required_freshness('indian-equities', source_states),
         'freshness_diagnostic': Pipeline._freshness_diagnostic('indian-equities', source_states)},
        Pipeline._required_freshness_age('indian-equities', source_states),
    ))


def decide(item):
    others = tuple(AgentEvidence(
        agent, domain, 'TEST', Stance.BUY, Decimal('0.9'), Decimal('0.03'),
        Decimal('0.01'), ('test',), NOW, 10,
    ) for agent, domain in [('technical', AgentDomain.TECHNICAL), ('news', AgentDomain.NEWS)])
    return AtlasInvestmentAgent().decide('TEST', (*others, item), NOW)


@pytest.mark.parametrize('category', list(DataCategory))
def test_future_source_never_fresh(category):
    result = FreshnessValidator().validate(category, NOW + timedelta(microseconds=1), NOW)
    assert result.state != FreshnessState.FRESH
    assert result.confidence_multiplier == 0


@pytest.mark.parametrize('category', list(DataCategory))
def test_exact_ttl_and_fractional_expiry(category):
    ttl = FreshnessValidator.TTL[category]
    validator = FreshnessValidator()
    assert validator.validate(category, NOW - timedelta(seconds=ttl), NOW).state == FreshnessState.FRESH
    assert validator.validate(category, NOW - timedelta(seconds=ttl, microseconds=1), NOW).state == FreshnessState.STALE


@pytest.mark.parametrize('agent,source,ttl', [
    ('technical-quant-mas', 'price', 60), ('risk-desk', 'price', 60),
    ('liquidity-desk', 'price', 60), ('indian-equities', 'news', 1800),
    ('geopolitical-analyst', 'news', 1800), ('us-equities', 'news', 1800),
])
def test_fast_source_expiry_is_fail_closed(agent, source, ttl):
    assert Pipeline._required_freshness(agent, states(**{source: ttl})) == 1
    assert Pipeline._required_freshness(agent, states(**{source: ttl + 0.000001})) == 0


@pytest.mark.parametrize('news,fundamentals', [(None, 10), (10, None), (-1, 10), (10, -1), (1801, 10)])
def test_invalid_source_silences_vote_and_atlas(news, fundamentals):
    item = vote(states(news=news, fundamentals=fundamentals))
    assert item.stance == Stance.NEUTRAL
    assert item.confidence == 0
    assert 'insufficient_usable_agent_coverage' in decide(item).rationale


def test_mixed_ages_preserve_stricter_atlas_policy_and_provenance():
    source_states = states(news=20, fundamentals=7200)
    assert Pipeline._required_freshness('indian-equities', source_states) == 1
    item = vote(source_states)
    assert item.stance in {Stance.BUY, Stance.STRONG_BUY}
    assert item.source_freshness_seconds == 7200
    assert 'stale_specialist_evidence' in decide(item).rationale
    assert any('fundamentals=FRESH(age_seconds=7200,ttl_seconds=86400)' in note for note in item.rationale)


def test_fresh_inputs_and_atlas_exact_hour_boundary():
    item = vote(states(news=20, fundamentals=3600))
    assert decide(item).action in {Stance.BUY, Stance.STRONG_BUY}
    assert 'stale_specialist_evidence' in decide(replace(item, source_freshness_seconds=3601)).rationale


def test_missing_provenance_does_not_claim_fresh():
    item = vote(states(fundamentals=None))
    assert any('fundamentals=MISSING(age_seconds=unknown,ttl_seconds=86400)' in note for note in item.rationale)


@pytest.mark.parametrize('asset_class', list(AssetClass))
@pytest.mark.parametrize('age', [None, -0.000001, 60.000001, 1800])
def test_bad_price_never_votes_in_any_asset_class(asset_class, age):
    from quant_ai.agents.swarm import TechnicalQuantAgent
    source_states = states(price=age)
    agent = TechnicalQuantAgent()
    item = agent.analyze(AgentAnalysisRequest(
        'TEST', Market.INDIA, asset_class, NOW,
        {'momentum': Decimal('0.2'), 'sma_spread': Decimal('0.1'),
         'freshness_multiplier': Pipeline._required_freshness(agent.agent_id, source_states),
         'freshness_diagnostic': Pipeline._freshness_diagnostic(agent.agent_id, source_states)},
        Pipeline._required_freshness_age(agent.agent_id, source_states),
    ))
    assert item.stance == Stance.NEUTRAL
    assert item.confidence == 0
    assert 'insufficient_usable_agent_coverage' in decide(item).rationale


@pytest.mark.parametrize('category', list(DataCategory))
def test_missing_age_is_unknown_and_naive_timestamp_rejected(category):
    validator = FreshnessValidator()
    missing = validator.validate(category, None, NOW)
    assert missing.state == FreshnessState.MISSING
    assert missing.age_seconds is None
    assert missing.confidence_multiplier == 0
    with pytest.raises(ValueError, match='timezone-aware'):
        validator.validate(category, NOW.replace(tzinfo=None), NOW)


def test_slow_source_ttls_and_penalties_unchanged():
    assert FreshnessValidator.TTL[DataCategory.FUNDAMENTAL] == 86400
    assert FreshnessValidator.TTL[DataCategory.MACRO] == 604800
    assert Pipeline._required_freshness('indian-equities', states(fundamentals=86401)) == Decimal('0.50')
