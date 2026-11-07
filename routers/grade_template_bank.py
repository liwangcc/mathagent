"""Copy a confirmed template into the teacher's private question bank, once.

POST /batch-grade/template/{template_id}/bank-import
(``batch_grade_template_bank_import``) first lets in-flight knowledge labels
land, within ``TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S``; ``_template_bank_import``
then adds or updates one bank row per answered question and records the
import on the session.  A repeated call reports the earlier import.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import QuestionBank
from models_user import User
from routers.auth import _require_user
from routers.grade_common import (
    _load_owned_template_session,
    _locked_owned_template_session,
    _template_knowledge_classifier,
)
from services import template_knowledge
from services.grading_contract import AnswerSpec
from services.teaching_context import record_teaching_fields, use_teaching_context

# routers.grade_template's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade_template")
router = APIRouter()


# Bounded so the client's 60 s request timeout is never at risk.
TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S = 25.0


@router.post("/batch-grade/template/{template_id}/bank-import")
async def batch_grade_template_bank_import(
    template_id: str,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Persist a confirmed template to the private bank exactly once."""
    sess = await asyncio.to_thread(_load_owned_template_session, template_id, user.id)
    if sess.status in ("confirmed", "frozen") and sess.pages and not sess.bank_imported_at:
        # Saving the template already started its Flash classification. Let
        # in-flight labels land so the bank reuses them instead of queueing
        # the same text again; past the bound the bank worker takes over.
        try:
            await template_knowledge.ensure_session_knowledge(
                sess, classifier=_template_knowledge_classifier,
                timeout=TEMPLATE_BANK_IMPORT_KNOWLEDGE_WAIT_S,
            )
        except Exception as exc:
            logger.warning("模板入库前知识点分类暂缓 template=%s: %s", template_id, type(exc).__name__)
    return await asyncio.to_thread(_template_bank_import, template_id, user, db)


def _template_bank_import(template_id: str, user: User, db: Session):
    from services.answer_import import find_fuzzy_match

    sess = _load_owned_template_session(template_id, user.id)
    if sess.status not in ("confirmed", "frozen") or not sess.pages:
        raise HTTPException(409, detail="请先保存本次模板，再执行入库")
    current_hash = sess.content_hash()
    if sess.bank_imported_at:
        return {
            "success": True,
            "template_id": template_id,
            "added_to_bank": 0,
            "already_imported": True,
            "imported_count": sess.bank_imported_count,
            "template_changed_after_import": sess.bank_import_hash != current_hash,
        }

    added_to_bank = 0
    processed = 0
    try:
        effective_map = sess.build_answer_map()
        for page in sess.pages:
            for raw_question in page.get("questions", []):
                question_no = str(raw_question.get("question_no") or "").strip()
                effective = dict(raw_question)
                classified = effective_map.get(question_no) or {}
                if classified.get("chapter") and classified.get("topic"):
                    effective["chapter"] = classified["chapter"]
                    effective["topic"] = classified["topic"]
                spec = AnswerSpec.model_validate(effective)
                content = spec.question_text
                answer = spec.standard_answer
                if not content or not answer:
                    continue
                processed += 1
                with use_teaching_context(sess.teaching_context):
                    dup = find_fuzzy_match(db, content, user.id)
                if dup is not None:
                    dup.question = content
                    dup.answer = answer
                    dup.grading_method = spec.grading_method
                    dup.set_variants(spec.answer_variants)
                    dup.set_solution_steps(spec.solution_steps)
                    if spec.chapter:
                        dup.chapter = spec.chapter
                    if spec.topic:
                        dup.topic = spec.topic
                else:
                    qb = QuestionBank(
                        **record_teaching_fields(sess.teaching_context, chapter=spec.chapter, topic=spec.topic),
                        teacher_id=user.id,
                        is_public=False,
                        question=content,
                        answer=answer,
                        chapter=spec.chapter,
                        topic=spec.topic,
                        grading_method=spec.grading_method,
                        source="template_confirmed",
                    )
                    qb.set_variants(spec.answer_variants)
                    qb.set_solution_steps(spec.solution_steps)
                    db.add(qb)
                    added_to_bank += 1
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.error("模板入库失败 template=%s: %s", template_id, exc, exc_info=True)
        raise HTTPException(500, detail="模板入库失败，请稍后重试") from exc

    # Do not let this endpoint's pre-import snapshot restore confirmed after a
    # concurrent recognition request has frozen the template.
    with _locked_owned_template_session(template_id, user.id) as latest_sess:
        latest_sess.bank_imported_at = datetime.now(timezone.utc).isoformat()
        latest_sess.bank_import_hash = current_hash
        latest_sess.bank_imported_count = processed
        latest_sess.save_meta()
    return {
        "success": True,
        "template_id": template_id,
        "added_to_bank": added_to_bank,
        "already_imported": False,
        "imported_count": processed,
        "template_changed_after_import": False,
    }
