"""ContextRelay AI - Streamlit interface.

The UI only collects input and renders results. Every number shown comes from
the services in app/services, which have no Streamlit dependency.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `streamlit run app/main.py` puts app/ on the path, not the project root
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from app.models import (
    CATEGORY_LABELS,
    STATUS_LABELS,
    AnalysisResult,
    Category,
    Decision,
    Gap,
    Handover,
    ItemState,
    Scenario,
)
from app.observability import METRICS
from app.providers import get_provider
from app.rules import RulesError, load_rules
from app.services import report_service
from app.services.gap_analyzer import HEADER
from app.services.handover_service import (
    CorrectionError,
    DecisionError,
    HandoverService,
    apply_corrections,
    compare,
)
from app.services.intake import (
    MAX_UPLOAD_BYTES,
    IntakeError,
    describe_validation_error,
    new_handover,
    next_item_id,
    parse_json_bytes,
    parse_text_bytes,
)
from app.storage.repository import Repository, StorageError

DEFAULT_DB_PATH = str(ROOT / "data" / "local" / "contextrelay.db")

FIELD_LABELS = {
    "title": "Title",
    "detail": "Details",
    "owner": "Owner",
    "due_date": "Due date (YYYY-MM-DD)",
    "state": "State",
    "critical": "Critical",
    "escalation": "Escalation contact",
    "evidence": "Evidence reference",
}
BOOL_FIELDS = ("critical", "escalation")

# Which fields are edited per category (other fields keep their stored values).
CATEGORY_COLUMNS: dict[Category, list[str]] = {
    Category.RESPONSIBILITIES: ["title", "detail", "owner", "critical", "evidence"],
    Category.ACTIVE_WORK: ["title", "detail", "owner", "due_date", "state", "evidence"],
    Category.DECISIONS: ["title", "detail", "evidence"],
    Category.DOCUMENTATION: ["title", "detail", "evidence"],
    Category.DEPENDENCIES: ["title", "detail", "owner", "critical", "evidence"],
    Category.DEADLINES: ["title", "due_date", "owner", "state", "evidence"],
    Category.RISKS_BLOCKERS: ["title", "detail", "owner", "critical", "state", "evidence"],
    Category.CONTACTS: ["title", "detail", "escalation", "evidence"],
    Category.ACCESS: ["title", "detail", "owner", "evidence"],
    Category.OPEN_QUESTIONS: ["title", "detail", "owner", "critical", "state", "evidence"],
}

# Which fields a gap of a given rule can be corrected with.
ITEM_FIX_FIELDS: dict[str, list[str]] = {
    "OWNER-UNASSIGNED": ["owner"],
    "OWNER-OUTGOING": ["owner"],
    "DEADLINE-NODATE": ["due_date"],
    "DEADLINE-INVALID": ["due_date"],
    "DEADLINE-OVERDUE": ["due_date", "state"],
    "DATE-INVALID": ["due_date"],
    "BLOCKER-CRITICAL": ["state", "evidence"],
    "RISK-UNMANAGED": ["owner", "detail"],
    "DEP-NOOWNER": ["owner"],
    "ACCESS-NODETAIL": ["detail"],
    "QUESTION-OPEN": ["state", "evidence"],
    "PLACEHOLDER": ["title", "detail"],
}


@st.cache_resource
def get_service(db_path: str) -> HandoverService:
    return HandoverService(Repository(db_path), load_rules())


def widget_key(*parts: object) -> str:
    return "_".join(re.sub(r"[^A-Za-z0-9]", "_", str(part)) for part in parts)


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


def set_source(handover: Handover, notice: tuple[str, str] | None = None, keep_analysis: bool = False) -> None:
    """Replace the working copy. Bumping `ver` resets all input widgets to it."""
    st.session_state.source = handover
    st.session_state.ver = st.session_state.get("ver", 0) + 1
    if not keep_analysis:
        st.session_state.analysis = None
        st.session_state.analyzed = None
        st.session_state.revision = 0
    if notice:
        st.session_state.notice = notice


def store_analysis(handover: Handover, result: AnalysisResult, revision: int) -> None:
    st.session_state.analysis = result
    st.session_state.analyzed = handover
    st.session_state.revision = revision


def init_state() -> None:
    if "source" not in st.session_state:
        set_source(new_handover())
        st.session_state.notice = None


# --------------------------------------------------------------------------
# Callbacks (run before the script, so they may replace widget defaults)
# --------------------------------------------------------------------------


def cb_load_example(service: HandoverService) -> None:
    scenario = st.session_state.example_choice
    try:
        handover = service.load_fixture(scenario)
    except IntakeError as exc:
        st.session_state.notice = ("error", str(exc))
        return
    label = service.rules.for_scenario(scenario).label
    set_source(handover, ("success", f"Loaded the synthetic example '{label}'. Review it, then select Analyze Handover."))


def cb_new_blank() -> None:
    set_source(new_handover(), ("info", "Started a new, empty handover."))


def cb_open_saved(service: HandoverService) -> None:
    handover_id = st.session_state.get("saved_choice")
    if not handover_id:
        return
    try:
        handover, revision, result = service.analyze_revision(handover_id)
    except StorageError as exc:
        st.session_state.notice = ("error", str(exc))
        return
    set_source(handover, ("success", f"Opened saved handover '{handover_id}' (revision {revision})."))
    store_analysis(handover, result, revision)


def cb_reset_demo(service: HandoverService) -> None:
    try:
        removed = service.reset_demo()
    except (StorageError, IntakeError) as exc:
        st.session_state.notice = ("error", str(exc))
        return
    current: Handover = st.session_state.source
    if current.id.startswith("demo-"):
        set_source(service.load_fixture(current.scenario))
    st.session_state.notice = (
        "success",
        f"Demo data reset: {removed} stored synthetic example(s) removed. Handovers you created were not touched.",
    )


# --------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------


def editor_frame(handover: Handover, category: Category) -> pd.DataFrame:
    columns = ["id"] + CATEGORY_COLUMNS[category]
    rows = [{col: getattr(item, col) for col in columns} for item in handover.items_in(category)]
    for row in rows:
        if "state" in row:
            row["state"] = row["state"].value
    frame = pd.DataFrame(rows, columns=columns)
    for col in columns:
        frame[col] = frame[col].astype(bool) if col in BOOL_FIELDS else frame[col].astype(object)
    return frame


def column_config(category: Category) -> dict:
    config: dict = {"id": st.column_config.TextColumn("ID", disabled=True, width="small")}
    for col in CATEGORY_COLUMNS[category]:
        if col in BOOL_FIELDS:
            config[col] = st.column_config.CheckboxColumn(FIELD_LABELS[col], default=False)
        elif col == "state":
            config[col] = st.column_config.SelectboxColumn(
                FIELD_LABELS[col], options=[s.value for s in ItemState], default=ItemState.OPEN.value, required=True
            )
        else:
            config[col] = st.column_config.TextColumn(FIELD_LABELS[col], width="large" if col == "detail" else None)
    return config


def _text(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()


def frame_to_items(frame: pd.DataFrame, category: Category, source: Handover, used_ids: set[str]) -> list[dict]:
    stored = {item.id: item.model_dump(mode="json") for item in source.items}
    items = []
    for row in frame.to_dict("records"):
        visible = {}
        for col in CATEGORY_COLUMNS[category]:
            if col in BOOL_FIELDS:
                visible[col] = bool(row.get(col)) if _text(row.get(col)) else False
            elif col == "state":
                visible[col] = _text(row.get(col)) or ItemState.OPEN.value
            else:
                visible[col] = _text(row.get(col))
        item_id = _text(row.get("id"))
        if not item_id:
            if not any(visible[c] for c in visible if c not in BOOL_FIELDS and c != "state"):
                continue  # an untouched new row
            item_id = next_item_id(used_ids, category)
        used_ids.add(item_id)
        item = dict(stored.get(item_id, {}))
        item.update(visible)
        item.update({"id": item_id, "category": category.value})
        items.append(item)
    return items


def render_workspace(service: HandoverService) -> Handover | None:
    source: Handover = st.session_state.source
    ver = st.session_state.ver
    rules = service.rules

    st.subheader("Start from an example or your own data")
    c1, c2, c3 = st.columns([3, 2, 2], vertical_alignment="bottom")
    c1.selectbox(
        "Synthetic example scenario",
        list(Scenario),
        format_func=lambda s: rules.for_scenario(s).label,
        key="example_choice",
    )
    c2.button("Load Synthetic Example", on_click=cb_load_example, args=(service,), width="stretch", key="load_example")
    c3.button("New empty handover", on_click=cb_new_blank, width="stretch", key="new_blank")

    with st.expander("Import a file or open a saved handover"):
        left, right = st.columns(2)
        uploaded = left.file_uploader(
            f"Handover file (JSON for a full handover, TXT for notes - max {MAX_UPLOAD_BYTES // 1000} KB)",
            type=["json", "txt"],
            key=f"upload_{ver}",
        )
        left.caption("TXT: one fact per line, optionally prefixed with a category, e.g. `Deadline: submit report`.")
        import_clicked = left.button("Import file", disabled=uploaded is None, key="import_file")
        saved = service.repository.list_handovers()
        if saved:
            right.selectbox(
                "Saved handovers",
                [row["id"] for row in saved],
                format_func=lambda i: next(f"{r['title'] or i} (rev {r['revision']})" for r in saved if r["id"] == i),
                key="saved_choice",
            )
            right.button("Open saved handover", on_click=cb_open_saved, args=(service,), key="open_saved")
        else:
            right.info("No saved handovers yet. A handover is saved when you analyze it.")

    st.subheader("Handover details")
    c1, c2 = st.columns(2)
    scenario = c1.selectbox(
        "Scenario type",
        list(Scenario),
        index=list(Scenario).index(source.scenario),
        format_func=lambda s: rules.for_scenario(s).label,
        key=f"scenario_{ver}",
    )
    title = c2.text_input("Handover title", value=source.title, max_chars=200, key=f"title_{ver}")
    c1, c2, c3 = st.columns(3)
    outgoing = c1.text_input("Outgoing owner", value=source.outgoing_owner, max_chars=120, key=f"outgoing_{ver}")
    receiving = c2.text_input("Receiving owner", value=source.receiving_owner, max_chars=120, key=f"receiving_{ver}")
    target = c3.text_input("Target handover date (YYYY-MM-DD)", value=source.target_date, max_chars=40, key=f"target_{ver}")
    description = st.text_area("Description", value=source.description, max_chars=4000, height=80, key=f"description_{ver}")

    st.subheader("Structured knowledge")
    scenario_rules = rules.for_scenario(scenario)
    st.caption(
        "Add, edit or delete rows per category. Categories marked *required* come from the scenario rules. "
        "An *evidence reference* (document, ticket, file name) marks an entry as verified. Never enter passwords or keys."
    )
    used_ids = {item.id for item in source.items}
    items: list[dict] = []
    not_applicable: dict[str, str] = {}
    for category in Category:
        count = len(source.items_in(category))
        required = " · required" if category in scenario_rules.required else ""
        with st.expander(f"{scenario_rules.label_for(category)} ({count}){required}", expanded=False):
            edited = st.data_editor(
                editor_frame(source, category),
                column_config=column_config(category),
                num_rows="dynamic",
                hide_index=True,
                width="stretch",
                key=f"ed_{category.value}_{ver}",
            )
            reason = st.text_input(
                "Not applicable? Give the reason (leave empty if this category applies)",
                value=source.not_applicable.get(category, ""),
                max_chars=500,
                key=f"na_{category.value}_{ver}",
            )
            items.extend(frame_to_items(edited, category, source, used_ids))
            if reason.strip():
                not_applicable[category.value] = reason.strip()

    try:
        working = Handover.model_validate(
            {
                "id": source.id,
                "scenario": scenario,
                "title": title,
                "description": description,
                "outgoing_owner": outgoing,
                "receiving_owner": receiving,
                "target_date": target,
                "items": items,
                "not_applicable": not_applicable,
            }
        )
    except ValidationError as exc:
        st.error(f"The handover cannot be analyzed yet - {describe_validation_error(exc)}")
        return None

    if import_clicked and uploaded is not None:
        import_file(service, uploaded, working)

    with st.expander("Reclassify an entry"):
        st.caption("Every entry sits in exactly one category. Move it here if it was classified incorrectly.")
        if working.items:
            c1, c2, c3 = st.columns([3, 2, 1], vertical_alignment="bottom")
            item_id = c1.selectbox(
                "Entry",
                [item.id for item in working.items],
                format_func=lambda i: next(f"{i} - {it.title[:60]} [{CATEGORY_LABELS[it.category]}]" for it in working.items if it.id == i),
                key=f"move_item_{ver}",
            )
            new_category = c2.selectbox("Move to", list(Category), format_func=CATEGORY_LABELS.get, key=f"move_cat_{ver}")
            if c3.button("Move", key=f"move_btn_{ver}"):
                moved = apply_corrections(working, [{"op": "move_item", "item_id": item_id, "category": new_category.value}])
                set_source(moved, ("success", f"Moved '{item_id}' to {CATEGORY_LABELS[new_category]}. Run the analysis again."))
                st.rerun()
        else:
            st.info("No entries yet.")

    st.divider()
    if st.button("Analyze Handover", type="primary", key="analyze"):
        run_analysis(service, working)
    st.caption(f"Handover ID: `{working.id}` · analyzing saves a revision to the local database.")
    return working


def import_file(service: HandoverService, uploaded, working: Handover) -> None:
    try:
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise IntakeError(f"File is too large. The limit is {MAX_UPLOAD_BYTES:,} bytes.")
        data = uploaded.getvalue()
        if uploaded.name.lower().endswith(".json"):
            handover = parse_json_bytes(data)
            set_source(handover, ("success", f"Imported handover '{handover.id}' with {len(handover.items)} entries."))
        else:
            new_items, stats = parse_text_bytes(data, service.rules, {i.id for i in working.items})
            merged = working.model_copy(update={"items": working.items + new_items})
            merged = Handover.model_validate(merged.model_dump(mode="json"))
            set_source(
                merged,
                (
                    "success",
                    f"Imported {len(new_items)} lines: {stats['prefix']} by category prefix, {stats['keyword']} by keyword rule, "
                    f"{stats['unmatched']} unmatched (placed under Outstanding questions). Please review the classification.",
                ),
            )
    except (IntakeError, ValidationError) as exc:
        message = describe_validation_error(exc) if isinstance(exc, ValidationError) else str(exc)
        st.error(f"Import rejected: {message}")
        return
    st.rerun()


def run_analysis(service: HandoverService, handover: Handover) -> bool:
    try:
        revision, result = service.save_and_analyze(handover)
    except StorageError as exc:
        st.error(f"The handover could not be saved: {exc}")
        return False
    store_analysis(handover, result, revision)
    return True


# --------------------------------------------------------------------------
# Readiness dashboard
# --------------------------------------------------------------------------


def status_banner(result: AnalysisResult) -> None:
    text = f"**{result.readiness_status.value}** - {result.status_reason}"
    {"Ready for Review": st.success, "Needs Clarification": st.warning}.get(result.readiness_status.value, st.error)(text)


def render_dashboard(service: HandoverService, result: AnalysisResult, final_status: str) -> None:
    coverage = "n/a" if result.ownership_coverage is None else f"{result.ownership_coverage:.0f}%"
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Readiness Score", f"{result.readiness_score} / 100")
    c2.metric("Missing Knowledge Count", len(result.gaps))
    c3.metric("Critical Gap Count", result.critical_count)
    c4.metric("Ownership Coverage", coverage)
    c5.metric("Overall Status", final_status)
    status_banner(result)

    st.subheader("How the score is composed")
    total_weight = sum(d.weight for d in result.dimensions)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Dimension": d.label,
                    "Weight": f"{d.weight / total_weight:.0%}",
                    "Score": d.score,
                    "Why": " ".join(d.notes),
                }
                for d in result.dimensions
            ]
        ),
        column_config={
            "Score": st.column_config.ProgressColumn("Score (0-100)", min_value=0, max_value=100, format="%d"),
            "Why": st.column_config.TextColumn("Why", width="large"),
        },
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"The score is a rule-based heuristic (weights and thresholds in `app/config/rules.json`, version "
        f"{result.rules_version}), not a statistical prediction. Critical gaps block readiness regardless of the score."
    )

    st.subheader("Knowledge completeness by category")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Category": a.label,
                    "Required": "Yes" if a.required else "No",
                    "Status": STATUS_LABELS[a.status],
                    "Entries": a.item_count,
                    "With evidence": a.verified_count,
                    "Note": a.reason,
                }
                for a in result.assessments
            ]
        ),
        hide_index=True,
        width="stretch",
    )


# --------------------------------------------------------------------------
# Knowledge gaps
# --------------------------------------------------------------------------


def gap_category_label(service: HandoverService, result: AnalysisResult, gap: Gap) -> str:
    if gap.category == HEADER:
        return "Handover details"
    return service.rules.for_scenario(result.scenario).label_for(Category(gap.category))


def gaps_frame(service: HandoverService, result: AnalysisResult, gaps: list[Gap]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Category": gap_category_label(service, result, gap),
                "Missing Information": gap.description,
                "Severity": gap.severity.value.capitalize(),
                "Recommended Action": gap.recommended_action,
                "Status": gap.status.capitalize(),
                "Gap ID": gap.id,
            }
            for gap in gaps
        ],
        columns=["Category", "Missing Information", "Severity", "Recommended Action", "Status", "Gap ID"],
    )


def field_input(field: str, current: object, key: str):
    if field in BOOL_FIELDS:
        return st.checkbox(FIELD_LABELS[field], value=bool(current), key=key)
    if field == "state":
        options = [s.value for s in ItemState]
        value = current.value if isinstance(current, ItemState) else (current or ItemState.OPEN.value)
        return st.selectbox(FIELD_LABELS[field], options, index=options.index(value), key=key)
    return st.text_input(FIELD_LABELS[field], value=str(current or ""), key=key)


def apply_fix(service: HandoverService, working: Handover, operations: list[dict], gap: Gap) -> None:
    try:
        corrected = apply_corrections(working, operations)
        resolved, result = service.confirm_resolved(corrected, gap.id)
        revision = service.save(corrected)
    except (CorrectionError, StorageError) as exc:
        st.error(str(exc))
        return
    set_source(corrected, keep_analysis=True)
    store_analysis(corrected, result, revision)
    if resolved:
        st.session_state.notice = ("success", f"Gap `{gap.id}` is resolved: the engine no longer detects it (revision {revision}).")
    else:
        st.session_state.notice = (
            "warning",
            f"Gap `{gap.id}` is still open. The engine re-checked the data and the required information is still missing: {gap.description}",
        )
    st.rerun()


def render_fix_form(service: HandoverService, working: Handover, gap: Gap) -> None:
    key = widget_key("fix", gap.id)
    if gap.item_id:
        item = next((i for i in working.items if i.id == gap.item_id), None)
        fields = ITEM_FIX_FIELDS.get(gap.rule_id, [])
        if item is None or not fields:
            st.info("Correct this entry in the Handover Workspace tab, then analyze again.")
            return
        values = {f: field_input(f, getattr(item, f), f"{key}_{f}") for f in fields}
        if st.button("Apply correction and re-check", key=f"{key}_apply", type="primary"):
            apply_fix(service, working, [{"op": "set_item", "item_id": item.id, "fields": values}], gap)
    elif gap.rule_id in ("REQ-MISSING", "ESCALATION-MISSING"):
        category = Category(gap.category)
        st.markdown("**Add the missing entry**")
        values = {}
        for f in CATEGORY_COLUMNS[category]:
            default = True if (f == "escalation" and gap.rule_id == "ESCALATION-MISSING") else ("" if f not in BOOL_FIELDS else False)
            values[f] = field_input(f, default, f"{key}_{f}")
        if st.button("Add entry and re-check", key=f"{key}_apply", type="primary"):
            apply_fix(service, working, [{"op": "add_item", "item": {"category": category.value, **values}}], gap)
        if gap.rule_id == "REQ-MISSING":
            reason = st.text_input("...or mark the category not applicable, with a reason", key=f"{key}_na")
            if st.button("Mark not applicable and re-check", key=f"{key}_na_apply"):
                apply_fix(service, working, [{"op": "set_not_applicable", "category": category.value, "reason": reason}], gap)
    elif gap.rule_id == "UNVERIFIED":
        pending = [i for i in working.items_in(Category(gap.category)) if service.rules.is_blank(i.evidence) and not service.rules.is_blank(i.title)]
        values = {i.id: st.text_input(f"Evidence reference for '{i.title}'", key=f"{key}_{widget_key(i.id)}") for i in pending}
        if st.button("Apply correction and re-check", key=f"{key}_apply", type="primary"):
            ops = [{"op": "set_item", "item_id": i, "fields": {"evidence": v}} for i, v in values.items() if v.strip()]
            apply_fix(service, working, ops, gap)
    elif gap.rule_id == "CONTRADICTION-NA":
        if st.button("Remove the not-applicable reason and re-check", key=f"{key}_apply", type="primary"):
            apply_fix(service, working, [{"op": "set_not_applicable", "category": gap.category, "reason": ""}], gap)
    else:
        st.info("Correct this in the handover details on the Handover Workspace tab, then analyze again.")


def render_gaps(service: HandoverService, result: AnalysisResult, working: Handover | None, stale: bool) -> None:
    if not result.gaps:
        st.success("The configured rules detect no gaps in this handover.")
        return
    st.dataframe(gaps_frame(service, result, result.gaps), hide_index=True, width="stretch")
    st.caption("Sorted by severity. A gap closes only when a re-analysis no longer detects it - there is no manual 'resolved' checkbox.")

    st.subheader("Correct a gap")
    if working is None or stale:
        st.info("The workspace has changes that were not analyzed yet. Select Analyze Handover first.")
        return
    gap_id = st.selectbox(
        "Gap",
        [gap.id for gap in result.gaps],
        format_func=lambda i: next(f"[{g.severity.value.capitalize()}] {g.description}" for g in result.gaps if g.id == i),
        key="gap_select",
    )
    gap = next(g for g in result.gaps if g.id == gap_id)
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Why it matters:** {gap.explanation}")
        st.markdown(f"**Recommended action:** {gap.recommended_action}")
        st.markdown(f"**Responsible:** {gap.responsible or 'not known'}")
        st.caption(f"Traceability: {gap.source}. Gap ID `{gap.id}`.")
    with right:
        render_fix_form(service, working, gap)


# --------------------------------------------------------------------------
# Before / After
# --------------------------------------------------------------------------


def render_before_after(service: HandoverService, result: AnalysisResult, working: Handover | None, stale: bool) -> None:
    handover: Handover = st.session_state.analyzed
    latest = st.session_state.revision
    st.caption(
        "Both sides are computed now by the same engine and rules from saved revisions of this handover. "
        "Nothing on this tab is a stored or hardcoded figure. With the synthetic examples, results are synthetic."
    )

    try:
        corrections = service.demo_corrections(handover.scenario) if handover.id.startswith("demo-") else []
    except (OSError, ValueError, KeyError):
        corrections = []
    if corrections:
        c1, c2 = st.columns([2, 5], vertical_alignment="center")
        clicked = c1.button("Apply sample corrections", key="apply_sample", disabled=working is None or stale)
        c2.caption(
            f"Applies {len(corrections)} documented synthetic corrections from `data/synthetic/corrections/` "
            "to the workspace, saves a new revision and re-runs the analysis."
        )
        if clicked and working is not None:
            try:
                corrected = apply_corrections(working, corrections)
            except CorrectionError as exc:
                st.error(f"Sample corrections could not be applied: {exc}")
            else:
                if run_analysis(service, corrected):
                    set_source(corrected, ("success", "Sample corrections applied and re-analyzed."), keep_analysis=True)
                    st.rerun()

    if latest < 2:
        st.info(
            "Only one revision is saved so far. Correct gaps on the Knowledge Gaps tab"
            + (" or apply the sample corrections above" if corrections else "")
            + ", then compare here."
        )
        return

    base = st.selectbox("Compare against revision", list(range(1, latest)), index=0, key=f"baseline_{handover.id}")
    cache_key = (handover.id, base, latest)
    if st.session_state.get("comparison_key") != cache_key:
        try:
            _, _, before = service.analyze_revision(handover.id, base)
            _, _, after = service.analyze_revision(handover.id, latest)
        except StorageError as exc:
            st.error(str(exc))
            return
        st.session_state.comparison = compare(before, after)
        st.session_state.comparison_key = cache_key
    cmp = st.session_state.comparison

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Readiness (rev {base} → rev {latest})", f"{cmp.score_before} → {cmp.score_after}", delta=cmp.score_after - cmp.score_before)
    c2.metric("Open gaps", f"{cmp.gaps_before} → {cmp.gaps_after}", delta=cmp.gaps_after - cmp.gaps_before, delta_color="inverse")
    c3.metric("Critical blockers", f"{cmp.critical_before} → {cmp.critical_after}", delta=cmp.critical_after - cmp.critical_before, delta_color="inverse")
    c4.metric("Gaps resolved / remaining", f"{len(cmp.resolved)} / {len(cmp.remaining)}")
    st.markdown(f"**Status:** {cmp.status_before} → {cmp.status_after}")

    st.dataframe(
        pd.DataFrame(
            [
                {"Severity": sev.capitalize(), f"Revision {base}": cmp.severity_before[sev], f"Revision {latest}": cmp.severity_after[sev]}
                for sev in cmp.severity_before
            ]
        ),
        hide_index=True,
        width="content",
    )
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Resolved gaps ({len(cmp.resolved)})**")
        st.dataframe(gaps_frame(service, result, cmp.resolved)[["Severity", "Missing Information", "Status"]], hide_index=True, width="stretch")
    with right:
        st.markdown(f"**Remaining gaps ({len(cmp.remaining)})**")
        st.dataframe(gaps_frame(service, result, cmp.remaining)[["Severity", "Missing Information", "Status"]], hide_index=True, width="stretch")
    if cmp.introduced:
        st.warning(f"{len(cmp.introduced)} new gap(s) appeared since revision {base}: " + ", ".join(g.id for g in cmp.introduced))


# --------------------------------------------------------------------------
# Handover package and receiver decision
# --------------------------------------------------------------------------

DECISION_LABELS = {
    Decision.REQUEST_CLARIFICATION: "Request Clarification",
    Decision.ACCEPT: "Accept Handover",
    Decision.ACCEPT_WITH_EXCEPTIONS: "Accept with Exceptions",
}


def render_package(service: HandoverService, result: AnalysisResult, stale: bool) -> None:
    handover: Handover = st.session_state.analyzed
    revision = st.session_state.revision
    state = service.acceptance_state(handover.id)
    if stale:
        st.warning("The workspace has changes that were not analyzed. This package reflects the last analyzed revision.")

    insight_key = (handover.content_hash(), revision)
    if st.session_state.get("insights_key") != insight_key:
        st.session_state.insights = get_provider().generate_insights(handover, result)
        st.session_state.insights_key = insight_key
    insights = st.session_state.insights

    package = report_service.build_package(
        handover, result, revision, service.rules,
        acceptance_status=state.status, acceptance=state.current, insights=insights,
    )
    markdown = report_service.render_markdown(package)

    left, right = st.columns([3, 2])
    with left:
        st.subheader(f"Report preview (revision {revision})")
        c1, c2 = st.columns(2)
        c1.download_button(
            "Download as Markdown", markdown, file_name=report_service.safe_filename(handover, revision, "md"),
            mime="text/markdown", width="stretch",
        )
        c2.download_button(
            "Download as JSON", report_service.render_json(package), file_name=report_service.safe_filename(handover, revision, "json"),
            mime="application/json", width="stretch",
        )
        with st.container(height=560, border=True):
            st.markdown(markdown)

    with right:
        st.subheader("Receiver review")
        final = state.status if state.current else result.readiness_status.value
        st.metric("Final handover status", final)
        st.caption(f"Readiness: {result.readiness_status.value} ({result.readiness_score}/100) · Acceptance: {state.status}")
        if state.superseded:
            st.warning(
                f"The earlier decision '{state.superseded.status}' was recorded for revision {state.superseded.revision}. "
                f"The handover changed since (now revision {state.revision}), so it must be reviewed again."
            )

        receiver = st.text_input("Receiver label", value=handover.receiving_owner, max_chars=120, key=f"receiver_{handover.id}")
        decision = st.radio("Decision", list(Decision), format_func=DECISION_LABELS.get, key="decision_choice")
        acknowledged: list[str] = []
        if decision == Decision.ACCEPT_WITH_EXCEPTIONS:
            acknowledged = st.multiselect(
                "Open gaps accepted as exceptions (all critical and high gaps must be selected)",
                [gap.id for gap in result.gaps],
                format_func=lambda i: next(f"[{g.severity.value.capitalize()}] {g.description}" for g in result.gaps if g.id == i),
                key=f"ack_{revision}",
            )
        note = st.text_area("Note (required for exceptions)", max_chars=2000, height=80, key=f"note_{revision}")
        if st.button("Record decision", type="primary", key="record_decision", disabled=stale):
            try:
                record = service.record_decision(handover.id, receiver, decision, note, acknowledged)
            except (DecisionError, StorageError) as exc:
                st.error(str(exc))
            else:
                st.session_state.notice = ("success", f"Decision recorded for revision {record.revision}: {record.status}.")
                st.rerun()
        st.caption("A decision is a local application record. It is not a legally binding signature, identity proof or compliance certification.")

        history = service.repository.list_decisions(handover.id)
        if history:
            st.markdown("**Decision history**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Revision": r.revision, "Decision": r.status, "Receiver": r.receiver, "Score": r.readiness_score, "Recorded (UTC)": r.created_at}
                        for r in reversed(history)
                    ]
                ),
                hide_index=True,
                width="stretch",
            )

    with st.expander(f"Suggestions - {insights.label}"):
        if insights.fallback_reason:
            st.warning(insights.fallback_reason)
        st.caption("Suggestions rephrase the findings above. They are not verified facts and do not affect score or acceptance.")
        st.write(insights.summary)
        for question in insights.questions:
            st.markdown(f"- {question}")


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------


def render_sidebar(service: HandoverService) -> None:
    with st.sidebar:
        st.header("About this demo")
        st.markdown(
            "- Example data is **synthetic**.\n"
            "- Findings and scores are **rule-based** and reproducible.\n"
            "- AI suggestions are **optional**; the default is an offline Mock mode.\n"
            "- A person makes the final acceptance decision.\n"
            "- Not a compliance or identity tool."
        )
        st.divider()
        st.button("Reset demo data", on_click=cb_reset_demo, args=(service,), key="reset_demo", help="Removes stored copies of the three synthetic examples only.")
        with st.expander("Run diagnostics"):
            st.json(METRICS.snapshot())


def main() -> None:
    st.set_page_config(page_title="ContextRelay AI", page_icon="🔁", layout="wide")
    try:
        service = get_service(os.environ.get("CONTEXTRELAY_DB_PATH", DEFAULT_DB_PATH))
    except (StorageError, RulesError) as exc:
        st.error(f"ContextRelay AI cannot start: {exc}")
        st.stop()
    init_state()

    st.title("ContextRelay AI")
    st.caption(
        "Find out what is missing from a handover before you accept it: missing knowledge, unclear ownership, "
        "open blockers - and an evidence-based handover package."
    )
    render_sidebar(service)

    # A permanent slot keeps the page structure identical with and without a
    # message, so the selected tab survives the rerun that follows an action.
    notice_slot = st.empty()
    notice = st.session_state.get("notice")
    if notice:
        getattr(notice_slot, notice[0])(notice[1])
        st.session_state.notice = None

    tabs = st.tabs(["1 · Handover Workspace", "2 · Readiness Dashboard", "3 · Knowledge Gaps", "4 · Before / After", "5 · Handover Package"])
    with tabs[0]:
        working = render_workspace(service)

    result: AnalysisResult | None = st.session_state.analysis
    analyzed: Handover | None = st.session_state.analyzed
    stale = bool(result and (working is None or working.content_hash() != analyzed.content_hash()))
    placeholder = "Load an example or enter a handover on the Handover Workspace tab, then select **Analyze Handover**."

    with tabs[1]:
        if result:
            if stale:
                st.warning("The workspace changed after the last analysis. Select Analyze Handover to refresh these results.")
            state = service.acceptance_state(analyzed.id)
            render_dashboard(service, result, state.status if state.current else result.readiness_status.value)
        else:
            st.info(placeholder)
    with tabs[2]:
        if result:
            render_gaps(service, result, working, stale)
        else:
            st.info(placeholder)
    with tabs[3]:
        if result:
            render_before_after(service, result, working, stale)
        else:
            st.info(placeholder)
    with tabs[4]:
        if result:
            render_package(service, result, stale)
        else:
            st.info(placeholder)


main()
