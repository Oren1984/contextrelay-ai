from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Handover, Scenario  # noqa: E402
from app.rules import load_rules  # noqa: E402
from app.services.handover_service import HandoverService  # noqa: E402
from app.storage.repository import Repository  # noqa: E402


@pytest.fixture(scope="session")
def rules():
    return load_rules()


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "test.db")


@pytest.fixture
def service(repository, rules):
    return HandoverService(repository, rules)


@pytest.fixture
def departure(service) -> Handover:
    return service.load_fixture(Scenario.EMPLOYEE_DEPARTURE)


def make_handover(**overrides) -> Handover:
    """A small, fully complete and verified employee-departure handover."""
    data = {
        "id": "ho-test",
        "scenario": "employee_departure",
        "title": "Test handover",
        "outgoing_owner": "Alex Out",
        "receiving_owner": "Robin In",
        "target_date": "2026-11-02",
        "items": [
            {"id": "resp-1", "category": "responsibilities", "title": "Weekly report", "owner": "Robin In", "evidence": "doc A"},
            {"id": "work-1", "category": "active_work", "title": "Migration", "owner": "Robin In", "evidence": "ticket 1"},
            {"id": "doc-1", "category": "documentation", "title": "Runbook", "evidence": "runbook.md"},
            {"id": "dl-1", "category": "deadlines", "title": "Quarterly filing", "due_date": "2026-12-15", "owner": "Robin In", "evidence": "calendar"},
            {"id": "con-1", "category": "contacts", "title": "Sam Lead", "escalation": True, "evidence": "org chart"},
            {"id": "acc-1", "category": "access", "title": "Billing tool", "detail": "Request via service desk", "evidence": "access guide"},
            {"id": "dep-1", "category": "dependencies", "title": "Nightly export", "owner": "Data team", "evidence": "sla"},
        ],
        "not_applicable": {},
    }
    data.update(overrides)
    return Handover.model_validate(data)


@pytest.fixture
def complete() -> Handover:
    return make_handover()


@pytest.fixture
def make():
    """Factory for variations of the complete handover."""
    return make_handover
