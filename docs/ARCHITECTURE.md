# Architecture

A single-process Streamlit application with a local SQLite file. The diagram is in
[assets/architecture/workflow.md](../assets/architecture/workflow.md).

## Components

| Module | Responsibility | Depends on |
|---|---|---|
| `app/main.py` | Streamlit UI: collects input, renders results. No business rules. | services |
| `app/models.py` | Pydantic data contracts: `Handover`, `KnowledgeItem`, `Gap`, `AnalysisResult`, `AcceptanceRecord`. | — |
| `app/rules.py` + `app/config/rules.json` | Loads and validates weights, thresholds, scenario requirements, placeholders. | models |
| `app/services/intake.py` | Parses untrusted JSON/TXT, normalizes enum casing, generates missing ids, classifies text lines. | models, rules |
| `app/services/completeness_engine.py` | One status per category: missing / unverified / verified / not applicable. | common |
| `app/services/gap_analyzer.py` | 21 rules that turn observations into traceable gaps. | common |
| `app/services/risk_engine.py` | Six dimension scores, weighted total, blocking rules, readiness status. | common |
| `app/services/common.py` | Shared definitions ("assigned", "resolved", "urgent") so all engines agree. | models, rules |
| `app/services/handover_service.py` | Pipeline, corrections, before/after comparison, acceptance safeguards, demo data. | all of the above, storage |
| `app/services/report_service.py` | Builds the handover package (JSON) and renders Markdown. | models |
| `app/storage/repository.py` | SQLite: handovers, append-only revisions, decisions. | models |
| `app/providers/` | Optional suggestions: `MockInsightProvider` (default), `OptionalLLMInsightProvider`. | models |
| `app/observability.py` | Key=value logging and in-process counters. | — |

## Data flow

```
input -> intake (validate) -> Handover
Handover -> completeness -> gaps -> risk -> AnalysisResult
Handover -> repository.save -> revision N (only if content changed)
AnalysisResult + Handover + decision -> report package -> Markdown / JSON
```

Analysis results are **never stored**. Scores shown anywhere, including Before / After, are recomputed from the
stored revision by the same code. Stored: handover content per revision (with SHA-256 content hash) and receiver decisions.

## Design choices

- **Rules, not a model, decide.** Every finding must be explainable to the person receiving the handover.
- **Gap ids are stable** (`<RULE>:<item or category>`). That makes before/after comparison a set difference.
- **Corrections are data edits.** `apply_corrections` returns a new validated handover; a gap closes only if a fresh analysis no longer produces it.
- **Canonical item order** (by category) so identical content always yields the same hash and no accidental revisions.
- **Lenient on business gaps, strict on structure.** An empty owner or unreadable date is accepted and reported; unknown fields, unknown scenarios, duplicate ids and oversized values are rejected.
- **SQLite via the standard library**, behind a small repository class. One connection per operation keeps it safe with Streamlit's threads.
- **Acceptance is bound to a revision.** A decision counts only while it refers to the latest revision.
- **UI state:** the workspace keeps one source handover in session state; input widgets are keyed by a version counter that is bumped whenever the source is replaced.

## What was deliberately left out

No web API, no ORM, no background workers, no vector store, no agent framework, no separate database or
monitoring containers. None of them would make the handover check better at this size.
