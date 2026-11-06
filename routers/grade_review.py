"""复核接口 — 人工复核、作废、入档与导出（/batch-grade/review/*）。"""

import copy
import hashlib
import json as _json
import re
import tempfile
import uuid
import zipfile
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from services.quick_grader import QuickBatch
from services.grading_persistence import persist_grading_result, write_deferred_filesystem_archives
from services.generated_files import register_and_respond
from services.scoring_engine import recalculate_result_from_details
from services.student_service import canonical_student_no
from models import GradingCorrection, Student
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _review_avg_score,
    _stable_review_image_hash,
    _load_owned_review_batch,
    _locked_owned_review_batch,
    _review_timestamp,
    _find_review_result,
)
from routers.grade_report import (
    _batch_report_response,
)
from routers.grade_papers import (
    _RAW_PAPER_EXPIRED_MESSAGE,
    _batch_review_image_path,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _invalidate_batch_report(batch: QuickBatch) -> None:
    """Mark a report stale after a teacher changes a provisional verdict."""
    batch.report_status = "queued"
    batch.report_text = ""
    batch.report_error = "批改结果已更新，等待重新生成报告"
    batch.report_updated_at = datetime.now(timezone.utc).isoformat()


class _ReviewBatchReq(BaseModel):
    batch_id: str


class _VoidReviewPaperReq(_ReviewBatchReq):
    paper_id: str
    reason: str = ""


class _ManualReviewDecision(BaseModel):
    question_no: str
    result: Literal["correct", "partial", "wrong"]
    note: str = ""


class _ManualReviewDecisionReq(_ReviewBatchReq):
    paper_id: str
    review_revision: int
    decisions: list[_ManualReviewDecision]


def _sync_review_summary_disposition(batch: QuickBatch, paper_id: str, disposition: str) -> None:
    for row in batch.results:
        if str(row.get("paper_id") or row.get("review_id") or "") == paper_id:
            row["disposition"] = disposition
            return


def _refresh_review_batch_status(batch: QuickBatch) -> None:
    dispositions = {str(item.get("disposition") or "active") for item in batch.review_results}
    if dispositions == {"voided"}:
        batch.status = "voided"
        return
    if dispositions and dispositions <= {"voided", "archived"}:
        batch.status = "archived"
        if not batch.review_archived_at:
            batch.review_archived_at = _review_timestamp()
        return
    batch.status = "review_ready"
    if not batch.review_ready_at:
        batch.review_ready_at = _review_timestamp()


def _sync_review_summary_result(batch: QuickBatch, review: dict) -> None:
    paper_id = str(review.get("paper_id") or review.get("review_id") or "")
    result = review.get("result") or {}
    for row in batch.results:
        if str(row.get("paper_id") or row.get("review_id") or "") == paper_id:
            row.update({
                "score": result.get("score"),
                "max_score": result.get("max_score"),
                "correct_rate": result.get("correct_rate"),
                "category": review.get("category") or row.get("category") or "daily",
                "exam_name": review.get("exam_name") or row.get("exam_name") or "",
                "correct": result.get("correct_count", 0),
                "partial": result.get("partial_count", 0),
                "wrong": result.get("wrong_count", 0),
                "needs_review": result.get("needs_review_count", 0),
                "total": result.get("total", 0),
                "details": result.get("details") or [],
            })
            return


def _review_identity_group_key(review: dict) -> str:
    """Return a frozen teacher-confirmed identity key; never group by display name."""
    assignment = review.get("identity_assignment") or {}
    action = str(assignment.get("action") or "")
    if action == "existing" and assignment.get("student_id") is not None:
        return f"existing:{assignment['student_id']}"
    if action == "create":
        student_no = canonical_student_no(assignment.get("student_no"))
        if student_no:
            return f"create:{student_no}"
    paper_id = str(review.get("paper_id") or review.get("review_id") or "")
    return f"paper:{paper_id}"


def _review_page_contracts(batch: QuickBatch) -> dict[str, dict]:
    """Build explicit, upload-ordered page groups from frozen identity assignments."""
    upload_order = {
        str(item.get("paper_id") or ""): index
        for index, item in enumerate(batch.uploads)
    }
    grouped: dict[str, list[dict]] = {}
    for source_index, review in enumerate(batch.review_results):
        paper_id = str(review.get("paper_id") or review.get("review_id") or "")
        if not paper_id:
            continue
        grouped.setdefault(_review_identity_group_key(review), []).append({
            "paper_id": paper_id,
            "filename": str(review.get("filename") or ""),
            "source_index": source_index,
            "upload_index": upload_order.get(paper_id, len(upload_order) + source_index),
        })

    contracts: dict[str, dict] = {}
    for identity_key, pages in grouped.items():
        pages.sort(key=lambda item: (item["upload_index"], item["source_index"]))
        opaque_group_id = hashlib.sha256(
            f"review-group:{batch.batch_id}:{identity_key}".encode("utf-8")
        ).hexdigest()[:24]
        public_pages = [
            {
                "paper_id": page["paper_id"],
                "page_index": index,
                "filename": page["filename"],
            }
            for index, page in enumerate(pages)
        ]
        for page in public_pages:
            contracts[page["paper_id"]] = {
                "review_group_id": opaque_group_id,
                "page_index": page["page_index"],
                "page_count": len(public_pages),
                "pages": [dict(item) for item in public_pages],
            }
    return contracts


def _review_groups(batch: QuickBatch) -> list[list[dict]]:
    """Return review rows grouped only by the frozen teacher-confirmed identity."""
    grouped: dict[str, list[dict]] = {}
    for review in batch.review_results:
        grouped.setdefault(_review_identity_group_key(review), []).append(review)
    return list(grouped.values())


def _review_group_archive_payload(batch: QuickBatch, group: list[dict]) -> dict:
    """Merge one confirmed student's pages into one formal grading contract."""
    page_contracts = _review_page_contracts(batch)
    ordered = sorted(
        group,
        key=lambda item: int((page_contracts.get(str(
            item.get("paper_id") or item.get("review_id") or ""
        )) or {}).get("page_index") or 0),
    )
    categories = {str(item.get("category") or "daily") for item in ordered}
    exam_names = {str(item.get("exam_name") or "") for item in ordered}
    if len(categories) != 1 or len(exam_names) != 1:
        raise HTTPException(409, detail="同一学生多页试卷的批次类型不一致，不能入档")

    details: list[dict] = []
    knowledge_items: list[dict] = []
    page_refs: list[dict] = []
    page_hashes: list[dict] = []
    corrections: list[tuple[str, dict]] = []
    for page_review in ordered:
        paper_id = str(page_review.get("paper_id") or page_review.get("review_id") or "")
        page_contract = page_contracts.get(paper_id) or {"page_index": 0}
        page_result = page_review.get("result") or {}
        page_details = page_result.get("details") or []
        if len(ordered) > 1 and int(page_result.get("total") or 0) != len(page_details):
            raise HTTPException(409, detail="同一学生多页试卷缺少逐题明细，不能合并入档")
        corrected_questions = {
            str(item.get("question_no") or "")
            for item in (page_review.get("manual_corrections") or [])
        }
        for detail in page_details:
            if not isinstance(detail, dict):
                continue
            detail.setdefault("paper_id", paper_id)
            detail.setdefault("page_index", int(page_contract.get("page_index") or 0))
            question_no = str(detail.get("question_no") or detail.get("question_number") or "")
            if question_no in corrected_questions:
                detail["teacher_regraded"] = True
            details.append(detail)
        knowledge_items.extend(page_review.get("knowledge_items") or [])
        page_hash = str(page_review.get("image_content_hash") or "").strip()
        if not page_hash:
            raise HTTPException(500, detail="批次归档幂等键缺失")
        page_hashes.append({"paper_id": paper_id, "sha256": page_hash})
        page_refs.append({
            "paper_id": paper_id,
            "page_index": int(page_contract.get("page_index") or 0),
            "filename": str(page_review.get("filename") or ""),
        })
        corrections.extend(
            (paper_id, correction)
            for correction in (page_review.get("manual_corrections") or [])
        )

    if len(ordered) == 1:
        result = dict(ordered[0].get("result") or {})
        result["details"] = details
        group_hash = page_hashes[0]["sha256"]
        ocr_raw_json = ordered[0].get("ocr_raw_json") or {}
    else:
        result = {"details": details}
        recalculate_result_from_details(result, next(iter(categories)))
        group_hash = hashlib.sha256(_json.dumps(
            {
                "batch_id": batch.batch_id,
                "identity": _review_identity_group_key(ordered[0]),
                "pages": page_hashes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        ocr_raw_json = {
            "source": "ordinary_paper_batch",
            "batch_id": batch.batch_id,
            "paper_ids": [item["paper_id"] for item in page_refs],
            "pages": page_refs,
            "page_ocr": [
                {
                    "paper_id": str(item.get("paper_id") or item.get("review_id") or ""),
                    "ocr": item.get("ocr_raw_json") or {},
                }
                for item in ordered
            ],
        }
    return {
        "ordered": ordered,
        "category": next(iter(categories)),
        "exam_name": next(iter(exam_names)),
        "result": result,
        "image_hash": group_hash,
        "image_path": str(ordered[0].get("image_path") or ""),
        "knowledge_items": knowledge_items,
        "ocr_raw_json": ocr_raw_json,
        "corrections": corrections,
    }


def _review_rows_for_response(batch: QuickBatch) -> list[dict]:
    """Build current UI rows from authoritative review state, not stale summaries."""
    summaries = {
        str(row.get("paper_id") or row.get("review_id") or ""): row
        for row in batch.results
    }
    question_types = {
        str(question.get("question_no") or ""): str(
            question.get("question_type") or "solve"
        ).strip().lower()
        for question in batch.questions
        if str(question.get("question_no") or "")
    }
    page_contracts = _review_page_contracts(batch)
    rows: list[dict] = []
    for review in batch.review_results:
        paper_id = str(review.get("paper_id") or review.get("review_id") or "")
        row = dict(summaries.get(paper_id) or {})
        result = review.get("result") or {}
        row.update({
            "review_id": str(review.get("review_id") or paper_id),
            "paper_id": paper_id,
            "filename": review.get("filename") or row.get("filename") or "",
            "student_name": review.get("student_name") or row.get("student_name") or "",
            "student_no": review.get("student_no") or row.get("student_no") or "",
            "disposition": review.get("disposition") or row.get("disposition") or "active",
            "category": review.get("category") or row.get("category") or "daily",
            "exam_name": review.get("exam_name") or row.get("exam_name") or "",
            "score": result.get("score"),
            "max_score": result.get("max_score"),
            "correct_rate": result.get("correct_rate"),
            "correct": result.get("correct_count", 0),
            "partial": result.get("partial_count", 0),
            "wrong": result.get("wrong_count", 0),
            "needs_review": result.get("needs_review_count", 0),
            "total": result.get("total", 0),
            "details": result.get("details") or [],
            "grading_result_id": review.get("archived_grading_result_id"),
            "profile_id": review.get("archived_profile_id"),
        })
        page_contract = page_contracts.get(paper_id) or {
            "review_group_id": hashlib.sha256(
                f"review-group:{batch.batch_id}:paper:{paper_id}".encode("utf-8")
            ).hexdigest()[:24],
            "page_index": 0,
            "page_count": 1,
            "pages": [{"paper_id": paper_id, "page_index": 0, "filename": row.get("filename") or ""}],
        }
        row.update(page_contract)
        for detail in row["details"]:
            if isinstance(detail, dict):
                detail.setdefault("paper_id", paper_id)
                detail.setdefault("page_index", page_contract["page_index"])
                question_no = str(
                    detail.get("question_no") or detail.get("question_number") or ""
                ).strip()
                detail.setdefault("question_type", question_types.get(question_no, "solve"))
        rows.append(row)
    return rows



@router.get("/batch-grade/review/{batch_id}")
def batch_grade_get_review_state(
    batch_id: str,
    user: User = Depends(_require_user),
):
    """Reload an owned provisional review after refresh or route navigation."""
    batch = _load_owned_review_batch(batch_id, user)
    rows = _review_rows_for_response(batch)
    return {
        "ok": True,
        "batch_id": batch.batch_id,
        "identity_mode": batch.identity_mode,
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "status": batch.status,
        "review_revision": batch.review_revision,
        "review_archived_at": batch.review_archived_at,
        "asset_lifecycle": batch.asset_lifecycle,
        "results": rows,
        "summary": {
            "total": len(rows),
            "avg_score": _review_avg_score(rows),
            "grading_elapsed_seconds": batch.grading_elapsed_seconds,
        },
        "report": _batch_report_response(batch),
    }


@router.post("/batch-grade/review/decision")
def batch_grade_save_manual_review_decision(
    req: _ManualReviewDecisionReq,
    user: User = Depends(_require_user),
):
    """Save one student's final teacher verdicts before formal archive."""
    with _locked_owned_review_batch(req.batch_id, user) as batch:
        if batch.asset_lifecycle.get("status") == "expired":
            raise HTTPException(410, detail=_RAW_PAPER_EXPIRED_MESSAGE)
        if req.review_revision != batch.review_revision:
            raise HTTPException(409, detail="审阅结果已更新，请刷新后重试")
        if batch.status == "archived":
            raise HTTPException(409, detail="批次已入档，不能再修改暂存判定")

        review = next(
            (item for item in batch.review_results if str(item.get("paper_id") or item.get("review_id") or "") == req.paper_id),
            None,
        )
        if review is None:
            raise HTTPException(404, detail="该学生试卷不属于本批次")
        if review.get("disposition") != "active":
            raise HTTPException(409, detail="该学生试卷已作废或已入档，不能再修改")

        result = review.get("result") or {}
        details = result.get("details") or []
        by_question = {}
        for detail in details:
            question_no = str(detail.get("question_no") or detail.get("question_number") or "").strip()
            if not question_no or question_no in by_question:
                raise HTTPException(400, detail="暂存题目编号无效，不能保存人工判定")
            by_question[question_no] = detail

        submitted = {}
        for decision in req.decisions:
            question_no = str(decision.question_no or "").strip()
            if not question_no or question_no in submitted:
                raise HTTPException(400, detail="人工判定题号不能为空且不得重复")
            submitted[question_no] = decision
        if set(submitted) != set(by_question):
            raise HTTPException(400, detail="人工判定必须完整覆盖该学生的全部题目")

        corrections = review.setdefault("manual_corrections", [])
        changed = 0
        timestamp = _review_timestamp()
        for question_no, decision in submitted.items():
            detail = by_question[question_no]
            old_result = str(detail.get("result") or "needs_review")
            new_result = decision.result
            if old_result == new_result:
                continue
            detail["result"] = new_result
            if isinstance(detail.get("grading_units"), list):
                for unit in detail["grading_units"]:
                    if isinstance(unit, dict):
                        unit["result"] = new_result
            corrections.append({
                "question_no": question_no,
                "old_result": old_result,
                "new_result": new_result,
                "note": str(decision.note or "").strip()[:500],
                "teacher_id": user.id,
                "created_at": timestamp,
                "audit_key": uuid.uuid4().hex,
            })
            changed += 1

        # Rebuild the category-specific aggregates from the changed verdicts.
        recalculate_result_from_details(result, str(review.get("category") or "daily"))
        _sync_review_summary_result(batch, review)
        batch.review_revision += 1
        _invalidate_batch_report(batch)
        batch.save_meta()
        return {
            "ok": True,
            "batch_id": batch.batch_id,
            "paper_id": req.paper_id,
            "changed": changed,
            "review_revision": batch.review_revision,
            "review": review,
        }

@router.post("/batch-grade/review/void")
def batch_grade_void_review_paper(
    req: _VoidReviewPaperReq,
    user: User = Depends(_require_user),
):
    """Soft-void one provisional result before formal archive."""
    with _locked_owned_review_batch(req.batch_id, user) as batch:
        if batch.status == "archived":
            raise HTTPException(409, detail="批次已入档，不能再作废暂存结果")

        review = next(
            (item for item in batch.review_results if str(item.get("paper_id") or item.get("review_id") or "") == req.paper_id),
            None,
        )
        if review is None:
            raise HTTPException(404, detail="该学生试卷不属于本批次")
        identity_key = _review_identity_group_key(review)
        group = [
            item for item in batch.review_results
            if _review_identity_group_key(item) == identity_key
        ]
        if any(item.get("disposition") == "archived" for item in group):
            raise HTTPException(409, detail="该学生整份结果已入档，不能再作废")

        changed = 0
        paper_ids: list[str] = []
        now = _review_timestamp()
        for item in group:
            paper_id = str(item.get("paper_id") or item.get("review_id") or "")
            paper_ids.append(paper_id)
            if item.get("disposition") == "voided":
                continue
            item["disposition"] = "voided"
            item["voided_at"] = now
            item["void_reason"] = str(req.reason or "").strip()[:200]
            _sync_review_summary_disposition(batch, paper_id, "voided")
            changed += 1

        _refresh_review_batch_status(batch)
        if changed:
            batch.review_revision += 1
            _invalidate_batch_report(batch)
        batch.save_meta()
        return {
            "ok": True, "voided": changed, "batch_id": batch.batch_id,
            "paper_id": req.paper_id, "paper_ids": paper_ids,
            "review_revision": batch.review_revision,
        }


@router.post("/batch-grade/review/void-all")
def batch_grade_void_all_review_results(
    req: _ReviewBatchReq,
    user: User = Depends(_require_user),
):
    """Soft-void every active provisional result in one batch."""
    with _locked_owned_review_batch(req.batch_id, user) as batch:
        if batch.status == "archived":
            raise HTTPException(409, detail="批次已入档，不能再作废暂存结果")

        now = _review_timestamp()
        changed = 0
        for review in batch.review_results:
            if review.get("disposition") != "active":
                continue
            review["disposition"] = "voided"
            review["voided_at"] = now
            review["void_reason"] = "整批作废"
            paper_id = str(review.get("paper_id") or review.get("review_id") or "")
            _sync_review_summary_disposition(batch, paper_id, "voided")
            changed += 1
        _refresh_review_batch_status(batch)
        if changed:
            batch.review_revision += 1
            _invalidate_batch_report(batch)
        batch.save_meta()
        return {
            "ok": True, "voided": changed, "batch_id": batch.batch_id,
            "review_revision": batch.review_revision,
        }


def _archive_response(
    batch: QuickBatch,
    *,
    idempotent: bool,
    archived: int = 0,
    skipped_pending: int = 0,
    skipped_voided: int = 0,
    archived_paper_ids: list[str] | None = None,
) -> dict:
    return {
        "ok": True,
        "archived": archived,
        "skipped_pending": skipped_pending,
        "skipped_voided": skipped_voided,
        "idempotent": idempotent,
        "batch_id": batch.batch_id,
        "review_revision": batch.review_revision,
        "archived_paper_ids": archived_paper_ids or [],
    }


def _archivable_review_groups(batch: QuickBatch) -> tuple[list[list[dict]], int, int]:
    """Student groups ready to archive, plus the pending and voided counts skipped.

    A group archives only when all its pages are active and resolved; a group
    whose pages disagree is refused rather than archived in part.
    """
    archivable_groups: list[list[dict]] = []
    skipped_pending = 0
    skipped_voided = 0
    for group in _review_groups(batch):
        dispositions = {str(item.get("disposition") or "active") for item in group}
        if dispositions == {"voided"}:
            skipped_voided += 1
            continue
        if dispositions == {"archived"}:
            continue
        if dispositions != {"active"}:
            raise HTTPException(
                409,
                detail="同一学生的多页试卷状态不一致，请刷新后整份作废或联系管理员处理",
            )
        if any(
            int((item.get("result") or {}).get("needs_review_count") or 0) > 0
            for item in group
        ):
            skipped_pending += 1
            continue
        archivable_groups.append(group)
    return archivable_groups, skipped_pending, skipped_voided


def _backfill_review_image_hashes(batch: QuickBatch, groups: list[list[dict]]) -> bool:
    """Give legacy reviews a stable image hash; True when any was missing."""
    backfilled = False
    for group in groups:
        for review in group:
            if str(review.get("image_content_hash") or "").strip():
                continue
            paper_id = str(review.get("paper_id") or review.get("review_id") or "")
            review["image_content_hash"] = _stable_review_image_hash(
                batch.batch_id, paper_id, str(review.get("image_path") or "")
            )
            backfilled = True
    return backfilled


def _archive_student(
    db: Session,
    batch: QuickBatch,
    user: User,
    review: dict,
    created_students: dict[str, Student],
) -> Student:
    """The student a review group archives to, as the teacher's identity plan says.

    "existing" must still be the teacher's student in this class. "create"
    reuses a student created earlier in this run, or an identical student
    with the same number, and refuses a number another student holds.
    """
    assignment = review.get("identity_assignment") or {}
    action = assignment.get("action")
    if action == "existing":
        student = db.query(Student).filter(
            Student.id == assignment.get("student_id"),
            Student.teacher_id == user.id,
            Student.class_id == batch.class_id,
        ).first()
        if student is None:
            raise HTTPException(400, detail=f"{review.get('filename') or review.get('paper_id')} 的已选学生已失效")
        return student
    if action != "create":
        raise HTTPException(400, detail=f"{review.get('filename') or review.get('paper_id')} 的身份计划无效")
    name = str(assignment.get("name") or "").strip()
    student_no = canonical_student_no(assignment.get("student_no"))
    if not name:
        raise HTTPException(400, detail=f"{review.get('filename') or review.get('paper_id')} 缺少已确认的新学生姓名")
    paper_id = str(review.get("paper_id") or review.get("review_id") or "")
    student_key = f"number:{student_no}" if student_no else f"paper:{paper_id}"
    student = created_students.get(student_key)
    if student is not None:
        return student
    duplicate = None
    if student_no:
        duplicate = db.query(Student).filter(
            Student.teacher_id == user.id,
            Student.student_no == student_no,
        ).first()
    if duplicate is not None:
        if duplicate.name != name or duplicate.class_id != batch.class_id:
            raise HTTPException(409, detail=f"学号 {student_no} 已被其他学生占用")
        student = duplicate
    else:
        student = Student(
            teacher_id=user.id,
            class_id=batch.class_id,
            name=name,
            student_no=student_no,
        )
        db.add(student)
        db.flush()
    created_students[student_key] = student
    return student


def _persist_review_group(
    db: Session,
    batch: QuickBatch,
    user: User,
    review: dict,
    archive_payload: dict,
    student: Student,
    archive_jobs: list[dict],
):
    """Write one student's graded paper into the archive (uncommitted)."""
    result = archive_payload["result"]
    manual_corrections = [correction for _, correction in archive_payload["corrections"]]
    grading_result, profile, _created = persist_grading_result(
        db,
        student=student,
        teacher_id=user.id,
        category=archive_payload["category"],
        exam_name=archive_payload["exam_name"],
        image_path=archive_payload["image_path"],
        image_hash=archive_payload["image_hash"],
        total_questions=int(result.get("total") or 0),
        correct_count=int(result.get("correct_count") or 0),
        partial_count=int(result.get("partial_count") or 0),
        wrong_count=int(result.get("wrong_count") or 0),
        needs_review_count=int(result.get("needs_review_count") or 0),
        score=result.get("score"),
        max_score=result.get("max_score"),
        correct_rate=result.get("correct_rate"),
        scoring_version=int(result.get("scoring_version") or 2),
        details=result.get("details") or [],
        knowledge_items=archive_payload["knowledge_items"],
        ocr_raw_name=str(review.get("ocr_raw_name") or ""),
        ocr_raw_json=archive_payload["ocr_raw_json"],
        grading_mode=str(review.get("grading_mode") or "precise"),
        assessment_purpose=str(review.get("assessment_purpose") or "standard"),
        template_id=str(review.get("template_id") or batch.template_id or ""),
        correction_evidence_allowed=not bool(manual_corrections),
        commit=False,
        archive_jobs=archive_jobs,
        teaching_context=batch.teaching_context,
    )
    return grading_result, profile


def _record_review_corrections(
    db: Session,
    batch: QuickBatch,
    user: User,
    grading_result,
    corrections: list[tuple[str, dict]],
) -> None:
    """Audit the teacher's manual corrections once each, keyed by a stable audit key."""
    for correction_index, (paper_id, correction) in enumerate(corrections):
        audit_key = str(correction.get("audit_key") or "").strip()
        if not audit_key:
            audit_key = hashlib.sha256(
                f"review-audit:{batch.batch_id}:{paper_id}:{correction_index}:"
                f"{correction.get('created_at', '')}:{correction.get('old_result', '')}:"
                f"{correction.get('new_result', '')}".encode("utf-8")
            ).hexdigest()
            correction["audit_key"] = audit_key
        if db.query(GradingCorrection.id).filter(GradingCorrection.audit_key == audit_key).first():
            continue
        db.add(GradingCorrection(
            grading_result_id=grading_result.id,
            teacher_id=user.id,
            paper_id=paper_id,
            question_no=str(correction.get("question_no") or ""),
            audit_key=audit_key,
            old_result=str(correction.get("old_result") or ""),
            new_result=str(correction.get("new_result") or ""),
            note=str(correction.get("note") or ""),
        ))


def _mark_review_group_archived(
    batch: QuickBatch,
    pages: list[dict],
    grading_result,
    profile,
) -> list[str]:
    """Mark a group's pages and result rows archived; returns their paper ids."""
    archived_at = _review_timestamp()
    paper_ids = []
    for page_review in pages:
        paper_id = str(page_review.get("paper_id") or page_review.get("review_id") or "")
        page_review["archived_grading_result_id"] = grading_result.id
        page_review["archived_profile_id"] = profile.id if profile else None
        page_review["disposition"] = "archived"
        page_review["archived_at"] = archived_at
        _sync_review_summary_disposition(batch, paper_id, "archived")
        for row in batch.results:
            if str(row.get("paper_id") or row.get("review_id") or "") == paper_id:
                row["grading_result_id"] = grading_result.id
                row["profile_id"] = profile.id if profile else None
                break
        paper_ids.append(paper_id)
    return paper_ids


def _archive_all_locked_review_results(batch: QuickBatch, user: User, db: Session):
    """Archive resolved active reviews while the caller holds the batch operation lock."""
    if batch.identity_mode == "anonymous":
        raise HTTPException(409, detail="无身份批阅结果不能写入学生档案")
    if batch.status == "archived":
        return _archive_response(
            batch,
            idempotent=True,
            skipped_voided=sum(1 for item in batch.review_results if item.get("disposition") == "voided"),
        )

    for review in batch.review_results:
        review.setdefault("archived_grading_result_id", None)
        review.setdefault("archived_profile_id", None)
        review.setdefault("manual_corrections", [])

    archivable_groups, skipped_pending, skipped_voided = _archivable_review_groups(batch)
    if not archivable_groups:
        if skipped_pending:
            return _archive_response(
                batch,
                idempotent=True,
                skipped_pending=skipped_pending,
                skipped_voided=skipped_voided,
            )
        raise HTTPException(409, detail="没有可入档的批改结果")

    # Persist any recovered legacy hash before the DB transaction begins.  If
    # the process dies after commit, retry sees the same key even if the upload
    # image has already been cleaned up.
    if _backfill_review_image_hashes(batch, archivable_groups):
        batch.save_meta()

    created_students: dict[str, Student] = {}
    archived_paper_ids: list[str] = []
    archive_jobs: list[dict] = []
    try:
        for group in archivable_groups:
            review = group[0]
            archive_payload = _review_group_archive_payload(batch, group)
            student = _archive_student(db, batch, user, review, created_students)
            grading_result, profile = _persist_review_group(
                db, batch, user, review, archive_payload, student, archive_jobs,
            )
            _record_review_corrections(db, batch, user, grading_result, archive_payload["corrections"])
            archived_paper_ids += _mark_review_group_archived(
                batch, archive_payload["ordered"], grading_result, profile,
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.error("batch archive failed batch=%s: %s", batch.batch_id, exc, exc_info=True)
        raise HTTPException(500, detail="批次入档失败，请稍后重试")

    write_deferred_filesystem_archives(archive_jobs)
    _refresh_review_batch_status(batch)
    batch.review_revision += 1
    batch.save_meta()
    return _archive_response(
        batch,
        idempotent=False,
        archived=len(archivable_groups),
        skipped_pending=skipped_pending,
        skipped_voided=skipped_voided,
        archived_paper_ids=archived_paper_ids,
    )


@router.post("/batch-grade/review/archive-all")
def batch_grade_archive_all_review_results(
    req: _ReviewBatchReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Archive every non-voided provisional result in one DB transaction."""
    with _locked_owned_review_batch(req.batch_id, user) as batch:
        return _archive_all_locked_review_results(batch, user, db)


class _ReviewStudentExportReq(_ReviewBatchReq):
    paper_id: str


class _ReviewImagePreviewReq(_ReviewStudentExportReq):
    decisions: list[_ManualReviewDecision] = Field(default_factory=list, max_length=500)


@router.post("/batch-grade/review/image-preview")
def batch_grade_review_image_preview(req: _ReviewImagePreviewReq, user: User = Depends(_require_user)):
    """Render unsaved teacher decisions without changing scores or audit history."""
    batch = _load_owned_review_batch(req.batch_id, user)
    output_path = _batch_review_image_path(batch, req.paper_id, user, req.decisions)
    return FileResponse(str(output_path), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/batch-grade/review/export/images")
def batch_grade_export_review_images(req: _ReviewBatchReq, user: User = Depends(_require_user)):
    """Package all non-voided pages, including archived results, without model calls."""
    batch = _load_owned_review_batch(req.batch_id, user)
    reviews = [item for item in batch.review_results if item.get("disposition") != "voided"]
    if not reviews:
        raise HTTPException(409, detail="没有可下载的未作废批阅结果")
    contracts = _review_page_contracts(batch)
    archive_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        # PNG is already compressed; ZIP_STORED avoids compressing every page twice.
        with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, review in enumerate(reviews, 1):
                paper_id = str(review.get("paper_id") or review.get("review_id") or "")
                output_path = _batch_review_image_path(batch, paper_id, user)
                name = "匿名卷" if batch.identity_mode == "anonymous" else str(review.get("student_name") or "学生")
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" ._")[:80] or "学生"
                page = int((contracts.get(paper_id) or {}).get("page_index") or 0) + 1
                archive.write(output_path, f"{index:03d}_{name}_第{page}页_批阅痕迹.png")
        archive_file.seek(0)
    except Exception:
        archive_file.close()
        raise

    def chunks():
        try:
            while chunk := archive_file.read(1024 * 1024):
                yield chunk
        finally:
            archive_file.close()

    cleanup = BackgroundTasks()
    cleanup.add_task(archive_file.close)
    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="review-images.zip"',
        "Cache-Control": "private, no-store",
    }, background=cleanup)


def _review_class_name(db: Session, batch: QuickBatch, user_id: int) -> str:
    if not batch.class_id:
        return "当前班级"
    cls = db.query(TeacherClass).filter(
        TeacherClass.id == batch.class_id,
        TeacherClass.teacher_id == user_id,
    ).first()
    return str(cls.class_name) if cls and cls.class_name else "当前班级"


@router.post("/batch-grade/review/export/student")
def batch_grade_export_review_student(
    req: _ReviewStudentExportReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    from services.batch_review_export import (
        ReviewExportError,
        export_single_review_error_sheet_pdf,
    )

    batch = _load_owned_review_batch(req.batch_id, user)
    review = _find_review_result(batch, req.paper_id)
    if review.get("disposition") != "active":
        raise HTTPException(409, detail="该学生结果已作废，不能导出")
    try:
        pdf = export_single_review_error_sheet_pdf(review, _review_class_name(db, batch, user.id))
    except ReviewExportError as exc:
        raise HTTPException(409, detail=str(exc))
    return register_and_respond(db, user.id, pdf, "学生错题", str(review.get("student_name") or "学生"), "pdf")


@router.post("/batch-grade/review/export/all-errors")
def batch_grade_export_all_review_errors(
    req: _ReviewBatchReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    from services.batch_review_export import (
        ReviewExportError,
        export_all_review_error_sheets_zip,
    )

    batch = _load_owned_review_batch(req.batch_id, user)
    try:
        payload = export_all_review_error_sheets_zip(
            batch.review_results, _review_class_name(db, batch, user.id),
        )
    except ReviewExportError as exc:
        raise HTTPException(409, detail=str(exc))
    return register_and_respond(db, user.id, payload, "全体学生错题", batch.batch_id, "zip")


@router.post("/batch-grade/review/export/summary")
def batch_grade_export_review_summary(
    req: _ReviewBatchReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    from services.batch_review_export import (
        ReviewExportError,
        export_review_question_summary_pdf,
    )

    batch = _load_owned_review_batch(req.batch_id, user)
    try:
        pdf = export_review_question_summary_pdf(
            batch.review_results, _review_class_name(db, batch, user.id),
        )
    except ReviewExportError as exc:
        raise HTTPException(409, detail=str(exc))
    return register_and_respond(db, user.id, pdf, "老师错题汇总", batch.batch_id, "pdf")
