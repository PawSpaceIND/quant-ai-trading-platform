"""Point-in-time knowledge access for research, training and trading decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from quant_ai.learning.contracts import (
    AccessPlane,
    KnowledgeCategory,
    KnowledgeItem,
    RightsStatus,
    SourceGrant,
)


@dataclass(frozen=True)
class KnowledgeRejection:
    item_id: str
    reason: str


@dataclass(frozen=True)
class KnowledgeSelection:
    items: tuple[KnowledgeItem, ...]
    rejections: tuple[KnowledgeRejection, ...]
    missing_required: tuple[KnowledgeCategory, ...]

    @property
    def usable(self) -> bool:
        return not self.missing_required


class KnowledgeAccessController:
    """Select only evidence that was legally/temporally available for the requested plane.

    Research may inspect explicitly unverified-rights material, but it remains barred from
    training and decision prompts. Training/decision inputs must be point-in-time so a later
    revision cannot be mistaken for information the model could have known then.
    """

    def __init__(
        self,
        grants: tuple[SourceGrant, ...],
        *,
        required_decision_categories: tuple[KnowledgeCategory, ...] = (),
    ) -> None:
        by_id = {grant.source_id: grant for grant in grants}
        if len(by_id) != len(grants):
            raise ValueError("duplicate_knowledge_source")
        self.grants = by_id
        self.required_decision_categories = tuple(dict.fromkeys(required_decision_categories))

    def select(
        self,
        items: tuple[KnowledgeItem, ...],
        *,
        plane: AccessPlane,
        as_of: datetime,
    ) -> KnowledgeSelection:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("knowledge_as_of_must_be_timezone_aware")
        now = as_of.astimezone(timezone.utc)
        accepted: list[KnowledgeItem] = []
        rejected: list[KnowledgeRejection] = []
        seen_ids: set[str] = set()
        for item in items:
            if item.item_id in seen_ids:
                raise ValueError(f"duplicate_knowledge_item:{item.item_id}")
            seen_ids.add(item.item_id)
            grant = self.grants.get(item.source_id)
            reason = self._rejection(grant, item, plane, now)
            if reason:
                rejected.append(KnowledgeRejection(item.item_id, reason))
            else:
                accepted.append(item)
        required = self.required_decision_categories if plane is AccessPlane.DECISION else ()
        present = {item.category for item in accepted}
        missing = tuple(category for category in required if category not in present)
        return KnowledgeSelection(tuple(accepted), tuple(rejected), missing)

    @staticmethod
    def _rejection(
        grant: SourceGrant | None,
        item: KnowledgeItem,
        plane: AccessPlane,
        as_of,
    ) -> str | None:
        if grant is None:
            return "unknown_source"
        if item.category not in grant.categories:
            return "category_not_granted"
        if plane not in grant.planes:
            return "plane_not_granted"
        if item.available_at.astimezone(timezone.utc) > as_of:
            return "future_evidence"
        if plane in {AccessPlane.TRAINING, AccessPlane.DECISION}:
            if not grant.point_in_time:
                return "source_not_point_in_time"
            if grant.rights_status is RightsStatus.UNVERIFIED:
                return "source_rights_unverified"
        if plane is AccessPlane.DECISION and grant.max_age_seconds is not None:
            age = (as_of - item.observed_at.astimezone(timezone.utc)).total_seconds()
            if age < 0:
                return "future_observation"
            if age > grant.max_age_seconds:
                return "stale_evidence"
        return None

    def assert_decision_ready(
        self, items: tuple[KnowledgeItem, ...], *, as_of: datetime
    ) -> KnowledgeSelection:
        selection = self.select(items, plane=AccessPlane.DECISION, as_of=as_of)
        if selection.missing_required:
            names = ",".join(item.value for item in selection.missing_required)
            raise ValueError(f"ai_required_knowledge_missing:{names}")
        return selection
