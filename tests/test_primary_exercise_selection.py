"""Primary exercise selection through real routes and synthetic SQLite rows.

Run with scripts/run_math_stage_tests.py. No selection/model function is mocked:
only authentication and database dependencies use isolated test objects.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_accelerator  # noqa: F401 - register referenced metadata
from database import Base, get_db
from models import GradingResult, QuestionBank, Student, StudentProfile, Weakness
from models_correction import QuestionAttemptEvent, StudentObservationPeriod, WrongQuestionCycle
from models_rate_limit import TeacherClass
from models_user import User
from routers import exercise
from routers.auth import _require_user
from services.math_curriculum import get_knowledge_point, get_math_catalog
from services.teaching_context import current_teaching_context, make_teaching_context, use_teaching_context


def _core_points():
    return [
        (module["chapter"], topic)
        for module in get_math_catalog("primary")
        for topic in module["topics"]
        if get_knowledge_point("primary", module["chapter"], topic)["scope"] == "core"
    ]


def _bank_row(db, *, stage, kind, marker, point, owner=1, version=None):
    context = make_teaching_context(stage) if stage else {}
    stems = {
        "choice": f"合成{marker}：1+1=（ ） A. 1 B. 2 C. 3 D. 4",
        "fill": f"合成{marker}：1+1=____。",
        "solution": f"合成{marker}：计算1+1，写出过程。",
    }
    row = QuestionBank(
        teacher_id=owner, is_public=False, question=stems[kind],
        answer="B" if kind == "choice" else "2", question_type=kind,
        chapter=point[0], topic=point[1], education_stage=stage,
        catalog_version=context.get("curriculum_version", "") if version is None else version,
    )
    db.add(row)
    db.flush()
    return row


def _cycle(db, *, student_id, stage, point, identity=None, count=1, status="pending", owner=1):
    context = make_teaching_context(stage) if stage else {}
    identity = identity or f"{stage or 'legacy'}-original-{student_id}-{status}"
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    result = GradingResult(
        student_id=student_id, education_stage=stage,
        catalog_version=context.get("curriculum_version", ""),
    )
    db.add(result)
    db.flush()
    attempt = QuestionAttemptEvent(
        student_id=student_id, class_id=1 if owner == 1 else 2, teacher_id=owner,
        grading_result_id=result.id, question_no="1", question_identity=identity,
        question_fingerprint=fingerprint, question_type="fill", final_result="wrong",
        question_snapshot_json=json.dumps({
            "teaching_context": context, "question_type": "fill",
            "question_text": f"合成原题[{identity}]：1+1=____。",
            "standard_answer": "2", "solution_steps": ["相加得到2。"],
            "chapter": point[0], "topic": point[1],
        }, ensure_ascii=False),
    )
    db.add(attempt)
    db.flush()
    cycle = WrongQuestionCycle(
        student_id=student_id, class_id=attempt.class_id, teacher_id=owner,
        question_identity=identity, question_fingerprint=fingerprint,
        opened_attempt_id=attempt.id, wrong_count=count, status=status,
    )
    db.add(cycle)
    db.flush()
    return cycle


@pytest.fixture
def selection_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    user = User(id=1, name="合成小学教师", role="teacher", education_stage="primary")
    db.add_all([
        user, User(id=2, name="合成其他教师", role="teacher", education_stage="primary"),
        TeacherClass(id=1, teacher_id=1, class_name="合成一班"),
        TeacherClass(id=2, teacher_id=2, class_name="合成他人班"),
        *(Student(id=sid, teacher_id=1, class_id=1, name=f"合成学生{sid}") for sid in (1, 2, 3)),
        Student(id=4, teacher_id=2, class_id=2, name="合成他人学生"),
    ])
    db.flush()
    points = _core_points()
    assert len(points) >= 2
    target, unrelated = points[0], points[-1]
    wanted = [
        _bank_row(db, stage="primary", kind=kind, marker=f"目标{kind}", point=target)
        for kind in ("choice", "fill", "solution")
    ]
    distractors = []
    # Same labels intentionally exercise the stage boundary, not text matching.
    for stage in ("junior", "senior", ""):
        for kind in ("choice", "fill", "solution"):
            distractors.append(_bank_row(db, stage=stage, kind=kind, marker=f"异段{stage}{kind}", point=target))
    distractors.extend([
        _bank_row(db, stage="primary", kind="fill", marker="他人", point=target, owner=2),
        _bank_row(db, stage="primary", kind="fill", marker="旧目录", point=target, version="obsolete"),
        _bank_row(db, stage="primary", kind="fill", marker="无关知识点", point=unrelated),
    ])
    primary_cycles = [
        _cycle(db, student_id=sid, stage="primary", point=target, identity="primary-shared-original", count=sid + 1)
        for sid in (1, 2)
    ]
    excluded_cycles = [
        _cycle(db, student_id=1, stage=stage, point=unrelated, count=999)
        for stage in ("junior", "senior", "")
    ]
    excluded_cycles.extend([
        _cycle(db, student_id=1, stage="primary", point=unrelated, count=999, status="corrected"),
        _cycle(db, student_id=3, stage="primary", point=target, identity="primary-shared-original", count=999),
        _cycle(db, student_id=4, stage="primary", point=target, count=999, owner=2),
    ])
    db.add(StudentObservationPeriod(student_id=3, class_id=1, teacher_id=1, created_by=1, reason="合成观察模式"))
    db.commit()
    app = FastAPI()
    app.include_router(exercise.router, prefix="/api")

    def sessions():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[_require_user] = lambda: user
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                db=db, client=client, user=user, target=target,
                wanted=wanted, distractors=distractors,
                primary_cycles=primary_cycles, excluded_cycles=excluded_cycles,
            )
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("mode", ["bank", "personal", "class"])
def test_primary_real_selection_uses_only_current_stage_bank_and_pending_knowledge(selection_db, mode):
    state = selection_db
    payload = {
        "mode": mode, "question_count": 3,
        "type_counts": {"choice": 1, "fill": 1, "solution": 1},
    }
    if mode == "bank":
        payload["topics"] = [state.target[1]]
    elif mode == "personal":
        payload["student_id"] = 1
    else:
        payload["class_id"] = 1
    caller = make_teaching_context("senior")
    with use_teaching_context(caller):
        response = state.client.post("/api/exercises/generate", json=payload)
        assert current_teaching_context() == caller
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"] == "question_bank"
    assert result["teaching_context"] == make_teaching_context("primary")
    assert [row["type"] for row in result["exercises"]] == ["choice", "fill", "solution"]
    assert {row["bank_question_id"] for row in result["exercises"]} == {row.id for row in state.wanted}
    assert {row["knowledge_point"] for row in result["exercises"]} == {state.target[1]}
    assert result["exercises"][0]["options"] == {"A": "1", "B": "2", "C": "3", "D": "4"}
    assert [row["answer"] for row in result["exercises"]] == ["B", "2", "2"]
    state.db.expire_all()
    assert [row.usage_count for row in state.wanted] == [1, 1, 1]
    assert all(row.usage_count == 0 for row in state.distractors)


@pytest.mark.parametrize("scope", ["personal", "class"])
def test_primary_original_correction_uses_exact_stage_cycles_without_bank_replacement(selection_db, scope):
    state = selection_db
    payload = {"mode": "correction", "correction_scope": scope, "question_count": 10}
    payload["student_id" if scope == "personal" else "class_id"] = 1
    response = state.client.post("/api/exercises/generate", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"] == "pending_original_questions"
    assert result["teaching_context"] == make_teaching_context("primary")
    assert result["available_count"] == result["selected_count"] == 1
    assert result["requested_count"] == 10 and result["shortage_message"]
    [item] = result["exercises"]
    assert item["question"] == "合成原题[primary-shared-original]：1+1=____。"
    assert item["source_wrong_cycle_id"] == state.primary_cycles[0].id
    assert item["source_question_identity"] == "primary-shared-original"
    assert item["answer"] == "2" and item["solution"] == "相加得到2。"
    assert item["type"] == "fill" and item["knowledge_point"] == state.target[1]
    if scope == "class":
        assert item["wrong_student_count"] == 2 and item["wrong_event_count"] == 5
        assert item["source_wrong_cycles"] == [
            {"student_id": sid, "cycle_id": cycle.id}
            for sid, cycle in zip((1, 2), state.primary_cycles)
        ]
    else:
        assert "source_wrong_cycles" not in item
    state.db.expire_all()
    assert all(row.usage_count == 0 for row in [*state.wanted, *state.distractors])


@pytest.mark.parametrize("mode", ["personal", "class", "correction"])
def test_other_stage_cycles_cannot_supply_missing_primary_wrong_questions(selection_db, mode):
    state = selection_db
    for cycle in state.primary_cycles:
        cycle.status = "corrected"
    state.db.commit()
    payload = {"mode": mode, "student_id": 1, "class_id": 1, "question_count": 1}
    response = state.client.post("/api/exercises/generate", json=payload)
    assert response.status_code == 409, response.text
    state.db.expire_all()
    assert all(row.usage_count == 0 for row in [*state.wanted, *state.distractors])


def test_primary_bank_shortage_does_not_borrow_other_stage_choices(selection_db):
    state = selection_db
    response = state.client.post("/api/exercises/generate", json={
        "mode": "bank", "topics": [state.target[1]], "question_count": 2,
        "type_counts": {"choice": 2, "fill": 0, "solution": 0},
    })
    assert response.status_code == 409, response.text
    assert "选择题缺 1 道" in response.json()["detail"]
    state.db.expire_all()
    assert all(row.usage_count == 0 for row in [*state.wanted, *state.distractors])


def _pre_cycle_primary_history(state):
    # Keep other-stage cycles and the observer/other-teacher primary cycles.
    for cycle in state.db.query(WrongQuestionCycle).filter(
        WrongQuestionCycle.student_id.in_((1, 2)),
    ).all():
        attempt = state.db.get(QuestionAttemptEvent, cycle.opened_attempt_id)
        if attempt.question_snapshot().get("teaching_context", {}).get("education_stage") == "primary":
            state.db.delete(cycle)
    for stage in ("primary", "junior", "senior", ""):
        context = make_teaching_context(stage) if stage else {}
        point = state.target if stage == "primary" else _core_points()[-1]
        profile = StudentProfile(
            student_id=1, education_stage=stage,
            catalog_version=context.get("curriculum_version", ""), analytics_included=True,
        )
        profile.set_details([{"result": "wrong", "chapter": point[0], "topic": point[1]}])
        state.db.add_all([
            profile,
            Weakness(student_id=1, education_stage=stage,
                     catalog_version=context.get("curriculum_version", ""),
                     chapter=point[0], topic=point[1], occurrences=1 if stage == "primary" else 999),
        ])
    state.db.commit()


@pytest.mark.parametrize("mode", ["personal", "class"])
def test_other_stage_cycles_do_not_block_primary_pre_cycle_archive_fallback(selection_db, mode):
    state = selection_db
    _pre_cycle_primary_history(state)
    before_cycles = {row.id: (row.status, row.opened_attempt_id) for row in state.db.query(WrongQuestionCycle).all()}
    before_profiles = {row.id: (row.education_stage, row.details_json) for row in state.db.query(StudentProfile).all()}
    response = state.client.post("/api/exercises/generate", json={
        "mode": mode, "student_id": 1, "class_id": 1, "question_count": 3,
        "type_counts": {"choice": 1, "fill": 1, "solution": 1},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["teaching_context"] == make_teaching_context("primary")
    assert {row["bank_question_id"] for row in result["exercises"]} == {row.id for row in state.wanted}
    assert {row["knowledge_point"] for row in result["exercises"]} == {state.target[1]}
    state.db.expire_all()
    assert {row.id: (row.status, row.opened_attempt_id) for row in state.db.query(WrongQuestionCycle).all()} == before_cycles
    assert {row.id: (row.education_stage, row.details_json) for row in state.db.query(StudentProfile).all()} == before_profiles
    assert [row.usage_count for row in state.wanted] == [1, 1, 1]
    assert all(row.usage_count == 0 for row in state.distractors)


@pytest.mark.parametrize("mode", ["personal", "class"])
@pytest.mark.parametrize("adopted_snapshot", [False, True])
def test_closed_primary_cycle_still_blocks_reviving_pre_cycle_wrong_history(selection_db, mode, adopted_snapshot):
    state = selection_db
    _pre_cycle_primary_history(state)
    closed = _cycle(state.db, student_id=1, stage="primary", point=state.target,
                    identity="primary-completed-history", status="corrected")
    if adopted_snapshot:
        # Adopted archives resolve the stage from their own result, not the profile.
        state.db.get(QuestionAttemptEvent, closed.opened_attempt_id).question_snapshot_json = "{}"
    state.db.commit()
    response = state.client.post("/api/exercises/generate", json={
        "mode": mode, "student_id": 1, "class_id": 1, "question_count": 3,
        "type_counts": {"choice": 1, "fill": 1, "solution": 1},
    })
    assert response.status_code == 409, response.text
    state.db.expire_all()
    assert closed.status == "corrected"
    assert all(row.usage_count == 0 for row in [*state.wanted, *state.distractors])
