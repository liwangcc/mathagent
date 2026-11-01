"""PDF/ZIP exports for provisional QuickBatch review results.

These functions intentionally accept JSON-safe review entries rather than ORM
records: teachers can export and inspect a batch before any result is archived.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict
from typing import Iterable

from services.error_sheet import (
    HAS_REPORTLAB,
    A4,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    _build_question_summary,
    _build_student_card,
    _escape_xml,
    _grading_metric_text,
    _make_styles,
    _page_header_footer,
    _preferred_correct_answer,
    _preferred_solution_steps,
    mm,
)


class ReviewExportError(ValueError):
    """A user-facing reason a provisional review export cannot be generated."""


def _active_detailed_reviews(review_results: Iterable[dict]) -> list[dict]:
    active = [item for item in review_results if item.get("disposition") == "active"]
    if not active:
        raise ReviewExportError("没有可导出的未作废批改结果")
    detailed_modes = {"precise", "auto"}  # auto 是历史详细批改的兼容值
    if any(str(item.get("grading_mode") or "precise") not in detailed_modes for item in active):
        raise ReviewExportError("错题文件仅支持详细批改结果，请重新使用精准模式批改")
    if not HAS_REPORTLAB:
        raise ReviewExportError("PDF 导出组件未安装")
    return active


def _student_error_entry(review: dict, class_name: str) -> dict:
    result = review.get("result") or {}
    errors = []
    for idx, detail in enumerate(result.get("details") or [], 1):
        if not isinstance(detail, dict):
            continue
        status = str(detail.get("result") or "")
        if status not in ("wrong", "partial", "错误", "部分正确"):
            continue
        q_index = str(detail.get("question_no") or detail.get("question_number") or idx)
        errors.append({
            "q_index": q_index,
            "question": detail.get("question") or detail.get("content") or "",
            "student_answer": detail.get("student_answer") or detail.get("answer") or "未记录",
            "correct_answer": _preferred_correct_answer(detail),
            "solution_steps": _preferred_solution_steps(detail),
            "status": status,
        })

    metric_source = dict(result)
    metric_source["category"] = review.get("category") or "daily"
    return {
        "name": str(review.get("student_name") or "未识别学生"),
        "student_no": str(review.get("student_no") or ""),
        "class_name": class_name or "当前班级",
        "score": float(result.get("score") or 0.0),
        "metric_text": _grading_metric_text(metric_source),
        "total": int(result.get("total") or 0),
        "correct": int(result.get("correct_count") or 0),
        "wrong_count": int(result.get("wrong_count") or len(errors)),
        "errors": errors,
    }


def export_single_review_error_sheet_pdf(review: dict, class_name: str = "") -> bytes:
    """Return one detailed, printable PDF for one active provisional student."""
    _active_detailed_reviews([review])
    student = _student_error_entry(review, class_name)
    if not student["errors"]:
        raise ReviewExportError(f"{student['name']} 没有可导出的错题")
    styles = _make_styles()
    elements = _build_student_card(student, styles)
    # `_build_student_card` must share the same style object used by the document.
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=25 * mm,
        bottomMargin=20 * mm,
    )
    story = [
        Paragraph(_escape_xml(f"{student['name']} — 错题条"), styles["CNTitle"]),
        Paragraph("详细批改结果 · 供学生整理错题", styles["CNSubtitle"]),
        Spacer(1, 4 * mm),
        *elements,
    ]
    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    return buf.getvalue()


def _safe_zip_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return cleaned or fallback


def export_all_review_error_sheets_zip(review_results: Iterable[dict], class_name: str = "") -> bytes:
    """Return a ZIP containing one PDF per active student with actual wrong items."""
    active = _active_detailed_reviews(review_results)
    out = io.BytesIO()
    written_names: set[str] = set()
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, review in enumerate(active, 1):
            student = _student_error_entry(review, class_name)
            if not student["errors"]:
                continue
            label = _safe_zip_name(student["name"], f"学生{index}")
            student_no = _safe_zip_name(student["student_no"], str(index))
            filename = f"{label}_{student_no}_错题条.pdf"
            serial = 2
            base = filename[:-4]
            while filename in written_names:
                filename = f"{base}_{serial}.pdf"
                serial += 1
            written_names.add(filename)
            archive.writestr(filename, export_single_review_error_sheet_pdf(review, class_name))
            count += 1
    if count == 0:
        raise ReviewExportError("未作废学生均没有错题，无法生成错题文件")
    return out.getvalue()


def _question_sort_key(question_no: str) -> tuple:
    chunks = re.split(r"(\d+)", str(question_no))
    return tuple(int(chunk) if chunk.isdigit() else chunk for chunk in chunks)


def export_review_question_summary_pdf(review_results: Iterable[dict], class_name: str = "") -> bytes:
    """Return a teacher PDF grouped by question number and error count."""
    active = _active_detailed_reviews(review_results)
    groups: dict[str, dict] = defaultdict(lambda: {"question": "", "errors": []})
    for review in active:
        student = _student_error_entry(review, class_name)
        for error in student["errors"]:
            question_no = str(error["q_index"])
            group = groups[question_no]
            group["question"] = group["question"] or error["question"]
            # Keep the first usable answer/solution rather than allowing an
            # older sparse record to hide a later model-generated solution.
            group.setdefault("correct_answer", "")
            group.setdefault("solution_steps", "")
            group["correct_answer"] = group["correct_answer"] or error["correct_answer"]
            group["solution_steps"] = group["solution_steps"] or error["solution_steps"]
            group["errors"].append({
                "student_name": student["name"],
                "student_no": student["student_no"],
                "student_answer": error["student_answer"],
                "status": error["status"],
                "correct_answer": error["correct_answer"],
                "solution_steps": error["solution_steps"],
            })
    if not groups:
        raise ReviewExportError("未作废学生均没有错题，无法生成老师汇总")

    styles = _make_styles()
    elements = []
    for question_no in sorted(groups, key=_question_sort_key):
        group = groups[question_no]
        elements.extend(_build_question_summary(question_no, group["question"], group["errors"], styles))
        elements.append(Spacer(1, 5 * mm))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=25 * mm,
        bottomMargin=20 * mm,
    )
    title = f"{class_name or '当前班级'} — 错题汇总"
    story = [
        Paragraph(_escape_xml(title), styles["CNTitle"]),
        Paragraph(f"共 {len(groups)} 道题有错 · 按题号统计错误人数", styles["CNSubtitle"]),
        Spacer(1, 4 * mm),
        *elements,
    ]
    doc.build(story, onFirstPage=_page_header_footer, onLaterPages=_page_header_footer)
    return buf.getvalue()
