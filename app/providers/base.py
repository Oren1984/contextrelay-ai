from __future__ import annotations

from app.models import AnalysisResult, Handover, Insights


class InsightProvider:
    """Turns deterministic findings into readable suggestions.

    Providers may rephrase and prioritise findings. They never change the
    score, the gaps or the acceptance state.
    """

    name = "base"

    def generate_insights(self, handover: Handover, findings: AnalysisResult) -> Insights:
        raise NotImplementedError
