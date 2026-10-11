"""End-to-end business workflow at service level (no UI), on the synthetic demo."""

import io
import json
import logging

import pytest

from app.models import Decision, ReadinessStatus, Scenario
from app.observability import METRICS, get_logger
from app.services import report_service
from app.services.handover_service import DecisionError, apply_corrections, compare


def test_employee_departure_from_incomplete_to_accepted(service):
    runs_before = METRICS.snapshot()["analyses_succeeded"]

    # 1-4: load the incomplete synthetic handover, analyze, detect gaps, initial readiness
    handover = service.load_fixture(Scenario.EMPLOYEE_DEPARTURE)
    revision, before = service.save_and_analyze(handover)
    assert revision == 1
    assert (before.readiness_score, before.readiness_status) == (50, ReadinessStatus.HIGH_RISK)
    assert len(before.gaps) == 16 and before.critical_count == 2
    with pytest.raises(DecisionError):
        service.record_decision(handover.id, handover.receiving_owner, Decision.ACCEPT)

    # 5-7: apply the documented corrections, re-run, readiness changes accordingly
    corrected = apply_corrections(handover, service.demo_corrections(Scenario.EMPLOYEE_DEPARTURE))
    revision, after = service.save_and_analyze(corrected)
    change = compare(before, after)
    assert revision == 2
    assert (after.readiness_score, after.readiness_status) == (91, ReadinessStatus.READY)
    assert (len(change.resolved), len(change.remaining), len(change.introduced)) == (9, 7, 0)
    assert (change.critical_before, change.critical_after) == (2, 0)
    assert {g.id for g in change.remaining} >= {"QUESTION-OPEN:q-1", "PLACEHOLDER:doc-2"}

    # 9: receiver review
    record = service.record_decision(handover.id, handover.receiving_owner, Decision.ACCEPT)
    state = service.acceptance_state(handover.id)
    assert state.status == "Accepted" and record.revision == 2

    # 8: handover package reflects findings and the decision
    package = report_service.build_package(
        corrected, after, revision, service.rules, acceptance_status=state.status, acceptance=state.current
    )
    markdown = report_service.render_markdown(package)
    assert json.loads(report_service.render_json(package))["acceptance"]["record"]["decision"] == "accept"
    assert "**Acceptance status:** Accepted" in markdown and "91 / 100" in markdown

    # 10: final state and saved revisions survive a reload
    stored, stored_revision = service.repository.load_handover(handover.id)
    assert stored_revision == 2 and stored.content_hash() == corrected.content_hash()
    assert [r["revision"] for r in service.repository.list_revisions(handover.id)] == [1, 2]

    # observability: runs are counted and logs carry no handover content
    snapshot = METRICS.snapshot()
    assert snapshot["analyses_succeeded"] >= runs_before + 2 and snapshot["last_duration_ms"] > 0


def test_logs_do_not_contain_handover_content(service):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    get_logger().addHandler(handler)
    try:
        handover = service.load_fixture(Scenario.EMPLOYEE_DEPARTURE)
        service.save_and_analyze(handover)
    finally:
        get_logger().removeHandler(handler)
    logged = stream.getvalue()
    assert "event=analysis_completed" in logged and "duration_ms=" in logged and "gaps=16" in logged
    for secret in (handover.outgoing_owner, handover.receiving_owner, "Month-end close", "Payment tool"):
        assert secret not in logged


def test_temporary_handover_can_only_be_accepted_with_explicit_exceptions(service):
    handover = service.load_fixture(Scenario.TEMPORARY_HANDOVER)
    _, result = service.save_and_analyze(handover)
    assert result.readiness_status == ReadinessStatus.HIGH_RISK and result.critical_count == 3
    with pytest.raises(DecisionError, match="critical gap"):
        service.record_decision(handover.id, "Jonas Lindqvist", Decision.ACCEPT)
    must = [g.id for g in result.gaps if g.severity.value in ("critical", "high")]
    service.record_decision(
        handover.id, "Jonas Lindqvist", Decision.ACCEPT_WITH_EXCEPTIONS, "Covering for two weeks; manager informed.", must
    )
    assert service.acceptance_state(handover.id).status == "Accepted with Exceptions"
