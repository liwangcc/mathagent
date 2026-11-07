"""What a template session's layout runs say about each page.

A layout run (``sess.layout_cut_runs``) records, per page, the column layout
the model or teacher produced, the frozen crop plan and any review leftovers.
Every flow that touches page geometry reads them through these helpers:

- review state: ``_template_geometry_review_state`` normalizes the current and
  legacy markers of one page run; ``_template_layout_review_pending_pages``,
  ``_template_anchor_review_pending_pages`` and
  ``_template_geometry_review_pending_pages`` list the pages still waiting for
  the teacher; ``_latest_template_geometry_review_metadata`` carries a page's
  unresolved review draft into a new run; ``_drawing_geometry_unresolved``
  says whether its drawing stage is still open.
- drafts and plans: ``_latest_page_layout_draft`` is the draft a teacher edit
  revises; ``_frozen_question_layout`` and
  ``_layout_result_from_frozen_question_plan`` give the layout already bound to
  an artifact; ``_plan_column_bounds`` reads a plan's column boundaries.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import copy
import hashlib
import json as _json

from services.template_anchor_crops import (
    AnchorCropPlanError,
    validate_anchor_crop_plan,
)
from services.template_session import TemplateSession


def _template_geometry_review_state(page_run: object) -> tuple[bool, bool]:
    """Normalize current and legacy geometry-review markers for one page."""
    if not isinstance(page_run, dict):
        return False, False
    anchor_diagnostics = page_run.get("anchor_diagnostics")
    template_ocr_diagnostics = page_run.get("template_ocr_diagnostics")
    has_candidate_draft = any(
        isinstance(page_run.get(key), list) and bool(page_run[key])
        for key in (
            "question_anchor_candidates",
            "template_answer_region_candidates",
            "template_handwriting_region_candidates",
        )
    )
    anchor_review_pending = bool(
        page_run.get("anchor_review_pending")
        or (
            isinstance(anchor_diagnostics, dict)
            and anchor_diagnostics.get("needs_review") is True
        )
        or has_candidate_draft
    )
    geometry_review_pending = bool(
        page_run.get("geometry_review_pending")
        or anchor_review_pending
        or (
            isinstance(template_ocr_diagnostics, dict)
            and template_ocr_diagnostics.get(
                "geometry_review_pending", template_ocr_diagnostics.get("needs_review")
            ) is True
        )
    )
    return anchor_review_pending, geometry_review_pending


def _layout_result_from_frozen_question_plan(
    artifact: dict,
    plan: dict,
) -> dict:
    """Build a preview-compatible result without calling the layout model again."""
    columns = list(plan["parameters"]["columns"])
    candidates = []
    for candidate_index, column in enumerate(columns):
        left = int(column["x1_1000"])
        right = int(column["x2_1000"])
        column_name = f"col_{int(column['column_index'])}"
        canonical = {
            "column": column_name,
            "bbox_1000": [left, 0, right, 1000],
        }
        candidates.append({
            "candidate_index": candidate_index,
            "candidate_role": "question_block",
            "question_number_hint": "",
            "question_number_hint_source": "frozen_plan",
            "coordinate_trust": "model_coarse",
            "usable_for_original_overlay": True,
            "template_bbox_1000": [left, 0, right, 1000],
            "raw_sha256": hashlib.sha256(
                _json.dumps(canonical, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        })
    return {
        "provider": "frozen_template_plan",
        "operation": "RecognizeColumnLayout",
        "cut_type": "question",
        "output_oricoord": True,
        "request_id": "frozen-plan",
        "source": {
            "sha256": artifact["sha256"],
            "width": artifact["width"],
            "height": artifact["height"],
        },
        "provider_dimensions": {
            "org_width": artifact["width"],
            "org_height": artifact["height"],
            "rectified_width": artifact["width"],
            "rectified_height": artifact["height"],
        },
        "pages": [{
            "page_index": 0,
            "provider_page_no": 1,
            "provider_dimensions": {},
            "candidates": candidates,
        }],
        "summary": {
            "total": len(candidates),
            "original": len(candidates),
            "rectified_untrusted": 0,
            "invalid": 0,
            "missing": 0,
        },
        "column_layout": {
            "column_count": len(columns),
            "columns": columns,
        },
    }


def _latest_page_layout_draft(
    sess: TemplateSession,
    page_label: str,
    artifact: dict,
) -> dict:
    """Return the newest layout draft bound to this artifact, or a source stub.

    A teacher edit is a revision of the draft the confirmation page displayed,
    so the revision counter and column identities must continue from it. A
    page that predates layout drafts gets a source-only stub; its first edit
    then starts at revision 1 instead of inheriting a synthetic history.
    """
    artifact_sha = str(artifact.get("sha256") or "")
    matching: list[tuple[tuple[int, str], dict]] = []
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict):
            continue
        mode = (page_run.get("modes") or {}).get("question")
        if not isinstance(mode, dict) or not isinstance(mode.get("column_layout"), dict):
            continue
        if str((mode.get("source") or {}).get("sha256") or "") != artifact_sha:
            continue
        revision = mode.get("layout_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 0
        stamp = str(run.get("completed_at") or run.get("created_at") or "")
        matching.append(((revision, stamp), mode))
    if matching:
        return max(matching, key=lambda item: item[0])[1]
    stub: dict = {
        "source": {
            "sha256": artifact_sha,
            "width": artifact.get("width"),
            "height": artifact.get("height"),
        },
    }
    # No draft survived in the runs, but the artifact may still hold the
    # frozen plan of the previous edit. Rebuild enough of it for the revision
    # counter to continue rather than restarting at 1 on every save.
    plan = artifact.get("question_crop_plan")
    columns = (plan.get("parameters") or {}).get("columns") if isinstance(plan, dict) else None
    if isinstance(columns, list) and columns:
        stub["column_layout"] = {"columns": [dict(column) for column in columns]}
        stub["layout_mode"] = {1: "single", 2: "double", 3: "triple"}.get(len(columns), "")
        revisions = {
            column.get("layout_revision")
            for column in columns
            if isinstance(column, dict) and column.get("layout_revision") is not None
        }
        context_revision = (plan.get("layout_context") or {}).get("layout_revision")
        if context_revision is not None:
            revisions.add(context_revision)
        if len(revisions) == 1:
            stub["layout_revision"] = next(iter(revisions))
    return stub


_TEMPLATE_GEOMETRY_REVIEW_METADATA_KEYS = (
    "anchor_review_pending",
    "geometry_review_pending",
    "question_anchor_candidates",
    "template_answer_region_candidates",
    "template_handwriting_region_candidates",
    "question_schema_candidates",
    "anchor_diagnostics",
    "template_ocr_diagnostics",
)


def _latest_template_geometry_review_metadata(
    sess: TemplateSession,
    page_label: str,
    artifact_sha256: str,
) -> dict:
    """Copy review and bounded-import metadata for one immutable page.

    A new cut action owns a fresh layout-model run, but it does not rerun the
    question-anchor/exemption recognizer.  Keep that review-only draft attached
    to the page until a teacher override clears it from every historical run.
    Completed geometry also retains model-stage metadata: pruning old cut
    runs must not erase the missing-answer/reference confirmation contract.
    The artifact hash prevents data from an older upload revision crossing
    into the new page.
    """
    expected_sha = str(artifact_sha256 or "").strip()
    if not expected_sha:
        return {}
    matching: list[tuple[tuple[str, int], dict]] = []
    for order, run in enumerate((sess.layout_cut_runs or {}).values()):
        if not isinstance(run, dict):
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict):
            continue
        page_sha = str(page_run.get("artifact_sha256") or "").strip()
        if not page_sha:
            question_mode = (page_run.get("modes") or {}).get("question")
            if isinstance(question_mode, dict):
                page_sha = str(
                    (question_mode.get("source") or {}).get("sha256") or ""
                ).strip()
        anchor_review_pending, geometry_review_pending = (
            _template_geometry_review_state(page_run)
        )
        if page_sha != expected_sha:
            continue
        bounded_diagnostics = page_run.get("template_ocr_diagnostics") or {}
        if not geometry_review_pending and not bounded_diagnostics.get("model_stages"):
            continue
        keys = (_TEMPLATE_GEOMETRY_REVIEW_METADATA_KEYS if geometry_review_pending
                else ("template_ocr_diagnostics",))
        metadata = {
            key: copy.deepcopy(page_run[key])
            for key in keys
            if key in page_run
        }
        if not metadata:
            continue
        if anchor_review_pending:
            metadata["anchor_review_pending"] = True
        metadata["geometry_review_pending"] = geometry_review_pending
        stamp = str(run.get("completed_at") or run.get("created_at") or "")
        matching.append(((stamp, order), metadata))
    if not matching:
        return {}
    return max(matching, key=lambda item: item[0])[1]


def _plan_column_bounds(plan: object) -> list[tuple[int, int]] | None:
    if not isinstance(plan, dict):
        return None
    columns = (plan.get("parameters") or {}).get("columns")
    if not isinstance(columns, list):
        return None
    try:
        return [(int(column["x1_1000"]), int(column["x2_1000"])) for column in columns]
    except (KeyError, TypeError, ValueError):
        return None


def _template_layout_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Pages whose column draft still needs the teacher's boundaries.

    A review draft is resolved only by a saved layout override, which writes a
    frozen ``question_crop_plan`` into the artifact and clears the marker on
    every run. Until then the template must not be confirmed.
    """
    pending: set[int] = set()
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            if not isinstance(page_run, dict) or not page_run.get("layout_review_pending"):
                continue
            artifact = (sess.page_artifacts or {}).get(page_label)
            if isinstance(artifact, dict) and isinstance(
                artifact.get("question_crop_plan"), dict
            ):
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _template_anchor_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Pages whose model anchor/exemption draft still needs teacher approval.

    Unlike a column-layout review, this marker is independent of the frozen
    column crop plan.  A page can have trusted column boundaries while its
    question-number or exemption boxes remain non-authoritative candidates.
    """
    pending: set[int] = set()
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            anchor_review_pending, _ = _template_geometry_review_state(page_run)
            if not anchor_review_pending:
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _template_geometry_review_pending_pages(sess: TemplateSession) -> list[int]:
    """Return every page blocked on layout, anchor, or OCR geometry review."""
    pending = set(_template_layout_review_pending_pages(sess))
    pending.update(_template_anchor_review_pending_pages(sess))
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict):
            continue
        for page_label, page_run in (run.get("pages") or {}).items():
            _, geometry_review_pending = _template_geometry_review_state(page_run)
            if not geometry_review_pending:
                continue
            try:
                pending.add(int(str(page_label).removeprefix("page_")) - 1)
            except ValueError:
                continue
    return sorted(pending)


def _frozen_question_layout(
    sess: TemplateSession,
    page_label: str,
    artifact: dict,
) -> tuple[dict, dict] | None:
    """Return the layout and plan already bound to this artifact, if any."""
    raw_plan = artifact.get("question_crop_plan")
    if not isinstance(raw_plan, dict):
        return None
    artifact_sha = str(artifact.get("sha256") or "")
    plan = validate_anchor_crop_plan(raw_plan, artifact_sha256=artifact_sha)
    matching: list[tuple[str, dict]] = []
    for run in (sess.layout_cut_runs or {}).values():
        if not isinstance(run, dict) or run.get("status") != "complete":
            continue
        page_run = (run.get("pages") or {}).get(page_label)
        if not isinstance(page_run, dict) or page_run.get("artifact_sha256") != artifact_sha:
            continue
        mode = (page_run.get("modes") or {}).get("question")
        if not isinstance(mode, dict):
            continue
        if str((mode.get("source") or {}).get("sha256") or "") != artifact_sha:
            continue
        stored_plan = page_run.get("anchor_crop_plan")
        if isinstance(stored_plan, dict):
            try:
                stored_plan = validate_anchor_crop_plan(
                    stored_plan,
                    artifact_sha256=artifact_sha,
                )
            except (AnchorCropPlanError, TypeError, ValueError):
                continue
            if stored_plan != plan:
                continue
        matching.append((
            str(run.get("completed_at") or run.get("created_at") or ""),
            mode,
        ))
    if matching:
        _timestamp, mode = max(matching, key=lambda item: item[0])
        return mode, plan
    # Preserve older sessions that persisted the plan before layout metadata.
    return _layout_result_from_frozen_question_plan(artifact, plan), plan


def _drawing_geometry_unresolved(sess: TemplateSession, page_label: str) -> bool:
    """A layout run still has this page's drawing stage unfinished and unconfirmed."""
    return any(
        (page_run.get("template_ocr_diagnostics") or {}).get("teacher_geometry_confirmed") is not True
        and (page_run.get("template_ocr_diagnostics") or {}).get("model_stages", {}).get(
            "template-directory-drawing-v1", {}
        ).get("status") not in (None, "completed")
        for run in (sess.layout_cut_runs or {}).values()
        if isinstance(run, dict)
        for page_run in [(run.get("pages") or {}).get(page_label) or {}]
    )
