"""UI end-to-end test: drives the real Streamlit script with AppTest (no browser)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.models import Decision

APP = str(Path(__file__).resolve().parents[2] / "app" / "main.py")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTRELAY_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.delenv("CONTEXTRELAY_LLM_ENABLED", raising=False)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    return at


def metrics(at):
    return {m.label: m.value for m in at.metric}


def messages(at, kind):
    return " | ".join(element.value for element in getattr(at, kind))


def click(at, key):
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    return at


def test_app_starts_empty_and_asks_for_input(app):
    assert app.title[0].value == "ContextRelay AI"
    assert "Analyze Handover" in messages(app, "info")
    assert metrics(app) == {}


def test_full_workflow_through_the_ui(app):
    at = click(app, "load_example")  # default example: Employee Departure
    assert "Loaded the synthetic example" in messages(at, "success")
    assert at.text_input(key=next(w.key for w in at.text_input if w.key.startswith("outgoing_"))).value == "Dana Whitfield"

    at = click(at, "analyze")
    shown = metrics(at)
    assert shown["Readiness Score"] == "50 / 100"
    assert shown["Missing Knowledge Count"] == "16"
    assert shown["Critical Gap Count"] == "2"
    assert shown["Ownership Coverage"] == "43%"
    assert shown["Overall Status"] == "High Risk"
    assert "Blocked by 2 unresolved critical gap(s)" in messages(at, "error")

    # Acceptance with blocking issues is refused.
    at.radio(key="decision_choice").set_value(Decision.ACCEPT).run()
    at = click(at, "record_decision")
    assert "Unconditional acceptance is not possible" in messages(at, "error")
    assert metrics(at)["Final handover status"] == "High Risk"

    # A gap cannot be closed without the supporting information.
    at.selectbox(key="gap_select").set_value("BLOCKER-CRITICAL:risk-1").run()
    at.selectbox(key="fix_BLOCKER_CRITICAL_risk_1_state").set_value("completed").run()
    at = click(at, "fix_BLOCKER_CRITICAL_risk_1_apply")
    assert "is still open" in messages(at, "warning")
    assert metrics(at)["Critical Gap Count"] == "2"

    # Providing the missing owner resolves exactly that gap.
    at.selectbox(key="gap_select").set_value("OWNER-UNASSIGNED:resp-2").run()
    at.text_input(key="fix_OWNER_UNASSIGNED_resp_2_owner").input("Marcus Oyelaran").run()
    at = click(at, "fix_OWNER_UNASSIGNED_resp_2_apply")
    assert "`OWNER-UNASSIGNED:resp-2` is resolved" in messages(at, "success")
    assert metrics(at)["Missing Knowledge Count"] == "15"

    # Sample corrections + recomputed before/after.
    at = click(at, "apply_sample")
    shown = metrics(at)
    assert shown["Readiness Score"] == "91 / 100"
    assert shown["Critical Gap Count"] == "0"
    assert shown["Overall Status"] == "Ready for Review"
    assert shown["Open gaps"] == "16 → 7"
    assert shown["Critical blockers"] == "2 → 0"
    assert shown["Gaps resolved / remaining"] == "9 / 7"
    assert any(label.startswith("Readiness (rev 1") and value == "50 → 91" for label, value in shown.items())

    # Handover package and receiver decision.
    report = " ".join(m.value for m in at.markdown)
    assert "# Handover Package: Billing Operations Analyst - role handover" in report
    assert "Mock mode" in " ".join(e.label for e in at.expander)
    at.radio(key="decision_choice").set_value(Decision.ACCEPT).run()
    at = click(at, "record_decision")
    assert "Accepted" in messages(at, "success")
    shown = metrics(at)
    assert shown["Final handover status"] == "Accepted"
    assert shown["Overall Status"] == "Accepted"
    assert not at.exception


def test_other_scenarios_load_and_analyze(app):
    from app.models import Scenario

    expected = {Scenario.FREELANCER_COMPLETION: ("91 / 100", "Needs Clarification"), Scenario.TEMPORARY_HANDOVER: ("48 / 100", "High Risk")}
    at = app
    for scenario, (score, status) in expected.items():
        at.selectbox(key="example_choice").set_value(scenario).run()
        at = click(click(at, "load_example"), "analyze")
        shown = metrics(at)
        assert (shown["Readiness Score"], shown["Overall Status"]) == (score, status)


def test_empty_handover_can_be_analyzed_without_errors(app):
    at = click(app, "analyze")
    shown = metrics(at)
    assert shown["Overall Status"] == "High Risk"
    assert shown["Ownership Coverage"] == "n/a"
    assert int(shown["Critical Gap Count"]) >= 1
