import json

import pytest

from app.models import Category, Scenario
from app.services.intake import (
    MAX_UPLOAD_BYTES,
    IntakeError,
    handover_from_dict,
    parse_json_bytes,
    parse_text_bytes,
)


def _raw(**overrides):
    data = {"id": "ho-1", "scenario": "employee_departure", "items": []}
    data.update(overrides)
    return data


def test_all_synthetic_fixtures_load(service):
    ids = {service.load_fixture(s).id for s in Scenario}
    assert len(ids) == 3


def test_scenario_and_category_casing_is_normalized_but_facts_are_not():
    handover = handover_from_dict(
        _raw(scenario="Employee Departure", items=[{"category": "Active-Work", "title": "  Keep  THIS text ", "owner": "tbd"}])
    )
    assert handover.scenario == Scenario.EMPLOYEE_DEPARTURE
    item = handover.items[0]
    assert item.category == Category.ACTIVE_WORK
    assert item.id == "work-1"  # generated because none was supplied
    assert item.title == "Keep  THIS text"  # only surrounding whitespace is trimmed
    assert item.owner == "tbd"  # placeholders are reported later, never rewritten


def test_malformed_json_is_rejected():
    with pytest.raises(IntakeError, match="Malformed JSON"):
        parse_json_bytes(b'{"id": "x", ')


def test_non_object_json_is_rejected():
    with pytest.raises(IntakeError, match="JSON object"):
        parse_json_bytes(b"[1, 2, 3]")


def test_oversized_upload_is_rejected_before_parsing():
    with pytest.raises(IntakeError, match="too large"):
        parse_json_bytes(b" " * (MAX_UPLOAD_BYTES + 1))


def test_non_utf8_upload_is_rejected():
    with pytest.raises(IntakeError, match="UTF-8"):
        parse_json_bytes(b"\xff\xfe\x00bad")


def test_unknown_scenario_is_rejected():
    with pytest.raises(IntakeError, match="Unknown scenario"):
        handover_from_dict(_raw(scenario="merger"))


def test_duplicate_item_identifiers_are_rejected():
    items = [{"id": "a-1", "category": "decisions", "title": "x"}, {"id": "a-1", "category": "access", "title": "y"}]
    with pytest.raises(IntakeError, match="Duplicate item identifiers: a-1"):
        handover_from_dict(_raw(items=items))


def test_unknown_fields_are_rejected_not_executed():
    with pytest.raises(IntakeError, match="Invalid handover data"):
        handover_from_dict(_raw(items=[{"category": "decisions", "title": "x", "__class__": "os.system"}]))


def test_unsafe_identifier_is_rejected():
    with pytest.raises(IntakeError):
        handover_from_dict(_raw(id="../../etc/passwd"))


def test_overlong_values_are_rejected():
    with pytest.raises(IntakeError):
        handover_from_dict(_raw(title="x" * 201))


def test_json_round_trip_of_fixture(departure):
    again = parse_json_bytes(json.dumps(departure.model_dump(mode="json")).encode())
    assert again.content_hash() == departure.content_hash()


def test_text_notes_are_classified_by_prefix_keyword_or_flagged(rules):
    text = "Deadline: submit the tax form\nThe VPN access must be requested early\nSomething nobody can place\n"
    items, stats = parse_text_bytes(text.encode(), rules)
    assert stats == {"prefix": 1, "keyword": 1, "unmatched": 1}
    assert [i.category for i in items] == [Category.DEADLINES, Category.ACCESS, Category.OPEN_QUESTIONS]
    assert items[0].title == "submit the tax form"
    assert "could not be classified" in items[2].detail


def test_text_import_does_not_reuse_existing_ids(rules):
    items, _ = parse_text_bytes(b"Deadline: one\nDeadline: two", rules, existing_ids={"dl-1"})
    assert [i.id for i in items] == ["dl-2", "dl-3"]


def test_empty_text_file_is_rejected(rules):
    with pytest.raises(IntakeError, match="no content"):
        parse_text_bytes(b"\n  \n", rules)
