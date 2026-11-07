"""Starting a template import job: validate the upload form and store its pages.

- ``_parse_template_import_form`` checks the form before anything is stored
  (template and column modes, page count, client canonical candidates,
  category and exam name, page roles through ``_parse_template_page_roles``)
  and returns a ``_TemplateImportForm``.
- ``_save_template_import`` stores the pages and candidates and returns the
  job, a ``_TemplateImport`` with its deadline; if storing fails, the whole
  template directory is removed.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import asyncio
import json as _json
import os
import shutil
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile

from config import MAX_TEMPLATE_PAGES, MAX_UPLOAD_TOTAL_SIZE
from routers.grade_template_import_steps import _template_import_clock
from routers.grade_uploads import _save_batch_uploads
from services.grading_contract import VALID_GRADING_CATEGORIES
from services.template_column_layout import normalize_column_mode
from services.template_evidence import parse_canonical_metadata
from services.template_session import (
    GRADED_PAGE_ROLES,
    TEMPLATE_MODES,
    TemplateSession,
    normalize_page_label,
    normalize_page_role,
)


def _parse_template_page_roles(
    page_roles: object,
    page_labels: object,
    page_count: int,
) -> tuple[list[str], list[str]]:
    """Validate the teacher's per-page role and printed-label choice at upload.

    Both fields are optional JSON arrays with one entry per uploaded page. An
    omitted role list means every page is an ordinary ``answer_page``.
    """
    def parse(raw: object, name: str) -> list | None:
        # Direct Python callers receive FastAPI's ``Form`` marker when omitted.
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            value = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是 JSON 数组") from exc
        if not isinstance(value, list) or len(value) != page_count:
            raise ValueError(f"{name} 数量必须与模板页数一致")
        return value

    raw_roles = parse(page_roles, "page_roles")
    raw_labels = parse(page_labels, "page_labels")
    roles = (
        [normalize_page_role(value) for value in raw_roles]
        if raw_roles is not None else [normalize_page_role("")] * page_count
    )
    labels = (
        [normalize_page_label(value) for value in raw_labels]
        if raw_labels is not None else [""] * page_count
    )
    if not any(role in GRADED_PAGE_ROLES for role in roles):
        raise ValueError("模板至少需要一页答题页或答题卡")
    return roles, labels


@dataclass(frozen=True)
class _TemplateImportForm:
    """Validated fields of a template upload form."""

    template_mode: str
    column_mode: str
    category: str
    exam_name: str
    page_roles: list[str]
    page_labels: list[str]
    # Parsed metadata of client canonical candidates, or None without them.
    canonical_metadata: list | None


def _parse_template_import_form(
    files: list[UploadFile],
    canonical_files: list[UploadFile] | None,
    canonical_metadata: str | None,
    *,
    has_answers: bool,
    template_mode: str,
    column_mode: str,
    layout_mode: str,
    category: str,
    exam_name: str,
    page_roles: str,
    page_labels: str,
) -> _TemplateImportForm:
    """Validate the upload form before anything is stored."""
    if not files:
        raise HTTPException(400, detail="未上传模板图片")
    supplied_column_modes = [
        value for value in (column_mode, layout_mode)
        if isinstance(value, str) and value.strip()
    ]
    try:
        normalized_column_modes = [
            normalize_column_mode(value) for value in supplied_column_modes
        ]
    except ValueError as exc:
        raise HTTPException(400, detail="column_mode 只能是 single、double 或 triple") from exc
    if len(set(normalized_column_modes)) > 1:
        raise HTTPException(400, detail="column_mode 与 layout_mode 参数冲突")
    resolved_column_mode = normalized_column_modes[0] if normalized_column_modes else "double"
    # Direct Python callers (tests, task replayers and older integrations)
    # receive FastAPI's ``Form`` marker when they omit this argument.  Treat
    # that marker as absent instead of serializing it into an invalid mode.
    raw_template_mode = (
        template_mode.strip().lower()
        if isinstance(template_mode, str)
        else ""
    )
    if raw_template_mode:
        if raw_template_mode not in TEMPLATE_MODES:
            raise HTTPException(400, detail="template_mode 只能是 blank 或 marked")
        resolved_template_mode = raw_template_mode
    else:
        # Older clients sent only has_answers.  Preserve their visual intent
        # while requiring all new clients to send the explicit mode.
        legacy_has_answers = (
            has_answers if isinstance(has_answers, bool) else True
        )
        resolved_template_mode = "marked" if legacy_has_answers else "blank"
    has_canonical_files = canonical_files is not None
    has_canonical_metadata = canonical_metadata is not None
    if has_canonical_files != has_canonical_metadata:
        raise HTTPException(
            422,
            detail="canonical_files 与 canonical_metadata 必须同时提交",
        )
    if len(files) > MAX_TEMPLATE_PAGES:
        raise HTTPException(
            413,
            detail=f"单次最多上传 {MAX_TEMPLATE_PAGES} 页模板原图",
        )
    submitted_metadata = None
    if has_canonical_files:
        if len(canonical_files) != len(files):
            raise HTTPException(422, detail="标准模板候选数量必须与原图一致")
        if any(
            os.path.splitext(upload.filename or "")[1].lower() != ".png"
            for upload in canonical_files
        ):
            raise HTTPException(422, detail="标准模板候选必须为 PNG")
        try:
            submitted_metadata = parse_canonical_metadata(
                canonical_metadata,
                len(files),
            )
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    if category not in VALID_GRADING_CATEGORIES:
        raise HTTPException(400, detail="category 必须是 daily 或 exam")
    exam_name = str(exam_name or "").strip()
    if category == "exam" and not exam_name:
        raise HTTPException(400, detail="考试模板必须填写考试名称")
    try:
        resolved_page_roles, resolved_page_labels = _parse_template_page_roles(
            page_roles, page_labels, len(files),
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return _TemplateImportForm(
        template_mode=resolved_template_mode,
        column_mode=resolved_column_mode,
        category=category,
        exam_name=exam_name,
        page_roles=resolved_page_roles,
        page_labels=resolved_page_labels,
        canonical_metadata=submitted_metadata,
    )


@dataclass
class _TemplateImport:
    """One running template import: its session, saved pages and deadline."""

    sess: TemplateSession
    teacher_id: int
    column_mode: str
    page_roles: list[str]
    page_labels: list[str]
    paths: list[str]
    # (original, client canonical candidate, metadata) per page when the
    # desktop client submitted canonical candidates, otherwise None.
    canonical_inputs: list[tuple[str, str, dict]] | None
    candidate_dir: str
    deadline_s: float
    deadline_at: float

    @property
    def total(self) -> int:
        return len(self.paths)

    @property
    def graded_page_count(self) -> int:
        # Only graded pages run layout/model/artifact work and join the import run.
        return sum(role in GRADED_PAGE_ROLES for role in self.page_roles)

    @property
    def layout_action_id(self) -> str:
        return f"template-import-{self.sess.template_id}"


async def _save_template_import(
    sess: TemplateSession,
    teacher_id: int,
    form: _TemplateImportForm,
    files: list[UploadFile],
    canonical_files: list[UploadFile] | None,
    deadline_s: float,
) -> _TemplateImport:
    """Store the uploaded pages; any failure removes the whole template dir."""
    candidate_dir = os.path.join(sess.template_dir, "_submitted_canonical")
    canonical_inputs = None
    try:
        saved_uploads = await _save_batch_uploads(files, sess.template_dir)
        deadline_at = _template_import_clock() + deadline_s
        if form.canonical_metadata is not None:
            os.makedirs(candidate_dir, exist_ok=True)
            saved_candidates = await _save_batch_uploads(
                canonical_files,
                candidate_dir,
                total_byte_budget=(
                    MAX_UPLOAD_TOTAL_SIZE
                    - sum(os.path.getsize(path) for _, path in saved_uploads)
                ),
            )
            canonical_inputs = [
                (source_path, candidate_path, metadata)
                for ((_, source_path), (_, candidate_path), metadata) in zip(
                    saved_uploads,
                    saved_candidates,
                    form.canonical_metadata,
                )
            ]
    except ValueError as exc:
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise HTTPException(
            422,
            detail="客户端标准模板候选校验失败",
        ) from exc
    except (asyncio.CancelledError, Exception):
        shutil.rmtree(sess.template_dir, ignore_errors=True)
        raise
    return _TemplateImport(
        sess=sess,
        teacher_id=teacher_id,
        column_mode=form.column_mode,
        page_roles=form.page_roles,
        page_labels=form.page_labels,
        paths=[path for _, path in saved_uploads],
        canonical_inputs=canonical_inputs,
        candidate_dir=candidate_dir,
        deadline_s=deadline_s,
        deadline_at=deadline_at,
    )
