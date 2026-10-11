from __future__ import annotations

from app.models import AnalysisResult, Handover, Insights
from app.providers.base import InsightProvider

MAX_QUESTIONS = 5


class MockInsightProvider(InsightProvider):
    """Offline, deterministic provider: templates filled only from engine findings."""

    name = "mock"
    label = "Mock mode - templated from rule findings, no AI model involved"

    def generate_insights(self, handover: Handover, findings: AnalysisResult) -> Insights:
        counts = findings.severity_counts()
        if not findings.gaps:
            summary = (
                f"The rules found no gaps. Readiness is {findings.readiness_score}/100 "
                f"({findings.readiness_status.value})."
            )
        else:
            breakdown = ", ".join(f"{count} {severity}" for severity, count in counts.items() if count)
            summary = (
                f"The rules found {len(findings.gaps)} gap(s): {breakdown}. Readiness is "
                f"{findings.readiness_score}/100 ({findings.readiness_status.value}). {findings.status_reason}"
            )
        questions = []
        for gap in findings.gaps[:MAX_QUESTIONS]:  # already ordered by severity
            addressee = gap.responsible or "the outgoing owner"
            questions.append(f"[{gap.id}] For {addressee}: {gap.description} {gap.recommended_action}")
        return Insights(provider=self.name, label=self.label, summary=summary, questions=questions)
