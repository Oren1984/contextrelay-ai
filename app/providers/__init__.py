"""Optional insight providers. The deterministic workflow never depends on them."""

from __future__ import annotations

import os

from app.providers.base import InsightProvider
from app.providers.mock_provider import MockInsightProvider


def get_provider() -> InsightProvider:
    """Mock unless the LLM is explicitly enabled through the environment."""
    if os.environ.get("CONTEXTRELAY_LLM_ENABLED", "false").strip().lower() in ("1", "true", "yes"):
        from app.providers.llm_provider import OptionalLLMInsightProvider

        return OptionalLLMInsightProvider()
    return MockInsightProvider()
