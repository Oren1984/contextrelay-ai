"""Data contracts shared by the engines, storage, reports and the UI.

The input models are deliberately lenient about *business* completeness (an
empty owner or an unparseable deadline is accepted and later reported as a
gap) and strict about *structure* (unknown fields, unknown scenarios, duplicate
identifiers and oversized values are rejected at intake).
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ITEMS = 500
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class Scenario(str, Enum):
    EMPLOYEE_DEPARTURE = "employee_departure"
    FREELANCER_COMPLETION = "freelancer_completion"
    TEMPORARY_HANDOVER = "temporary_handover"


class Category(str, Enum):
    RESPONSIBILITIES = "responsibilities"
    ACTIVE_WORK = "active_work"
    DECISIONS = "decisions"
    DOCUMENTATION = "documentation"
    DEPENDENCIES = "dependencies"
    DEADLINES = "deadlines"
    RISKS_BLOCKERS = "risks_blockers"
    CONTACTS = "contacts"
    ACCESS = "access"
    OPEN_QUESTIONS = "open_questions"


CATEGORY_LABELS: dict[Category, str] = {
    Category.RESPONSIBILITIES: "Responsibilities",
    Category.ACTIVE_WORK: "Active work",
    Category.DECISIONS: "Important decisions",
    Category.DOCUMENTATION: "Documentation and resources",
    Category.DEPENDENCIES: "Dependencies",
    Category.DEADLINES: "Deadlines",
    Category.RISKS_BLOCKERS: "Risks and blockers",
    Category.CONTACTS: "Ownership and contacts",
    Category.ACCESS: "Access prerequisites",
    Category.OPEN_QUESTIONS: "Outstanding questions",
}

ID_PREFIXES: dict[Category, str] = {
    Category.RESPONSIBILITIES: "resp",
    Category.ACTIVE_WORK: "work",
    Category.DECISIONS: "dec",
    Category.DOCUMENTATION: "doc",
    Category.DEPENDENCIES: "dep",
    Category.DEADLINES: "dl",
    Category.RISKS_BLOCKERS: "risk",
    Category.CONTACTS: "con",
    Category.ACCESS: "acc",
    Category.OPEN_QUESTIONS: "q",
}


class ItemState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"


class KnowledgeItem(BaseModel):
    """One piece of handover knowledge, classified into a category."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    category: Category
    title: str = Field(default="", max_length=200)
    detail: str = Field(default="", max_length=2000)
    owner: str = Field(default="", max_length=120)
    due_date: str = Field(default="", max_length=40)
    state: ItemState = ItemState.OPEN
    critical: bool = False
    escalation: bool = False
    evidence: str = Field(default="", max_length=500)


class Handover(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    scenario: Scenario
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    outgoing_owner: str = Field(default="", max_length=120)
    receiving_owner: str = Field(default="", max_length=120)
    target_date: str = Field(default="", max_length=40)
    items: list[KnowledgeItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    not_applicable: dict[Category, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "Handover":
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.items:
            if item.id in seen and item.id not in duplicates:
                duplicates.append(item.id)
            seen.add(item.id)
        if duplicates:
            raise ValueError(f"Duplicate item identifiers: {', '.join(duplicates)}")
        for category, reason in self.not_applicable.items():
            if len(reason) > 500:
                raise ValueError(f"Not-applicable reason for '{category.value}' is too long")
        # Canonical order (by category, entry order kept within one) so that the
        # same content always produces the same revision hash.
        order = {category: index for index, category in enumerate(Category)}
        self.items.sort(key=lambda item: order[item.category])
        return self

    def items_in(self, category: Category) -> list[KnowledgeItem]:
        return [item for item in self.items if item.category == category]

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


class KnowledgeStatus(str, Enum):
    MISSING = "missing"
    UNVERIFIED = "present_unverified"
    VERIFIED = "verified"
    NOT_APPLICABLE = "not_applicable"


STATUS_LABELS: dict[KnowledgeStatus, str] = {
    KnowledgeStatus.MISSING: "Missing",
    KnowledgeStatus.UNVERIFIED: "Present but unverified",
    KnowledgeStatus.VERIFIED: "Verified by provided evidence",
    KnowledgeStatus.NOT_APPLICABLE: "Not applicable",
}


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


class CategoryAssessment(BaseModel):
    category: Category
    label: str
    required: bool
    status: KnowledgeStatus
    item_count: int = 0
    verified_count: int = 0
    reason: str = ""


class Gap(BaseModel):
    """An actionable finding. `rule_id` and `source` make it traceable."""

    id: str
    rule_id: str
    category: str
    item_id: str = ""
    description: str
    severity: Severity
    explanation: str
    recommended_action: str
    responsible: str = ""
    status: str = "open"
    source: str = ""


class DimensionScore(BaseModel):
    key: str
    label: str
    weight: float
    score: int
    notes: list[str] = Field(default_factory=list)


class ReadinessStatus(str, Enum):
    READY = "Ready for Review"
    NEEDS_CLARIFICATION = "Needs Clarification"
    HIGH_RISK = "High Risk"


class AnalysisResult(BaseModel):
    handover_id: str
    scenario: Scenario
    rules_version: str
    assessments: list[CategoryAssessment]
    gaps: list[Gap]
    dimensions: list[DimensionScore]
    readiness_score: int
    readiness_status: ReadinessStatus
    status_reason: str
    blocking_gap_ids: list[str] = Field(default_factory=list)
    ownership_coverage: float | None = None

    def gaps_by_severity(self, severity: Severity) -> list[Gap]:
        return [gap for gap in self.gaps if gap.severity == severity]

    def severity_counts(self) -> dict[str, int]:
        return {sev.value: len(self.gaps_by_severity(sev)) for sev in Severity}

    @property
    def critical_count(self) -> int:
        return len(self.gaps_by_severity(Severity.CRITICAL))


# --------------------------------------------------------------------------
# Receiver decisions
# --------------------------------------------------------------------------


class Decision(str, Enum):
    REQUEST_CLARIFICATION = "request_clarification"
    ACCEPT = "accept"
    ACCEPT_WITH_EXCEPTIONS = "accept_with_exceptions"


DECISION_STATUS: dict[Decision, str] = {
    Decision.REQUEST_CLARIFICATION: "Clarification Requested",
    Decision.ACCEPT: "Accepted",
    Decision.ACCEPT_WITH_EXCEPTIONS: "Accepted with Exceptions",
}

PENDING_REVIEW = "Pending Review"


class AcceptanceRecord(BaseModel):
    id: int | None = None
    handover_id: str
    revision: int
    receiver: str
    decision: Decision
    note: str = ""
    acknowledged_gap_ids: list[str] = Field(default_factory=list)
    readiness_score: int
    created_at: str

    @property
    def status(self) -> str:
        return DECISION_STATUS[self.decision]


class Insights(BaseModel):
    """Optional suggestions layered on top of deterministic findings."""

    provider: str
    label: str
    summary: str
    questions: list[str] = Field(default_factory=list)
    fallback_reason: str = ""
