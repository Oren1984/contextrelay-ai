# Limitations

## Known limitations

- **Single local user.** No authentication, roles or concurrent editing. The receiver label is free text.
- **Acceptance is a local record.** Not a signature, not identity verification, not a compliance certificate.
- **"Verified" means "a reference was given".** The app does not open, fetch or validate the referenced document or ticket.
- **The score is a heuristic.** Weights and thresholds are reasonable defaults, not calibrated against real handover outcomes.
- **Rules are generic.** They cannot judge whether a description is actually sufficient, only whether it is present.
- **Text import is keyword-based** and English-only. Misclassification is expected; unmatched lines go to *Outstanding questions* for review.
- **Dates must be `YYYY-MM-DD`.** Urgency is measured from the target handover date, not from today, so results are reproducible.
- **Owner matching is by exact name** (case-insensitive). "D. Whitfield" and "Dana Whitfield" are different people to the app.
- **Counters are in memory** and reset on restart. Logs go to the container output only.
- **Scenario requirements are edited in a JSON file**, not in the UI.
- **Optional LLM mode was not run against the live API** in this project, and the Docker image does not include its dependency. Its fallback and validation logic are tested.
- **Docker image is about 780 MB**, almost all of it Streamlit's own dependencies.
- **UI automation gap:** table-cell editing and file downloads were not exercised by automated tests.
- **Gap table:** on narrow screens the *Status* column requires horizontal scrolling.

## Non-goals

Production deployment, multi-tenant use, cloud hosting, HR/ticketing/document-system integrations, legally binding
sign-off, regulatory compliance, document parsing (PDF/Word), and measuring real productivity effects.

## Possible next steps

- CSV import and PDF export.
- Per-organisation rule sets and a small rule editor.
- Evidence links that can be checked (file exists, ticket is closed).
- Clarification requests as a tracked back-and-forth between outgoing owner and receiver.
- A browser-level regression test in CI.
- Calibrating weights with feedback from real handovers.
