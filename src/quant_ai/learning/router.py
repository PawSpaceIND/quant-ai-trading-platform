"""Bounded, hash-verified knowledge context for Atlas.

Retrieved content is data, never an instruction surface. The access controller decides which
records were permitted and available at the decision time; this router verifies the bytes
match the retained hash and bounds what can enter one prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, KnowledgeItem, SourceGrant
from quant_ai.learning.knowledge import KnowledgeAccessController

MAX_KNOWLEDGE_ITEMS = 8
MAX_KNOWLEDGE_CONTENT_CHARS = 320
MAX_KNOWLEDGE_TOTAL_CHARS = 1600


@dataclass(frozen=True)
class KnowledgeRecord:
    item: KnowledgeItem
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.item, KnowledgeItem):
            raise TypeError("knowledge_record_item_required")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("knowledge_record_content_required")
        if any(char in self.content for char in "\r\n"):
            raise ValueError("knowledge_record_content_must_be_single_line")
        if len(self.content) > MAX_KNOWLEDGE_CONTENT_CHARS:
            raise ValueError("knowledge_record_content_too_long")
        if hashlib.sha256(self.content.encode()).hexdigest() != self.item.content_sha256:
            raise ValueError(f"knowledge_record_hash_mismatch:{self.item.item_id}")


CONTEXT_SCHEMA = "pramana.decision_knowledge.v2"
_UNSPECIFIED = object()


def _stamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("knowledge_selection_time_must_be_datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("knowledge_selection_time_must_be_timezone_aware")
    return value.astimezone(timezone.utc).isoformat()


def _payload(records, required_categories, grants, as_of) -> dict:
    return {
        "schema": CONTEXT_SCHEMA,
        "as_of": _stamp(as_of),
        "required_categories": [item.value for item in required_categories],
        "grants": [
            {"source_id": grant.source_id, "provider": grant.provider,
             "categories": sorted(item.value for item in grant.categories),
             "planes": sorted(item.value for item in grant.planes),
             "point_in_time": grant.point_in_time, "rights_status": grant.rights_status.value,
             "max_age_seconds": grant.max_age_seconds}
            for grant in sorted(grants, key=lambda item: item.source_id)
        ],
        "items": [
            {"item_id": record.item.item_id, "source_id": record.item.source_id,
             "category": record.item.category.value,
             "observed_at": _stamp(record.item.observed_at),
             "available_at": _stamp(record.item.available_at),
             "content_sha256": record.item.content_sha256, "reference": record.item.reference}
            for record in records
        ],
    }


def _selection_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode()).hexdigest()


def _checked_records(records) -> tuple[KnowledgeRecord, ...]:
    if not isinstance(records, (tuple, list)):
        raise TypeError("knowledge_records_must_be_ordered")
    if not records or len(records) > MAX_KNOWLEDGE_ITEMS:
        raise ValueError("decision_knowledge_context_record_count_invalid")
    checked = []
    for record in records:
        if not isinstance(record, KnowledgeRecord):
            raise TypeError("knowledge_record_contract_required")
        # Reconstruct the typed item and record, checking bytes even if an old instance
        # has been mutated. Frozen dataclasses do not authenticate their nested inputs.
        checked.append(KnowledgeRecord(replace(record.item), record.content))
    if len({item.item.item_id for item in checked}) != len(checked):
        raise ValueError("duplicate_knowledge_item")
    if sum(len(item.content) for item in checked) > MAX_KNOWLEDGE_TOTAL_CHARS:
        raise ValueError("decision_knowledge_context_too_large")
    return tuple(checked)


@dataclass(frozen=True)
class DecisionKnowledgeContext:
    records: tuple[KnowledgeRecord, ...]
    required_categories: tuple[KnowledgeCategory, ...]
    selection_sha256: str
    # Legacy constructors cannot acquire a decision-time/policy binding by inference.
    # They remain representable but are unusable for prompts or decisions.
    as_of: datetime | None = None
    grants: tuple[SourceGrant, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", _checked_records(self.records))
        if (not isinstance(self.required_categories, (tuple, list))
                or any(not isinstance(item, KnowledgeCategory) for item in self.required_categories)):
            raise TypeError("knowledge_required_categories_must_use_declared_enum")
        object.__setattr__(self, "required_categories", tuple(self.required_categories))
        if len(set(self.required_categories)) != len(self.required_categories):
            raise ValueError("knowledge_required_categories_duplicate")
        if not isinstance(self.grants, (tuple, list)) or any(not isinstance(g, SourceGrant) for g in self.grants):
            raise TypeError("knowledge_context_grants_required")
        object.__setattr__(self, "grants", tuple(replace(g) for g in self.grants))
        self._validate_shape()
        if self.as_of is not None:
            _stamp(self.as_of)
            object.__setattr__(self, "as_of", self.as_of.astimezone(timezone.utc))
            self.validate()
        elif self.grants:
            raise ValueError("knowledge_selection_binding_missing")

    def _validate_shape(self) -> None:
        if not isinstance(self.selection_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.selection_sha256):
            raise ValueError("decision_knowledge_context_digest_invalid")
        _checked_records(self.records)
        if (not isinstance(self.required_categories, tuple)
                or any(not isinstance(item, KnowledgeCategory) for item in self.required_categories)
                or len(set(self.required_categories)) != len(self.required_categories)):
            raise ValueError("knowledge_required_categories_invalid")
        if not isinstance(self.grants, tuple) or any(not isinstance(g, SourceGrant) for g in self.grants):
            raise ValueError("knowledge_context_grants_invalid")
        for grant in self.grants:
            replace(grant)
        present = {record.item.category for record in self.records}
        if set(self.required_categories) - present:
            raise ValueError("decision_knowledge_context_missing_required")

    def validate(self, *, as_of: datetime | object = _UNSPECIFIED) -> None:
        """Check retained declarations and exact decision time, not provider authenticity.

        A selection is for one decision instant. Even a still-fresh bundle must be selected
        again for a later decision; changing only the timestamp/hash is not a refresh API.
        """
        self._validate_shape()
        if self.as_of is None or not self.grants:
            raise ValueError("knowledge_selection_binding_missing")
        if as_of is not _UNSPECIFIED and _stamp(as_of) != _stamp(self.as_of):
            raise ValueError("knowledge_selection_decision_time_mismatch")
        payload = _payload(self.records, self.required_categories, self.grants, self.as_of)
        if _selection_digest(payload) != self.selection_sha256:
            raise ValueError("knowledge_selection_digest_mismatch")
        sources = {record.item.source_id for record in self.records}
        if {grant.source_id for grant in self.grants} != sources:
            raise ValueError("knowledge_selection_grant_scope_mismatch")
        access = KnowledgeAccessController(self.grants, required_decision_categories=self.required_categories)
        selection = access.assert_decision_ready(tuple(record.item for record in self.records), as_of=self.as_of)
        if selection.rejections or len(selection.items) != len(self.records):
            raise ValueError("knowledge_selection_policy_rejected")

    def selection_payload(self) -> dict:
        self.validate()
        return _payload(self.records, self.required_categories, self.grants, self.as_of)

    def provenance(self) -> dict[str, object]:
        return {"selection_sha256": self.selection_sha256, **self.selection_payload()}

    def prompt_lines(self) -> tuple[str, ...]:
        """Validate before rendering; metadata and retrieved content remain quoted data."""
        self.validate()
        return tuple(
            "knowledge="
            f"category={record.item.category.value};source={json.dumps(record.item.source_id, ensure_ascii=True)};"
            f"available_at={_stamp(record.item.available_at)};"
            f"sha256={record.item.content_sha256};reference={json.dumps(record.item.reference, ensure_ascii=True)};"
            f"content={json.dumps(record.content, ensure_ascii=True)}"
            for record in self.records
        )


class KnowledgeRouter:
    def __init__(self, access: KnowledgeAccessController) -> None:
        self.access = access

    def decision_context(
        self,
        records: tuple[KnowledgeRecord, ...],
        *,
        as_of: datetime,
    ) -> DecisionKnowledgeContext:
        records = _checked_records(records)
        # Verify actual bytes again at selection, not just when objects were constructed.
        selection = self.access.assert_decision_ready(
            tuple(record.item for record in records), as_of=as_of
        )
        accepted_ids = {item.item_id for item in selection.items}
        accepted = tuple(record for record in records if record.item.item_id in accepted_ids)
        if not accepted:
            raise ValueError("knowledge_selection_has_no_accepted_items")
        required = self.access.required_decision_categories
        grants = tuple(self.access.grants[source] for source in sorted({record.item.source_id for record in accepted}))
        payload = _payload(accepted, required, grants, as_of)
        return DecisionKnowledgeContext(accepted, required, _selection_digest(payload), as_of, grants)


    def research_items(
        self,
        records: tuple[KnowledgeRecord, ...],
        *,
        as_of: datetime,
    ) -> tuple[KnowledgeRecord, ...]:
        selection = self.access.select(
            tuple(record.item for record in records), plane=AccessPlane.RESEARCH, as_of=as_of
        )
        accepted = {item.item_id for item in selection.items}
        return tuple(record for record in records if record.item.item_id in accepted)
