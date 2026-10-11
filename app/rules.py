"""Loads and validates the local business-rule configuration (config/rules.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.models import CATEGORY_LABELS, Category, Scenario, Severity

DEFAULT_RULES_PATH = Path(__file__).parent / "config" / "rules.json"

DIMENSIONS: dict[str, str] = {
    "knowledge_completeness": "Knowledge completeness",
    "ownership_clarity": "Ownership clarity",
    "dependency_exposure": "Dependency exposure",
    "deadline_sensitivity": "Deadline sensitivity",
    "unresolved_blockers": "Unresolved blockers",
    "critical_information": "Critical information availability",
}


class RulesError(ValueError):
    """The rule configuration is missing or inconsistent."""


@dataclass(frozen=True)
class ScenarioRules:
    scenario: Scenario
    label: str
    required: dict[Category, Severity]
    escalation_contact_required: bool
    escalation_severity: Severity
    critical_information: tuple[Category, ...]
    category_labels: dict[Category, str]

    def label_for(self, category: Category) -> str:
        return self.category_labels.get(category, CATEGORY_LABELS[category])


@dataclass(frozen=True)
class Rules:
    version: str
    weights: dict[str, float]
    ready_threshold: int
    clarification_threshold: int
    status_credit: dict[str, float]
    critical_blocker_penalty: int
    unmanaged_risk_penalty: int
    unknown_dependency_score: int
    urgent_window_days: int
    placeholders: frozenset[str]
    scenarios: dict[Scenario, ScenarioRules]
    classification_keywords: dict[Category, tuple[str, ...]]

    def is_blank(self, value: str | None) -> bool:
        """True for empty values and configured placeholders such as 'TBD'."""
        text = (value or "").strip().casefold()
        return not text or text in self.placeholders

    def for_scenario(self, scenario: Scenario) -> ScenarioRules:
        return self.scenarios[scenario]


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> Rules:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RulesError(f"Cannot read rule configuration: {exc}") from exc
    try:
        return _build(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise RulesError(f"Invalid rule configuration: {exc}") from exc


def _build(raw: dict) -> Rules:
    weights = {key: float(value) for key, value in raw["weights"].items()}
    if set(weights) != set(DIMENSIONS):
        raise ValueError(f"weights must define exactly: {', '.join(DIMENSIONS)}")
    if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("weights must be non-negative and sum to a positive number")

    ready = int(raw["thresholds"]["ready_for_review"])
    clarification = int(raw["thresholds"]["needs_clarification"])
    if not 0 <= clarification < ready <= 100:
        raise ValueError("thresholds must satisfy 0 <= needs_clarification < ready_for_review <= 100")

    credit = {key: float(value) for key, value in raw["status_credit"].items()}
    if any(not 0.0 <= value <= 1.0 for value in credit.values()):
        raise ValueError("status_credit values must be between 0 and 1")

    scenarios: dict[Scenario, ScenarioRules] = {}
    for key, cfg in raw["scenarios"].items():
        scenario = Scenario(key)
        scenarios[scenario] = ScenarioRules(
            scenario=scenario,
            label=str(cfg["label"]),
            required={Category(c): Severity(s) for c, s in cfg["required"].items()},
            escalation_contact_required=bool(cfg["escalation_contact_required"]),
            escalation_severity=Severity(cfg["escalation_severity"]),
            critical_information=tuple(Category(c) for c in cfg["critical_information"]),
            category_labels={Category(c): str(v) for c, v in cfg.get("category_labels", {}).items()},
        )
    missing = [s.value for s in Scenario if s not in scenarios]
    if missing:
        raise ValueError(f"scenarios not configured: {', '.join(missing)}")

    return Rules(
        version=str(raw["version"]),
        weights=weights,
        ready_threshold=ready,
        clarification_threshold=clarification,
        status_credit=credit,
        critical_blocker_penalty=int(raw["blocker_penalty"]["critical"]),
        unmanaged_risk_penalty=int(raw["blocker_penalty"]["unmanaged_risk"]),
        unknown_dependency_score=int(raw["unknown_dependency_score"]),
        urgent_window_days=int(raw["urgent_window_days"]),
        placeholders=frozenset(str(v).strip().casefold() for v in raw["placeholder_values"]),
        scenarios=scenarios,
        classification_keywords={
            Category(c): tuple(str(k).casefold() for k in words)
            for c, words in raw.get("classification_keywords", {}).items()
        },
    )
