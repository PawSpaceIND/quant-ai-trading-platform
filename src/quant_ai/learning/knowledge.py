"""Point-in-time knowledge access for research, training and trading decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

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
        grants = tuple(grants)
        if any(not isinstance(grant, SourceGrant) for grant in grants):
            raise TypeError("knowledge_source_grant_required")
        if (not isinstance(required_decision_categories, (tuple, list))
                or any(not isinstance(category, KnowledgeCategory)
                       for category in required_decision_categories)):
            raise TypeError("knowledge_required_categories_must_use_declared_enum")
        by_id = {grant.source_id: grant for grant in grants}
        if len(by_id) != len(grants):
            raise ValueError("duplicate_knowledge_source")
        self._grants = MappingProxyType(by_id)
        self._required_decision_categories = tuple(dict.fromkeys(required_decision_categories))

    @property
    def grants(self):
        return self._grants

    @property
    def required_decision_categories(self) -> tuple[KnowledgeCategory, ...]:
        return self._required_decision_categories

    def select(
        self,
        items: tuple[KnowledgeItem, ...],
        *,
        plane: AccessPlane,
        as_of: datetime,
    ) -> KnowledgeSelection:
        # str-enums compare equal to raw strings, while `is` checks do not. Refuse
        # raw planes before membership checks can accidentally skip decision gates.
        if not isinstance(plane, AccessPlane):
            raise TypeError("knowledge_plane_must_use_declared_enum")
        if not isinstance(as_of, datetime):
            raise TypeError("knowledge_as_of_must_be_datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("knowledge_as_of_must_be_timezone_aware")
        now = as_of.astimezone(timezone.utc)
        accepted: list[KnowledgeItem] = []
        rejected: list[KnowledgeRejection] = []
        seen_ids: set[str] = set()
        for item in items:
            if not isinstance(item, KnowledgeItem):
                raise TypeError("knowledge_item_contract_required")
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
            if grant.point_in_time is not True:
                return "source_not_point_in_time"
            if grant.rights_status not in {RightsStatus.INTERNAL, RightsStatus.VERIFIED}:
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
