"""Synthetic decision-input integrity checks; no market skill or access grant implied."""
import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_atlas_governed_knowledge import NOW, context, evidence

from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import Stance
from quant_ai.learning.contracts import KnowledgeCategory, RightsStatus
from quant_ai.learning.router import DecisionKnowledgeContext, KnowledgeRecord, KnowledgeRouter


def agent(required=True, client=None):
    return AtlasInvestmentAgent(policy=AtlasPolicy(require_governed_knowledge=required), llm_client=client)


@pytest.mark.parametrize("delta", [timedelta(days=-1), timedelta(seconds=1), timedelta(days=3)])
def test_context_cannot_be_replayed_at_another_decision_instant(delta):
    result = agent().decide("INFY", evidence(), NOW + delta, knowledge_context=context())
    assert result.action is Stance.NEUTRAL
    assert "governed_knowledge_invalid" in result.rationale
    assert result.provenance["governed_knowledge"] is None


@pytest.mark.parametrize("required", [True, False])
def test_explicit_invalid_context_is_not_ignored_by_optional_policy(required):
    good = context()
    legacy = DecisionKnowledgeContext(good.records, good.required_categories, "b" * 64)
    result = agent(required).decide("INFY", evidence(), NOW, knowledge_context=legacy)
    assert result.action is Stance.NEUTRAL
    assert "governed_knowledge_invalid" in result.rationale


@pytest.mark.parametrize("kind", ["replayed", "legacy", "wrong_object"])
def test_invalid_knowledge_never_calls_model(kind):
    good = context()
    supplied = (DecisionKnowledgeContext(good.records, good.required_categories, "b" * 64)
                if kind == "legacy" else SimpleNamespace() if kind == "wrong_object" else good)
    at = NOW + timedelta(days=2) if kind == "replayed" else NOW
    client = SimpleNamespace(generate_trading_consensus=AsyncMock(side_effect=AssertionError("must not call")))
    result = asyncio.run(agent(client=client).decide_with_llm("INFY", evidence(), at, knowledge_context=supplied))
    assert result.action is Stance.NEUTRAL
    client.generate_trading_consensus.assert_not_awaited()


def test_same_utc_instant_remains_usable_and_provenance_recomputes():
    good = context()
    at = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    result = agent().decide("INFY", evidence(), at, knowledge_context=good)
    assert result.action is Stance.BUY
    payload = good.selection_payload()
    import json
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    assert digest == good.selection_sha256
    assert good.provenance()["as_of"] == NOW.isoformat()
    assert payload["items"][0]["observed_at"] == good.records[0].item.observed_at.isoformat()
    assert payload["grants"][0]["rights_status"] == "VERIFIED"


@pytest.mark.parametrize("digest", ["z" * 64, "", "a" * 63, "a" * 65, None, True])
def test_context_digest_must_be_hex_sha256(digest):
    with pytest.raises((TypeError, ValueError), match="knowledge_"):
        replace(context(), selection_sha256=digest)


@pytest.mark.parametrize("field", ["reference", "observed_at", "content"])
def test_retained_selection_digest_binds_complete_item_metadata(field):
    good = context()
    record = good.records[0]
    if field == "reference":
        changed = replace(record, item=replace(record.item, reference="evidence://different"))
    elif field == "observed_at":
        changed = replace(record, item=replace(record.item, observed_at=record.item.observed_at-timedelta(seconds=1)))
    else:
        text = "other independently hashed content"
        changed = KnowledgeRecord(replace(record.item, content_sha256=hashlib.sha256(text.encode()).hexdigest()), text)
    with pytest.raises(ValueError, match="knowledge_selection_digest_mismatch"):
        replace(good, records=(changed,))


@pytest.mark.parametrize("field,value", [("provider", "other"), ("max_age_seconds", 7200), ("rights_status", RightsStatus.INTERNAL)])
def test_retained_selection_digest_binds_grant_declarations(field, value):
    good = context()
    changed = replace(good.grants[0], **{field: value})
    with pytest.raises(ValueError, match="knowledge_selection_digest_mismatch"):
        replace(good, grants=(changed,))


def test_records_required_categories_and_grants_are_defensively_copied():
    good = context()
    records, required, grants = list(good.records), list(good.required_categories), list(good.grants)
    copy = replace(good, records=records, required_categories=required, grants=grants)
    records.clear(); required.clear(); grants.clear()
    assert copy == good
    assert agent().decide("INFY", evidence(), NOW, knowledge_context=copy).action is Stance.BUY


@pytest.mark.parametrize("changed", ["content", "required", "record_time", "grant"])
def test_use_time_revalidation_detects_post_construction_corruption(changed):
    good = context()
    if changed == "content":
        object.__setattr__(good.records[0], "content", "corrupted")
    elif changed == "required":
        object.__setattr__(good, "required_categories", ())
    elif changed == "record_time":
        object.__setattr__(good.records[0].item, "available_at", NOW+timedelta(days=1))
    else:
        object.__setattr__(good.grants[0], "rights_status", RightsStatus.UNVERIFIED)
    result = agent().decide("INFY", evidence(), NOW, knowledge_context=good)
    assert result.action is Stance.NEUTRAL
    assert "governed_knowledge_invalid" in result.rationale
    for method in (good.prompt_lines, good.provenance):
        with pytest.raises((TypeError, ValueError)):
            method()


def test_router_rechecks_content_bytes_not_just_preconstructed_item():
    from quant_ai.learning.knowledge import KnowledgeAccessController
    good = context()
    router = KnowledgeRouter(KnowledgeAccessController(good.grants, required_decision_categories=good.required_categories))
    object.__setattr__(good.records[0], "content", "mutated after item construction")
    with pytest.raises(ValueError, match="hash_mismatch"):
        router.decision_context(good.records, as_of=NOW)


def test_no_knowledge_legacy_optional_decision_is_unchanged():
    assert agent(False).decide("INFY", evidence(), NOW).action is Stance.BUY


def test_raw_required_category_cannot_bypass_context_validation():
    good = context()
    with pytest.raises((TypeError, ValueError), match="knowledge_"):
        replace(good, required_categories=[KnowledgeCategory.FUNDAMENTAL.value])


@pytest.mark.parametrize("at", [None, "2026-09-16", NOW.replace(tzinfo=None)])
def test_invalid_decision_clock_cannot_skip_context_validation(at):
    result = agent().decide("INFY", evidence(), at, knowledge_context=context())
    assert result.action is Stance.NEUTRAL
    assert "governed_knowledge_invalid" in result.rationale


@pytest.mark.parametrize("defect", ["future", "stale", "unverified_rights", "missing_source", "duplicate_grant"])
def test_rehashed_but_policy_invalid_selection_still_refuses(defect):
    import json

    from quant_ai.learning.router import _payload
    good = context()
    at, records, grants = good.as_of, good.records, good.grants
    if defect == "future":
        at = NOW - timedelta(days=1)
    elif defect == "stale":
        at = NOW + timedelta(days=3)
    elif defect == "unverified_rights":
        grants = (replace(grants[0], rights_status=RightsStatus.UNVERIFIED),)
    elif defect == "missing_source":
        grants = (replace(grants[0], source_id="different-source"),)
    else:
        grants = grants + grants
    raw = json.dumps(_payload(records, good.required_categories, grants, at),
                     sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    claimed = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises((TypeError, ValueError)):
        DecisionKnowledgeContext(records, good.required_categories, claimed, at, grants)


def test_equivalent_timezone_renderings_have_the_same_selection_hash():
    from quant_ai.learning.knowledge import KnowledgeAccessController
    good = context()
    zone = timezone(timedelta(hours=5, minutes=30))
    records = tuple(replace(record, item=replace(record.item,
        observed_at=record.item.observed_at.astimezone(zone),
        available_at=record.item.available_at.astimezone(zone))) for record in good.records)
    router = KnowledgeRouter(KnowledgeAccessController(good.grants,
        required_decision_categories=good.required_categories))
    again = router.decision_context(records, as_of=NOW.astimezone(zone))
    assert again.selection_sha256 == good.selection_sha256
    assert again.selection_payload() == good.selection_payload()
