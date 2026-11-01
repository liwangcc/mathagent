"""Structured exercise generation and editable Word export endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Student, StudentProfile, Weakness
from models_correction import (
    QuestionAttemptEvent,
    StudentObservationPeriod,
    WrongQuestionCycle,
)
from services.correction_tracking import attempt_teaching_context, pending_cycles, question_knowledge
from models_user import User
from routers.auth import _require_user
from services.exercise_bank_selector import (
    QuestionBankShortage,
    select_exercises_from_bank,
)
from services.exercise_schema import (
    ExercisePaper,
    ExerciseTypeCounts,
    MAX_EXERCISE_QUESTIONS,
)
from services.practice import load_math_catalog, filter_teaching_stage
from services.teaching_context import (
    current_teaching_context,
    make_teaching_context,
    require_teacher_stage,
    stage_label,
    use_teaching_context,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ExerciseGenerateRequest(BaseModel):
    mode: Literal["bank", "personal", "class", "correction"] = "bank"
    question_count: int = Field(10, ge=1, le=MAX_EXERCISE_QUESTIONS)
    type_counts: ExerciseTypeCounts | None = None
    chapters: list[str] = Field(default_factory=list, max_length=64)
    topics: list[str] = Field(default_factory=list, max_length=200)
    include_extension: bool = False
    student_id: int | None = Field(None, ge=1)
    class_id: int | None = Field(None, ge=1)
    class_name: str | None = Field(None, max_length=50)
    correction_scope: Literal["personal", "class"] = "personal"

    def resolved_counts(self) -> ExerciseTypeCounts:
        return self.type_counts or ExerciseTypeCounts.from_total(self.question_count)


class ExerciseExportRequest(ExercisePaper):
    layout: Literal["a3_landscape_2col"] = "a3_landscape_2col"


def _load_chapter_reference() -> list[dict]:
    return load_math_catalog()


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _matches_task_stage(record, db: Session | None = None) -> bool:
    """Do not reuse unlabelled or another stage's history in a new paper."""
    stage = current_teaching_context().get("education_stage")
    if not stage:
        return True
    if record is None:
        return False
    record_stage = getattr(record, "education_stage", None)
    if record_stage:
        return record_stage == stage
    snapshot_reader = getattr(record, "question_snapshot", None)
    if callable(snapshot_reader) and db is not None:
        return attempt_teaching_context(db, record).get("education_stage") == stage
    snapshot = snapshot_reader() if callable(snapshot_reader) else {}
    context = snapshot.get("teaching_context") if isinstance(snapshot, dict) else None
    return isinstance(context, dict) and context.get("education_stage") == stage


def _cycles_for_task_stage(db: Session, cycles: list) -> list:
    if not current_teaching_context():
        return cycles
    return [cycle for cycle in cycles if _matches_task_stage(db.get(QuestionAttemptEvent, cycle.opened_attempt_id), db)]


def _match_chapters_topics(
    chapters: list[str],
    topics: list[str],
) -> tuple[list[str], list[str]]:
    reference = _load_chapter_reference()
    matched_chapters: list[str] = []
    matched_topics: list[str] = []

    for requested in chapters:
        for chapter in reference:
            name = str(chapter.get("chapter", ""))
            if requested in name or name in requested:
                _append_unique(matched_chapters, name)
                break

    for requested in topics:
        for chapter in reference:
            for topic in chapter.get("topics", []):
                topic_name = str(topic)
                if requested in topic_name or topic_name in requested:
                    _append_unique(matched_topics, topic_name)
                    _append_unique(matched_chapters, str(chapter.get("chapter", "")))
                    break

    # Custom/private-bank chapter names may not exist in the bundled reference.
    for requested in chapters:
        _append_unique(matched_chapters, requested.strip())
    for requested in topics:
        _append_unique(matched_topics, requested.strip())
    return matched_chapters, matched_topics


def _scope_title(values: list[str], fallback: str) -> str:
    if not values:
        return fallback
    label = "、".join(values[:3])
    if len(values) > 3:
        label += f"等{len(values)}个范围"
    return f"{label}练习"


def _bank_selection_scope(
    chapters: list[str],
    topics: list[str],
) -> tuple[str, list[str], list[tuple[str, int]]]:
    matched_chapters, matched_topics = _match_chapters_topics(chapters, topics)
    scope_terms: list[str] = []
    for value in [*matched_chapters, *matched_topics]:
        _append_unique(scope_terms, value)
    title_values = matched_chapters or matched_topics
    return (
        _scope_title(title_values, f"{stage_label()}数学综合练习"),
        scope_terms,
        [(term, 1) for term in scope_terms],
    )


def _personal_selection_scope(
    db: Session,
    user_id: int,
    student_id: int | None,
    chapters: list[str],
    topics: list[str],
) -> tuple[str, list[list[str]], list[tuple[str, int]]]:
    if not student_id:
        raise HTTPException(400, detail="个人模式需要 student_id")
    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user_id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生不存在")

    weights: Counter[str] = Counter()
    cycles = _cycles_for_task_stage(db, pending_cycles(db, student_id))
    # Only this stage's cycles can suppress its pre-cycle archive fallback.
    has_cycle_history = any(
        _matches_task_stage(attempt, db)
        for _cycle, attempt in db.query(WrongQuestionCycle, QuestionAttemptEvent).outerjoin(
            QuestionAttemptEvent,
            QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
        ).filter(
            WrongQuestionCycle.student_id == student_id,
        )
    )
    for cycle in cycles:
        attempt = db.get(QuestionAttemptEvent, cycle.opened_attempt_id)
        knowledge = question_knowledge(db, attempt)
        term = str(knowledge.get("topic") or "").strip()
        if term and term not in {"未标注", "综合", "未知"}:
            weights[term] += max(1, int(cycle.wrong_count or 1))

    # Compatibility for pre-cycle archives. New grading always uses pending
    # WrongQuestionCycle rows, so corrected history cannot leak into this path.
    if not has_cycle_history:
        profiles = (
            filter_teaching_stage(db.query(StudentProfile), StudentProfile)
            .filter(
                StudentProfile.student_id == student_id,
                StudentProfile.analytics_included.is_(True),
            )
            .order_by(StudentProfile.created_at.desc())
            .limit(20)
            .all()
        )
        for profile in profiles:
            if not _matches_task_stage(profile):
                continue
            for detail in profile.get_details():
                if detail.get("result") not in ("partial", "wrong", "blank"):
                    continue
                term = str(
                    detail.get("topic") or detail.get("knowledge_point") or ""
                ).strip()
                if term and term not in {"未标注", "综合", "未知"}:
                    weights[term] += 1
    if not has_cycle_history and not weights:
        raise HTTPException(409, detail="该学生暂无错题记录")
    if has_cycle_history and not cycles:
        raise HTTPException(409, detail="该学生当前没有待纠正错题")
    if not weights:
        raise HTTPException(409, detail="该学生错题缺少知识点标签，无法从题库精确抽题")
    priorities = [(term, int(total)) for term, total in weights.most_common(20)]
    selected_chapters, selected_topics = _match_chapters_topics(chapters, topics)
    scope_groups = [[term for term, _ in priorities]]
    if selected_chapters:
        scope_groups.append(selected_chapters)
    if selected_topics:
        scope_groups.append(selected_topics)
    return f"{student.name}个人薄弱巩固", scope_groups, priorities


def _correction_question(db: Session, cycle: WrongQuestionCycle) -> dict | None:
    attempt = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.id == cycle.opened_attempt_id,
    ).first()
    if attempt is None:
        return None
    snapshot = attempt.question_snapshot()
    knowledge = question_knowledge(db, attempt)
    qtype = snapshot.get("question_type") or "solve"
    standard_answer = snapshot.get("standard_answer") or ""
    solution_steps = snapshot.get("solution_steps") or []
    return {
        "type": "solution" if qtype == "solve" else qtype,
        "question": snapshot.get("question_text") or "",
        "options": snapshot.get("options") or {},
        "answer": standard_answer,
        "solution": "\n".join(
            str(item) for item in solution_steps if str(item).strip()
        ) or standard_answer,
        "knowledge_point": knowledge.get("topic") or "原题订正",
        "difficulty": int(knowledge.get("difficulty") or 2),
        "source_wrong_cycle_id": cycle.id,
        "source_question_identity": cycle.question_identity,
    }


def _correction_paper(
    db: Session,
    user_id: int,
    *,
    scope: Literal["personal", "class"],
    question_count: int,
    student_id: int | None = None,
    class_id: int | None = None,
    class_name: str | None = None,
) -> tuple[str, list[dict], int]:
    if scope == "personal":
        if not student_id:
            raise HTTPException(400, detail="个人原题订正需要 student_id")
        student = db.query(Student).filter(
            Student.id == student_id,
            Student.teacher_id == user_id,
        ).first()
        if not student:
            raise HTTPException(404, detail="学生不存在")
        cycles = sorted(
            _cycles_for_task_stage(db, pending_cycles(db, student.id)),
            key=lambda row: (
                -int(row.wrong_count or 0),
                row.opened_at,
                row.id,
            ),
        )
        available = len(cycles)
        exercises = []
        for cycle in cycles[:question_count]:
            item = _correction_question(db, cycle)
            if item is not None:
                exercises.append(item)
        title = f"{student.name}原题订正卷"
    else:
        class_name = (class_name or "").strip()
        if not class_id and not class_name:
            raise HTTPException(400, detail="班级原题订正需要 class_id 或 class_name")
        student_query = db.query(Student).filter(Student.teacher_id == user_id)
        if class_id:
            student_query = student_query.filter(Student.class_id == class_id)
        else:
            student_query = student_query.filter(Student.class_name == class_name)
        students = student_query.all()
        student_ids = [student.id for student in students]
        if not student_ids:
            raise HTTPException(409, detail="该班级暂无学生")
        observed_ids = {
            row.student_id
            for row in db.query(StudentObservationPeriod).filter(
                StudentObservationPeriod.student_id.in_(student_ids),
                StudentObservationPeriod.ended_at.is_(None),
            ).all()
        }
        effective_ids = [value for value in student_ids if value not in observed_ids]
        if not effective_ids:
            raise HTTPException(409, detail="该班级当前没有纳入统计的学生")
        cycles = db.query(WrongQuestionCycle).filter(
            WrongQuestionCycle.teacher_id == user_id,
            WrongQuestionCycle.student_id.in_(effective_ids),
            WrongQuestionCycle.status == "pending",
        ).all()
        cycles = _cycles_for_task_stage(db, cycles)
        grouped: dict[str, list[WrongQuestionCycle]] = {}
        for cycle in cycles:
            grouped.setdefault(cycle.question_identity, []).append(cycle)
        ranked = sorted(
            grouped.values(),
            key=lambda rows: (
                -len({row.student_id for row in rows}),
                -sum(int(row.wrong_count or 0) for row in rows),
                min(row.opened_at for row in rows),
                rows[0].question_identity,
            ),
        )
        available = len(ranked)
        exercises = []
        for rows in ranked[:question_count]:
            item = _correction_question(db, rows[0])
            if item is None:
                continue
            item["source_wrong_cycles"] = [
                {"student_id": row.student_id, "cycle_id": row.id}
                for row in sorted(rows, key=lambda row: (row.student_id, row.id))
            ]
            item["wrong_student_count"] = len({row.student_id for row in rows})
            item["wrong_event_count"] = sum(int(row.wrong_count or 0) for row in rows)
            exercises.append(item)
        title = f"{class_name or '当前班级'}高频原题订正卷"
    if not exercises:
        raise HTTPException(409, detail="当前没有可用的待订正原题")
    return title, exercises, available


def _class_selection_scope(
    db: Session,
    user_id: int,
    class_id: int | None,
    class_name: str | None,
    chapters: list[str],
    topics: list[str],
) -> tuple[str, list[list[str]], list[tuple[str, int]]]:
    class_name = (class_name or "").strip()
    if not class_id and not class_name:
        raise HTTPException(400, detail="班级模式需要 class_id 或 class_name")

    student_query = db.query(Student).filter(Student.teacher_id == user_id)
    if class_id:
        student_query = student_query.filter(Student.class_id == class_id)
    else:
        student_query = student_query.filter(Student.class_name == class_name)
    students = student_query.all()
    student_ids = [student.id for student in students]
    if not student_ids:
        raise HTTPException(409, detail="该班级暂无学生")

    observed_ids = {
        row.student_id
        for row in db.query(StudentObservationPeriod).filter(
            StudentObservationPeriod.student_id.in_(student_ids),
            StudentObservationPeriod.ended_at.is_(None),
        ).all()
    }
    effective_ids = [student_id for student_id in student_ids if student_id not in observed_ids]
    if not effective_ids:
        raise HTTPException(409, detail="该班级当前没有纳入统计的学生")
    cycle_rows = db.query(WrongQuestionCycle, QuestionAttemptEvent).join(
        QuestionAttemptEvent,
        QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
    ).filter(
        WrongQuestionCycle.teacher_id == user_id,
        WrongQuestionCycle.student_id.in_(effective_ids),
        WrongQuestionCycle.status == "pending",
    ).all()
    has_cycle_history = any(
        _matches_task_stage(attempt, db)
        for _cycle, attempt in db.query(WrongQuestionCycle, QuestionAttemptEvent).outerjoin(
            QuestionAttemptEvent,
            QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
        ).filter(
            WrongQuestionCycle.teacher_id == user_id,
            WrongQuestionCycle.student_id.in_(effective_ids),
        )
    )
    topic_students: dict[str, set[int]] = {}
    topic_totals: Counter[str] = Counter()
    for cycle, attempt in cycle_rows:
        if not _matches_task_stage(attempt, db):
            continue
        term = str(question_knowledge(db, attempt).get("topic") or "").strip()
        if not term or term in {"未标注", "综合", "未知"}:
            continue
        topic_students.setdefault(term, set()).add(cycle.student_id)
        topic_totals[term] += max(1, int(cycle.wrong_count or 1))
    top_weaknesses = sorted(
        (
            (term, len(student_ids_for_topic), topic_totals[term])
            for term, student_ids_for_topic in topic_students.items()
        ),
        key=lambda item: (-item[1], -item[2], item[0]),
    )[:8]
    if not has_cycle_history:
        top_weaknesses = (
            filter_teaching_stage(db.query(
                Weakness.topic,
                func.count(func.distinct(Weakness.student_id)).label("student_count"),
                func.sum(Weakness.occurrences).label("total"),
            ), Weakness)
            .filter(
                Weakness.student_id.in_(effective_ids),
                Weakness.status == "pending",
            )
            .group_by(Weakness.topic)
            .order_by(
                func.count(func.distinct(Weakness.student_id)).desc(),
                func.sum(Weakness.occurrences).desc(),
                Weakness.topic.asc(),
            )
            .limit(8)
            .all()
        )
    if not top_weaknesses:
        raise HTTPException(409, detail="该班级暂无错题数据")

    priorities: list[tuple[str, int]] = []
    for topic, student_count, total in top_weaknesses:
        term = str(topic or "").strip()
        if term and term not in {"未标注", "综合", "未知"}:
            priorities.append((
                term,
                max(1, int(student_count or 1)) * 1_000_000 + max(1, int(total or 1)),
            ))
    if not priorities:
        raise HTTPException(409, detail="该班级错题缺少知识点标签，无法从题库精确抽题")
    selected_chapters, selected_topics = _match_chapters_topics(chapters, topics)
    scope_groups = [[term for term, _ in priorities]]
    if selected_chapters:
        scope_groups.append(selected_chapters)
    if selected_topics:
        scope_groups.append(selected_topics)
    return f"{class_name}班级共性强化", scope_groups, priorities


@router.post("/exercises/generate")
async def generate_exercises(
    data: ExerciseGenerateRequest = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    context = make_teaching_context(require_teacher_stage(user))
    with use_teaching_context(context):
        result = await _generate_exercises(data, db, user)
    result["teaching_context"] = context
    return result


async def _generate_exercises(data: ExerciseGenerateRequest, db: Session, user: User):
    """Select a validated choice/fill/solution paper from the question bank."""
    counts = data.resolved_counts()

    if data.mode == "correction":
        title, exercises, available = _correction_paper(
            db,
            user.id,
            scope=data.correction_scope,
            question_count=data.question_count,
            student_id=data.student_id,
            class_id=data.class_id,
            class_name=data.class_name,
        )
        return {
            "title": title,
            "source": "pending_original_questions",
            "type_counts": {
                "choice": sum(1 for item in exercises if item["type"] == "choice"),
                "fill": sum(1 for item in exercises if item["type"] == "fill"),
                "solution": sum(1 for item in exercises if item["type"] == "solution"),
            },
            "requested_count": data.question_count,
            "available_count": available,
            "selected_count": len(exercises),
            "shortage_message": (
                f"请求 {data.question_count} 道，当前共有 {available} 道待订正原题，"
                f"已生成 {len(exercises)} 道"
                if len(exercises) < data.question_count else ""
            ),
            "exercises": exercises,
        }

    if data.mode == "personal":
        title, scope_groups, priorities = _personal_selection_scope(
            db, user.id, data.student_id, data.chapters, data.topics
        )
    elif data.mode == "class":
        title, scope_groups, priorities = _class_selection_scope(
            db,
            user.id,
            data.class_id,
            data.class_name,
            data.chapters,
            data.topics,
        )
    else:
        title, scope_terms, priorities = _bank_selection_scope(
            data.chapters, data.topics
        )
        scope_groups = [scope_terms] if scope_terms else []

    try:
        selected = select_exercises_from_bank(
            db,
            user_id=user.id,
            counts=counts,
            scope_terms=scope_groups[0] if len(scope_groups) == 1 else (),
            scope_term_groups=scope_groups if len(scope_groups) > 1 else (),
            priority_terms=priorities,
            education_stage=current_teaching_context().get("education_stage"),
            include_extension=data.include_extension,
            explicit_topics=_match_chapters_topics([], data.topics)[1],
        )
    except QuestionBankShortage as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "Exercise bank selection failed mode=%s user_id=%s: %s",
            data.mode,
            user.id,
            exc,
            exc_info=True,
        )
        raise HTTPException(500, detail="题库抽题失败，请稍后重试") from exc

    exercises = []
    for item in selected:
        payload = item.question.model_dump(mode="json")
        payload["bank_question_id"] = item.row.id
        exercises.append(payload)

    return {
        "title": title,
        "source": "question_bank",
        "type_counts": counts.model_dump(),
        "exercises": exercises,
    }


@router.post("/exercise/export-docx")
async def exercise_export_docx(
    data: ExerciseExportRequest = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Export editable A3 landscape two-column student and answer DOCX files."""
    require_teacher_stage(user)
    try:
        from services.exercise_document import build_exercise_documents

        paper_bytes, answer_bytes = await asyncio.to_thread(
            build_exercise_documents,
            data.title,
            data.questions,
            teaching_context=data.teaching_context,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Exercise DOCX generation failed user_id=%s", user.id)
        raise HTTPException(500, detail="Word 试卷生成失败") from exc

    from services.generated_files import make_display_name, register_generated_file

    paper_name = make_display_name("练习试卷", data.title, "docx")
    answer_name = make_display_name("答案解析", data.title, "docx")
    paper_record = register_generated_file(db, user.id, paper_bytes, paper_name)
    answer_record = register_generated_file(db, user.id, answer_bytes, answer_name)
    db.commit()

    paper_url = f"/api/download/{paper_record.id}"
    answer_url = f"/api/download/{answer_record.id}"
    return {
        "paper_url": paper_url,
        "answer_url": answer_url,
        # Compatibility keys for clients deployed before the structured export update.
        "print_url": paper_url,
        "download_url": answer_url,
        "layout": data.layout,
    }


@router.post("/exercise/export-answer-sheet-docx")
async def exercise_export_answer_sheet_docx(
    data: ExercisePaper = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Export an A3 landscape duplex student answer sheet as an owned download."""
    require_teacher_stage(user)
    try:
        from services.answer_sheet_document import build_answer_sheet_document

        answer_sheet_bytes = await asyncio.to_thread(
            build_answer_sheet_document,
            data.title,
            data.questions,
            teaching_context=data.teaching_context,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Answer-sheet DOCX generation failed user_id=%s", user.id)
        raise HTTPException(500, detail="Word 答题卡生成失败") from exc

    from services.generated_files import make_display_name, register_generated_file

    display_name = make_display_name("数学答题卡", data.title, "docx")
    record = register_generated_file(db, user.id, answer_sheet_bytes, display_name)
    db.commit()
    return {
        "download_url": f"/api/download/{record.id}",
        "layout": "a3_landscape_duplex_three_panel",
    }
