# Business Workflow

## Process

1. The outgoing owner (or the receiver) records the handover: who, to whom, when, and the knowledge entries.
2. The app analyzes it against the rules of the chosen scenario.
3. Gaps are reviewed and corrected with the outgoing owner. Each analysis saves a revision if the content changed.
4. The receiver reviews the findings and records a decision on the current revision.
5. The handover package (Markdown/JSON) is the record of what was handed over and what stayed open.

**Input:** handover details + entries in ten categories (form, JSON file, or TXT notes).
**Output:** readiness score and status, gap list with actions, before/after comparison, handover package, decision record.

## Entries

Each entry has: title, details, owner, due date, state (open/completed), critical flag, escalation-contact flag,
evidence reference. Categories: responsibilities, active work, decisions, documentation, dependencies, deadlines,
risks and blockers, ownership and contacts, access prerequisites, outstanding questions.

## Scenarios and required knowledge

Severity is what a *missing* required category costs. Configured in `app/config/rules.json`.

| Scenario | Required categories (severity if missing) | Escalation contact |
|---|---|---|
| Employee Departure | Responsibilities (critical), active work, documentation, deadlines, contacts (high) | Required (high) |
| Freelancer Project Completion | Deliverables and outstanding work (critical), ongoing responsibilities, known limitations, documentation (high) | Not required |
| Temporary Team Handover | Pending activities, deadlines, escalation contacts (critical), known blockers (high), recurring tasks (medium) | Required (critical) |

In the freelancer scenario, completed work items are *deliverables*; open ones are *outstanding work* and need an owner.

## Knowledge status per category

| Status | Rule |
|---|---|
| Missing | No entry with a meaningful title, and no not-applicable reason. |
| Present but unverified | Entries exist; at least one has no evidence reference. |
| Verified by provided evidence | Every entry has an evidence reference. |
| Not applicable | No entries and an explicit reason. Excluded from the score — never counted as verified. |

Placeholders (`TBD`, `n/a`, `?`, `unknown`, …) count as empty everywhere, including as a not-applicable reason.

## Gap rules

| Rule | Detects | Severity |
|---|---|---|
| `HEADER-RECEIVER`, `HEADER-SAME-OWNER` | No receiver, or receiver equals outgoing owner | Critical |
| `HEADER-OUTGOING`, `HEADER-DATE` | No outgoing owner; handover date missing or invalid | High |
| `HEADER-TITLE` | No title | Medium |
| `REQ-MISSING` | Required category missing | Per scenario |
| `ESCALATION-MISSING` | Contacts exist but none is flagged as escalation contact | Per scenario |
| `OWNER-UNASSIGNED`, `OWNER-OUTGOING` | Open responsibility, work item or deadline with no owner, or still owned by the outgoing owner | High; critical if the entry is critical or the deadline is urgent |
| `DEADLINE-OVERDUE` | Open deadline dated before the handover date | High |
| `DEADLINE-NODATE`, `DEADLINE-INVALID` | Deadline without a date or with an unreadable one | Medium |
| `BLOCKER-CRITICAL` | Critical risk/blocker not resolved | Critical |
| `RISK-UNMANAGED` | Open non-critical risk without owner or without impact/mitigation text | Medium |
| `DEP-NOOWNER` | Dependency without a named owner | Medium; high if critical |
| `ACCESS-NODETAIL` | Access prerequisite without instructions | Medium |
| `QUESTION-OPEN` | Unanswered question | Medium; high if critical |
| `PLACEHOLDER`, `CONTRADICTION-NA` | Placeholder text; category marked not applicable but has entries | Medium |
| `UNVERIFIED`, `DATE-INVALID` | Required category lacks evidence references; unreadable date on a non-deadline entry | Low |

*Urgent* = due within 14 days after the handover date (configurable). A risk or question counts as *resolved* only
when it is marked completed **and** has an evidence reference (the resolution or the answer).

## Readiness score

Weighted average of six 0–100 dimensions:

| Dimension | Weight | Measures |
|---|---|---|
| Knowledge completeness | 30 | Required categories: verified 1.0, unverified 0.7, missing 0 |
| Ownership clarity | 20 | Share of open responsibilities, work items and deadlines with a new owner |
| Deadline sensitivity | 15 | Share of open deadlines with a valid future date and owner; urgent ones count double |
| Unresolved blockers | 15 | 100 minus 50 per critical blocker and 15 per unmanaged risk |
| Dependency exposure | 10 | Share of dependencies with an owner; 50 if none are listed (unknown, not safe) |
| Critical information | 10 | Scenario-critical categories present, plus the escalation contact |

## Readiness status

Checked in this order:

1. Any critical gap → **High Risk** (blocking rule, regardless of score)
2. Score below 50 → **High Risk**
3. Any high gap → **Needs Clarification** (regardless of score)
4. Score below 80 → **Needs Clarification**
5. Otherwise → **Ready for Review**

## Acceptance (separate from readiness)

| Decision | Allowed when |
|---|---|
| Request Clarification | Always. |
| Accept Handover | Only when status is Ready for Review (so no critical or high gaps). |
| Accept with Exceptions | A note is given and every critical and high gap is explicitly selected as an accepted exception. |

Acceptance states: Pending Review, Clarification Requested, Accepted, Accepted with Exceptions.
A decision belongs to one revision. If the handover content changes afterwards, the state returns to
Pending Review and the earlier decision is shown as superseded.
