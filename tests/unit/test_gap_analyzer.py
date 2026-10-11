import pytest

from app.models import Scenario, Severity
from app.services.gap_analyzer import RULE_CATALOGUE
from app.services.handover_service import analyze, apply_corrections


def gaps(handover, rules):
    return {gap.id: gap for gap in analyze(handover, rules).gaps}


def edit(handover, item_id, **fields):
    return apply_corrections(handover, [{"op": "set_item", "item_id": item_id, "fields": fields}])


def test_complete_verified_handover_has_no_gaps(complete, rules):
    assert gaps(complete, rules) == {}


def test_empty_handover_reports_header_and_required_gaps(rules, make):
    found = gaps(make(title="", outgoing_owner="", receiving_owner="", target_date="", items=[]), rules)
    assert {"HEADER-TITLE:handover", "HEADER-OUTGOING:handover", "HEADER-RECEIVER:handover", "HEADER-DATE:handover"} <= set(found)
    assert {"REQ-MISSING:responsibilities", "REQ-MISSING:contacts", "REQ-MISSING:deadlines"} <= set(found)
    assert found["HEADER-RECEIVER:handover"].severity == Severity.CRITICAL
    assert found["REQ-MISSING:responsibilities"].severity == Severity.CRITICAL


def test_every_gap_is_traceable_and_actionable(service, rules):
    for scenario in Scenario:
        for gap in analyze(service.load_fixture(scenario), rules).gaps:
            assert gap.rule_id in RULE_CATALOGUE
            assert gap.id == f"{gap.rule_id}:{gap.item_id or gap.category}"
            assert gap.rule_id in gap.source
            assert gap.description and gap.explanation and gap.recommended_action
            assert gap.responsible  # the outgoing owner is known in all fixtures
            assert gap.status == "open"


def test_unassigned_and_placeholder_owner(complete, rules):
    assert "OWNER-UNASSIGNED:resp-1" in gaps(edit(complete, "resp-1", owner=""), rules)
    assert "OWNER-UNASSIGNED:resp-1" in gaps(edit(complete, "resp-1", owner="TBD"), rules)


def test_item_still_owned_by_outgoing_owner(complete, rules):
    found = gaps(edit(complete, "work-1", owner="alex out"), rules)  # case-insensitive match
    assert found["OWNER-OUTGOING:work-1"].severity == Severity.HIGH


def test_completed_work_needs_no_owner(complete, rules):
    assert gaps(edit(complete, "work-1", owner="", state="completed"), rules) == {}


def test_same_outgoing_and_receiving_owner_is_a_critical_contradiction(rules, make):
    found = gaps(make(receiving_owner="ALEX OUT"), rules)
    assert found["HEADER-SAME-OWNER:handover"].severity == Severity.CRITICAL


def test_not_applicable_with_entries_is_a_contradiction(rules, make):
    assert "CONTRADICTION-NA:documentation" in gaps(make(not_applicable={"documentation": "none exists"}), rules)


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        ("", "DEADLINE-NODATE:dl-1"),
        ("15/12/2026", "DEADLINE-INVALID:dl-1"),
        ("2026-02-30", "DEADLINE-INVALID:dl-1"),
        ("2026-10-01", "DEADLINE-OVERDUE:dl-1"),
    ],
)
def test_incomplete_or_invalid_deadlines(complete, rules, due, expected):
    assert expected in gaps(edit(complete, "dl-1", due_date=due), rules)


def test_invalid_handover_date_is_reported(rules, make):
    found = gaps(make(target_date="next Monday"), rules)
    assert "not a valid date" in found["HEADER-DATE:handover"].description


def test_urgent_deadline_without_owner_is_critical(complete, rules):
    urgent = gaps(edit(complete, "dl-1", due_date="2026-11-10", owner=""), rules)
    relaxed = gaps(edit(complete, "dl-1", due_date="2027-03-01", owner=""), rules)
    assert urgent["OWNER-UNASSIGNED:dl-1"].severity == Severity.CRITICAL
    assert relaxed["OWNER-UNASSIGNED:dl-1"].severity == Severity.HIGH


def test_escalation_contact_required(complete, rules):
    without = edit(complete, "con-1", escalation=False)
    assert gaps(without, rules)["ESCALATION-MISSING:contacts"].severity == Severity.HIGH
    temporary = apply_corrections(without, [{"op": "set_header", "fields": {"scenario": "temporary_handover"}}])
    assert gaps(temporary, rules)["ESCALATION-MISSING:contacts"].severity == Severity.CRITICAL


def test_dependency_without_owner_and_access_without_instructions(complete, rules):
    assert gaps(edit(complete, "dep-1", owner=""), rules)["DEP-NOOWNER:dep-1"].severity == Severity.MEDIUM
    assert gaps(edit(complete, "dep-1", owner="", critical=True), rules)["DEP-NOOWNER:dep-1"].severity == Severity.HIGH
    assert "ACCESS-NODETAIL:acc-1" in gaps(edit(complete, "acc-1", detail=""), rules)


def with_blocker(complete, **fields):
    item = {"id": "risk-1", "category": "risks_blockers", "title": "Tool access missing", **fields}
    return apply_corrections(complete, [{"op": "add_item", "item": item}])


def test_critical_blocker_is_critical_until_resolved_with_reference(complete, rules):
    assert gaps(with_blocker(complete, critical=True), rules)["BLOCKER-CRITICAL:risk-1"].severity == Severity.CRITICAL
    # Flipping the state alone is not enough: a resolution reference is required.
    assert "BLOCKER-CRITICAL:risk-1" in gaps(with_blocker(complete, critical=True, state="completed"), rules)
    assert gaps(with_blocker(complete, critical=True, state="completed", evidence="REQ-1 approved"), rules) == {}


def test_documented_risk_is_fine_but_unmanaged_risk_is_not(complete, rules):
    assert "RISK-UNMANAGED:risk-1" in gaps(with_blocker(complete), rules)
    managed = with_blocker(complete, owner="Robin In", detail="Mitigation: keep the old tool available")
    assert gaps(managed, rules) == {}


def test_open_question_severity(complete, rules):
    question = {"id": "q-1", "category": "open_questions", "title": "Who signs off?"}
    found = gaps(apply_corrections(complete, [{"op": "add_item", "item": question}]), rules)
    assert found["QUESTION-OPEN:q-1"].severity == Severity.MEDIUM
    question["critical"] = True
    found = gaps(apply_corrections(complete, [{"op": "add_item", "item": question}]), rules)
    assert found["QUESTION-OPEN:q-1"].severity == Severity.HIGH


def test_all_fields_completed_but_not_verified(rules, make):
    handover = make()
    for item in handover.items:
        item.evidence = ""
    found = gaps(handover, rules)
    required = ("responsibilities", "active_work", "documentation", "deadlines", "contacts")
    assert set(found) == {f"UNVERIFIED:{c}" for c in required}
    assert all(gap.severity == Severity.LOW for gap in found.values())


def test_gaps_are_sorted_by_severity(departure, rules):
    ranks = [list(Severity).index(g.severity) for g in analyze(departure, rules).gaps]
    assert ranks == sorted(ranks)
