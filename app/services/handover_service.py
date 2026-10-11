"""Application service: analysis pipeline, corrections, revisions and acceptance.

This is the only layer the Streamlit UI talks to, and it has no Streamlit
dependency, so the whole business workflow can be tested without a browser.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.models import (
    PENDING_REVIEW,
    AcceptanceRecord,
    AnalysisResult,
    Category,
    Decision,
    Gap,
    Handover,
    ReadinessStatus,
    Scenario,
    Severity,
)
from app.observability import METRICS, log_event
from app.rules import Rules
from app.services.completeness_engine import assess_completeness
from app.services.gap_analyzer import analyze_gaps
from app.services.intake import IntakeError, describe_validation_error, handover_from_dict, next_item_id
from app.services.risk_engine import ownership_coverage, score_readiness
from app.storage.repository import Repository, utc_now

SYNTHETIC_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"


class CorrectionError(ValueError):
    """A correction could not be applied. Safe to show to the user."""


class DecisionError(ValueError):
    """A receiver decision violates an acceptance safeguard. Safe to show."""


def analyze(handover: Handover, rules: Rules) -> AnalysisResult:
    """The deterministic pipeline: completeness -> gaps -> risk score."""
    assessments = assess_completeness(handover, rules)
    gaps = analyze_gaps(handover, assessments, rules)
    dimensions, score, status, reason, blocking = score_readiness(handover, assessments, gaps, rules)
    assigned, total = ownership_coverage(handover, rules)
    return AnalysisResult(
        handover_id=handover.id,
        scenario=handover.scenario,
        rules_version=rules.version,
        assessments=assessments,
        gaps=gaps,
        dimensions=dimensions,
        readiness_score=score,
        readiness_status=status,
        status_reason=reason,
        blocking_gap_ids=blocking,
        ownership_coverage=round(100 * assigned / total, 1) if total else None,
    )


def apply_corrections(handover: Handover, operations: list[dict]) -> Handover:
    """Return a corrected copy of the handover. The original is not modified.

    Supported operations: set_item, add_item, remove_item, move_item,
    set_header, set_not_applicable. The result is re-validated as a whole.
    """
    data = handover.model_dump(mode="json")
    items: list[dict] = data["items"]

    def find(item_id: str) -> dict:
        for item in items:
            if item["id"] == item_id:
                return item
        raise CorrectionError(f"Item '{item_id}' does not exist.")

    for op in operations:
        kind = op.get("op")
        if kind == "set_item":
            target = find(op["item_id"])
            fields = dict(op["fields"])
            if "id" in fields or "category" in fields:
                raise CorrectionError("Use move_item to change a category; identifiers are fixed.")
            target.update(fields)
        elif kind == "add_item":
            item = dict(op["item"])
            if not item.get("id"):
                try:
                    category = Category(item.get("category"))
                except ValueError as exc:
                    raise CorrectionError("New item has an unknown category.") from exc
                item["id"] = next_item_id({i["id"] for i in items}, category)
            items.append(item)
        elif kind == "remove_item":
            items.remove(find(op["item_id"]))
        elif kind == "move_item":
            find(op["item_id"])["category"] = op["category"]
        elif kind == "set_header":
            fields = dict(op["fields"])
            if set(fields) - {"title", "description", "scenario", "outgoing_owner", "receiving_owner", "target_date"}:
                raise CorrectionError("set_header only accepts handover detail fields.")
            data.update(fields)
        elif kind == "set_not_applicable":
            reason = (op.get("reason") or "").strip()
            if reason:
                data["not_applicable"][op["category"]] = reason
            else:
                data["not_applicable"].pop(op["category"], None)
        else:
            raise CorrectionError(f"Unknown correction operation '{kind}'.")
    try:
        return Handover.model_validate(data)
    except ValidationError as exc:
        raise CorrectionError(f"Correction rejected - {describe_validation_error(exc)}") from exc


@dataclass
class Comparison:
    """Before/after of two real engine evaluations."""

    score_before: int
    score_after: int
    status_before: str
    status_after: str
    resolved: list[Gap] = field(default_factory=list)
    remaining: list[Gap] = field(default_factory=list)
    introduced: list[Gap] = field(default_factory=list)
    severity_before: dict[str, int] = field(default_factory=dict)
    severity_after: dict[str, int] = field(default_factory=dict)

    @property
    def gaps_before(self) -> int:
        return len(self.resolved) + len(self.remaining)

    @property
    def gaps_after(self) -> int:
        return len(self.remaining) + len(self.introduced)

    @property
    def critical_before(self) -> int:
        return self.severity_before.get(Severity.CRITICAL.value, 0)

    @property
    def critical_after(self) -> int:
        return self.severity_after.get(Severity.CRITICAL.value, 0)


def compare(before: AnalysisResult, after: AnalysisResult) -> Comparison:
    after_ids = {gap.id for gap in after.gaps}
    before_ids = {gap.id for gap in before.gaps}
    return Comparison(
        score_before=before.readiness_score,
        score_after=after.readiness_score,
        status_before=before.readiness_status.value,
        status_after=after.readiness_status.value,
        resolved=[g.model_copy(update={"status": "resolved"}) for g in before.gaps if g.id not in after_ids],
        remaining=[g for g in after.gaps if g.id in before_ids],
        introduced=[g for g in after.gaps if g.id not in before_ids],
        severity_before=before.severity_counts(),
        severity_after=after.severity_counts(),
    )


@dataclass
class AcceptanceState:
    status: str
    revision: int
    current: AcceptanceRecord | None = None
    superseded: AcceptanceRecord | None = None


class HandoverService:
    def __init__(self, repository: Repository, rules: Rules):
        self.repository = repository
        self.rules = rules

    # -- analysis ------------------------------------------------------------

    def analyze(self, handover: Handover) -> AnalysisResult:
        started = time.perf_counter()
        try:
            result = analyze(handover, self.rules)
        except Exception as exc:
            METRICS.record_failure()
            log_event("analysis_failed", logging.ERROR, handover_id=handover.id, error=type(exc).__name__)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        METRICS.record_success(duration_ms, len(result.gaps))
        log_event(
            "analysis_completed",
            handover_id=handover.id,
            scenario=handover.scenario.value,
            duration_ms=f"{duration_ms:.2f}",
            gaps=len(result.gaps),
            critical=result.critical_count,
            score=result.readiness_score,
            status=result.readiness_status.name,
        )
        return result

    def save(self, handover: Handover) -> int:
        previous = self.repository.latest_revision(handover.id)
        revision = self.repository.save_handover(handover)
        if revision != previous:
            log_event("revision_saved", handover_id=handover.id, revision=revision)
        return revision

    def save_and_analyze(self, handover: Handover) -> tuple[int, AnalysisResult]:
        return self.save(handover), self.analyze(handover)

    def analyze_revision(self, handover_id: str, revision: int | None = None) -> tuple[Handover, int, AnalysisResult]:
        """Recompute the analysis of a stored revision (scores are never cached)."""
        handover, loaded_revision = self.repository.load_handover(handover_id, revision)
        return handover, loaded_revision, self.analyze(handover)

    def confirm_resolved(self, handover: Handover, gap_id: str) -> tuple[bool, AnalysisResult]:
        """A gap is resolved only if the engine no longer detects it in the data."""
        result = self.analyze(handover)
        return all(gap.id != gap_id for gap in result.gaps), result

    # -- receiver decisions --------------------------------------------------

    def record_decision(
        self,
        handover_id: str,
        receiver: str,
        decision: Decision,
        note: str = "",
        acknowledged_gap_ids: list[str] | None = None,
    ) -> AcceptanceRecord:
        receiver, note = receiver.strip(), note.strip()
        acknowledged = sorted(set(acknowledged_gap_ids or []))
        if not receiver:
            raise DecisionError("Enter the receiver label before recording a decision.")
        if len(receiver) > 120 or len(note) > 2000:
            raise DecisionError("Receiver label or note is too long.")

        _, revision, result = self.analyze_revision(handover_id)
        open_ids = {gap.id for gap in result.gaps}
        must_acknowledge = {g.id for g in result.gaps if g.severity in (Severity.CRITICAL, Severity.HIGH)}

        if decision == Decision.ACCEPT:
            if result.blocking_gap_ids:
                raise DecisionError(
                    f"Unconditional acceptance is not possible: {len(result.blocking_gap_ids)} critical gap(s) "
                    "are unresolved. Resolve them, request clarification, or accept with exceptions."
                )
            if result.readiness_status != ReadinessStatus.READY:
                raise DecisionError(
                    f"Unconditional acceptance requires the status 'Ready for Review' "
                    f"(currently '{result.readiness_status.value}'). Use 'Accept with Exceptions' to accept the open gaps explicitly."
                )
            acknowledged = []
        elif decision == Decision.ACCEPT_WITH_EXCEPTIONS:
            if not note:
                raise DecisionError("Accepting with exceptions requires a note explaining the exceptions.")
            unknown = set(acknowledged) - open_ids
            if unknown:
                raise DecisionError(f"These gaps are not open in revision {revision}: {', '.join(sorted(unknown))}.")
            if not acknowledged:
                raise DecisionError("Select the open gaps you accept as exceptions.")
            unacknowledged = must_acknowledge - set(acknowledged)
            if unacknowledged:
                raise DecisionError(
                    "Every critical and high gap must be acknowledged explicitly. Not acknowledged: "
                    + ", ".join(sorted(unacknowledged))
                )
        else:
            acknowledged = []

        record = self.repository.add_decision(
            AcceptanceRecord(
                handover_id=handover_id,
                revision=revision,
                receiver=receiver,
                decision=decision,
                note=note,
                acknowledged_gap_ids=acknowledged,
                readiness_score=result.readiness_score,
                created_at=utc_now(),
            )
        )
        log_event("decision_recorded", handover_id=handover_id, revision=revision, decision=decision.value)
        return record

    def acceptance_state(self, handover_id: str) -> AcceptanceState:
        """The latest decision counts only while it refers to the latest revision."""
        revision = self.repository.latest_revision(handover_id)
        decisions = self.repository.list_decisions(handover_id)
        if not decisions:
            return AcceptanceState(status=PENDING_REVIEW, revision=revision)
        latest = decisions[-1]
        if latest.revision == revision:
            return AcceptanceState(status=latest.status, revision=revision, current=latest)
        return AcceptanceState(status=PENDING_REVIEW, revision=revision, superseded=latest)

    # -- synthetic demo data ---------------------------------------------------

    def load_fixture(self, scenario: Scenario) -> Handover:
        path = SYNTHETIC_DIR / f"{scenario.value}.json"
        try:
            return handover_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, IntakeError) as exc:
            raise IntakeError(f"Synthetic example '{scenario.value}' could not be loaded: {exc}") from exc

    def demo_corrections(self, scenario: Scenario) -> list[dict]:
        """Sample corrections shipped with a synthetic scenario (empty if none)."""
        path = SYNTHETIC_DIR / "corrections" / f"{scenario.value}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["operations"]

    def reset_demo(self) -> int:
        """Remove stored copies of the synthetic examples only. User records stay."""
        ids = [self.load_fixture(scenario).id for scenario in Scenario]
        removed = self.repository.delete_handovers(ids)
        log_event("demo_reset", removed=removed)
        return removed
