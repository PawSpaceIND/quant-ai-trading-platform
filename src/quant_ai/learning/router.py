"""Bounded, hash-verified knowledge context for Atlas.

Retrieved content is data, never an instruction surface. The access controller decides which
records were permitted and available at the decision time; this router verifies the bytes
match the retained hash and bounds what can enter one prompt.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, KnowledgeItem
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


@dataclass(frozen=True)
class DecisionKnowledgeContext:
    records: tuple[KnowledgeRecord, ...]
    required_categories: tuple[KnowledgeCategory, ...]
    selection_sha256: str

    def __post_init__(self) -> None:
        if not self.records:
            raise ValueError("decision_knowledge_context_empty")
        if len(self.records) > MAX_KNOWLEDGE_ITEMS:
            raise ValueError("decision_knowledge_context_too_many_items")
        if sum(len(item.content) for item in self.records) > MAX_KNOWLEDGE_TOTAL_CHARS:
            raise ValueError("decision_knowledge_context_too_large")
        if len(self.selection_sha256) != 64:
            raise ValueError("decision_knowledge_context_digest_invalid")
        present = {item.item.category for item in self.records}
        missing = set(self.required_categories) - present
        if missing:
            raise ValueError(
                "decision_knowledge_context_missing_required:"
                + ",".join(sorted(item.value for item in missing))
            )

    def provenance(self) -> dict[str, object]:
        return {
            "selection_sha256": self.selection_sha256,
            "required_categories": [item.value for item in self.required_categories],
            "items": [
                {
                    "item_id": record.item.item_id,
                    "source_id": record.item.source_id,
                    "category": record.item.category.value,
                    "observed_at": record.item.observed_at.isoformat(),
                    "available_at": record.item.available_at.isoformat(),
                    "content_sha256": record.item.content_sha256,
                    "reference": record.item.reference,
                }
                for record in self.records
            ],
        }

    def prompt_lines(self) -> tuple[str, ...]:
        """JSON-quote untrusted content so it stays one `content=` value on one data line."""
        return tuple(
            "knowledge="
            f"category={record.item.category.value};source={json.dumps(record.item.source_id, ensure_ascii=True)};"
            f"available_at={record.item.available_at.isoformat()};"
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
        if len(records) > MAX_KNOWLEDGE_ITEMS:
            raise ValueError("knowledge_selection_too_many_items")
        # Constructing KnowledgeRecord already verifies retained bytes against each item hash.
        selection = self.access.assert_decision_ready(
            tuple(record.item for record in records), as_of=as_of
        )
        accepted_ids = {item.item_id for item in selection.items}
        accepted = tuple(record for record in records if record.item.item_id in accepted_ids)
        if not accepted:
            raise ValueError("knowledge_selection_has_no_accepted_items")
        required = self.access.required_decision_categories
        payload = {
            "as_of": as_of.isoformat(),
            "required": [item.value for item in required],
            "items": [
                {
                    "item_id": record.item.item_id,
                    "source_id": record.item.source_id,
                    "category": record.item.category.value,
                    "content_sha256": record.item.content_sha256,
                    "reference": record.item.reference,
                    "available_at": record.item.available_at.isoformat(),
                }
                for record in accepted
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return DecisionKnowledgeContext(accepted, required, digest)

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
