"""Which historical wrong questions a template may correct.

- POST /batch-grade/template/correction-candidates
  (``batch_grade_template_correction_candidates``) shows the teacher the
  current class's pending originals that match a template question by
  identity, knowledge point or similarity.
- Confirming the template saves the pairs the teacher saw and kept, in one
  transaction (``_save_template_correction_authorizations``):
  ``_requested_correction_relations`` re-checks them, ``_authorize_corrections``
  writes the authorizations and correction relations (``_AUTHORIZED_RELATIONS``)
  and ``_revoke_dropped_authorizations`` withdraws the ones no longer kept.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from routers.grade_common import _load_owned_template_session
from routers.grade_template_models import (
    TemplateConfirmReq,
    TemplateCorrectionCandidatesReq,
)
from services.template_session import TemplateSession

router = APIRouter()


@router.post("/batch-grade/template/correction-candidates")
def batch_grade_template_correction_candidates(
    req: TemplateCorrectionCandidatesReq,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Show current-class pending originals by identity, knowledge point, or similarity."""
    sess = _load_owned_template_session(req.template_id, user.id)
    from services.correction_tracking import template_candidates

    questions = [question.model_dump() for page in req.pages for question in page.questions]
    return {
        "template_id": req.template_id,
        "candidates": template_candidates(
            db,
            teacher_id=user.id,
            class_id=getattr(user, "current_class_id", None),
            questions=questions,
            teaching_context=sess.teaching_context,
        ),
    }


def _requested_correction_relations(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
    class_id,
) -> list[tuple]:
    """(question, candidate) pairs the teacher saw, kept, and that still qualify."""
    from services.correction_tracking import template_candidates

    submitted_questions = [
        question
        for page in req.pages
        for question in page.questions
    ]
    shown_cycle_ids = {
        int(value)
        for question in submitted_questions
        if not question.correction_excluded
        for value in question.authorized_wrong_cycle_ids or []
    }
    if not shown_cycle_ids:
        return []
    # Re-check only the records the teacher saw, not the whole class history.
    discovered = template_candidates(
        db,
        teacher_id=user.id,
        class_id=class_id,
        questions=[question.model_dump() for question in submitted_questions],
        limit=5000,
        teaching_context=sess.teaching_context,
        cycle_ids=shown_cycle_ids,
    )
    requested_relations = []
    for question, discovery in zip(submitted_questions, discovered):
        if question.correction_excluded:
            continue
        displayed_cycle_ids = {
            int(value) for value in question.authorized_wrong_cycle_ids or []
        }
        # Save approves only the relations the teacher actually saw.
        # New discoveries and expired candidates must not require a
        # second confirmation or block the template itself.
        requested_relations.extend(
            (question, candidate)
            for candidate in discovery.get("candidates", [])
            if int(candidate["cycle_id"]) in displayed_cycle_ids
        )
    return requested_relations


# Discovery relation -> stored relation type and the note shown in history.
_AUTHORIZED_RELATIONS = {
    "same_original": ("teacher_authorized_same_original", "教师确认当前题与历史原题一致"),
    "similar_candidate": ("teacher_authorized_similar", "教师确认当前题可检验相似历史错题"),
}


def _authorize_corrections(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
    requested: list[tuple],
    class_id,
) -> set[tuple]:
    """Let the kept template questions correct their pending wrong-question cycles.

    Returns the (source fingerprint, cycle id) keys now authorized.
    """
    from models_correction import (
        QuestionCorrectionRelation,
        TemplateQuestionAuthorization,
        WrongQuestionCycle,
    )
    from services.correction_tracking import question_fingerprint

    if not requested:
        return set()
    fingerprints = {
        id(question): question_fingerprint({
            **question.model_dump(), "teaching_context": sess.teaching_context,
        })
        for question, _candidate in requested
    }
    cycle_ids = sorted({int(candidate["cycle_id"]) for _question, candidate in requested})
    targets = {
        cycle.id: cycle
        for start in range(0, len(cycle_ids), 500)
        for cycle in db.query(WrongQuestionCycle).filter(
            WrongQuestionCycle.id.in_(cycle_ids[start:start + 500]),
            WrongQuestionCycle.teacher_id == user.id,
            WrongQuestionCycle.class_id == class_id,
            WrongQuestionCycle.status == "pending",
        )
    }
    authorizations = {
        (row.source_question_fingerprint, row.target_cycle_id): row
        for row in db.query(TemplateQuestionAuthorization).filter(
            TemplateQuestionAuthorization.teacher_id == user.id,
            TemplateQuestionAuthorization.template_id == req.template_id,
        )
    }
    links = {
        (row.source_question_fingerprint, row.target_cycle_id): row
        for row in db.query(QuestionCorrectionRelation).filter(
            QuestionCorrectionRelation.teacher_id == user.id,
            QuestionCorrectionRelation.source_question_fingerprint.in_(set(fingerprints.values())),
        )
    }
    kept: set[tuple] = set()
    for question, candidate in requested:
        fingerprint = fingerprints[id(question)]
        cycle_id = int(candidate["cycle_id"])
        target = targets.get(cycle_id)
        if target is None:
            raise HTTPException(400, detail=f"待纠正题目 {cycle_id} 无效或已关闭")
        key = (fingerprint, target.id)
        kept.add(key)
        authorization = authorizations.get(key)
        if authorization is None:
            authorizations[key] = TemplateQuestionAuthorization(
                teacher_id=user.id,
                template_id=req.template_id,
                target_cycle_id=target.id,
                source_question_fingerprint=fingerprint,
                authorized_by=user.id,
            )
            db.add(authorizations[key])
        else:
            authorization.revoked_at = None
        relation_type, difference_summary = _AUTHORIZED_RELATIONS[candidate["relation"]]
        linked = links.get(key)
        if linked is None:
            links[key] = QuestionCorrectionRelation(
                teacher_id=user.id,
                target_cycle_id=target.id,
                source_question_identity=f"template:{req.template_id}:{question.question_no}",
                source_question_fingerprint=fingerprint,
                relation_type=relation_type,
                similarity_score=candidate["similarity_score"],
                difference_summary=difference_summary,
                authorized_by=user.id,
            )
            db.add(links[key])
        else:
            linked.relation_type = relation_type
            linked.similarity_score = candidate["similarity_score"]
            linked.difference_summary = difference_summary
            linked.revoked_at = None
    return kept


def _revoke_dropped_authorizations(
    db,
    user: User,
    template_id: str,
    kept_keys: set[tuple],
) -> None:
    """Revoke this template's authorizations the teacher no longer keeps.

    The correction relation goes too, unless another template still
    authorizes the same question for the same cycle.
    """
    from models_correction import QuestionCorrectionRelation, TemplateQuestionAuthorization

    existing_authorizations = db.query(TemplateQuestionAuthorization).filter(
        TemplateQuestionAuthorization.teacher_id == user.id,
        TemplateQuestionAuthorization.template_id == template_id,
        TemplateQuestionAuthorization.revoked_at.is_(None),
    ).all()
    for authorization in existing_authorizations:
        key = (authorization.source_question_fingerprint, authorization.target_cycle_id)
        if key in kept_keys:
            continue
        authorization.revoked_at = datetime.now()
        other_active_authorization = db.query(TemplateQuestionAuthorization.id).filter(
            TemplateQuestionAuthorization.teacher_id == user.id,
            TemplateQuestionAuthorization.template_id != template_id,
            TemplateQuestionAuthorization.source_question_fingerprint == authorization.source_question_fingerprint,
            TemplateQuestionAuthorization.target_cycle_id == authorization.target_cycle_id,
            TemplateQuestionAuthorization.revoked_at.is_(None),
        ).first()
        if other_active_authorization is None:
            linked = db.query(QuestionCorrectionRelation).filter(
                QuestionCorrectionRelation.teacher_id == user.id,
                QuestionCorrectionRelation.source_question_fingerprint == authorization.source_question_fingerprint,
                QuestionCorrectionRelation.target_cycle_id == authorization.target_cycle_id,
                QuestionCorrectionRelation.revoked_at.is_(None),
            ).first()
            if linked is not None:
                linked.revoked_at = datetime.now()


def _save_template_correction_authorizations(
    db,
    user: User,
    req: TemplateConfirmReq,
    sess: TemplateSession,
) -> None:
    """Record which historical wrong questions this template may correct.

    Any rejection rolls the whole transaction back.
    """
    from models_correction import TemplateQuestionAuthorization
    from sqlalchemy import inspect as sqlalchemy_inspect

    owns_db_session = not hasattr(db, "query")
    if owns_db_session:
        from database import SessionLocal
        db = SessionLocal()

    try:
        current_class_id = getattr(user, "current_class_id", None)
        requested_relations = _requested_correction_relations(
            db, user, req, sess, current_class_id,
        )
        authorization_table_exists = sqlalchemy_inspect(db.get_bind()).has_table(
            TemplateQuestionAuthorization.__tablename__
        )
        if requested_relations and not authorization_table_exists:
            raise HTTPException(503, detail="纠错授权表尚未初始化，请重启服务后重试")
        if requested_relations and not current_class_id:
            raise HTTPException(400, detail="请先选择当前班级，再确认历史错题纠错关系")
        kept_keys = _authorize_corrections(
            db, user, req, sess, requested_relations, current_class_id,
        )
        if authorization_table_exists:
            _revoke_dropped_authorizations(db, user, req.template_id, kept_keys)
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if owns_db_session:
            db.close()
