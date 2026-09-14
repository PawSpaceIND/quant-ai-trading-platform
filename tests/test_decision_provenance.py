import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.provenance import content_hash
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def payload():
    return {'stance':'BUY','confidence':.8,'expected_return':.03,'expected_risk':.02,
            'rationale':['synthetic provenance test'], 'xai_proof':{'summary':'synthetic', 'supporting_factors':[], 'risk_factors':[]}}


def response(body=None, model='synthetic-resolved-model', identifier='synthetic-response'):
    return SimpleNamespace(model=model,id=identifier,usage=SimpleNamespace(input_tokens=123,output_tokens=45),
                           content=[SimpleNamespace(type='tool_use',name='trading_consensus',input=payload() if body is None else body)])


def client(result=None, side_effect=None):
    create=AsyncMock(return_value=response() if result is None else result,side_effect=side_effect)
    return AnthropicSwarmClient(model='synthetic-requested-alias',client=SimpleNamespace(messages=SimpleNamespace(create=create))),create


def evidence(now):
    return tuple(AgentEvidence(f'synthetic-{i}',AgentDomain.TECHNICAL,'INFY',Stance.BUY,D('.8'),D('.03'),D('.02'),('synthetic',),now,0) for i in range(4))


def test_actual_request_hash_and_returned_identity_are_recorded_without_auth_material():
    llm,create=client()
    result=asyncio.run(llm.generate_trading_consensus('synthetic prompt'))
    p=result.provenance
    assert p['request'] == create.await_args.kwargs
    assert p['request_sha256'] == content_hash(create.await_args.kwargs)
    assert p['requested_model'] == 'synthetic-requested-alias'
    assert p['resolved_model'] == 'synthetic-resolved-model'
    assert p['response_id'] == 'synthetic-response'
    assert p['usage']['input_tokens'] == 123
    assert p['usage']['cache_read_input_tokens'] is None
    assert llm.parse_consensus(result)[1].model == 'synthetic-resolved-model'
    assert 'api_key' not in json.dumps(p)
    assert set(result) == set(payload())  # Metadata never enters the model's strict tool schema.


def test_concurrent_calls_keep_their_own_request_and_response_identity():
    async def scenario():
        second=asyncio.Event()
        async def create(**kwargs):
            prompt=kwargs['messages'][0]['content']
            if prompt=='first': await second.wait()
            else: second.set()
            return response(model='resolved-'+prompt,identifier='id-'+prompt)
        llm=AnthropicSwarmClient(model='alias',client=SimpleNamespace(messages=SimpleNamespace(create=create)))
        return await asyncio.gather(llm.generate_trading_consensus('first'),llm.generate_trading_consensus('second'))
    first,second=asyncio.run(scenario())
    assert first.provenance['response_id']=='id-first'
    assert second.provenance['response_id']=='id-second'
    assert first.provenance['request_sha256']!=second.provenance['request_sha256']
    assert first.provenance['request']['messages'][0]['content']=='first'


def test_timeout_is_an_attempt_with_no_claimed_provider_response():
    llm,_=client(side_effect=TimeoutError())
    now=datetime.now(timezone.utc)
    decision=asyncio.run(AtlasInvestmentAgent(llm_client=llm).decide_with_llm('INFY',evidence(now),now))
    assert decision.action==Stance.NEUTRAL
    assert decision.provenance['mode']=='llm_unavailable'
    attempt=decision.provenance['inference']
    assert attempt['status']=='unavailable' and attempt['resolved_model'] is None
    assert attempt['request_sha256']==content_hash(attempt['request'])


def test_invalid_schema_keeps_attempt_metadata_on_the_rejected_decision():
    llm,_=client(result=response(body={'bad':'schema'}))
    now=datetime.now(timezone.utc)
    decision=asyncio.run(AtlasInvestmentAgent(llm_client=llm).decide_with_llm('INFY',evidence(now),now))
    assert decision.action==Stance.NEUTRAL
    assert decision.provenance['mode']=='llm_invalid_schema'
    assert decision.provenance['inference']['status']=='invalid_schema'
    assert decision.provenance['inference']['response_id']=='synthetic-response'


def test_deterministic_inputs_and_configuration_change_have_distinct_fingerprints():
    now=datetime.now(timezone.utc)
    a=AtlasInvestmentAgent(founder_instructions='synthetic mandate A').decide('INFY',evidence(now),now)
    b=AtlasInvestmentAgent(founder_instructions='synthetic mandate B').decide('INFY',evidence(now),now)
    assert a.provenance['mode']=='deterministic' and a.provenance['inference'] is None
    assert a.provenance['inputs_sha256']==b.provenance['inputs_sha256']
    assert a.provenance['configuration_sha256']!=b.provenance['configuration_sha256']
    assert a.provenance['inputs_sha256']==content_hash(a.provenance['inputs'])


def test_request_provenance_reaches_the_atomic_fill_record(tmp_path):
    llm,_=client()
    broker=PaperBrokerService(tmp_path/'ledger.sqlite')
    now=datetime.now(timezone.utc)
    runtime=SwarmPaperTradingService(cio=AtlasCIOAgent(AtlasInvestmentAgent(llm_client=llm)),broker=broker)
    plan=CapitalGoalEngine().recommend(CapitalPlanRequest(D(100000),D('.8'),D('.2'),expected_edge=D('.02'),requested_mode=RiskMode.BALANCED))
    result=asyncio.run(runtime.execute_async(
        AgentAnalysisRequest('INFY',Market.INDIA,AssetClass.EQUITY,now,{}),evidence(now),plan,
        PortfolioSnapshot(D(100000),D(0),D(0)),quantity=10,reference_price=D(100),stop_price=D(95),take_profit_price=D(110),country='INDIA',tenant_id='pilot'))
    assert result.fill
    stored=json.loads(broker._connection.execute('SELECT payload FROM paper_decision_evidence').fetchone()[0])
    assert stored['provenance']==result.xai_trace.provenance
    assert stored['provenance']['inference']['resolved_model']=='synthetic-resolved-model'
    assert stored['provenance']['inference']['request_sha256']==content_hash(stored['provenance']['inference']['request'])


def test_missing_returned_model_is_not_replaced_by_the_requested_alias():
    sdk_response=response()
    del sdk_response.model
    sdk_response.usage=SimpleNamespace(input_tokens=True,output_tokens='45')
    llm,_=client(result=sdk_response)
    result=asyncio.run(llm.generate_trading_consensus('synthetic missing metadata'))
    assert result.provenance['resolved_model'] is None
    assert result.provenance['requested_model']=='synthetic-requested-alias'
    assert result.provenance['transport']=='injected_client'
    assert result.provenance['usage']['input_tokens'] is None
    assert result.provenance['usage']['output_tokens'] is None
