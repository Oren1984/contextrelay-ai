import json

import pytest

from app.models import ReadinessStatus, Scenario
from app.rules import DEFAULT_RULES_PATH, RulesError, load_rules
from app.services.handover_service import analyze, apply_corrections


def dims(result):
    return {d.key: d.score for d in result.dimensions}


def test_complete_verified_handover_scores_100_and_is_ready(complete, rules):
    result = analyze(complete, rules)
    assert result.readiness_score == 100
    assert result.readiness_status == ReadinessStatus.READY
    assert result.ownership_coverage == 100.0
    assert result.blocking_gap_ids == []


def test_score_is_the_weighted_average_of_the_dimensions(departure, rules):
    result = analyze(departure, rules)
    expected = round(sum(d.weight * d.score for d in result.dimensions) / sum(d.weight for d in result.dimensions))
    assert result.readiness_score == expected
    assert {d.key for d in result.dimensions} == set(rules.weights)
    assert all(0 <= d.score <= 100 and d.notes for d in result.dimensions)


def test_empty_handover_is_high_risk(rules, make):
    result = analyze(make(title="", outgoing_owner="", receiving_owner="", target_date="", items=[]), rules)
    assert result.readiness_status == ReadinessStatus.HIGH_RISK
    assert result.readiness_score < rules.clarification_threshold
    assert result.ownership_coverage is None


def test_high_score_does_not_hide_a_critical_blocker(complete, rules):
    blocker = {"id": "risk-1", "category": "risks_blockers", "title": "Tool access missing", "critical": True}
    result = analyze(apply_corrections(complete, [{"op": "add_item", "item": blocker}]), rules)
    assert result.readiness_score >= rules.ready_threshold  # numerically it would pass
    assert result.readiness_status == ReadinessStatus.HIGH_RISK
    assert result.blocking_gap_ids == ["BLOCKER-CRITICAL:risk-1"]
    assert "regardless of the score" in result.status_reason


def test_high_gap_caps_status_at_needs_clarification(service, rules):
    result = analyze(service.load_fixture(Scenario.FREELANCER_COMPLETION), rules)
    assert result.readiness_score >= rules.ready_threshold
    assert result.readiness_status == ReadinessStatus.NEEDS_CLARIFICATION


def test_unverified_knowledge_scores_below_verified(rules, make):
    unverified = make()
    for item in unverified.items:
        item.evidence = ""
    assert dims(analyze(unverified, rules))["knowledge_completeness"] == 70
    assert dims(analyze(make(), rules))["knowledge_completeness"] == 100


def test_not_applicable_is_excluded_not_credited(rules, make):
    handover = make(not_applicable={"deadlines": "No dated commitments in this role."})
    handover = apply_corrections(handover, [{"op": "remove_item", "item_id": "dl-1"}])
    completeness = next(d for d in analyze(handover, rules).dimensions if d.key == "knowledge_completeness")
    assert completeness.score == 100
    assert any("excluded, not counted as verified" in note for note in completeness.notes)


def test_ownership_coverage_counts_only_new_owners(complete, rules):
    partial = apply_corrections(
        complete,
        [
            {"op": "set_item", "item_id": "resp-1", "fields": {"owner": ""}},
            {"op": "set_item", "item_id": "work-1", "fields": {"owner": "Alex Out"}},
        ],
    )
    result = analyze(partial, rules)
    assert result.ownership_coverage == pytest.approx(33.3)
    assert dims(result)["ownership_clarity"] == 33


def test_unknown_dependencies_are_not_scored_as_safe(complete, rules):
    none_listed = apply_corrections(complete, [{"op": "remove_item", "item_id": "dep-1"}])
    assert dims(analyze(none_listed, rules))["dependency_exposure"] == rules.unknown_dependency_score
    declared = apply_corrections(
        none_listed, [{"op": "set_not_applicable", "category": "dependencies", "reason": "Standalone role"}]
    )
    assert dims(analyze(declared, rules))["dependency_exposure"] == 100


def test_urgent_deadlines_weigh_double(complete, rules):
    urgent = {"id": "dl-2", "category": "deadlines", "title": "Urgent", "due_date": "2026-11-04", "owner": ""}
    handover = apply_corrections(complete, [{"op": "add_item", "item": urgent}])
    assert dims(analyze(handover, rules))["deadline_sensitivity"] == 33  # weight 1 covered, weight 2 not


def test_blocker_penalties(complete, rules):
    two = apply_corrections(
        complete,
        [
            {"op": "add_item", "item": {"id": "risk-1", "category": "risks_blockers", "title": "A", "critical": True}},
            {"op": "add_item", "item": {"id": "risk-2", "category": "risks_blockers", "title": "B"}},
        ],
    )
    assert dims(analyze(two, rules))["unresolved_blockers"] == 100 - 50 - 15


def test_analysis_is_deterministic(departure, rules):
    assert analyze(departure, rules).model_dump() == analyze(departure, rules).model_dump()


def test_three_scenarios_differ_meaningfully(service, rules):
    results = {s: analyze(service.load_fixture(s), rules) for s in Scenario}
    assert results[Scenario.EMPLOYEE_DEPARTURE].readiness_status == ReadinessStatus.HIGH_RISK
    assert results[Scenario.FREELANCER_COMPLETION].readiness_status == ReadinessStatus.NEEDS_CLARIFICATION
    assert results[Scenario.TEMPORARY_HANDOVER].readiness_status == ReadinessStatus.HIGH_RISK
    assert results[Scenario.TEMPORARY_HANDOVER].critical_count > results[Scenario.FREELANCER_COMPLETION].critical_count


def write_rules(tmp_path, mutate):
    raw = json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_weights_are_configurable(tmp_path, departure):
    def only_blockers(raw):
        raw["weights"] = {key: (100 if key == "unresolved_blockers" else 0) for key in raw["weights"]}

    custom = analyze(departure, load_rules(write_rules(tmp_path, only_blockers)))
    assert custom.readiness_score == next(d.score for d in custom.dimensions if d.key == "unresolved_blockers")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["weights"].pop("ownership_clarity"),
        lambda raw: raw["thresholds"].update(ready_for_review=40),
        lambda raw: raw["scenarios"].pop("temporary_handover"),
        lambda raw: raw["scenarios"]["employee_departure"]["required"].update(nonsense="high"),
    ],
)
def test_invalid_rule_configuration_is_rejected(tmp_path, mutate):
    with pytest.raises(RulesError):
        load_rules(write_rules(tmp_path, mutate))


def test_missing_rule_file_is_rejected(tmp_path):
    with pytest.raises(RulesError):
        load_rules(tmp_path / "absent.json")
