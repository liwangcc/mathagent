"""Grading a reviewed batch: drafts per student, the teacher's verdicts, the archive.

- POST /answer-sheet/sessions/{session_id}/grading/draft
  (``build_grading_draft``) and its SSE form .../grading/draft/stream
  (``build_grading_draft_stream``) grade each student's pages as one group
  (``_grading_groups``, ``_grade_answer_sheet_group``), once every page is
  oriented, fully sourced and has a ready identity (``_IDENTITY_READY_STATUSES``);
  the stream resumes from a checkpoint that matches the batch
  (``_grading_checkpoint_drafts``).
- Before grading, the batch must still match its frozen paper template
  (``_verified_paper_template``, ``_result_scoring``) and its choice marks must
  resolve through teacher-confirmed bubble grids (``_verified_bubble_grids``).
- POST .../grading/decision (``save_grading_decision``, ``GradingDecision*``)
  saves a complete set of teacher verdicts with optimistic concurrency;
  POST .../grading/archive (``archive_grading_drafts``) writes the results once.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import asyncio
import copy
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import GradingCorrection, Student
from models_user import User
from routers.answer_sheet_availability import _require_answer_sheet_chain
from routers.answer_sheet_common import (
    _ANSWER_SHEET_HEARTBEAT_SECONDS,
    _answer_sheet_sse,
    _answer_sheet_stream,
    _load_owned,
    _locked_owned,
    _requires_answer_sheet_writes,
)
from routers.answer_sheet_student_state import (
    _GRADING_DRAFT_CHECKPOINT_KEY,
    _require_no_student_upload,
)
from routers.auth import _require_user
from services.answer_sheet_page_pairing import PATH_PAIRED_STATUS
from services.answer_sheet_paper_templates import (
    get_paper_template_registry,
    paper_answer_spec_hash,
)
from services.answer_sheet_session import AnswerSheetSession
from services.answer_sheet_template_registry import get_answer_sheet_template_registry
from services.bounded_dispatch import bounded_completion_map
from services.flash_grading import FLASH_LOCAL_MAX_CONCURRENT
from services.grading_contract import AnswerSpec
from services.paper_orientation import orientation_is_ready
from services.scoring_engine import recalculate_result_from_details
from services.teaching_context import (
    await_teaching,
    current_teaching_context,
    stream_teaching,
)

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


_IDENTITY_READY_STATUSES = {
    "recognized", "teacher_confirmed", "ordered_pair_inherited", PATH_PAIRED_STATUS,
}


class GradingDecision(BaseModel):
    question_no: str = Field(..., min_length=1, max_length=50)
    result: Literal["correct", "partial", "wrong"]
    note: str = Field("", max_length=500)


class GradingDecisionRequest(BaseModel):
    student_id: int = Field(..., ge=1)
    review_revision: int = Field(..., ge=0)
    decisions: list[GradingDecision] = Field(..., min_length=1, max_length=200)


def _verified_paper_template(session: AnswerSheetSession) -> dict | None:
    """Fail closed when a batch snapshot no longer matches its paper template."""
    if not session.is_batch:
        return None
    try:
        manifest = get_paper_template_registry().load(
            session.paper_template_code, int(session.teacher_id or 0),
        )
    except ValueError as exc:
        raise HTTPException(409, detail=f"试卷模板不可用，禁止判分：{exc}") from exc
    content = manifest["content"]
    expected = manifest["answer_spec_hash"]
    actual = paper_answer_spec_hash(
        session.answer_spec_snapshot,
        content.get("teaching_context") or {},
        str(content.get("scoring_category") or ""),
    )
    if session.answer_spec_hash != expected or actual != expected:
        raise HTTPException(409, detail="试卷模板 answer_spec_hash 与批次快照不一致，禁止判分")
    return manifest


def _result_scoring(session: AnswerSheetSession) -> tuple[str, str]:
    """Return (result-system category, exam name) without a batch-level choice.

    A new batch takes both from its frozen paper template; a historical
    session keeps its read-only legacy values.
    """
    manifest = _verified_paper_template(session)
    if manifest is None:
        return session.result_category(), session.result_exam_name()
    content = manifest["content"]
    return str(content["scoring_category"]), str(content.get("exam_name") or "")


def _verified_bubble_grids(session: AnswerSheetSession) -> dict | None:
    """Fail closed unless a batch's choice marks resolve via confirmed grids.

    The geometry is read from the frozen answer-sheet template only; a batch
    without a published template, with a changed template, or with any grid
    row that is still a model proposal cannot be graded.
    """
    if not session.is_batch or not any(
        region.get("region_type") == "choice_mark" for region in session.answer_regions
    ):
        return None
    try:
        return get_answer_sheet_template_registry().resolve_bubble_regions(session)
    except ValueError as exc:
        raise HTTPException(409, detail=f"涂卡网格未通过教师确认模板校验，禁止判分：{exc}") from exc


@router.post("/sessions/{session_id}/grading/draft")
@_requires_answer_sheet_writes
async def build_grading_draft(session_id: str, user: User = Depends(_require_user)):
    from services.grader import grade_paper_unified

    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    start_revision = session.review_revision
    if session.status != "review" or not session.student_pages:
        raise HTTPException(409, detail="请先完成学生答题卡识别与教师复核")
    if any(page.get("review_reasons") for page in session.student_pages):
        raise HTTPException(409, detail="仍有页面需要教师复核，不能开始批阅")
    _verified_bubble_grids(session)
    groups = _grading_groups(session)
    result_category, _exam_name = _result_scoring(session)
    answer_map = {
        item["question_no"]: AnswerSpec.model_validate(item).to_answer_map_entry(source="answer_sheet_confirmed")
        for item in session.answer_spec_snapshot
    }
    await _require_answer_sheet_chain(user)
    async def grade_group(item: tuple[int, dict]) -> dict:
        student_id, group = item
        return await _grade_answer_sheet_group(
            grade_paper_unified,
            student_id,
            group,
            answer_map,
            user.id,
            result_category,
            teaching_context=session.teaching_context,
        )

    drafts: list[dict] = []
    result_stream = bounded_completion_map(
        groups.items(),
        grade_group,
        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
    )
    try:
        async for _position, draft in result_stream:
            drafts.append(draft)
    finally:
        await result_stream.aclose()
    drafts_by_student = {int(item["student_id"]): item for item in drafts}
    drafts = [drafts_by_student[student_id] for student_id in groups]
    with _locked_owned(session_id, user.id) as latest:
        if latest.review_revision != start_revision:
            raise HTTPException(409, detail="答题卡证据已更新，请重新生成批阅草稿")
        latest.replace_grading_drafts(drafts)
        latest.save()
        persisted_revision = latest.review_revision
    return {
        "session_id": session.session_id, "layout_hash": session.layout_hash,
        "answer_spec_hash": session.answer_spec_hash,
        "review_revision": persisted_revision,
        "drafts": drafts,
    }


def _grading_groups(session: AnswerSheetSession) -> dict[int, dict]:
    groups: dict[int, dict] = {}
    for page in session.student_pages:
        if not orientation_is_ready(page):
            raise HTTPException(409, detail="仍有页面方向未确认，不能开始批阅")
        registration = page.get("registration") or {}
        diagnostics = registration.get("diagnostics") or {}
        if (
            registration.get("status") != "passed"
            or not diagnostics.get("answer_source_complete", False)
        ):
            raise HTTPException(409, detail="仍有答案区域缺少真实源像素，不能开始批阅")
        identity = page.get("identity") or {}
        student_id = identity.get("student_id")
        if identity.get("status") not in _IDENTITY_READY_STATUSES or not student_id:
            raise HTTPException(409, detail="仍有学生身份未确认")
        group = groups.setdefault(int(student_id), {
            "student_name": identity.get("student_name") or "",
            "questions": [],
            "pages": [],
            "question_pages": {},
        })
        group["pages"].append(page["paper_id"])
        for item in page.get("answers") or []:
            if (
                item.get("status") not in {"recognized", "teacher_confirmed"}
                or not str(item.get("answer") or "").strip()
            ):
                raise HTTPException(
                    409,
                    detail=f"第{item.get('question_no')}题答案仍未确认",
                )
            group["questions"].append({
                "question_number": item["question_no"],
                "student_answer": item["answer"],
            })
            question_no = str(item["question_no"])
            question_pages = group["question_pages"].setdefault(question_no, [])
            if page["paper_id"] not in question_pages:
                question_pages.append(page["paper_id"])
    return groups


async def _grade_answer_sheet_group(
    grade_function,
    student_id: int,
    group: dict,
    answer_map: dict,
    teacher_id: int,
    category: str,
    *,
    teaching_context: dict | None = None,
) -> dict:
    snapshot = current_teaching_context() if teaching_context is None else teaching_context
    recognized = {
        "questions": group["questions"],
        "ocr_answers_only": True,
        "ocr_alignment_verified": True,
    }
    if snapshot:
        recognized["teaching_context"] = snapshot
    result = await await_teaching(snapshot, grade_function(
        recognized,
        answer_map,
        group["student_name"],
        teacher_id,
        category=category,
    ))
    if not isinstance(result, dict):
        raise RuntimeError("grading task returned an invalid result")
    for detail in result.get("details") or []:
        if snapshot:
            detail["teaching_context"] = dict(snapshot)
        question_no = str(
            detail.get("parent_question_no")
            or detail.get("question_no")
            or detail.get("question_number")
            or ""
        )
        detail["paper_ids"] = list(
            group["question_pages"].get(question_no, [])
        )
    if snapshot:
        result["teaching_context"] = dict(snapshot)
    return {
        "student_id": student_id,
        "paper_ids": group["pages"],
        "question_pages": group["question_pages"],
        "manual_corrections": [],
        "result": result,
    }


def _grading_checkpoint_drafts(
    session: AnswerSheetSession,
    groups: dict[int, dict],
) -> list[dict]:
    lifecycle = session.asset_lifecycle
    if not isinstance(lifecycle, dict):
        session.asset_lifecycle = {}
        return []
    checkpoint = lifecycle.get(_GRADING_DRAFT_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict):
        return []
    if (
        checkpoint.get("review_revision") != session.review_revision
        or checkpoint.get("layout_hash") != session.layout_hash
        or checkpoint.get("answer_spec_hash") != session.answer_spec_hash
        or checkpoint.get("student_ids") != list(groups)
        or (checkpoint.get("teaching_context") or {}) != session.teaching_context
    ):
        lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
        return []
    drafts = checkpoint.get("drafts")
    if not isinstance(drafts, list):
        lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
        return []
    seen: set[int] = set()
    valid: list[dict] = []
    for draft in drafts:
        if not isinstance(draft, dict):
            return []
        try:
            student_id = int(draft.get("student_id"))
        except (TypeError, ValueError):
            return []
        if student_id not in groups or student_id in seen or not isinstance(draft.get("result"), dict):
            return []
        seen.add(student_id)
        valid.append(draft)
    return valid


@router.post("/sessions/{session_id}/grading/draft/stream")
@_requires_answer_sheet_writes
async def build_grading_draft_stream(
    session_id: str,
    user: User = Depends(_require_user),
):
    from services.grader import grade_paper_unified

    session = _load_owned(session_id, user.id)
    _require_no_student_upload(session)
    if session.status != "review" or not session.student_pages:
        raise HTTPException(409, detail="请先完成学生答题卡识别与教师复核")
    if any(page.get("review_reasons") for page in session.student_pages):
        raise HTTPException(409, detail="仍有页面需要教师复核，不能开始批阅")

    async def events():
        try:
            with _locked_owned(session_id, user.id) as locked:
                _require_no_student_upload(locked)
                finalized = False
                try:
                    lifecycle = locked.asset_lifecycle
                    checkpoint = (
                        lifecycle.get(_GRADING_DRAFT_CHECKPOINT_KEY)
                        if isinstance(lifecycle, dict)
                        else None
                    )
                    if locked.grouping_decisions and not isinstance(checkpoint, dict):
                        finalized = True
                        yield _answer_sheet_sse("done", {
                            "message": "批阅草稿已生成",
                            "current": len(locked.grouping_decisions),
                            "total": len(locked.grouping_decisions),
                            "idempotent": True,
                            "review_revision": locked.review_revision,
                            "drafts": locked.grouping_decisions,
                        })
                        return
                    _verified_bubble_grids(locked)
                    groups = _grading_groups(locked)
                    result_category, _exam_name = _result_scoring(locked)
                    await _require_answer_sheet_chain(user)
                    answer_map = {
                        item["question_no"]: AnswerSpec.model_validate(item).to_answer_map_entry(
                            source="answer_sheet_confirmed"
                        )
                        for item in locked.answer_spec_snapshot
                    }
                    drafts = _grading_checkpoint_drafts(locked, groups)
                    completed_ids = {
                        int(item["student_id"])
                        for item in drafts
                    }
                    total = len(groups)
                    yield _answer_sheet_sse("progress", {
                        "message": "正在恢复已保存的批阅草稿" if drafts else "正在生成批阅草稿",
                        "current": len(drafts),
                        "total": total,
                    })
                    pending_groups = [
                        item
                        for item in groups.items()
                        if item[0] not in completed_ids
                    ]

                    async def grade_group(item: tuple[int, dict]) -> dict:
                        student_id, group = item
                        return await _grade_answer_sheet_group(
                            grade_paper_unified,
                            student_id,
                            group,
                            answer_map,
                            user.id,
                            result_category,
                            teaching_context=locked.teaching_context,
                        )

                    result_stream = bounded_completion_map(
                        pending_groups,
                        grade_group,
                        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
                    )
                    next_result = asyncio.create_task(anext(result_stream))
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_result},
                                timeout=_ANSWER_SHEET_HEARTBEAT_SECONDS,
                            )
                            if not done:
                                yield _answer_sheet_sse("progress", {
                                    "message": "正在并发生成批阅草稿",
                                    "current": len(drafts),
                                    "total": total,
                                    "heartbeat": True,
                                })
                                continue
                            try:
                                _position, draft = next_result.result()
                            except StopAsyncIteration:
                                break
                            drafts.append(draft)
                            locked.asset_lifecycle[_GRADING_DRAFT_CHECKPOINT_KEY] = {
                                "review_revision": locked.review_revision,
                                "layout_hash": locked.layout_hash,
                                "answer_spec_hash": locked.answer_spec_hash,
                                "student_ids": list(groups),
                                "teaching_context": session.teaching_context,
                                "drafts": drafts,
                            }
                            locked.error = None
                            await asyncio.to_thread(locked.save)
                            yield _answer_sheet_sse("progress", {
                                "message": "当前学生批阅进度已保存",
                                "current": len(drafts),
                                "total": total,
                                "checkpoint": True,
                                "student_id": draft["student_id"],
                            })
                            next_result = asyncio.create_task(anext(result_stream))
                    finally:
                        if not next_result.done():
                            next_result.cancel()
                        await asyncio.gather(next_result, return_exceptions=True)
                        await result_stream.aclose()

                    drafts_by_student = {
                        int(item["student_id"]): item for item in drafts
                    }
                    ordered_drafts = [
                        drafts_by_student[student_id] for student_id in groups
                    ]
                    locked.replace_grading_drafts(ordered_drafts)
                    locked.asset_lifecycle.pop(_GRADING_DRAFT_CHECKPOINT_KEY, None)
                    locked.error = None
                    await asyncio.to_thread(locked.save)
                    finalized = True
                    yield _answer_sheet_sse("done", {
                        "message": "批阅草稿已生成",
                        "current": total,
                        "total": total,
                        "review_revision": locked.review_revision,
                        "drafts": ordered_drafts,
                    })
                except (asyncio.CancelledError, GeneratorExit):
                    if not finalized:
                        locked.error = "批阅草稿连接中断，已保存完成学生；再次生成即可恢复。"
                        await asyncio.to_thread(locked.save)
                    raise
                except HTTPException as exc:
                    yield _answer_sheet_sse("error", {
                        "code": (
                            "GRADING_CHAIN_FAILED"
                            if exc.status_code >= 500
                            else "ANSWER_SHEET_VALIDATION_FAILED"
                        ),
                        "message": str(exc.detail),
                    })
                except Exception:
                    logger.exception("answer-sheet grading draft stream failed")
                    locked.error = "数学批改链路中断，已保存完成学生；请稍后重试恢复。"
                    await asyncio.to_thread(locked.save)
                    yield _answer_sheet_sse("error", {
                        "code": "GRADING_CHAIN_FAILED",
                        "message": locked.error,
                    })
        except HTTPException as exc:
            yield _answer_sheet_sse("error", {
                "code": "ANSWER_SHEET_OPERATION_BUSY" if exc.status_code == 409 else "GRADING_CHAIN_FAILED",
                "message": str(exc.detail),
            })

    return _answer_sheet_stream(stream_teaching(session.teaching_context, events()))


@router.post("/sessions/{session_id}/grading/decision")
@_requires_answer_sheet_writes
def save_grading_decision(
    session_id: str,
    req: GradingDecisionRequest,
    user: User = Depends(_require_user),
):
    """Persist a complete teacher verdict set with optimistic concurrency."""
    with _locked_owned(session_id, user.id) as session:
        _require_no_student_upload(session)
        if session.status != "review" or not session.grouping_decisions:
            raise HTTPException(409, detail="请先生成批阅草稿")
        if req.review_revision != session.review_revision:
            raise HTTPException(409, detail="批阅草稿已更新，请刷新后重试")
        draft = next(
            (
                item for item in session.grouping_decisions
                if int(item.get("student_id") or 0) == req.student_id
            ),
            None,
        )
        if draft is None:
            raise HTTPException(404, detail="该学生批阅草稿不存在")

        result = draft.get("result") or {}
        details = result.get("details") or []
        by_question: dict[str, dict] = {}
        for detail in details:
            question_no = str(
                detail.get("question_no")
                or detail.get("question_number")
                or detail.get("parent_question_no")
                or ""
            ).strip()
            if not question_no or question_no in by_question:
                raise HTTPException(409, detail="批阅草稿题号无效，不能保存人工判定")
            by_question[question_no] = detail
        submitted = {item.question_no.strip(): item for item in req.decisions}
        if "" in submitted or len(submitted) != len(req.decisions):
            raise HTTPException(400, detail="人工判定题号不能为空且不得重复")
        if set(submitted) != set(by_question):
            raise HTTPException(400, detail="人工判定必须完整覆盖该学生的全部题目")

        corrections = draft.setdefault("manual_corrections", [])
        changed = 0
        timestamp = datetime.now(timezone.utc).isoformat()
        default_paper_id = str((draft.get("paper_ids") or [""])[0])
        for question_no, decision in submitted.items():
            detail = by_question[question_no]
            old_result = str(detail.get("result") or "needs_review")
            if old_result == decision.result:
                continue
            detail["result"] = decision.result
            detail["teacher_regraded"] = True
            if decision.result == "correct":
                detail["error_type"] = ""
            paper_id = str((detail.get("paper_ids") or [default_paper_id])[0])
            corrections.append({
                "question_no": question_no,
                "paper_id": paper_id,
                "old_result": old_result,
                "new_result": decision.result,
                "note": decision.note.strip(),
                "teacher_id": user.id,
                "created_at": timestamp,
                "audit_key": uuid.uuid4().hex,
            })
            changed += 1

        recalculate_result_from_details(result, _result_scoring(session)[0])
        session.review_revision += 1
        session.save()
        return {
            "ok": True,
            "session_id": session.session_id,
            "student_id": req.student_id,
            "changed": changed,
            "review_revision": session.review_revision,
            "draft": draft,
        }


@router.post("/sessions/{session_id}/grading/archive")
@_requires_answer_sheet_writes
def archive_grading_drafts(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(_require_user),
):
    from services.grading_persistence import (
        persist_grading_result,
        write_deferred_filesystem_archives,
    )

    with _locked_owned(session_id, user.id) as session:
        _require_no_student_upload(session)
        if session.status == "archived":
            return {
                "ok": True,
                "archived": 0,
                "idempotent": True,
                "result_ids": list(session.archived_result_ids),
            }
        if session.status != "review" or not session.grouping_decisions:
            raise HTTPException(409, detail="请先生成并复核批阅草稿")
        for page in session.student_pages:
            registration = page.get("registration") or {}
            diagnostics = registration.get("diagnostics") or {}
            if (
                not orientation_is_ready(page)
                or registration.get("status") != "passed"
                or not diagnostics.get("answer_source_complete", False)
            ):
                raise HTTPException(409, detail="方向或答案区域完整性门禁未通过，不能归档")
        _verified_bubble_grids(session)
        result_category, result_exam_name = _result_scoring(session)

        pages_by_id = {
            str(page.get("paper_id") or ""): (index, page)
            for index, page in enumerate(session.student_pages)
        }
        archived_ids: list[int] = []
        archive_jobs: list[dict] = []
        try:
            for draft in session.grouping_decisions:
                result = draft.get("result") or {}
                if int(result.get("needs_review_count") or 0) > 0:
                    raise HTTPException(409, detail="仍有判分结果需要教师确认，不能归档")
                student = db.query(Student).filter(
                    Student.id == draft.get("student_id"),
                    Student.teacher_id == user.id,
                    Student.class_id == session.class_id,
                ).first()
                if not student:
                    raise HTTPException(404, detail="待归档学生已不存在")

                paper_ids = [str(item) for item in draft.get("paper_ids") or []]
                if not paper_ids or any(paper_id not in pages_by_id for paper_id in paper_ids):
                    raise HTTPException(409, detail="批阅草稿引用的学生原卷已失效")
                page_assets = []
                page_hashes = []
                for paper_id in paper_ids:
                    page_index, page = pages_by_id[paper_id]
                    page_hash = str(page.get("sha256") or "").strip()
                    if not page_hash:
                        raise HTTPException(409, detail="学生原卷缺少内容哈希，不能归档")
                    page_hashes.append(f"{paper_id}:{page_hash}")
                    page_assets.append({
                        "paper_id": paper_id,
                        "page_index": page_index,
                        "filename": str(page.get("filename") or ""),
                    })
                image_hash = hashlib.sha256(
                    (
                        f"{session.session_id}:{session.layout_hash}:"
                        + "|".join(page_hashes)
                    ).encode("utf-8")
                ).hexdigest()
                contract_hash = hashlib.sha256(
                    f"{session.answer_spec_hash}:{session.layout_hash}".encode("utf-8")
                ).hexdigest()
                corrections = list(draft.get("manual_corrections") or [])
                details = copy.deepcopy(result.get("details") or [])
                if session.teaching_context:
                    for detail in details:
                        detail["teaching_context"] = dict(session.teaching_context)
                row, _profile, _created = persist_grading_result(
                    db,
                    teaching_context=session.teaching_context,
                    student=student,
                    teacher_id=user.id,
                    category=result_category,
                    exam_name=result_exam_name,
                    image_path="",
                    image_hash=image_hash,
                    total_questions=int(result.get("total") or 0),
                    correct_count=int(result.get("correct_count") or 0),
                    partial_count=int(result.get("partial_count") or 0),
                    wrong_count=int(result.get("wrong_count") or 0),
                    needs_review_count=int(result.get("needs_review_count") or 0),
                    score=result.get("score"),
                    max_score=result.get("max_score"),
                    correct_rate=result.get("correct_rate"),
                    scoring_version=int(result.get("scoring_version") or 2),
                    grading_contract_hash=contract_hash,
                    details=details,
                    ocr_raw_name=student.name,
                    ocr_confidence=1.0,
                    ocr_raw_json={
                        "source": "answer_sheet",
                        "session_id": session.session_id,
                        "pages": page_assets,
                        "teaching_context": session.teaching_context,
                        **({
                            "batch_label": session.batch_label,
                            "answer_sheet_template_code": session.answer_sheet_template_code,
                            "paper_template_code": session.paper_template_code,
                        } if session.is_batch else {}),
                    },
                    grading_mode="precise",
                    correction_evidence_allowed=not bool(corrections),
                    commit=False,
                    archive_jobs=archive_jobs,
                )
                db.flush()
                for correction in corrections:
                    audit_key = str(correction.get("audit_key") or "").strip()
                    if not audit_key:
                        raise HTTPException(409, detail="人工改判审计键缺失，不能归档")
                    if db.query(GradingCorrection.id).filter(
                        GradingCorrection.audit_key == audit_key
                    ).first():
                        continue
                    db.add(GradingCorrection(
                        grading_result_id=row.id,
                        teacher_id=user.id,
                        paper_id=str(correction.get("paper_id") or ""),
                        question_no=str(correction.get("question_no") or ""),
                        audit_key=audit_key,
                        old_result=str(correction.get("old_result") or ""),
                        new_result=str(correction.get("new_result") or ""),
                        note=str(correction.get("note") or ""),
                    ))
                archived_ids.append(row.id)
            db.commit()
        except Exception:
            db.rollback()
            raise

        write_deferred_filesystem_archives(archive_jobs)
        session.mark_archived(archived_ids)
        session.save()
        return {
            "ok": True,
            "archived": len(archived_ids),
            "idempotent": False,
            "result_ids": archived_ids,
        }
