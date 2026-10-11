"""Rule-based missing-knowledge detection.

Every gap carries the rule that produced it (`rule_id`) and the data it was
observed on (`source`), so each recommendation is traceable. Gap identifiers are
stable (`<RULE>:<target>`), which is what makes before/after comparison possible.
"""

from __future__ import annotations

from app.models import (
    CATEGORY_LABELS,
    Category,
    CategoryAssessment,
    Gap,
    Handover,
    KnowledgeItem,
    KnowledgeStatus,
    SEVERITY_ORDER,
    Severity,
)
from app.rules import Rules
from app.services.common import (
    OWNABLE_CATEGORIES,
    is_assigned,
    is_meaningful,
    is_open,
    is_overdue,
    is_resolved,
    is_urgent,
    meaningful_items,
    parse_date,
)

HEADER = "handover"

# One-line catalogue used by the documentation and the UI.
RULE_CATALOGUE: dict[str, str] = {
    "HEADER-TITLE": "The handover has no title.",
    "HEADER-OUTGOING": "The outgoing owner is not named.",
    "HEADER-RECEIVER": "The receiving owner is not named.",
    "HEADER-DATE": "The target handover date is missing or not a valid date.",
    "HEADER-SAME-OWNER": "Outgoing and receiving owner are the same person.",
    "REQ-MISSING": "A knowledge category required for the scenario is missing.",
    "CONTRADICTION-NA": "A category is marked not applicable but contains items.",
    "UNVERIFIED": "A required category has items without an evidence reference.",
    "ESCALATION-MISSING": "The scenario requires an escalation contact and none is flagged.",
    "PLACEHOLDER": "An item contains an empty or placeholder value.",
    "OWNER-UNASSIGNED": "An open responsibility, work item or deadline has no owner.",
    "OWNER-OUTGOING": "An open item is still assigned to the outgoing owner.",
    "DEADLINE-NODATE": "A deadline has no date.",
    "DEADLINE-INVALID": "A deadline date cannot be read as YYYY-MM-DD.",
    "DEADLINE-OVERDUE": "An open deadline falls before the handover date.",
    "DATE-INVALID": "An item has a due date that cannot be read as YYYY-MM-DD.",
    "BLOCKER-CRITICAL": "A critical blocker is unresolved.",
    "RISK-UNMANAGED": "An open risk has no owner or no description of impact/mitigation.",
    "DEP-NOOWNER": "A dependency has no named owner.",
    "ACCESS-NODETAIL": "An access prerequisite does not say how to obtain it.",
    "QUESTION-OPEN": "An outstanding question is unanswered.",
}


class _Collector:
    def __init__(self, handover: Handover, rules: Rules):
        self.handover = handover
        self.rules = rules
        self.gaps: list[Gap] = []
        self.responsible = "" if rules.is_blank(handover.outgoing_owner) else handover.outgoing_owner

    def add(
        self,
        rule_id: str,
        severity: Severity,
        description: str,
        explanation: str,
        action: str,
        *,
        category: str = HEADER,
        item: KnowledgeItem | None = None,
    ) -> None:
        target = item.id if item else category
        if item:
            source = f"Rule {rule_id} observed on item '{item.id}' in '{category}'"
        elif category == HEADER:
            source = f"Rule {rule_id} observed on the handover details"
        else:
            source = f"Rule {rule_id} for category '{category}' (rules.json v{self.rules.version}, scenario '{self.handover.scenario.value}')"
        self.gaps.append(
            Gap(
                id=f"{rule_id}:{target}",
                rule_id=rule_id,
                category=category,
                item_id=item.id if item else "",
                description=description,
                severity=severity,
                explanation=explanation,
                recommended_action=action,
                responsible=self.responsible,
                source=source,
            )
        )


def analyze_gaps(handover: Handover, assessments: list[CategoryAssessment], rules: Rules) -> list[Gap]:
    out = _Collector(handover, rules)
    _header_rules(out)
    _category_rules(out, assessments)
    for item in handover.items:
        _item_rules(out, item)
    return sorted(out.gaps, key=lambda gap: (SEVERITY_ORDER[gap.severity], gap.id))


def _header_rules(out: _Collector) -> None:
    h, rules = out.handover, out.rules
    if rules.is_blank(h.title):
        out.add(
            "HEADER-TITLE", Severity.MEDIUM,
            "The handover has no title.",
            "Without a title the handover package cannot be identified or referenced later.",
            "Enter a short title describing what is being handed over.",
        )
    if rules.is_blank(h.outgoing_owner):
        out.add(
            "HEADER-OUTGOING", Severity.HIGH,
            "The outgoing owner is not named.",
            "Nobody is identified as the source of the knowledge, so clarification requests have no addressee.",
            "Enter the person who is handing over the responsibilities.",
        )
    if rules.is_blank(h.receiving_owner):
        out.add(
            "HEADER-RECEIVER", Severity.CRITICAL,
            "The receiving owner is not named.",
            "Responsibilities cannot transfer when nobody is identified to receive them.",
            "Enter the person who takes over before continuing the handover.",
        )
    elif not rules.is_blank(h.outgoing_owner) and h.receiving_owner.casefold() == h.outgoing_owner.casefold():
        out.add(
            "HEADER-SAME-OWNER", Severity.CRITICAL,
            "Outgoing and receiving owner are the same person.",
            "The record is contradictory: no ownership actually changes hands.",
            "Correct the outgoing or the receiving owner.",
        )
    if rules.is_blank(h.target_date):
        out.add(
            "HEADER-DATE", Severity.HIGH,
            "The target handover date is missing.",
            "Deadline urgency cannot be evaluated and the receiver does not know when ownership starts.",
            "Set the target handover date.",
        )
    elif parse_date(h.target_date) is None:
        out.add(
            "HEADER-DATE", Severity.HIGH,
            f"The target handover date '{h.target_date}' is not a valid date.",
            "Deadline urgency cannot be evaluated against an unreadable date.",
            "Enter the target handover date as YYYY-MM-DD.",
        )


def _category_rules(out: _Collector, assessments: list[CategoryAssessment]) -> None:
    h, rules = out.handover, out.rules
    scenario = rules.for_scenario(h.scenario)
    by_category = {a.category: a for a in assessments}

    for a in assessments:
        category = a.category.value
        if a.required and a.status == KnowledgeStatus.MISSING:
            out.add(
                "REQ-MISSING", scenario.required[a.category],
                f"No information provided for '{a.label}'.",
                f"'{a.label}' is required for a {scenario.label} handover. Without it the receiver "
                "must reconstruct this knowledge alone or work without it.",
                f"Add the relevant '{a.label}' entries, or mark the category not applicable with a reason.",
                category=category,
            )
        if a.required and a.status == KnowledgeStatus.UNVERIFIED:
            missing = a.item_count - a.verified_count
            out.add(
                "UNVERIFIED", Severity.LOW,
                f"{missing} of {a.item_count} entries in '{a.label}' have no evidence reference.",
                "The information is present but the receiver cannot check it against a document, ticket or record.",
                "Add an evidence reference (document, ticket, file or record name) to each entry.",
                category=category,
            )
        if a.item_count and not rules.is_blank(h.not_applicable.get(a.category, "")):
            out.add(
                "CONTRADICTION-NA", Severity.MEDIUM,
                f"'{a.label}' is marked not applicable but contains {a.item_count} entries.",
                "The record contradicts itself, so the receiver cannot tell whether the entries still apply.",
                "Remove the not-applicable reason or remove the entries.",
                category=category,
            )

    contacts = by_category[Category.CONTACTS]
    if scenario.escalation_contact_required and contacts.status in (KnowledgeStatus.UNVERIFIED, KnowledgeStatus.VERIFIED):
        has_escalation = any(i.escalation for i in meaningful_items(h, Category.CONTACTS, rules))
        if not has_escalation:
            out.add(
                "ESCALATION-MISSING", scenario.escalation_severity,
                "No escalation contact provided for urgent operational incidents.",
                "The receiving owner may not know whom to contact during an urgent failure.",
                "Flag an existing contact as the escalation contact, or add one, before accepting the handover.",
                category=Category.CONTACTS.value,
            )


def _item_rules(out: _Collector, item: KnowledgeItem) -> None:
    h, rules = out.handover, out.rules
    category = item.category.value
    label = CATEGORY_LABELS[item.category].lower()
    name = f"'{item.title}'"

    if not is_meaningful(item, rules):
        out.add(
            "PLACEHOLDER", Severity.MEDIUM,
            f"Entry {item.id} in {label} has an empty or placeholder title ('{item.title}').",
            "A placeholder is not knowledge: this entry does not count towards completeness.",
            "Replace the placeholder with the actual information or remove the entry.",
            category=category, item=item,
        )
        return
    if item.detail and rules.is_blank(item.detail):
        out.add(
            "PLACEHOLDER", Severity.MEDIUM,
            f"{name} has a placeholder description ('{item.detail}').",
            "The entry announces information that was never filled in.",
            "Replace the placeholder with the actual details.",
            category=category, item=item,
        )

    if item.category in OWNABLE_CATEGORIES and is_open(item):
        urgent_deadline = item.category == Category.DEADLINES and (is_urgent(item, h, rules) or is_overdue(item, h))
        severity = Severity.CRITICAL if (item.critical or urgent_deadline) else Severity.HIGH
        urgency = " The deadline falls within the urgent window around the handover date." if urgent_deadline else ""
        if rules.is_blank(item.owner):
            out.add(
                "OWNER-UNASSIGNED", severity,
                f"{name} has no owner.",
                "An open item without an owner is likely to be dropped after the handover." + urgency,
                "Assign a named owner on the receiving side.",
                category=category, item=item,
            )
        elif not is_assigned(item, h, rules):
            out.add(
                "OWNER-OUTGOING", severity,
                f"{name} is still assigned to the outgoing owner ({item.owner}).",
                "Ownership has not actually been transferred, so the item is uncovered once the outgoing owner is unavailable." + urgency,
                "Reassign the item to the receiving owner or another named person.",
                category=category, item=item,
            )

    if item.category == Category.DEADLINES and is_open(item):
        if rules.is_blank(item.due_date):
            out.add(
                "DEADLINE-NODATE", Severity.MEDIUM,
                f"Deadline {name} has no date.",
                "The receiver cannot plan or prioritise a deadline without knowing when it is due.",
                "Enter the due date as YYYY-MM-DD.",
                category=category, item=item,
            )
        elif parse_date(item.due_date) is None:
            out.add(
                "DEADLINE-INVALID", Severity.MEDIUM,
                f"Deadline {name} has an unreadable date ('{item.due_date}').",
                "The date cannot be evaluated, so its urgency is unknown.",
                "Enter the due date as YYYY-MM-DD.",
                category=category, item=item,
            )
        elif is_overdue(item, h):
            out.add(
                "DEADLINE-OVERDUE", Severity.HIGH,
                f"Deadline {name} ({item.due_date}) falls before the handover date and is still open.",
                "The receiver would inherit a deadline that has already passed.",
                "Confirm the current status: mark it completed or agree a new date.",
                category=category, item=item,
            )
    elif item.due_date and not rules.is_blank(item.due_date) and parse_date(item.due_date) is None:
        out.add(
            "DATE-INVALID", Severity.LOW,
            f"{name} has an unreadable due date ('{item.due_date}').",
            "The date cannot be evaluated or sorted.",
            "Enter the due date as YYYY-MM-DD or clear it.",
            category=category, item=item,
        )

    if item.category == Category.RISKS_BLOCKERS and not is_resolved(item, rules):
        unevidenced = " It is marked completed but has no resolution reference." if not is_open(item) else ""
        if item.critical:
            out.add(
                "BLOCKER-CRITICAL", Severity.CRITICAL,
                f"Critical blocker {name} is unresolved.",
                "A critical blocker prevents the receiver from operating safely. It blocks unconditional acceptance "
                "regardless of the numeric score." + unevidenced,
                "Resolve the blocker and record a resolution reference, or have the receiver accept it explicitly as an exception.",
                category=category, item=item,
            )
        elif rules.is_blank(item.owner) or rules.is_blank(item.detail):
            missing = " and ".join(
                part for part, absent in (("an owner", rules.is_blank(item.owner)), ("a description of impact or mitigation", rules.is_blank(item.detail))) if absent
            )
            out.add(
                "RISK-UNMANAGED", Severity.MEDIUM,
                f"Open risk {name} is missing {missing}.",
                "A risk that is listed but not managed gives the receiver a warning without a way to act on it." + unevidenced,
                f"Add {missing} for this risk.",
                category=category, item=item,
            )

    if item.category == Category.DEPENDENCIES and rules.is_blank(item.owner):
        out.add(
            "DEP-NOOWNER", Severity.HIGH if item.critical else Severity.MEDIUM,
            f"Dependency {name} has no named owner.",
            "If the dependency fails or changes, the receiver does not know who is responsible for it.",
            "Name the team or person who owns this dependency.",
            category=category, item=item,
        )

    if item.category == Category.ACCESS and rules.is_blank(item.detail):
        out.add(
            "ACCESS-NODETAIL", Severity.MEDIUM,
            f"Access prerequisite {name} does not say how to obtain it.",
            "The receiver knows access is needed but not how to request it, which delays the first day of work.",
            "Describe how the access is requested and who grants it. Never record passwords or keys.",
            category=category, item=item,
        )

    if item.category == Category.OPEN_QUESTIONS and not is_resolved(item, rules):
        unevidenced = " It is marked completed but the answer is not recorded." if not is_open(item) else ""
        out.add(
            "QUESTION-OPEN", Severity.HIGH if item.critical else Severity.MEDIUM,
            f"Outstanding question {name} is unanswered.",
            "The receiver would take ownership with a known open question." + unevidenced,
            "Record the answer (or where it is documented) and mark the question completed.",
            category=category, item=item,
        )
