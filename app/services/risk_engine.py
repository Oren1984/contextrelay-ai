"""Rule-based readiness scoring.

The score is a configurable heuristic (weights in config/rules.json), not a
statistical prediction. Blocking rules sit on top of the number: an unresolved
critical gap forces "High Risk" no matter how high the score is.
"""

from __future__ import annotations

from app.models import (
    Category,
    CategoryAssessment,
    DimensionScore,
    Gap,
    Handover,
    KnowledgeStatus,
    ReadinessStatus,
    Severity,
)
from app.rules import DIMENSIONS, Rules
from app.services.common import (
    OWNABLE_CATEGORIES,
    is_assigned,
    is_open,
    is_overdue,
    is_resolved,
    is_urgent,
    meaningful_items,
    parse_date,
)


def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


def _completeness(assessments: list[CategoryAssessment], rules: Rules) -> tuple[int, list[str]]:
    required = [a for a in assessments if a.required and a.status != KnowledgeStatus.NOT_APPLICABLE]
    excluded = [a for a in assessments if a.required and a.status == KnowledgeStatus.NOT_APPLICABLE]
    notes = []
    if excluded:
        notes.append(f"{len(excluded)} required categories marked not applicable are excluded, not counted as verified.")
    if not required:
        return 0, notes + ["No applicable required categories to assess."]
    credit = sum(rules.status_credit.get(a.status.value, 0.0) for a in required)
    counts = {s: sum(1 for a in required if a.status == s) for s in KnowledgeStatus}
    notes.insert(
        0,
        f"{len(required)} required categories: {counts[KnowledgeStatus.VERIFIED]} verified, "
        f"{counts[KnowledgeStatus.UNVERIFIED]} unverified, {counts[KnowledgeStatus.MISSING]} missing.",
    )
    return _pct(credit, len(required)), notes


def ownership_coverage(handover: Handover, rules: Rules) -> tuple[int, int]:
    """(assigned, total) over open responsibilities, work items and deadlines."""
    ownable = [
        item
        for category in OWNABLE_CATEGORIES
        for item in meaningful_items(handover, category, rules)
        if is_open(item)
    ]
    return sum(1 for item in ownable if is_assigned(item, handover, rules)), len(ownable)


def _ownership(handover: Handover, rules: Rules) -> tuple[int, list[str]]:
    receiver_ok = not rules.is_blank(handover.receiving_owner) and (
        handover.receiving_owner.casefold() != handover.outgoing_owner.casefold()
    )
    assigned, total = ownership_coverage(handover, rules)
    if not receiver_ok:
        return 0, ["No distinct receiving owner is named, so ownership cannot transfer."]
    if total == 0:
        return 0, ["No open responsibilities, work items or deadlines are recorded, so ownership cannot be demonstrated."]
    return _pct(assigned, total), [f"{assigned} of {total} open items have an owner other than the outgoing owner."]


def _dependencies(handover: Handover, assessments: dict[Category, CategoryAssessment], rules: Rules) -> tuple[int, list[str]]:
    items = meaningful_items(handover, Category.DEPENDENCIES, rules)
    if not items:
        if assessments[Category.DEPENDENCIES].status == KnowledgeStatus.NOT_APPLICABLE:
            return 100, ["Dependencies are explicitly marked not applicable."]
        return rules.unknown_dependency_score, ["No dependencies recorded: exposure is unknown rather than zero."]
    owned = sum(1 for item in items if not rules.is_blank(item.owner))
    return _pct(owned, len(items)), [f"{owned} of {len(items)} dependencies have a named owner."]


def _deadlines(handover: Handover, assessments: dict[Category, CategoryAssessment], rules: Rules) -> tuple[int, list[str]]:
    items = [i for i in meaningful_items(handover, Category.DEADLINES, rules) if is_open(i)]
    if not items:
        a = assessments[Category.DEADLINES]
        if a.required and a.status == KnowledgeStatus.MISSING:
            return 0, ["Deadlines are required for this scenario but none are recorded."]
        return 100, ["No open deadlines to cover."]
    earned = total = 0
    urgent_count = 0
    for item in items:
        pressing = is_urgent(item, handover, rules) or is_overdue(item, handover)
        weight = 2 if pressing else 1
        urgent_count += int(pressing)
        covered = parse_date(item.due_date) is not None and not is_overdue(item, handover) and is_assigned(item, handover, rules)
        total += weight
        earned += weight if covered else 0
    return _pct(earned, total), [
        f"{len(items)} open deadlines, {urgent_count} urgent or overdue (counted double).",
        "A deadline is covered when it has a valid future date and an assigned owner.",
    ]


def _blockers(handover: Handover, rules: Rules) -> tuple[int, list[str]]:
    items = [i for i in meaningful_items(handover, Category.RISKS_BLOCKERS, rules) if not is_resolved(i, rules)]
    critical = sum(1 for i in items if i.critical)
    unmanaged = sum(1 for i in items if not i.critical and (rules.is_blank(i.owner) or rules.is_blank(i.detail)))
    score = max(0, 100 - critical * rules.critical_blocker_penalty - unmanaged * rules.unmanaged_risk_penalty)
    return score, [
        f"{critical} unresolved critical blockers (-{rules.critical_blocker_penalty} each), "
        f"{unmanaged} unmanaged risks (-{rules.unmanaged_risk_penalty} each)."
    ]


def _critical_information(handover: Handover, assessments: dict[Category, CategoryAssessment], rules: Rules) -> tuple[int, list[str]]:
    scenario = rules.for_scenario(handover.scenario)
    checks: list[tuple[str, bool]] = []
    for category in scenario.critical_information:
        a = assessments[category]
        if a.status == KnowledgeStatus.NOT_APPLICABLE:
            continue
        checks.append((a.label, a.status != KnowledgeStatus.MISSING))
    if scenario.escalation_contact_required:
        has = any(i.escalation for i in meaningful_items(handover, Category.CONTACTS, rules))
        checks.append(("Escalation contact", has))
    if not checks:
        return 100, ["No critical information is configured for this scenario."]
    available = [name for name, ok in checks if ok]
    absent = [name for name, ok in checks if not ok]
    notes = [f"{len(available)} of {len(checks)} critical information checks satisfied."]
    if absent:
        notes.append("Not available: " + ", ".join(absent) + ".")
    return _pct(len(available), len(checks)), notes


def score_readiness(
    handover: Handover, assessments: list[CategoryAssessment], gaps: list[Gap], rules: Rules
) -> tuple[list[DimensionScore], int, ReadinessStatus, str, list[str]]:
    by_category = {a.category: a for a in assessments}
    raw = {
        "knowledge_completeness": _completeness(assessments, rules),
        "ownership_clarity": _ownership(handover, rules),
        "dependency_exposure": _dependencies(handover, by_category, rules),
        "deadline_sensitivity": _deadlines(handover, by_category, rules),
        "unresolved_blockers": _blockers(handover, rules),
        "critical_information": _critical_information(handover, by_category, rules),
    }
    dimensions = [
        DimensionScore(key=key, label=DIMENSIONS[key], weight=rules.weights[key], score=raw[key][0], notes=raw[key][1])
        for key in DIMENSIONS
    ]
    total_weight = sum(d.weight for d in dimensions)
    score = round(sum(d.weight * d.score for d in dimensions) / total_weight)

    blocking = [g.id for g in gaps if g.severity == Severity.CRITICAL]
    high = [g for g in gaps if g.severity == Severity.HIGH]

    if blocking:
        status = ReadinessStatus.HIGH_RISK
        reason = f"Blocked by {len(blocking)} unresolved critical gap(s), regardless of the score of {score}."
    elif score < rules.clarification_threshold:
        status = ReadinessStatus.HIGH_RISK
        reason = f"Score {score} is below the High Risk threshold of {rules.clarification_threshold}."
    elif high:
        status = ReadinessStatus.NEEDS_CLARIFICATION
        reason = f"{len(high)} high-severity gap(s) must be clarified before review, regardless of the score of {score}."
    elif score < rules.ready_threshold:
        status = ReadinessStatus.NEEDS_CLARIFICATION
        reason = f"Score {score} is below the Ready for Review threshold of {rules.ready_threshold}."
    else:
        status = ReadinessStatus.READY
        reason = f"Score {score} meets the threshold of {rules.ready_threshold} and no critical or high gaps are open."
    return dimensions, score, status, reason, blocking
