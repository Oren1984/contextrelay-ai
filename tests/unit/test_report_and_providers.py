import json

from app.models import Scenario
from app.providers import get_provider
from app.providers.llm_provider import LLMOutput, OptionalLLMInsightProvider
from app.providers.mock_provider import MockInsightProvider
from app.services import report_service
from app.services.handover_service import analyze

STAMP = "2026-10-11T00:00:00+00:00"


def package_for(handover, rules, **kwargs):
    return report_service.build_package(handover, analyze(handover, rules), 1, rules, generated_at=STAMP, **kwargs)


def test_package_contains_the_required_sections(departure, rules):
    package = package_for(departure, rules)
    result = analyze(departure, rules)
    assert package["handover"]["title"] == departure.title
    assert package["handover"]["scenario_label"] == "Employee Departure"
    assert package["readiness"]["score"] == result.readiness_score
    assert package["acceptance"]["status"] == "Pending Review"
    assert len(package["gaps"]) == len(result.gaps)
    assert {"responsibilities", "active_work", "decisions", "dependencies", "deadlines", "risks_blockers"} <= set(package["knowledge"])
    json.loads(report_service.render_json(package))  # valid JSON


def test_evidence_references_come_only_from_source_data(departure, rules):
    package = package_for(departure, rules)
    provided = {i.id: i.evidence for i in departure.items if i.evidence}
    assert {e["item_id"]: e["evidence"] for e in package["evidence_references"]} == provided


def test_markdown_report_is_complete_and_deterministic(departure, rules):
    markdown = report_service.render_markdown(package_for(departure, rules))
    expected_parts = (
        "# Handover Package:",
        "Dana Whitfield",
        "Marcus Oyelaran",
        "## Readiness",
        "Work in progress",
        "## Missing Information and Recommended Actions",
        "## Evidence References",
        "BLOCKER-CRITICAL:risk-1",
        "not a legally binding signature",
        STAMP,
    )
    for expected in expected_parts:
        assert expected in markdown
    assert markdown == report_service.render_markdown(package_for(departure, rules))


def test_freelancer_report_separates_delivered_from_outstanding_work(service, rules):
    markdown = report_service.render_markdown(package_for(service.load_fixture(Scenario.FREELANCER_COMPLETION), rules))
    progress = markdown.index("### Work in progress")
    done = markdown.index("### Completed / delivered work")
    assert progress < markdown.index("Mobile layout fixes") < done < markdown.index("Dashboard v1.0")


def test_empty_sections_are_reported_as_empty_not_invented(rules, make):
    markdown = report_service.render_markdown(package_for(make(items=[]), rules))
    assert markdown.count("No entries provided.") >= 8
    assert "No evidence references were provided." in markdown


def test_user_text_cannot_break_report_tables(rules, make):
    handover = make(title="Role | handover\nwith newline")
    markdown = report_service.render_markdown(package_for(handover, rules))
    assert "# Handover Package: Role \\| handover with newline" in markdown


def test_report_filenames_are_safe(make):
    name = report_service.safe_filename(make(title="../../etc/passwd; rm -rf <x>"), 3, "md")
    assert name == "handover-etc-passwd-rm-rf-x-rev3.md"
    assert report_service.safe_filename(make(title=""), 1, "../json") == "handover-ho-test-rev1.json"


def test_mock_provider_is_deterministic_and_grounded_in_findings(departure, rules):
    result = analyze(departure, rules)
    first = MockInsightProvider().generate_insights(departure, result)
    assert first == MockInsightProvider().generate_insights(departure, result)
    assert first.provider == "mock" and "no AI model" in first.label
    assert f"{len(result.gaps)} gap(s)" in first.summary
    gap_ids = {gap.id for gap in result.gaps}
    assert first.questions and all(q[1 : q.index("]")] in gap_ids for q in first.questions)


def test_mock_provider_invents_nothing_for_a_clean_handover(complete, rules):
    insights = MockInsightProvider().generate_insights(complete, analyze(complete, rules))
    assert insights.questions == [] and "no gaps" in insights.summary


def test_mock_is_the_default_provider(monkeypatch):
    monkeypatch.delenv("CONTEXTRELAY_LLM_ENABLED", raising=False)
    assert isinstance(get_provider(), MockInsightProvider)


def test_llm_unavailable_falls_back_to_mock(departure, rules):
    def offline(system, user):
        raise ConnectionError("network unreachable")

    result = analyze(departure, rules)
    insights = OptionalLLMInsightProvider(call=offline).generate_insights(departure, result)
    assert insights.provider == "mock"
    assert "LLM unavailable (ConnectionError)" in insights.fallback_reason
    assert insights.summary == MockInsightProvider().generate_insights(departure, result).summary


def test_llm_enabled_without_sdk_or_credentials_falls_back_to_mock(departure, rules, monkeypatch):
    """The real adapter path, with no API key and possibly no SDK installed."""
    monkeypatch.setenv("CONTEXTRELAY_LLM_ENABLED", "true")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # never reaches a real service
    provider = get_provider()
    assert isinstance(provider, OptionalLLMInsightProvider)
    insights = provider.generate_insights(departure, analyze(departure, rules))
    assert insights.provider == "mock" and insights.fallback_reason


def test_llm_invalid_output_falls_back_to_mock(departure, rules):
    result = analyze(departure, rules)
    for bad in ({"summary": 42}, {"questions": ["only questions"]}, LLMOutput(summary="   ")):
        provider = OptionalLLMInsightProvider(call=lambda system, user, bad=bad: bad)
        insights = provider.generate_insights(departure, result)
        assert insights.provider == "mock" and insights.fallback_reason


def test_llm_receives_findings_only_and_output_is_bounded(departure, rules):
    seen = {}

    def fake(system, user):
        seen.update(system=system, user=user)
        return LLMOutput(summary="Short summary.", questions=[f"Question {n}?" for n in range(20)] + ["x" * 900])

    result = analyze(departure, rules)
    insights = OptionalLLMInsightProvider(call=fake).generate_insights(departure, result)
    assert insights.provider == "llm" and "not verified facts" in insights.label
    assert len(insights.questions) == 5
    assert "untrusted data" in seen["system"]
    assert departure.description not in seen["user"]  # free-text notes are not transmitted
    assert len(seen["user"]) < 13_000
    # The provider returns suggestions only; the findings are untouched.
    assert result.model_dump() == analyze(departure, rules).model_dump()
