"""Intake: turns untrusted input (uploads, form data) into validated models.

Uploaded content is only ever parsed as data (json.loads / plain text). Nothing
is evaluated, imported, unpickled or written to a caller-supplied path.
"""

from __future__ import annotations

import json
import uuid

from pydantic import ValidationError

from app.models import CATEGORY_LABELS, ID_PREFIXES, Category, Handover, KnowledgeItem, Scenario
from app.rules import Rules

MAX_UPLOAD_BYTES = 200_000
MAX_TEXT_LINES = 300

# Line prefixes accepted in plain-text notes, e.g. "Deadline: ...".
PREFIX_ALIASES: dict[str, Category] = {
    "responsibility": Category.RESPONSIBILITIES,
    "task": Category.ACTIVE_WORK,
    "work": Category.ACTIVE_WORK,
    "decision": Category.DECISIONS,
    "document": Category.DOCUMENTATION,
    "dependency": Category.DEPENDENCIES,
    "deadline": Category.DEADLINES,
    "risk": Category.RISKS_BLOCKERS,
    "blocker": Category.RISKS_BLOCKERS,
    "contact": Category.CONTACTS,
    "escalation": Category.CONTACTS,
    "question": Category.OPEN_QUESTIONS,
}
for _category in Category:
    PREFIX_ALIASES[_category.value] = _category
    PREFIX_ALIASES[CATEGORY_LABELS[_category].lower().replace(" ", "_")] = _category


class IntakeError(ValueError):
    """Input was rejected. The message is safe to show to the user."""


def new_handover_id() -> str:
    return f"ho-{uuid.uuid4().hex[:8]}"


def new_handover(scenario: Scenario = Scenario.EMPLOYEE_DEPARTURE) -> Handover:
    return Handover(id=new_handover_id(), scenario=scenario)


def next_item_id(existing: set[str], category: Category) -> str:
    prefix = ID_PREFIXES[category]
    number = 1
    while f"{prefix}-{number}" in existing:
        number += 1
    return f"{prefix}-{number}"


def describe_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(p) for p in error["loc"]) or "handover"
        parts.append(f"{location}: {error['msg']}")
    more = len(exc.errors()) - len(parts)
    return "; ".join(parts) + (f" (+{more} more)" if more > 0 else "")


def _slug(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower().replace(" ", "_").replace("-", "_")
    return value


def normalize(raw: dict) -> dict:
    """Light, non-destructive normalization: casing of enums and missing ids.

    User-entered facts (titles, owners, dates, evidence) are never rewritten.
    """
    data = dict(raw)
    if "scenario" in data:
        data["scenario"] = _slug(data["scenario"])
    if not data.get("id"):
        data["id"] = new_handover_id()

    items = data.get("items", [])
    if not isinstance(items, list):
        raise IntakeError("'items' must be a list of knowledge items.")
    existing = {str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")}
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise IntakeError(f"Item {index + 1} must be an object.")
        entry = dict(item)
        if "category" in entry:
            entry["category"] = _slug(entry["category"])
        if "state" in entry:
            entry["state"] = _slug(entry["state"])
        if not entry.get("id"):
            try:
                category = Category(entry.get("category"))
            except ValueError as exc:
                raise IntakeError(f"Item {index + 1} has an unknown or missing category.") from exc
            entry["id"] = next_item_id(existing, category)
            existing.add(entry["id"])
        normalized.append(entry)
    data["items"] = normalized
    return data


def handover_from_dict(raw: object) -> Handover:
    if not isinstance(raw, dict):
        raise IntakeError("The handover must be a JSON object.")
    scenario = _slug(raw.get("scenario"))
    if scenario not in {s.value for s in Scenario}:
        allowed = ", ".join(s.value for s in Scenario)
        raise IntakeError(f"Unknown scenario '{raw.get('scenario')}'. Supported: {allowed}.")
    try:
        return Handover.model_validate(normalize(raw))
    except ValidationError as exc:
        raise IntakeError(f"Invalid handover data - {describe_validation_error(exc)}") from exc


def _decode(data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise IntakeError(
            f"File is too large ({len(data):,} bytes). The limit is {MAX_UPLOAD_BYTES:,} bytes."
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntakeError("File must be UTF-8 encoded text.") from exc


def parse_json_bytes(data: bytes) -> Handover:
    text = _decode(data)
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise IntakeError(f"Malformed JSON: {exc}") from exc
    return handover_from_dict(raw)


def classify_line(line: str, rules: Rules) -> tuple[Category, str, str]:
    """Classify one free-text line with explicit rules.

    Returns (category, text, how) where `how` is 'prefix', 'keyword' or 'unmatched'.
    """
    if ":" in line:
        head, _, rest = line.partition(":")
        category = PREFIX_ALIASES.get(_slug(head))
        if category and rest.strip():
            return category, rest.strip(), "prefix"
    lowered = line.casefold()
    for category, keywords in rules.classification_keywords.items():
        if any(keyword in lowered for keyword in keywords):
            return category, line, "keyword"
    return Category.OPEN_QUESTIONS, line, "unmatched"


def parse_text_bytes(data: bytes, rules: Rules, existing_ids: set[str] | None = None) -> tuple[list[KnowledgeItem], dict[str, int]]:
    """Convert plain-text notes (one fact per line) into classified items.

    Lines may start with a category prefix ("Deadline: ..."); otherwise keyword
    rules apply. Unmatched lines land in Outstanding questions, flagged for
    review, so nothing is dropped or silently guessed.
    """
    text = _decode(data)
    lines = [line.strip(" \t-*") for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise IntakeError("The text file contains no content.")
    if len(lines) > MAX_TEXT_LINES:
        raise IntakeError(f"Too many lines ({len(lines)}). The limit is {MAX_TEXT_LINES}.")

    ids = set(existing_ids or ())
    items: list[KnowledgeItem] = []
    stats = {"prefix": 0, "keyword": 0, "unmatched": 0}
    for line in lines:
        if len(line) > 2000:
            raise IntakeError("A line exceeds 2,000 characters. Split it into shorter facts.")
        category, body, how = classify_line(line, rules)
        stats[how] += 1
        title, detail = (body, "") if len(body) <= 200 else (body[:197] + "...", body)
        if how == "unmatched":
            detail = (detail + " " if detail else "") + "[Imported line could not be classified - please review its category]"
        item_id = next_item_id(ids, category)
        ids.add(item_id)
        items.append(KnowledgeItem(id=item_id, category=category, title=title, detail=detail))
    return items, stats
