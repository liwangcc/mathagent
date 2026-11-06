"""How a confirmed template becomes the answer authority of one grading batch.

- The template: ``_validate_template_for_recognition`` checks it still fits the
  batch; ``_build_template_paper_cache`` turns its verified page artifacts into
  the answers-only OCR cache; ``_template_handwriting_regions_for_mode``
  normalizes its answer boxes; ``_single_page_template_failure_questions``
  gives review placeholders when OCR of a page fails.
- The batch answer map: ``_initialize_batch_answer_map`` freezes the template's
  answers into the batch; ``_confirmation_questions`` lists what the teacher
  confirms; ``_authoritative_answers_for_confirmation`` is what grading uses.
- The grading contract: ``_build_grading_contract`` and ``_grading_contract_hash``
  pin those answers (``_GRADING_CONTRACT_VERSION``) so a confirmed batch is
  graded against exactly what the teacher saw.

Moved unchanged from routers/grade.py, which still exposes these names.
"""

import copy
import hashlib
import json as _json
import logging
import re

from fastapi import HTTPException

from routers.grade_common import _load_owned_template_session
from services.grading_contract import AnswerSpec, finalize_answer_spec_for_category
from services.paper_cache import PaperCache
from services.quick_grader import QuickBatch
from services.review_marks import normalize_review_boxes
from services.teaching_context import validate_teaching_context
from services.template_evidence import (
    TemplateEvidenceError,
    normalize_template_handwriting_regions,
    verify_template_artifacts,
)
from services.template_session import TemplateSession, normalize_template_mode

# routers.grade's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade")


# ── The template, as this batch's answer authority ───────────────────────
def _validate_template_for_recognition(
    sess: TemplateSession,
    category: str,
    exam_name: str,
    *,
    persist_reconfirmation: bool = False,
) -> None:
    if sess.status not in ("confirmed", "frozen") or not sess.pages:
        raise HTTPException(400, detail="模板未保存或不存在，请先保存模板")
    contract_error = sess.recognition_contract_error()
    if contract_error:
        if (
            persist_reconfirmation
            and (
                not sess.requires_reconfirmation
                or sess.reconfirmation_reason != contract_error
            )
        ):
            sess.requires_reconfirmation = True
            sess.reconfirmation_reason = contract_error
            sess.save_meta()
        raise HTTPException(409, detail=contract_error)
    if sess.category and sess.category != category:
        raise HTTPException(409, detail="模板批次类型与当前批次不一致，请重新导入模板")
    if category == "exam" and sess.exam_name and sess.exam_name != exam_name:
        raise HTTPException(409, detail="模板考试名称与当前批次不一致，请重新导入模板")


def _template_handwriting_regions_for_mode(
    template_mode: object,
    raw_regions: object,
    expected_numbers: list[str] | None = None,
    *,
    teacher_confirmed: bool = False,
) -> list[dict]:
    """Normalize model/teacher boxes for either legacy template mode.

    ``template_mode`` is retained as descriptive metadata for old clients. Both
    modes now use the same visual contract: every expected question has a
    ``regions`` list, which may be empty when the model found no handwriting.
    """
    normalize_template_mode(template_mode, default="marked")
    expected = [str(number).strip() for number in (expected_numbers or [])]
    if raw_regions is None:
        normalized = [
            {"question_number": number, "regions": []}
            for number in expected
        ]
    else:
        normalized = normalize_template_handwriting_regions(raw_regions)
    if expected_numbers is not None:
        by_number = {item["question_number"]: item for item in normalized}
        if set(by_number) != set(expected) or len(normalized) != len(expected):
            raise ValueError("模板已有笔迹框必须与本页题目逐一对应")
        normalized = [by_number[number] for number in expected]
    if teacher_confirmed:
        # Geometry submitted through the teacher override endpoint is the
        # authority for this revision. Ignore any client-supplied provenance
        # and stamp an auditable server-owned teacher marker.
        normalized = [
            {
                **item,
                "confidence": 1.0,
                "human_confirmed": True,
                "confidence_source": "teacher",
            }
            for item in normalized
        ]
    return normalized


def _build_template_paper_cache(template_session: TemplateSession) -> PaperCache:
    """Adapt a frozen teacher template to the shared answers-only OCR path."""
    try:
        template_mode = normalize_template_mode(
            template_session.template_mode,
            default="marked",
        )
        if not template_session.page_artifacts:
            if template_mode == "blank":
                raise ValueError("空白模板缺少视觉基准，请重新导入模板")
            if template_session.has_answers:
                # Compatibility for templates confirmed before per-template ROI
                # artifacts were introduced. New imports always create artifacts.
                return PaperCache.from_confirmed_template_pages(template_session.pages)
            raise ValueError("blank template has no visual evidence artifacts")
        artifacts = verify_template_artifacts(
            template_session.template_dir,
            template_session.page_artifacts,
        )
        for artifact in artifacts.values():
            _template_handwriting_regions_for_mode(
                template_mode,
                artifact.get("template_handwriting_regions"),
            )
        return PaperCache.from_confirmed_template_pages(
            template_session.pages,
            template_artifacts=artifacts,
            template_dir=template_session.template_dir,
            require_local_evidence=True,
            template_id=template_session.template_id,
        )
    except (TemplateEvidenceError, ValueError) as exc:
        raise HTTPException(400, detail=f"模板题干无效：{exc}") from exc


def _single_page_template_failure_questions(
    template_session: TemplateSession,
) -> list[dict]:
    """Build fail-closed review placeholders only when page identity is certain."""
    if len(template_session.pages) != 1:
        return []
    from services.template_slots import ocr_failed_placeholder

    answer_map = template_session.build_answer_map()
    questions: list[dict] = []
    for question in template_session.pages[0].get("questions", []):
        question_number = str(question.get("question_no") or "").strip()
        entry = answer_map.get(question_number, {})
        if not question_number or not entry:
            continue
        questions.append(ocr_failed_placeholder(
            question_number, entry.get("question_type"), entry.get("blank_count"),
        ))
    return questions
# ── The batch answer map ─────────────────────────────────────────────────
def _initialize_batch_answer_map(
    batch: QuickBatch,
    teacher_id: int,
    template_session: TemplateSession,
) -> None:
    """Derive the batch's answer map and question list from its frozen template."""
    batch.answer_map = template_session.build_answer_map()
    try:
        if (
            template_session.teacher_id == teacher_id
            and template_session.status in {"confirmed", "frozen"}
            and not template_session.recognition_contract_error()
        ):
            latest = _load_owned_template_session(
                template_session.template_id, teacher_id
            )
            if (
                latest.teacher_id == teacher_id
                and latest.status in {"confirmed", "frozen"}
                and latest.confirmed_contract_hash
                    == template_session.confirmed_contract_hash
                and not latest.recognition_contract_error()
            ):
                # OCR may outlive the background classification. Refresh only
                # derived metadata; keep this batch's frozen answers/pages.
                enriched = copy.copy(template_session)
                enriched.knowledge_classifications = copy.deepcopy(
                    latest.knowledge_classifications
                )
                batch.answer_map = enriched.build_answer_map()
    except Exception as exc:
        # An unavailable/newer template must not change the frozen authority.
        logger.warning(
            "模板派生知识点刷新暂缓 template=%s: %s",
            template_session.template_id,
            exc,
        )
    # The frozen template keeps answer geometry in page artifacts rather
    # than in the answer-text contract. Carry it into the final grading
    # map after any derived metadata refresh above.
    for artifact in (template_session.page_artifacts or {}).values():
        if not isinstance(artifact, dict):
            continue
        regions = artifact.get("template_answer_regions") or artifact.get("template_handwriting_regions") or []
        if not isinstance(regions, list):
            continue
        for region in regions:
            if not isinstance(region, dict):
                continue
            number = str(region.get("question_number") or region.get("question_no") or "").strip()
            boxes = normalize_review_boxes(region.get("regions"))
            if number and boxes and number in batch.answer_map:
                batch.answer_map[number]["answer_bboxes"] = [list(box) for box in boxes]
                batch.answer_map[number]["answer_box_source"] = "template"
    batch.questions = [
        {
            "question_no": qn,
            "question_text": entry.get("question_text", ""),
            "question_type": entry.get("question_type", "solve"),
        }
        for qn, entry in batch.answer_map.items()
    ]


def _confirmation_questions(batch: QuickBatch) -> list[dict]:
    """Return the frozen template questions in the unified AnswerSpec payload."""
    questions_out = []
    for question in batch.questions:
        question_no = question["question_no"]
        entry = batch.answer_map.get(question_no, {}) or {}
        questions_out.append({
            **AnswerSpec.model_validate({**entry, "question_no": question_no}).directory_metadata(),
            **({"drawing_reference": copy.deepcopy(entry["drawing_reference"])} if entry.get("drawing_reference") else {}),
            "question_no": question_no,
            "content": question["question_text"],
            "question_type": entry.get("question_type") or question.get("question_type") or "solve",
            "answer": entry.get("standard_answer") or "",
            "answer_variants": entry.get("answer_variants", []) or [],
            "solution_steps": entry.get("solution_steps", []) or [],
            "max_score": entry.get("max_score"),
            "blank_count": entry.get("blank_count", 1),
            "answer_slots": entry.get("answer_slots", []) or [],
            "fill_scoring_mode": entry.get("fill_scoring_mode", "equal") or "equal",
            "blank_scores": entry.get("blank_scores", []) or [],
            "subquestion_count": entry.get("subquestion_count", 1),
            "subquestion_scores": entry.get("subquestion_scores", []) or [],
            "grading_method": entry.get("grading_method", "") or "",
            "chapter": entry.get("chapter") or "",
            "topic": entry.get("topic") or "",
            "matched": bool(entry.get("matched")),
            "source": entry.get("source") or "bank",
        })
    return questions_out


def _mark_unverified_answer_alignment(paper: dict) -> dict:
    """Flag an answers-only page whose unreadable answers cannot be re-aligned.

    Another full-page Vision pass is no evidence of alignment: a model can
    solve the same question consistently and still replace a blank or wrong
    student answer. The original transcription is therefore kept and only
    marked; the ``ocr_alignment_retry`` key is part of stored OCR results.
    """
    if not paper.get("ocr_answers_only") or paper.get("ocr_alignment_verified"):
        return paper

    has_unreadable = any(
        str(question.get("answer_state") or "").strip().lower() == "unreadable"
        or str(question.get("student_answer") or "").strip() == "[无法识别]"
        for question in paper.get("questions", [])
    )
    if not has_unreadable:
        return paper

    unresolved = dict(paper)
    unresolved["ocr_alignment_retry"] = "skipped_no_spatial_evidence"
    return unresolved


def _reject_answer_key_matching_local_recoveries(
    paper: dict,
    answer_map: dict,
) -> dict:
    """Do not auto-grade a local OCR recovery that matches the answer key."""
    changed = False
    questions = []
    normalized_answer_map = {
        str(key).strip(): value
        for key, value in (answer_map or {}).items()
    }
    for question in paper.get("questions", []):
        current = dict(question)
        question_no = str(current.get("question_no") or "").strip()
        if current.pop("_ocr_local_recovery", False):
            from services.grading_contract import normalize_choice_answer

            recovered = normalize_choice_answer(current.get("student_answer"))
            standard = normalize_choice_answer(
                (normalized_answer_map.get(question_no) or {}).get("standard_answer")
            )
            if recovered and standard and recovered == standard:
                current["student_answer"] = "[无法识别]"
                current["answer_state"] = "unreadable"
                changed = True
        questions.append(current)
    if not changed and questions == paper.get("questions", []):
        return paper
    result = dict(paper)
    result["questions"] = questions
    if changed:
        result["ocr_local_recovery"] = "answer_key_match_rejected"
    return result


def _authoritative_answers_for_confirmation(
    batch: QuickBatch,
    category: str,
) -> list[AnswerSpec]:
    """Build the grading answers from the batch's frozen template snapshot."""
    if batch.source_mode != "template":
        raise HTTPException(409, detail="该批次没有冻结模板，不能批阅；请重新上传试卷")
    answer_map = batch.answer_map if isinstance(batch.answer_map, dict) else {}
    answers: list[AnswerSpec] = []
    seen: set[str] = set()
    try:
        for question in batch.questions:
            question_no = str(question.get("question_no") or "").strip()
            entry = answer_map.get(question_no)
            if not question_no or not isinstance(entry, dict):
                raise ValueError("冻结模板中存在空题号或缺失题目")
            if question_no in seen:
                raise ValueError("冻结模板中存在重复题号")
            seen.add(question_no)
            answers.append(finalize_answer_spec_for_category({
                **entry,
                "question_no": question_no,
            }, category))
    except ValueError as exc:
        raise HTTPException(409, detail=f"冻结模板答案合同无效：{exc}") from exc
    if not answers:
        raise HTTPException(409, detail="冻结模板没有可批阅题目")
    return answers
# ── The grading contract ─────────────────────────────────────────────────
_GRADING_CONTRACT_VERSION = 1


def _grading_question_sort_key(value: str) -> tuple:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.split(r"(\d+)", str(value or ""))
        if token
    )


def _frozen_answer_payload(item: AnswerSpec) -> dict:
    """Keep only normalized teacher inputs that affect grading or scoring."""
    return {
        **item.directory_metadata(),
        **({"drawing_reference": copy.deepcopy(item.drawing_reference)} if item.drawing_reference is not None else {}),
        "question_no": item.question_no,
        "question_text": item.question_text,
        "question_type": item.question_type,
        "choice_mode": item.choice_mode,
        "standard_answer": item.standard_answer,
        "answer_variants": sorted(item.answer_variants),
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
    }


def _build_grading_contract(
    *,
    mode: str,
    category: str,
    exam_name: str,
    confirmed: list[AnswerSpec],
    identity_plan: dict[str, dict],
    teaching_context: dict | None = None,
) -> dict:
    confirmed_payload = sorted(
        (_frozen_answer_payload(item) for item in confirmed),
        key=lambda item: _grading_question_sort_key(item["question_no"]),
    )
    frozen_identities = {
        paper_id: {
            "action": str(identity_plan[paper_id].get("action") or ""),
            "student_id": identity_plan[paper_id].get("student_id"),
            "name": str(identity_plan[paper_id].get("name") or ""),
            "student_no": str(identity_plan[paper_id].get("student_no") or ""),
        }
        for paper_id in sorted(identity_plan)
    }
    snapshot = validate_teaching_context(teaching_context)
    return {
        **({"teaching_context": snapshot} if snapshot else {}),
        "version": _GRADING_CONTRACT_VERSION,
        "mode": mode,
        "category": category,
        "exam_name": str(exam_name or "").strip(),
        "confirmed": confirmed_payload,
        "identity_plan": frozen_identities,
    }


def _grading_contract_hash(contract: dict) -> str:
    """Hash semantic grading inputs while ignoring mutable roster labels."""
    identities = []
    for paper_id in sorted(contract.get("identity_plan") or {}):
        assignment = contract["identity_plan"][paper_id]
        identity = {
            "paper_id": paper_id,
            "action": str(assignment.get("action") or ""),
        }
        if identity["action"] == "existing":
            identity["student_id"] = int(assignment.get("student_id") or 0)
        else:
            identity["name"] = str(assignment.get("name") or "").strip()
            identity["student_no"] = str(assignment.get("student_no") or "").strip()
        identities.append(identity)
    payload = {
        "version": int(contract.get("version") or _GRADING_CONTRACT_VERSION),
        "mode": str(contract.get("mode") or ""),
        "category": str(contract.get("category") or ""),
        "exam_name": str(contract.get("exam_name") or "").strip(),
        "confirmed": sorted(
            list(contract.get("confirmed") or []),
            key=lambda item: _grading_question_sort_key(item.get("question_no", "")),
        ),
        "identities": identities,
    }
    if contract.get("teaching_context"):
        payload["teaching_context"] = validate_teaching_context(contract["teaching_context"])
    encoded = _json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
