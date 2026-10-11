from app.models import Category, KnowledgeStatus, Scenario
from app.services.completeness_engine import assess_completeness


def _status(handover, rules):
    return {a.category: a for a in assess_completeness(handover, rules)}


def test_required_categories_follow_the_scenario(rules, make):
    departure = _status(make(), rules)
    freelancer = _status(make(scenario="freelancer_completion"), rules)
    assert departure[Category.CONTACTS].required and not freelancer[Category.CONTACTS].required
    assert freelancer[Category.RISKS_BLOCKERS].required and not departure[Category.RISKS_BLOCKERS].required
    assert freelancer[Category.ACTIVE_WORK].label == "Deliverables and outstanding work"


def test_four_knowledge_states(rules, make):
    handover = make(not_applicable={"decisions": "No decisions were taken in this role."})
    handover.items[0].evidence = ""  # responsibilities: present, not verified
    status = _status(handover, rules)
    assert status[Category.RESPONSIBILITIES].status == KnowledgeStatus.UNVERIFIED
    assert status[Category.DOCUMENTATION].status == KnowledgeStatus.VERIFIED
    assert status[Category.DECISIONS].status == KnowledgeStatus.NOT_APPLICABLE
    assert status[Category.OPEN_QUESTIONS].status == KnowledgeStatus.MISSING


def test_not_applicable_needs_a_real_reason(rules, make):
    status = _status(make(not_applicable={"decisions": "n/a", "risks_blockers": "   "}), rules)
    assert status[Category.DECISIONS].status == KnowledgeStatus.MISSING
    assert status[Category.RISKS_BLOCKERS].status == KnowledgeStatus.MISSING


def test_placeholder_entries_do_not_count_as_knowledge(rules, make):
    handover = make()
    for item in handover.items_in(Category.DOCUMENTATION):
        item.title = "TBD"
    assert _status(handover, rules)[Category.DOCUMENTATION].status == KnowledgeStatus.MISSING


def test_provided_data_wins_over_not_applicable_marker(rules, make):
    status = _status(make(not_applicable={"documentation": "We have none."}), rules)
    assert status[Category.DOCUMENTATION].status == KnowledgeStatus.VERIFIED


def test_every_scenario_is_configured(rules):
    assert set(rules.scenarios) == set(Scenario)
