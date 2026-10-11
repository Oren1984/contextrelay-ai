# Assurance

What was checked, how, and what the checks do not cover.

## Tests

`python -m pytest` — 97 tests, all passing at the time of writing (also run in a clean `python:3.12-slim` container
with only `requirements-dev.txt` installed).

| Suite | Tests | Covers |
|---|---|---|
| `tests/unit` | 75 | Intake validation, scenario requirements, knowledge states, every gap rule family, score arithmetic, blocking rules, rule-config validation, report content, safe filenames, Mock determinism, LLM fallback |
| `tests/integration` | 15 | Upload → pipeline → report; correction → re-analysis; save → reload; revision tracking; integrity check; demo reset; acceptance safeguards; revision → acceptance invalidation |
| `tests/e2e` | 7 | Ten-step business workflow at service level; the same workflow through the Streamlit UI with `streamlit.testing.v1.AppTest`; all three scenarios; empty handover |

Negative and boundary cases, with where they are tested:

| Case | Test file |
|---|---|
| Empty handover | `test_gap_analyzer`, `test_risk_engine`, `test_streamlit_ui` |
| Malformed JSON, non-UTF-8, non-object, oversized upload | `test_intake` |
| Unknown scenario, duplicate identifiers, unknown fields, unsafe id, overlong values | `test_intake` |
| Missing ownership, same outgoing/receiving owner (contradiction) | `test_gap_analyzer` |
| Invalid, missing and overdue deadlines; invalid handover date | `test_gap_analyzer` |
| Not applicable with entries (contradiction) | `test_gap_analyzer` |
| Unresolved critical blocker hidden behind a high score | `test_risk_engine` |
| All fields completed but not verified | `test_gap_analyzer`, `test_risk_engine` |
| Closing a gap without the missing information | `test_pipeline_and_storage`, `test_streamlit_ui` |
| Acceptance attempted with blocking issues | `test_pipeline_and_storage`, `test_business_workflow`, `test_streamlit_ui` |
| Handover changed after acceptance | `test_pipeline_and_storage` |
| Optional LLM unavailable or returning invalid output | `test_report_and_providers` |
| Storage error, tampered or corrupt record | `test_pipeline_and_storage` |

**Also verified by hand-driven browser automation against the Docker container** (headless Chromium, not part of
the pytest suite): the full demo flow, the five screenshots, rejection of a malformed and an oversized upload,
TXT and JSON import, and editing a handover detail field.

**Not covered by automation:** typing into the table editors (`st.data_editor`) and clicking the download buttons.
The table-to-model conversion runs in every UI test, but with unedited tables.

## Security decisions

| Risk | Control |
|---|---|
| Malicious or malformed upload | Parsed only with `json.loads` or as plain text. 200 KB limit in the app, 1 MB at the server. Strict schema: unknown fields rejected, lengths bounded, max 500 entries, ids restricted to `[A-Za-z0-9_.-]`. |
| Code execution / unsafe deserialization | No `eval`, `exec`, `pickle`, YAML loading, subprocess or dynamic import in `app/` (checked by pattern scan). |
| Path traversal | Uploads are never written to disk. Fixture paths come from an enum. Download names are built by `safe_filename` from a restricted character set. The database path comes from server configuration only. |
| SQL injection | All queries are parameterized. |
| Secrets | None in the repository (pattern scan clean). `.env` is git-ignored; `.env.example` holds empty placeholders. Optional API key is read from the environment only. |
| Outbound network | None by default. Streamlit usage statistics and external-IP lookup are disabled in `.streamlit/config.toml`. The container port is bound to `127.0.0.1`. |
| Vulnerable dependencies | `pip-audit -r requirements.txt` reported no known vulnerabilities (run once, in a throwaway container). Versions are pinned. |
| Prompt injection (LLM mode only) | The model receives rule findings wrapped as data, with an instruction to treat them as untrusted. Its output is schema-validated and length-limited, is labeled as unverified, and has no path to scores, gaps or acceptance. |
| Sensitive data in logs | Logs contain ids, counts, durations and status only (tested). |
| Container privileges | Runs as a non-root user; only `/data` is writable application state. |

**Honest limits:** no authentication, no encryption at rest, no rate limiting, no audit-grade logging. Markdown in the
report preview is rendered by Streamlit with HTML disabled (its default). This is a local single-user tool; do not
expose it on a network or enter real confidential data.

## Observability

- **Logs** (stderr, `key=value`): `analysis_completed` (duration, gap count, critical count, score, status), `analysis_failed`, `revision_saved`, `decision_recorded`, `demo_reset`, `llm_fallback`.
- **Counters** (in process, shown under *Run diagnostics* in the sidebar): analyses succeeded/failed, last and average duration, last gap count. They reset when the process restarts.
- **Health:** the container health check calls Streamlit's `/_stcore/health`; `docker compose ps` shows `healthy`.
- **Errors:** intake, correction, decision, storage and rule-config errors are caught and shown as plain messages.

## Governance and responsible use

- Demo data is synthetic and labeled as such in the fixtures, the UI and the report.
- The score is described everywhere as a rule-based heuristic, with its weights and reasons visible.
- AI suggestions are optional, labeled, and separated: *source data → rule findings → suggestions*.
- A person makes the acceptance decision. The app only enforces the safeguards.
- Acceptance is a local record, not identity proof, a signature or a compliance statement.

## Traceability

| Question | Where to look |
|---|---|
| Why does this gap exist? | `Gap.rule_id`, `Gap.source` (rule + item or category), shown under each gap in the UI |
| Which rule version produced the result? | `rules_version` in the analysis and in the report |
| What changed between two states? | Revisions in SQLite; Before / After recomputes both |
| Who decided what, on which content? | `decisions` table: receiver label, decision, note, accepted exceptions, revision, score, UTC timestamp |
| Is a stored revision intact? | SHA-256 content hash checked on every load |
