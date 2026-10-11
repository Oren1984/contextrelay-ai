"""Small fact helpers shared by the completeness, gap and risk engines.

Keeping these in one place guarantees that the engines agree on what counts as
"assigned", "resolved" or "urgent".
"""

from __future__ import annotations

from datetime import date

from app.models import Category, Handover, ItemState, KnowledgeItem
from app.rules import Rules

# Open items in these categories need a named owner on the receiving side.
OWNABLE_CATEGORIES = (Category.RESPONSIBILITIES, Category.ACTIVE_WORK, Category.DEADLINES)


def parse_date(value: str) -> date | None:
    """Strict ISO (YYYY-MM-DD) parsing; anything else is reported as invalid."""
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def is_meaningful(item: KnowledgeItem, rules: Rules) -> bool:
    """An item only counts as provided knowledge if its title says something."""
    return not rules.is_blank(item.title)


def is_assigned(item: KnowledgeItem, handover: Handover, rules: Rules) -> bool:
    """Owned by someone other than the person who is handing over."""
    if rules.is_blank(item.owner):
        return False
    return item.owner.casefold() != handover.outgoing_owner.casefold()


def is_resolved(item: KnowledgeItem, rules: Rules) -> bool:
    """Risks and questions are only resolved when a resolution reference exists."""
    return item.state == ItemState.COMPLETED and not rules.is_blank(item.evidence)


def is_open(item: KnowledgeItem) -> bool:
    return item.state == ItemState.OPEN


def is_urgent(item: KnowledgeItem, handover: Handover, rules: Rules) -> bool:
    """Due within the configured window after the handover date."""
    due, target = parse_date(item.due_date), parse_date(handover.target_date)
    if due is None or target is None:
        return False
    return 0 <= (due - target).days <= rules.urgent_window_days


def is_overdue(item: KnowledgeItem, handover: Handover) -> bool:
    due, target = parse_date(item.due_date), parse_date(handover.target_date)
    return due is not None and target is not None and due < target


def meaningful_items(handover: Handover, category: Category, rules: Rules) -> list[KnowledgeItem]:
    return [item for item in handover.items_in(category) if is_meaningful(item, rules)]
