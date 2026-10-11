# Demo Script

All data is synthetic. The numbers below were observed with rules version 1.0 and are reproduced by the automated
tests (`tests/e2e`). If you change `app/config/rules.json` or the fixtures, they will change.

## Setup

```bash
docker compose up --build
```

Open <http://localhost:8501>. To start from a clean state at any time, select **Reset demo data** in the sidebar
(this removes stored copies of the three examples only).

## Main demo: Employee Departure, before and after

| Step | Action | What you should see |
|---|---|---|
| 1 | **Load Synthetic Example** (Employee Departure is preselected) | Dana Whitfield → Marcus Oyelaran, 17 entries in the category sections |
| 2 | **Analyze Handover**, open **Readiness Dashboard** | Score **50 / 100**, **16** gaps, **2** critical, ownership coverage **43%**, status **High Risk** — "Blocked by 2 unresolved critical gap(s)" |
| 3 | Open **Knowledge Gaps** | Two critical gaps on top: the unresolved payment-tool blocker and the month-end deadline with no owner |
| 4 | Still on **Knowledge Gaps**: for the critical blocker, set *State* to `completed`, leave the evidence empty, **Apply correction and re-check** | Warning: the gap is **still open** — a resolution reference is required |
| 5 | Open **Handover Package**, choose **Accept Handover**, **Record decision** | Refused: unconditional acceptance is not possible with critical gaps |
| 6 | Open **Before / After**, **Apply sample corrections** | Readiness **50 → 91**, open gaps **16 → 7**, critical blockers **2 → 0**, resolved / remaining **9 / 7**, status High Risk → Ready for Review |
| 7 | Open **Handover Package**, **Accept Handover**, **Record decision** | Final handover status **Accepted** (revision 2 if you skipped step 4, revision 3 if you did it) |
| 8 | **Download as Markdown** / **Download as JSON** | The package with knowledge, gaps, score, decision and evidence references |

The nine sample corrections are listed in `data/synthetic/corrections/employee_departure.json`: five owner
assignments, one deadline date, one blocker resolved with a reference, one escalation contact flagged, one access
instruction added. Seven gaps remain on purpose: an unanswered question, a "TBD" description and five
missing-evidence notes.

### Optional: acceptance is invalidated by a change

After step 7, go to **Handover Workspace**, change any field (for example the title) and **Analyze Handover** again.
The **Handover Package** tab now shows *Pending Review* and a notice that the earlier decision was superseded.

## The other two scenarios

| Example | Score | Status | Why it is interesting |
|---|---|---|---|
| Freelancer Project Completion | 91 | Needs Clarification | High score, but one outstanding work item has no owner. Status is capped by the high gap. Delivered and outstanding work are listed separately in the report. |
| Temporary Team Handover | 48 | High Risk | 3 critical gaps: an unresolved blocker, no escalation contact, a deadline one day after the handover with no owner. Can only be accepted *with exceptions*. |

## Importing your own notes

Under **Import a file or open a saved handover**, upload a `.txt` file with one fact per line, for example:

```
Deadline: File the quarterly statement
The VPN access must be requested two days ahead
Something nobody can place
```

The app reports how each line was classified (by prefix, by keyword, or unmatched). Review the result in the
category sections and use **Reclassify an entry** to correct it.
