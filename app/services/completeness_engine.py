"""Deterministic completeness engine: one knowledge status per category."""

from __future__ import annotations

from app.models import Category, CategoryAssessment, Handover, KnowledgeStatus
from app.rules import Rules
from app.services.common import meaningful_items


def assess_completeness(handover: Handover, rules: Rules) -> list[CategoryAssessment]:
    scenario = rules.for_scenario(handover.scenario)
    assessments = []
    for category in Category:
        items = meaningful_items(handover, category, rules)
        verified = [item for item in items if not rules.is_blank(item.evidence)]
        reason = handover.not_applicable.get(category, "").strip()

        if items:
            # Provided data always wins over a "not applicable" marker.
            status = KnowledgeStatus.VERIFIED if len(verified) == len(items) else KnowledgeStatus.UNVERIFIED
        elif not rules.is_blank(reason):
            # Not applicable needs an explicit reason and is never treated as verified.
            status = KnowledgeStatus.NOT_APPLICABLE
        else:
            status = KnowledgeStatus.MISSING

        assessments.append(
            CategoryAssessment(
                category=category,
                label=scenario.label_for(category),
                required=category in scenario.required,
                status=status,
                item_count=len(items),
                verified_count=len(verified),
                reason=reason if status == KnowledgeStatus.NOT_APPLICABLE else "",
            )
        )
    return assessments
