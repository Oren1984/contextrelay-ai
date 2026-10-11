import json
import sqlite3

import pytest

from app.models import Category, Decision, ReadinessStatus, Scenario
from app.services import report_service
from app.services.handover_service import CorrectionError, DecisionError, apply_corrections, compare
from app.services.intake import parse_json_bytes
from app.storage.repository import Repository, StorageError


def fix(item_id, **fields):
    return {"op": "set_item", "item_id": item_id, "fields": fields}


# -- intake -> normalization -> completeness -> risk -> report ----------------


def test_uploaded_json_flows_through_the_whole_pipeline(service, departure):
    upload = json.dumps(departure.model_dump(mode="json")).encode()
    handover = parse_json_bytes(upload)
    revision, result = service.save_and_analyze(handover)
    package = report_service.build_package(handover, result, revision, service.rules)
    assert revision == 1
    assert result.readiness_status == ReadinessStatus.HIGH_RISK
    assert package["handover"]["revision"] == 1
    assert [g["id"] for g in package["gaps"]] == [g.id for g in result.gaps]
    assert "Month-end close submission" in report_service.render_markdown(package)


# -- gap correction -> re-analysis --------------------------------------------


def test_correcting_data_resolves_exactly_the_targeted_gap(service, departure):
    before = service.analyze(departure)
    corrected = apply_corrections(departure, [fix("resp-2", owner="Marcus Oyelaran")])
    resolved, after = service.confirm_resolved(corrected, "OWNER-UNASSIGNED:resp-2")
    change = compare(before, after)
    assert resolved
    assert [g.id for g in change.resolved] == ["OWNER-UNASSIGNED:resp-2"]
    assert change.introduced == []
    assert after.readiness_score > before.readiness_score


def test_a_gap_cannot_be_closed_without_the_missing_information(service, departure):
    # Marking the blocker completed without a resolution reference does not satisfy the rule.
    attempt = apply_corrections(departure, [fix("risk-1", state="completed")])
    resolved, result = service.confirm_resolved(attempt, "BLOCKER-CRITICAL:risk-1")
    assert not resolved
    assert "BLOCKER-CRITICAL:risk-1" in result.blocking_gap_ids
    # A placeholder owner does not count as an owner either.
    attempt = apply_corrections(departure, [fix("resp-2", owner="tbd")])
    assert not service.confirm_resolved(attempt, "OWNER-UNASSIGNED:resp-2")[0]


def test_corrections_never_mutate_the_original_and_are_validated(departure):
    snapshot = departure.content_hash()
    apply_corrections(departure, [fix("resp-2", owner="Someone")])
    assert departure.content_hash() == snapshot
    with pytest.raises(CorrectionError, match="does not exist"):
        apply_corrections(departure, [fix("nope-1", owner="x")])
    with pytest.raises(CorrectionError, match="Unknown correction"):
        apply_corrections(departure, [{"op": "drop_table"}])
    with pytest.raises(CorrectionError, match="rejected"):
        apply_corrections(departure, [fix("resp-2", owner="x" * 500)])
    with pytest.raises(CorrectionError, match="Duplicate"):
        apply_corrections(departure, [{"op": "add_item", "item": {"id": "resp-1", "category": "decisions", "title": "dup"}}])


def test_reclassifying_an_item_changes_the_findings(service, departure):
    moved = apply_corrections(departure, [{"op": "move_item", "item_id": "q-1", "category": "decisions"}])
    ids = {g.id for g in service.analyze(moved).gaps}
    assert "QUESTION-OPEN:q-1" not in ids
    assert [i.id for i in moved.items_in(Category.DECISIONS)] == ["dec-1", "q-1"]


# -- storage -> reload ----------------------------------------------------------


def test_save_reload_and_revision_tracking(service, repository, departure):
    assert service.save(departure) == 1
    assert service.save(departure) == 1  # unchanged content does not create a revision
    changed = apply_corrections(departure, [fix("resp-2", owner="Marcus Oyelaran")])
    assert service.save(changed) == 2

    reloaded = Repository(repository.path)  # a new connection, as after a restart
    latest, revision = reloaded.load_handover(departure.id)
    first, _ = reloaded.load_handover(departure.id, 1)
    assert revision == 2 and latest.content_hash() == changed.content_hash()
    assert first.content_hash() == departure.content_hash()
    assert [r["revision"] for r in reloaded.list_revisions(departure.id)] == [1, 2]
    assert reloaded.list_handovers()[0]["title"] == departure.title


def test_scores_are_recomputed_from_stored_revisions(service, departure):
    service.save(departure)
    _, _, first = service.analyze_revision(departure.id, 1)
    _, _, again = service.analyze_revision(departure.id, 1)
    assert first.model_dump() == again.model_dump() == service.analyze(departure).model_dump()


def test_missing_and_tampered_records_raise_clear_errors(service, repository, departure):
    with pytest.raises(StorageError, match="was not found"):
        repository.load_handover("ho-unknown")
    service.save(departure)
    with sqlite3.connect(repository.path) as conn:
        conn.execute("UPDATE revisions SET payload = replace(payload, 'Dana Whitfield', 'Somebody Else')")
    with pytest.raises(StorageError, match="Integrity check failed"):
        repository.load_handover(departure.id)
    with sqlite3.connect(repository.path) as conn:
        conn.execute("UPDATE revisions SET payload = '{not json'")
    with pytest.raises(StorageError, match="is invalid"):
        repository.load_handover(departure.id)


def test_unusable_database_location_is_a_storage_error(tmp_path, service, departure):
    with pytest.raises(StorageError):
        Repository(tmp_path)  # a directory, not a database file
    service.repository.path.unlink()
    service.repository.path.mkdir()  # the database disappears underneath the app
    with pytest.raises(StorageError):
        service.save(departure)


def test_demo_reset_keeps_user_records(service, departure, make):
    service.save(departure)
    service.save(service.load_fixture(Scenario.TEMPORARY_HANDOVER))
    service.save(make(id="ho-mine"))
    assert service.reset_demo() == 2
    assert [row["id"] for row in service.repository.list_handovers()] == ["ho-mine"]


# -- revision -> acceptance invalidation -----------------------------------------


def test_unconditional_acceptance_is_blocked_by_critical_gaps(service, departure):
    service.save(departure)
    with pytest.raises(DecisionError, match="critical gap"):
        service.record_decision(departure.id, "Marcus Oyelaran", Decision.ACCEPT)
    assert service.acceptance_state(departure.id).status == "Pending Review"


def test_unconditional_acceptance_requires_ready_for_review(service):
    freelancer = service.load_fixture(Scenario.FREELANCER_COMPLETION)  # score 91, one high gap
    service.save(freelancer)
    with pytest.raises(DecisionError, match="Ready for Review"):
        service.record_decision(freelancer.id, "Tomas Rivera", Decision.ACCEPT)


def test_accept_with_exceptions_needs_note_and_explicit_acknowledgement(service, departure):
    service.save(departure)
    result = service.analyze(departure)
    must = [g.id for g in result.gaps if g.severity.value in ("critical", "high")]
    with pytest.raises(DecisionError, match="requires a note"):
        service.record_decision(departure.id, "Marcus", Decision.ACCEPT_WITH_EXCEPTIONS, "", must)
    with pytest.raises(DecisionError, match="Not acknowledged"):
        service.record_decision(departure.id, "Marcus", Decision.ACCEPT_WITH_EXCEPTIONS, "ok", must[:1])
    with pytest.raises(DecisionError, match="not open"):
        service.record_decision(departure.id, "Marcus", Decision.ACCEPT_WITH_EXCEPTIONS, "ok", must + ["FAKE:gap"])
    with pytest.raises(DecisionError, match="receiver label"):
        service.record_decision(departure.id, "  ", Decision.REQUEST_CLARIFICATION)

    record = service.record_decision(departure.id, "Marcus", Decision.ACCEPT_WITH_EXCEPTIONS, "Access promised by Friday.", must)
    state = service.acceptance_state(departure.id)
    assert state.status == "Accepted with Exceptions"
    assert state.current.id == record.id and record.revision == 1
    assert record.acknowledged_gap_ids == sorted(must)
    assert record.readiness_score == result.readiness_score


def test_material_revision_supersedes_a_previous_acceptance(service, complete):
    service.save(complete)
    service.record_decision(complete.id, "Robin In", Decision.ACCEPT)
    assert service.acceptance_state(complete.id).status == "Accepted"

    service.save(complete)  # saving identical content is not a material change
    assert service.acceptance_state(complete.id).status == "Accepted"

    changed = apply_corrections(complete, [fix("resp-1", owner="")])
    service.save(changed)
    state = service.acceptance_state(complete.id)
    assert state.status == "Pending Review"
    assert state.current is None and state.superseded.status == "Accepted" and state.superseded.revision == 1
    with pytest.raises(DecisionError):
        service.record_decision(complete.id, "Robin In", Decision.ACCEPT)  # the new revision has a high gap


def test_request_clarification_is_always_possible(service, departure):
    service.save(departure)
    service.record_decision(departure.id, "Marcus", Decision.REQUEST_CLARIFICATION, "Who approves payment runs?")
    assert service.acceptance_state(departure.id).status == "Clarification Requested"
    assert len(service.repository.list_decisions(departure.id)) == 1
