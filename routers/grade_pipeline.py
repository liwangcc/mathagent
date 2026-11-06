"""Batch grading execution: one grading path, two ways for a paper to enter it.

``_grade_paper_core`` grades one paper under the batch's frozen rubric.  The
per-paper pipeline feeds it each paper as soon as its OCR is final, while the
recognition streams of ``routers.grade`` run; ``_grading_events`` (the
``/batch-grade/confirm`` SSE stream) grades the remaining papers against the
frozen contract.  Both stamp their results with the rubric/template pins kept
here.  Moved unchanged from ``routers/grade.py``, which re-exports these names.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json as _json
import os
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from services.processing_errors import report_failure
from services.teaching_context import await_teaching
from services.grader import (
    GradingModelUnavailable,
    grade_paper_unified,
)
from services.bounded_dispatch import (
    bounded_completion_map,
)
from services.paper_orientation import orientation_is_ready
from services.quick_grader import (
    BatchOperationBusy,
    QuickBatch,
    batch_operation_lock,
)
from services.template_session import TemplateSession
from services.flash_grading import FLASH_LOCAL_MAX_CONCURRENT
from services.grading_contract import AnswerSpec
from services.student_service import canonical_student_no
from models import Student
from models_user import User
from config import UPLOAD_DIR
from routers.grade_common import (
    _GRADE_HEARTBEAT_SECONDS,
    _require_owned_batch,
    _review_avg_score,
    _review_timestamp,
    _save_quick_batch_meta,
    _settle_template_component,
    _sse,
    _stable_review_image_hash,
)
from routers.grade_availability import (
    _require_batch_scoring_chain,
)
from routers.grade_report import (
    _refresh_batch_knowledge,
    _start_batch_report_generation,
)
from routers.grade_contract import (
    _GRADING_CONTRACT_VERSION,
    _authoritative_answers_for_confirmation,
    _build_grading_contract,
    _grading_contract_hash,
    _initialize_batch_answer_map,
    _mark_unverified_answer_alignment,
    _reject_answer_key_matching_local_recoveries,
)

_GENERIC_GRADE_ERROR = "批改处理失败，请稍后重试"
# Reserve nine minutes for the text-only Flash grading stage after the
# six-minute OCR target; anything unfinished remains resumable rather than
# keeping the SSE request alive indefinitely.
_GRADE_BATCH_DEADLINE_S = 540.0


# ── 版本戳与逐卷流水批阅（2026-09-27）────────────────────────────────
# 幂等身份 = paper_id + rubric_hash：同一输入只一份有效结果；旧模板/旧
# rubric 的任务不能覆盖新结果。批阅只有一条流程（_grade_paper_core，与
# /batch-grade/confirm 共用），流水线只决定"何时"让每张卷进入该流程——
# 识别定稿一张就地批一张，不再等全量识别。

_RUBRIC_METADATA_KEYS = {
    "chapter", "topic", "question_bank_id", "difficulty", "matched", "source",
    # Derived from chapter/topic by the background knowledge classifier. It
    # arrives asynchronously, so a resume must not treat it as a new rubric
    # and regrade every paper the pipeline already finished.
    "knowledge_point_id",
}


def _batch_rubric_hash(batch: QuickBatch) -> str:
    """Scoring-input identity: only grading-relevant fields change the hash.

    Roster/metadata keys (chapter/topic/difficulty/source/...) are excluded so
    a background knowledge enrichment cannot masquerade as a rubric change.
    """
    questions = []
    for question_no in sorted(batch.answer_map or {}):
        entry = (batch.answer_map or {}).get(question_no) or {}
        questions.append((
            str(question_no),
            {
                str(key): value
                for key, value in entry.items()
                if key not in _RUBRIC_METADATA_KEYS
            },
        ))
    material = _json.dumps({
        "mode": str(batch.grade_mode or ""),
        "category": str(batch.category or ""),
        "exam_name": str(batch.exam_name or ""),
        "questions": questions,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _pin_batch_rubric(
    batch: QuickBatch,
    template_session: Optional["TemplateSession"] = None,
) -> dict:
    """Pin the rubric/template version stamps for one recognition run.

    Re-deriving the pins on every (re)recognition run is intentional: when the
    rubric actually changed (e.g. the teacher fixed the template and re-ran the
    batch), previously checkpointed rows keep their old stamps and are refused
    at write-back — stale work never overwrites reworked results.
    """
    batch.grading_rubric_hash = _batch_rubric_hash(batch)
    batch.template_contract_hash = str(
        getattr(template_session, "confirmed_contract_hash", "") or ""
    )
    return {
        "grading_rubric_hash": batch.grading_rubric_hash,
        "template_contract_hash": batch.template_contract_hash,
    }


def _row_matches_rubric_pin(batch: QuickBatch, row: dict) -> bool:
    """A result is current iff it carries no stamp (legacy) or matches the pin."""
    if not isinstance(row, dict):
        return False
    for field in ("grading_rubric_hash", "template_contract_hash"):
        stamp = str(row.get(field) or "")
        if stamp and stamp != str(getattr(batch, field, "") or ""):
            return False
    return True


def _upsert_batch_paper_result(batch: QuickBatch, review: dict, row: dict) -> None:
    """Checkpoint one paper's result set; one effective result per paper_id."""
    keys = {
        str(row.get("paper_id") or row.get("review_id") or ""),
        str(review.get("paper_id") or review.get("review_id") or ""),
    }
    keys.discard("")
    reviews = [
        item for item in batch.review_results
        if str(item.get("paper_id") or item.get("review_id") or "") not in keys
    ]
    rows = [
        item for item in batch.results
        if str(item.get("paper_id") or item.get("review_id") or "") not in keys
    ]
    reviews.append(review)
    rows.append(row)
    order = {
        str(paper.get("paper_id") or ""): index
        for index, paper in enumerate(batch.papers)
    }

    def _sort_key(item: dict) -> tuple:
        paper_id = str(item.get("paper_id") or item.get("review_id") or "")
        return (order.get(paper_id, len(order)), paper_id)

    batch.review_results = sorted(reviews, key=_sort_key)
    batch.results = sorted(rows, key=_sort_key)


def _build_template_confirmed_map(
    batch: QuickBatch,
    confirmed_for_grading: list[AnswerSpec],
) -> dict:
    """Rebuild the frozen grading map exactly as /batch-grade/confirm does.

    Geometry lives in the template artifact rather than in AnswerSpec; keep it
    when rebuilding so scoring does not lose answer boxes at confirmation.
    """
    old_map = batch.answer_map or {}
    confirmed_map: dict = {}
    for answer in confirmed_for_grading:
        old_entry = old_map.get(answer.question_no, {}) or {}
        confirmed_entry = answer.model_copy(update={
            "matched": True,
            "question_bank_id": old_entry.get("question_bank_id"),
            "difficulty": old_entry.get("difficulty") or 3,
            "source": "template_confirmed",
        }).to_answer_map_entry(source="template_confirmed")
        for key in ("answer_bboxes", "answer_box_source"):
            if key in old_entry:
                confirmed_entry[key] = copy.deepcopy(old_entry[key])
        confirmed_map[answer.question_no] = confirmed_entry
    return confirmed_map


def _pipeline_grading_answer_map(batch: QuickBatch) -> dict:
    """The confirmed-map snapshot every pipeline-graded paper is scored against."""
    confirmed = _authoritative_answers_for_confirmation(batch, batch.category or "daily")
    contract = _build_grading_contract(
        mode=str(batch.grade_mode or "precise"),
        category=str(batch.category or "daily"),
        exam_name=str(batch.exam_name or ""),
        confirmed=confirmed,
        identity_plan={},
        teaching_context=batch.teaching_context,
    )
    return _build_template_confirmed_map(batch, _contract_answer_specs(contract))


def _pipeline_identity_assignment(
    db: Session,
    batch: QuickBatch,
    paper: dict,
    teacher_id: int,
) -> dict | None:
    """Per-paper identity for pipeline grading; None = wait for the teacher.

    Mirrors _prepare_identity_plan's per-paper branches.  Anything not
    verifiable here stays unassigned: uncertain OCR identity is never silently
    attributed to a student (fail-closed).
    """
    if batch.identity_mode == "anonymous":
        label, _identity = _anonymous_page_identity(
            batch, str(paper.get("image_path") or ""),
        )
        return {"action": "anonymous", "student_id": None, "name": label, "student_no": ""}
    paper_id = str(paper.get("paper_id") or "")
    identity = paper.get("identity") or {}
    if identity.get("status") == "matched" and identity.get("student_id"):
        student = db.query(Student).filter(
            Student.id == int(identity["student_id"]),
            Student.teacher_id == teacher_id,
            Student.class_id == batch.class_id,
        ).first()
        if student is None:
            return None
        return {
            "action": "existing",
            "student_id": student.id,
            "name": student.name,
            "student_no": student.student_no or "",
        }
    frozen = ((batch.grading_contract or {}).get("identity_plan") or {}).get(paper_id) or {}
    action = str(frozen.get("action") or "")
    if action == "existing" and int(frozen.get("student_id") or 0) > 0:
        return {
            "action": "existing",
            "student_id": int(frozen["student_id"]),
            "name": str(frozen.get("name") or ""),
            "student_no": str(frozen.get("student_no") or ""),
        }
    frozen_name = str(frozen.get("name") or "").strip()
    if action == "create" and frozen_name:
        return {
            "action": "create",
            "student_id": None,
            "name": frozen_name,
            "student_no": canonical_student_no(frozen.get("student_no")),
        }
    return None


async def _grade_paper_core(
    batch: QuickBatch,
    paper: dict,
    paper_id: str,
    assignment: dict,
    teacher_id: int,
    *,
    detail_level: str,
    grading_category: str,
    grading_exam_name: str,
    grading_mode: str,
    answer_map: dict | None = None,
    stamp: dict | None = None,
) -> tuple[str, dict | None, dict]:
    """Grade one paper under the batch's frozen rubric — the single grading path.

    Used by both /batch-grade/confirm and the per-paper recognition pipeline;
    there is only one grading flow, the pipeline merely decides when each paper
    enters it.
    """
    sname = assignment["name"]
    sno = assignment["student_no"]
    grading_map = batch.answer_map if answer_map is None else answer_map
    paper_for_grading = _mark_unverified_answer_alignment(paper)
    paper_for_grading = _reject_answer_key_matching_local_recoveries(
        paper_for_grading,
        grading_map,
    )
    try:
        result = await await_teaching(batch.teaching_context, grade_paper_unified(
            paper=paper_for_grading, answer_map=grading_map,
            student_name=sname, teacher_id=teacher_id,
            detail_level=detail_level, category=grading_category,
        ))
    except GradingModelUnavailable as exc:
        # The grading queue already made one complete retry. Do not
        # cancel unrelated papers or turn every question into a
        # teacher review item. Omitting this paper from the durable
        # result set makes the existing resume action pick it up.
        info = report_failure(exc, stage="grading", task_id=batch.batch_id, operation_id=paper_id)
        paper["grading_error_info"] = info
        return sname, None, {
            "paper_id": paper_id,
            "_grading_pending": True,
            "error_info": info,
        }

    # 身份已冻结；此阶段只保存暂存 assignment，正式档案由 archive-all 创建。
    paper.pop("grading_error_info", None)
    knowledge_items = []
    for d in result["details"]:
        if batch.teaching_context:
            d["teaching_context"] = copy.deepcopy(batch.teaching_context)
        source_question_no = str(
            d.get("parent_question_no") or d.get("question_no", d.get("question_number", ""))
        )
        entry = grading_map.get(source_question_no, {}) or {}
        knowledge_items.append({
            "question_no": source_question_no,
            "detail_question_no": str(d.get("question_no") or d.get("question_number") or ""),
            "question_bank_id": entry.get("question_bank_id"),
            "chapter": entry.get("chapter") or "未知",
            "topic": entry.get("topic") or entry.get("chapter") or "未知",
            "difficulty": entry.get("difficulty") or 3,
        })

    review = {
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "review_id": paper_id,
        "paper_id": paper_id,
        "filename": paper.get("filename", ""),
        "student_name": sname,
        "student_no": sno,
        "identity_assignment": assignment,
        "image_path": paper.get("image_path", ""),
        "image_content_hash": _stable_review_image_hash(
            batch.batch_id, paper_id, str(paper.get("image_path") or "")
        ),
        "ocr_raw_name": (paper.get("student_name") or "").strip(),
        "ocr_raw_json": paper_for_grading,
        "category": grading_category,
        "exam_name": grading_exam_name,
        "grading_mode": grading_mode,
        "result": result,
        "knowledge_items": knowledge_items,
        "disposition": "active",
        "manual_corrections": [],
        "voided_at": "",
        "void_reason": "",
        "archived_grading_result_id": None,
        "archived_profile_id": None,
    }
    row = {
        "teaching_context": copy.deepcopy(batch.teaching_context),
        "review_id": paper_id,
        "student_name": sname,
        "grading_result_id": None,
        "profile_id": None,
        "score": result["score"],
        "max_score": result.get("max_score"),
        "correct_rate": result.get("correct_rate"),
        "category": grading_category,
        "exam_name": grading_exam_name,
        "correct": result["correct_count"],
        "partial": result["partial_count"],
        "wrong": result["wrong_count"],
        "needs_review": result["needs_review_count"],
        "total": result["total"],
        "details": result["details"],
        "grading_mode": grading_mode,
        "idempotent": False,
        "needs_name_confirm": False,
        "paper_id": paper_id,
        "disposition": "active",
    }
    if stamp:
        review.update(stamp)
        row.update(stamp)
    return sname, review, row


class _PerPaperGradingPipeline:
    """Grade each paper as soon as its OCR outcome is final.

    Runs inside the recognition stream: dispatch is per ingested paper (single
    paper ordering — recognition finalizes first, then that paper is graded),
    cross-paper grading runs in parallel.  Every result is stamped with the
    batch's rubric/template pins and refused at write-back when the stamp no
    longer matches, so stale tasks can never overwrite reworked results.
    """

    def __init__(
        self,
        batch: QuickBatch,
        db_gen: Session,
        teacher_id: int,
        *,
        answer_map: dict,
    ) -> None:
        self.batch = batch
        self.db_gen = db_gen
        self.teacher_id = teacher_id
        self.answer_map = answer_map
        self.stamp = {
            "grading_rubric_hash": batch.grading_rubric_hash,
            "template_contract_hash": batch.template_contract_hash,
        }
        self.queue: asyncio.Queue = asyncio.Queue()
        self.tasks: set[asyncio.Task] = set()
        self.dispatched: set[str] = set()
        self.done_count = 0
        self.detail_level = (
            "simple" if str(batch.grade_mode or "") in ("auto", "simple") else "detailed"
        )

    @property
    def total_count(self) -> int:
        return len(self.batch.papers)

    def _already_graded(self, paper_id: str) -> bool:
        return any(
            str(row.get("paper_id") or row.get("review_id") or "") == paper_id
            and _row_matches_rubric_pin(self.batch, row)
            for row in self.batch.results
        )

    def dispatch(self, paper: dict) -> bool:
        batch = self.batch
        paper_id = str(paper.get("paper_id") or "")
        if not paper_id or paper_id in self.dispatched or self._already_graded(paper_id):
            return False
        if not orientation_is_ready(paper):
            return False
        assignment = _pipeline_identity_assignment(
            self.db_gen, batch, paper, self.teacher_id,
        )
        if assignment is None:
            return False
        self.dispatched.add(paper_id)
        _mark_grading_started(batch)
        task = asyncio.create_task(
            self._grade_and_checkpoint(paper, paper_id, assignment)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    def dispatch_inherited(self) -> None:
        """Pages that inherit a matched identity become ready with their anchor."""
        for paper in self.batch.papers:
            if (paper.get("identity") or {}).get("inherited_from"):
                self.dispatch(paper)

    def dispatch_upload(self, upload: dict) -> None:
        """Grade the paper just ingested from ``upload``, then its inheritors."""
        image_path = os.path.realpath(str(upload.get("image_path") or ""))
        ingested = next(
            (
                paper for paper in self.batch.papers
                if os.path.realpath(str(paper.get("image_path") or "")) == image_path
            ),
            None,
        )
        if ingested is not None:
            self.dispatch(ingested)
            self.dispatch_inherited()

    async def drain(self):
        """Yield ``("graded", item)`` until every dispatched paper is graded.

        Yields ``("heartbeat", None)`` when no result arrived within one
        heartbeat interval. Nothing is pending between yields.
        """
        while True:
            self.tasks = {task for task in self.tasks if not task.done()}
            if not self.tasks and self.queue.empty():
                return
            get_task = asyncio.create_task(self.queue.get())
            try:
                graded = await asyncio.wait_for(
                    get_task, timeout=_GRADE_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                yield "heartbeat", None
                continue
            yield "graded", graded

    async def _grade_and_checkpoint(
        self,
        paper: dict,
        paper_id: str,
        assignment: dict,
    ) -> None:
        batch = self.batch
        try:
            sname, review, row = await _grade_paper_core(
                batch, paper, paper_id, assignment, self.teacher_id,
                detail_level=self.detail_level,
                grading_category=str(batch.category or "daily"),
                grading_exam_name=str(batch.exam_name or ""),
                grading_mode=str(batch.grade_mode or "precise"),
                answer_map=self.answer_map,
                stamp=self.stamp,
            )
        except Exception as exc:
            # A pipeline grade must never take recognition down. Leave the
            # paper pending: the existing confirm/resume flow retries it.
            logger.exception("逐卷流水批阅异常 paper=%s: %s", paper_id, exc)
            await self.queue.put((
                None, None,
                {"paper_id": paper_id, "_grading_pending": True, "error": str(exc)},
            ))
            return
        if review is None:
            self.done_count += 1
            await self.queue.put((sname, None, row))
            return
        if not _row_matches_rubric_pin(batch, row):
            # Stale task (rubric/template re-pinned meanwhile): never overwrite.
            await self.queue.put((sname, None, {
                "paper_id": paper_id, "_grading_pending": True, "stale": True,
            }))
            return
        _upsert_batch_paper_result(batch, review, row)
        self.done_count += 1
        try:
            await _save_quick_batch_meta(batch)
        except Exception:
            logger.exception("逐卷流水落盘失败 paper=%s", paper_id)
        await self.queue.put((sname, review, row))

    async def aclose(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            async def settle_tasks():
                await asyncio.gather(*tasks, return_exceptions=True)

            # Shield the join, not the grading: all workers are cancelled once,
            # but repeated consumer cancellation must not interrupt their cleanup.
            close_task = asyncio.create_task(settle_tasks())
            await _settle_template_component(close_task, cancel_running=False)
        self.tasks.clear()


def _start_per_paper_grading(
    batch: QuickBatch,
    db: Session,
    teacher_id: int,
    template_session: TemplateSession,
) -> _PerPaperGradingPipeline:
    """Freeze the batch's answers and rubric pins, then start grading.

    The answer map comes from the frozen template and does not depend on
    recognition, so each paper can be graded as soon as its OCR is final.
    """
    _initialize_batch_answer_map(batch, teacher_id, template_session)
    _pin_batch_rubric(batch, template_session)
    return _PerPaperGradingPipeline(
        batch, db, teacher_id,
        answer_map=_pipeline_grading_answer_map(batch),
    )


def _pipeline_result_sse(pipeline: _PerPaperGradingPipeline, graded: tuple) -> str:
    sname, review, row = graded
    if review is None:
        marker = row or {}
        message = (
            f"{sname or '一张试卷'} 的批阅已按新版本作废，将按当前模板重新批阅"
            if marker.get("stale")
            else f"{sname or '一张试卷'} 的数学判分暂缓，其他试卷继续处理"
        )
        return _sse("progress", {
            "message": message,
            "current": pipeline.done_count,
            "total": pipeline.total_count,
        })
    return _sse("result", row)


def _contract_answer_specs(contract: dict) -> list[AnswerSpec]:
    return [
        AnswerSpec.model_validate(item)
        for item in (contract.get("confirmed") or [])
    ]


def _confirmation_questions_from_contract(contract: dict) -> list[dict]:
    questions = []
    for item in _contract_answer_specs(contract):
        questions.append({
            **item.directory_metadata(),
            **({"drawing_reference": copy.deepcopy(item.drawing_reference)} if item.drawing_reference is not None else {}),
            "question_no": item.question_no,
            "content": item.question_text,
            "question_type": item.question_type,
            "choice_mode": item.choice_mode,
            "answer": item.standard_answer,
            "answer_variants": list(item.answer_variants),
            "solution_steps": list(item.solution_steps),
            "max_score": item.max_score,
            "blank_count": item.blank_count,
            "answer_slots": list(item.answer_slots),
            "fill_scoring_mode": item.fill_scoring_mode,
            "blank_scores": list(item.blank_scores),
            "subquestion_count": item.subquestion_count,
            "subquestion_scores": list(item.subquestion_scores),
            "grading_method": item.grading_method,
            "chapter": item.chapter,
            "topic": item.topic,
            "matched": True,
            "source": "teacher_confirmed",
        })
    return questions


class _GradingRefused(Exception):
    """A grading request the batch's durable state cannot honour."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.payload = {"code": code, "message": message}


def _grading_contract_for_run(
    db: Session,
    batch: QuickBatch,
    req: ConfirmReq,
    teacher_id: int,
    confirmed: list[AnswerSpec],
) -> tuple[dict, str, bool]:
    """Choose the contract this run grades against.

    Returns ``(contract, request_hash, has_stored_contract)``. A batch with
    durable results keeps the contract it was frozen with: a request that
    differs from it, or a legacy batch without a recoverable contract, is
    refused rather than graded against something new.
    """
    stored_contract = (
        batch.grading_contract
        if isinstance(batch.grading_contract, dict)
        and batch.grading_contract.get("version") == _GRADING_CONTRACT_VERSION
        else {}
    )
    frozen_plan = stored_contract.get("identity_plan") or {}
    identity_plan = (
        _prepare_anonymous_identity_plan(batch)
        if batch.identity_mode == "anonymous"
        else _prepare_identity_plan(
            db,
            batch,
            req.identity_resolutions,
            teacher_id,
            frozen_fallback=frozen_plan,
        )
    )
    request_contract = _build_grading_contract(
        mode=req.mode,
        category=req.category,
        exam_name=str(req.exam_name or "").strip(),
        confirmed=confirmed,
        identity_plan=identity_plan,
        teaching_context=batch.teaching_context,
    )
    contract_hash = _grading_contract_hash(request_contract)
    has_durable_results = bool(batch.review_results or batch.results)
    if has_durable_results and batch.grading_contract_hash and not stored_contract:
        raise _GradingRefused(
            "GRADING_CONTRACT_UNRECOVERABLE",
            "旧批次缺少完整可恢复合同，不能安全续批；请新建批次",
        )
    stored_matches = bool(stored_contract) and _grading_contract_hash(stored_contract) == contract_hash
    if stored_contract and not stored_matches and has_durable_results:
        raise _GradingRefused(
            "GRADING_CONTRACT_CHANGED",
            "该批次已有部分结果，不能改用另一组答案或学生归属继续；请恢复原确认内容后重试",
        )
    contract = stored_contract if stored_matches else request_contract
    return contract, contract_hash, bool(stored_contract)


def _freeze_grading_contract(batch: QuickBatch, contract: dict, contract_hash: str) -> None:
    batch.grading_contract = contract
    batch.grading_contract_hash = contract_hash
    batch.confirmation_questions = _confirmation_questions_from_contract(contract)
    batch.grade_mode = str(contract["mode"])
    batch.category = str(contract["category"])
    batch.exam_name = str(contract.get("exam_name") or "")


def _start_grading_run(batch: QuickBatch, confirmed: list[AnswerSpec]) -> dict:
    """Bind the batch to its contract's answers and mark it grading.

    Returns the rubric stamp that every graded row carries.
    """
    # The frozen template is authoritative; grade only against the
    # confirmed contract.
    confirmed_map = _build_template_confirmed_map(batch, confirmed)
    if len(confirmed_map) < len(batch.questions):
        logger.warning(
            "batch %s: 确认题数 %d < 识别题数 %d，未确认题将不带预填答案批改",
            batch.batch_id, len(confirmed_map), len(batch.questions),
        )
    # 红线兜底：只含确认题的 map 替换原 map，彻底切断未确认答案进入判分的路径
    batch.answer_map = confirmed_map
    if not batch.grading_rubric_hash:
        _pin_batch_rubric(batch)
    batch.status = "grading"
    batch.error = None
    _mark_grading_started(batch)
    return {
        "grading_rubric_hash": batch.grading_rubric_hash,
        "template_contract_hash": batch.template_contract_hash,
    }


@dataclass
class _GradingProgress:
    """Which papers of a batch already have a checkpointed grade."""

    paper_ids: list[str]
    review_by_id: dict[str, dict]
    row_by_id: dict[str, dict]
    completed_ids: set[str]
    pending_items: list[tuple[int, dict]]
    # Papers whose grading is postponed to a later run.
    deferred_ids: set[str]

    @classmethod
    def of(cls, batch: QuickBatch) -> "_GradingProgress":
        """Papers whose review and row both match the batch's rubric pin are done."""
        paper_ids = [
            str(paper.get("paper_id") or f"paper-{index + 1}")
            for index, paper in enumerate(batch.papers)
        ]
        review_by_id = {
            str(item.get("paper_id") or item.get("review_id") or ""): item
            for item in batch.review_results
        }
        row_by_id = {
            str(item.get("paper_id") or item.get("review_id") or ""): item
            for item in batch.results
        }
        completed_ids = {
            paper_id
            for paper_id in (set(review_by_id) & set(row_by_id) & set(paper_ids))
            if _row_matches_rubric_pin(batch, row_by_id[paper_id])
            and _row_matches_rubric_pin(batch, review_by_id[paper_id])
        }
        pending_items = [
            (index, paper)
            for index, paper in enumerate(batch.papers)
            if paper_ids[index] not in completed_ids
        ]
        return cls(paper_ids, review_by_id, row_by_id, completed_ids, pending_items, set())

    def record(self, batch: QuickBatch, review: dict, row: dict) -> None:
        """Add one graded paper and mirror the progress on the batch."""
        paper_id = str(row.get("paper_id") or row.get("review_id") or "")
        self.review_by_id[paper_id] = review
        self.row_by_id[paper_id] = row
        self.completed_ids.add(paper_id)
        batch.review_results = [
            self.review_by_id[pid] for pid in self.paper_ids if pid in self.review_by_id
        ]
        batch.results = [self.row_by_id[pid] for pid in self.paper_ids if pid in self.row_by_id]
        batch.status = "grading"
        batch.error = None


async def _graded_papers(pending_items, grade_one, *, deadline_at: float):
    """Grade the pending papers, yielding each result as it completes.

    Yields ``("graded", (student_name, review, row))`` per finished paper,
    ``("heartbeat", None)`` when nothing finished within one heartbeat
    interval, and ``("deadline", None)`` once the batch deadline has passed.
    Completion order is intentional: a fast paper is checkpointed even if an
    earlier paper is slow or a concurrent model call fails. On exit the
    in-flight wait is settled and the dispatcher closed; consume this under
    ``contextlib.aclosing`` so that runs before the caller's own cleanup.
    """
    result_iter = bounded_completion_map(
        pending_items,
        grade_one,
        # Flash is the only grading model; its request lane is the valve.
        max_concurrent=FLASH_LOCAL_MAX_CONCURRENT,
    ).__aiter__()
    next_result_task = None
    try:
        while True:
            remaining = deadline_at - asyncio.get_running_loop().time()
            if remaining <= 0:
                yield "deadline", None
                return
            if next_result_task is None:
                next_result_task = asyncio.create_task(anext(result_iter))
            try:
                _position, graded = await asyncio.wait_for(
                    asyncio.shield(next_result_task),
                    timeout=min(_GRADE_HEARTBEAT_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                if asyncio.get_running_loop().time() >= deadline_at:
                    yield "deadline", None
                    return
                yield "heartbeat", None
                continue
            except StopAsyncIteration:
                next_result_task = None
                return
            next_result_task = None
            yield "graded", graded
    finally:
        if next_result_task is not None:
            if not next_result_task.done():
                next_result_task.cancel()
            await asyncio.gather(next_result_task, return_exceptions=True)
        await result_iter.aclose()


async def _stop_for_grading_deadline(batch: QuickBatch, progress: _GradingProgress) -> dict:
    """Keep the finished results and defer every paper still being graded."""
    progress.deferred_ids.update(
        str(paper.get("paper_id") or f"paper-{index + 1}")
        for index, paper in progress.pending_items
        if str(paper.get("paper_id") or f"paper-{index + 1}")
        not in progress.completed_ids
    )
    batch.status = "grading_interrupted"
    batch.error = (
        "批量数学判分达到总时限，已保存完成结果；"
        f"剩余 {len(progress.deferred_ids)} 张可重新提交继续。"
    )
    await _save_quick_batch_meta(batch)
    return {
        "code": "GRADING_BATCH_DEADLINE",
        "message": batch.error,
    }


async def _finish_grading_run(
    batch: QuickBatch,
    progress: _GradingProgress,
    teacher_id: int,
) -> None:
    """Publish the complete review, then start the batch report in the background."""
    batch.review_results = [progress.review_by_id[pid] for pid in progress.paper_ids]
    batch.results = [progress.row_by_id[pid] for pid in progress.paper_ids]
    batch.status = "review_ready"
    _refresh_batch_knowledge(batch, teacher_id)
    if not batch.review_ready_at:
        batch.review_ready_at = _review_timestamp()
    batch.grading_elapsed_seconds = _grading_elapsed_seconds(batch)
    batch.error = None
    await _save_quick_batch_meta(batch)
    logger.info(
        "批阅计时 batch=%s papers=%d graded_now=%d elapsed_s=%s",
        batch.batch_id,
        len(batch.results),
        len(progress.pending_items),
        batch.grading_elapsed_seconds,
    )
    # Publish the completed grading result first.  The report is a
    # separate, lightweight model call and must never delay the result
    # table or make a teacher wait for narrative text.
    await _start_batch_report_generation(batch, teacher_id)


def _finished_grading_replay(batch: QuickBatch) -> list[str]:
    """Re-send a finished batch's rows and its done event, marked idempotent."""
    return [
        *(_sse("result", row) for row in batch.results),
        _sse("done", {
            "total": len(batch.results),
            "avg_score": _review_avg_score(batch.results),
            "idempotent": True,
            "review_revision": batch.review_revision,
            "grading_elapsed_seconds": batch.grading_elapsed_seconds,
        }),
    ]


async def _scoring_chain_unavailable(batch: QuickBatch) -> dict | None:
    """The SSE error payload when the scoring chain cannot grade right now."""
    try:
        await _require_batch_scoring_chain(batch)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {
            "code": "GRADING_CHAIN_UNAVAILABLE",
            "status": str(detail.get("status") or "fault"),
            "message": str(
                detail.get("message")
                or "批改链路当前不可用，请等待恢复后再重试。"
            ),
        }
    return None


def _completed_papers_replay(progress: _GradingProgress, total: int) -> list[str]:
    """Re-send the already graded papers so a reconnecting client's counts add up."""
    completed = len([pid for pid in progress.paper_ids if pid in progress.completed_ids])
    events = []
    for paper_id in progress.paper_ids:
        if paper_id not in progress.completed_ids:
            continue
        row = {**progress.row_by_id[paper_id], "idempotent": True}
        events.append(_sse("progress", {
            "message": f"已恢复 {row.get('student_name') or '一张试卷'}",
            "current": completed,
            "total": total,
        }))
        events.append(_sse("result", row))
    return events


async def _record_graded_paper(
    batch: QuickBatch,
    progress: _GradingProgress,
    graded: tuple[str, dict | None, dict],
    total: int,
) -> list[str]:
    """Checkpoint one graded paper, or defer it when its grading is still pending."""
    student_name, review, row = graded
    if row.get("_grading_pending"):
        progress.deferred_ids.add(str(row.get("paper_id") or row.get("review_id") or ""))
        return [_sse("progress", {
            "message": f"{student_name} 的数学判分暂缓，其他试卷继续处理",
            "current": len(progress.completed_ids),
            "total": total,
        })]
    progress.record(batch, review, row)
    await _save_quick_batch_meta(batch)
    return [
        _sse("progress", {
            "message": f"已批改 {student_name}",
            "current": len(progress.completed_ids),
            "total": total,
            "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
        }),
        _sse("result", row),
    ]


async def _stop_for_deferred_papers(batch: QuickBatch, progress: _GradingProgress) -> dict:
    """Keep the finished results; deferred papers wait for a resubmission."""
    failures = [
        {"paper_id": paper.get("paper_id"), "error_info": paper["grading_error_info"]}
        for paper in batch.papers
        if paper.get("paper_id") in progress.deferred_ids and paper.get("grading_error_info")
    ]
    batch.status = "grading_interrupted"
    batch.error = (
        f"{len(progress.deferred_ids)} 张试卷的数学判分已重试一次仍未完成；"
        "其他结果已保存，服务恢复后重新提交即可继续。"
    )
    await _save_quick_batch_meta(batch)
    return {
        "code": "GRADING_PAPERS_PENDING",
        "message": batch.error,
        "error_info": failures[0]["error_info"] if failures else None,
        "failures": failures,
    }


async def _mark_grading_interrupted(
    batch: QuickBatch,
    error: str,
    *,
    reason: str,
    keep_error: bool = False,
) -> None:
    """Checkpoint a run that ended early; its finished results stay saved."""
    batch.status = "grading_interrupted"
    if not (keep_error and batch.error):
        batch.error = error
    try:
        await _save_quick_batch_meta(batch)
    except Exception:
        logger.exception("failed to checkpoint %s grading batch %s", reason, batch.batch_id)


async def _grading_events(
    req: ConfirmReq,
    user: User,
    batch: QuickBatch,
    confirmed: list[AnswerSpec],
):
    """Grade a batch against its frozen contract (SSE), resuming from checkpoints.

    The batch lock is held for the whole run. A reconnect first receives the
    already graded prefix, then only unfinished papers are dispatched. Every
    graded paper is checkpointed; a run that ends early leaves the batch
    ``grading_interrupted`` with its finished results kept.
    """
    teacher_id = user.id
    db = SessionLocal()
    batch_lock = None
    grading_started = False
    try:
        try:
            batch_lock = batch_operation_lock(req.batch_id, UPLOAD_DIR)
            batch_lock.__enter__()
        except BatchOperationBusy:
            batch_lock = None
            yield _sse("error", {
                "code": "BATCH_GRADING_BUSY",
                "message": "该批次正在批改，请等待当前任务完成，不要重复提交",
            })
            return

        # The request may have waited behind another worker before entering
        # the generator. Always resume from the newest durable checkpoint.
        batch = QuickBatch.load_meta(req.batch_id, UPLOAD_DIR)
        _require_owned_batch(batch, user)
        _require_complete_batch_ocr(batch)
        _require_orientation_ready(batch)
        contract, contract_hash, has_stored_contract = _grading_contract_for_run(
            db, batch, req, teacher_id, confirmed,
        )
        confirmed_for_grading = _contract_answer_specs(contract)
        identity_plan = contract["identity_plan"]
        grading_mode = str(contract["mode"])
        grading_category = str(contract["category"])
        grading_exam_name = str(contract.get("exam_name") or "")

        # Terminal replay remains idempotent, but only for the exact frozen
        # contract. A changed request must never silently replay old marks.
        if batch.status in _REVIEW_FINISHED_STATUSES and batch.results:
            if not has_stored_contract:
                raise _GradingRefused(
                    "GRADING_CONTRACT_UNRECOVERABLE",
                    "旧批次缺少可验证的批改合同，不能安全重放；请新建批次",
                )
            for event in _finished_grading_replay(batch):
                yield event
            return

        # Recheck immediately before freezing the contract or switching to
        # grading. This closes the race between the HTTP preflight and
        # generator start.
        unavailable = await _scoring_chain_unavailable(batch)
        if unavailable is not None:
            yield _sse("error", unavailable)
            return

        _freeze_grading_contract(batch, contract, contract_hash)
        # _prepare_identity_plan may assign stable IDs to legacy entries.
        await _save_quick_batch_meta(batch)
        stamp = _start_grading_run(batch, confirmed_for_grading)
        await _save_quick_batch_meta(batch)
        grading_started = True

        total = len(batch.papers)
        yield _sse("progress", {
            "message": "恢复批改" if batch.results else "开始批改",
            "current": 0,
            "total": total,
            "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
        })
        progress = _GradingProgress.of(batch)
        detail_level = "simple" if grading_mode in ("auto", "simple") else "detailed"
        deadline_at = asyncio.get_running_loop().time() + _GRADE_BATCH_DEADLINE_S
        # A reconnect receives the complete prefix, then only unfinished
        # papers are dispatched. This keeps frontend completion accounting
        # stable without paying for already checkpointed model calls.
        for event in _completed_papers_replay(progress, total):
            yield event

        async def grade_one(item: tuple[int, dict]) -> tuple[str, dict | None, dict]:
            idx, paper = item
            paper_id = str(paper.get("paper_id") or f"paper-{idx + 1}")
            return await _grade_paper_core(
                batch, paper, paper_id, identity_plan[paper_id], teacher_id,
                detail_level=detail_level,
                grading_category=grading_category,
                grading_exam_name=grading_exam_name,
                grading_mode=grading_mode,
                stamp=stamp,
            )

        async with aclosing(
            _graded_papers(progress.pending_items, grade_one, deadline_at=deadline_at)
        ) as graded_papers:
            async for kind, graded in graded_papers:
                if kind == "deadline":
                    yield _sse("error", await _stop_for_grading_deadline(batch, progress))
                    return
                if kind == "heartbeat":
                    yield _sse("progress", {
                        "message": "模型正在批改，请保持页面打开",
                        "current": len(progress.completed_ids),
                        "total": total,
                        "heartbeat": True,
                        "grading_elapsed_seconds": _grading_elapsed_seconds(batch),
                    })
                    continue
                for event in await _record_graded_paper(batch, progress, graded, total):
                    yield event

        if progress.deferred_ids:
            yield _sse("error", await _stop_for_deferred_papers(batch, progress))
            return
        if len(progress.completed_ids) != total:
            raise RuntimeError(
                f"grading checkpoint incomplete: {len(progress.completed_ids)}/{total}"
            )
        await _finish_grading_run(batch, progress, teacher_id)
        yield _sse("done", {
            "total": len(batch.results),
            "avg_score": _review_avg_score(batch.results),
            "review_revision": batch.review_revision,
            "grading_elapsed_seconds": batch.grading_elapsed_seconds,
        })
    except _GradingRefused as refused:
        yield _sse("error", refused.payload)
    except asyncio.CancelledError:
        if grading_started and batch.status == "grading":
            await _mark_grading_interrupted(
                batch, "批改连接中断，已保留完成结果，可重新提交继续", reason="cancelled",
            )
        raise
    except Exception as e:
        info = report_failure(e, stage="grading", task_id=batch.batch_id)
        try:
            db.rollback()
        except Exception:
            pass
        if grading_started:
            await _mark_grading_interrupted(batch, _GENERIC_GRADE_ERROR, reason="errored")
        yield _sse("error", {
            "code": "GRADING_CHAIN_FAILED",
            "message": "批改链路发生故障，已保留完成结果；请稍后重新提交以继续未完成试卷",
            "error_info": info,
        })
    finally:
        if grading_started and batch.status not in _REVIEW_FINISHED_STATUSES | {"done"}:
            await _mark_grading_interrupted(
                batch, "批改未完成，已保留完成结果，可重新提交继续",
                reason="unfinished", keep_error=True,
            )
        db.close()
        if batch_lock is not None:
            batch_lock.__exit__(None, None, None)


def _mark_grading_started(batch: QuickBatch) -> None:
    """Start the batch grading stopwatch once, when the first paper is graded."""
    if not batch.grading_started_at:
        batch.grading_started_at = _review_timestamp()


def _grading_elapsed_seconds(batch: QuickBatch) -> float | None:
    """Wall-clock seconds since the first paper started grading."""
    try:
        started = datetime.fromisoformat(batch.grading_started_at)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (datetime.now(timezone.utc) - started).total_seconds()), 1)


# routers.grade imports this module only after defining everything below, and
# re-exports the names above; importing it last works in either import order.
# ``logger`` is routers.grade's, so these records keep their logger name.
from routers.grade import (  # noqa: E402
    ConfirmReq,
    _REVIEW_FINISHED_STATUSES,
    _anonymous_page_identity,
    _prepare_anonymous_identity_plan,
    _prepare_identity_plan,
    _require_complete_batch_ocr,
    _require_orientation_ready,
    logger,
)
