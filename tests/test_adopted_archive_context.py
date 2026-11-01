"""Adopted senior archives remain usable without rewriting frozen identities."""
from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_accelerator  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base
from models import GradingResult, KnowledgeEvent, QuestionBank, Student, StudentProfile
from models_user import User
from models_rate_limit import TeacherClass
from models_correction import (
    KnowledgeEventClassificationJob, QuestionAttemptEvent,
    QuestionCorrectionRelation, WrongQuestionCycle,
)
from services import correction_tracking as tracking
from services import question_classification as classification
from services.math_curriculum import catalog_version
from services.teaching_context import make_teaching_context, use_teaching_context


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(classification, "SessionLocal", sessions)
    db = sessions()
    db.add(User(id=1, name="Synthetic teacher", education_stage="junior"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="Synthetic class"))
    db.add(Student(id=1, teacher_id=1, class_id=1, name="Synthetic student"))
    db.commit()
    yield db
    db.close()
    engine.dispose()


def archive(db, *, stage="senior", frozen=None, result="wrong", answer="1"):
    detail = {"question_no": "1", "question_text": "x+1=2", "question_type": "fill",
              "standard_answer": answer, "student_answer": "2", "result": result}
    if frozen is not None:
        detail["teaching_context"] = frozen
    row = GradingResult(student_id=1, education_stage=stage,
                        catalog_version=catalog_version(stage) if stage else "",
                        grading_contract_hash="unchanged-contract", total_questions=1,
                        grading_details=json.dumps([detail]))
    db.add(row)
    db.flush()
    identity, method, fingerprint = tracking.question_identity(detail)
    attempt = QuestionAttemptEvent(student_id=1, class_id=1, teacher_id=1,
        grading_result_id=row.id, question_no="1", question_identity=identity,
        identity_method=method, question_fingerprint=fingerprint,
        question_type="fill", question_snapshot_json=json.dumps(detail),
        final_result=result, analytics_included=True)
    db.add(attempt)
    db.flush()
    return row, attempt


def open_cycle(db, attempt):
    cycle = WrongQuestionCycle(student_id=1, class_id=1, teacher_id=1,
        question_identity=attempt.question_identity, question_fingerprint=attempt.question_fingerprint,
        opened_attempt_id=attempt.id, cycle_no=1, status="pending")
    db.add(cycle)
    db.commit()
    return cycle


def test_adoption_lookup_reads_only_unstamped_open_cycles(store):
    expected = []
    for frozen, raw in ((None, None), ({}, None), (make_teaching_context("senior"), None), (None, "not json")):
        _row, attempt = archive(store, frozen=frozen)
        if raw is not None:
            attempt.question_snapshot_json = raw
        attempt.question_identity += f":{attempt.id}"
        cycle = open_cycle(store, attempt)
        # The SQL filter must admit exactly what the Python check treats as adopted.
        if not attempt.question_snapshot().get("teaching_context"):
            expected.append(cycle.id)
    rows = tracking._unstamped_open_cycles(store, 1, 1)
    assert sorted(cycle.id for cycle, _opened in rows) == sorted(expected)
    assert len(expected) == 3


@pytest.mark.parametrize("frozen", [None, {}])
def test_adopted_attempt_reads_own_result_without_rewriting_snapshot(store, frozen):
    row, attempt = archive(store, frozen=frozen)
    before = (row.grading_details, row.grading_contract_hash, attempt.question_snapshot_json,
              attempt.question_identity, attempt.question_fingerprint)
    with use_teaching_context(make_teaching_context("junior")):
        assert tracking.attempt_teaching_context(store, attempt) == make_teaching_context("senior")
    assert before == (row.grading_details, row.grading_contract_hash, attempt.question_snapshot_json,
                      attempt.question_identity, attempt.question_fingerprint)


def test_unadopted_and_frozen_contexts_never_follow_profile(store):
    _row, legacy = archive(store, stage="")
    _other, frozen = archive(store, frozen=make_teaching_context("junior"))
    with use_teaching_context(make_teaching_context("senior")):
        assert tracking.attempt_teaching_context(store, legacy) == {}
        assert tracking.attempt_teaching_context(store, frozen) == make_teaching_context("junior")
    _other.catalog_version = "unavailable"
    assert tracking.attempt_teaching_context(store, frozen) == make_teaching_context("junior")


def test_invalid_adopted_catalog_fails_closed(store):
    row, attempt = archive(store)
    row.catalog_version = "unavailable"
    with pytest.raises(ValueError):
        tracking.attempt_teaching_context(store, attempt)


@pytest.mark.parametrize("invalid", [[], "", 0, {"education_stage": "senior"}])
def test_invalid_frozen_snapshot_is_not_replaced_by_adoption(store, invalid):
    row, attempt = archive(store)
    malformed = {**attempt.question_snapshot(), "teaching_context": invalid}
    attempt.question_snapshot_json = json.dumps(malformed)
    with pytest.raises(ValueError):
        tracking.attempt_teaching_context(store, attempt)
    with pytest.raises(ValueError):
        tracking.resolve_archived_teaching_context([malformed], row)


def test_archived_review_allows_empty_but_rejects_real_conflict(store):
    row, _attempt = archive(store)
    assert tracking.resolve_archived_teaching_context([{"teaching_context": {}}], row) == make_teaching_context("senior")
    with pytest.raises(ValueError):
        tracking.resolve_archived_teaching_context([{"teaching_context": make_teaching_context("junior")}], row)


def test_wrong_question_summary_and_exercise_scope_use_adopted_record(store):
    from routers.exercise import _cycles_for_task_stage, _matches_task_stage

    row, attempt = archive(store)
    cycle = open_cycle(store, attempt)
    before = attempt.question_snapshot_json
    with use_teaching_context(make_teaching_context("senior")):
        assert _matches_task_stage(attempt, store)
        assert _cycles_for_task_stage(store, [cycle]) == [cycle]
        assert tracking.wrong_question_summary(store, 1)[0]["education_stage"] == "senior"
    with use_teaching_context(make_teaching_context("junior")):
        assert not _matches_task_stage(attempt, store)
        assert _cycles_for_task_stage(store, [cycle]) == []
    assert attempt.question_snapshot_json == before
    assert row.grading_contract_hash == "unchanged-contract"


def test_classification_worker_claim_uses_adopted_stage_without_writing_frozen_json(store):
    row, attempt = archive(store)
    store.add(KnowledgeEventClassificationJob(attempt_id=attempt.id,
        question_fingerprint=attempt.question_fingerprint, status="pending"))
    store.commit()
    before = attempt.question_snapshot_json
    claimed = classification.claim_knowledge_event_classification("synthetic-worker")
    assert len(claimed) == 1
    assert claimed[0]["teaching_context"] == make_teaching_context("senior")
    store.refresh(attempt)
    assert attempt.question_snapshot_json == before
    assert row.grading_contract_hash == "unchanged-contract"


def test_classification_links_adopted_bank_without_injecting_context(store):
    row, attempt = archive(store)
    bank = QuestionBank(question="x+1=2", answer="1", education_stage="senior",
        catalog_version=catalog_version("senior"), chapter="", topic="")
    store.add(bank)
    store.flush()
    attempt.question_bank_id = bank.id
    store.commit()
    before = attempt.question_identity, attempt.question_fingerprint, row.grading_contract_hash
    changed = classification._propagate_fingerprint_knowledge(store,
        question_fingerprint=attempt.question_fingerprint,
        chapter="函数的概念与性质", topic="函数的定义域")
    assert changed == 1
    assert bank.chapter == "函数的概念与性质"
    assert "teaching_context" not in attempt.question_snapshot()
    assert "teaching_context" not in row.get_details()[0]
    assert before == (attempt.question_identity, attempt.question_fingerprint, row.grading_contract_hash)


def test_new_same_original_closes_adopted_cycle_without_rekeying(store):
    old, opened = archive(store)
    cycle = open_cycle(store, opened)
    old_identity = (cycle.question_identity, cycle.question_fingerprint, opened.question_snapshot_json)
    _new, correct = archive(store, frozen=make_teaching_context("senior"), result="correct")
    tracking._apply_attempt(store, correct)
    store.flush()
    assert cycle.status == "corrected"
    assert cycle.closed_attempt_id == correct.id
    assert old_identity == (cycle.question_identity, cycle.question_fingerprint, opened.question_snapshot_json)
    assert old.grading_contract_hash == "unchanged-contract"


@pytest.mark.parametrize("stage,answer", [("junior", "1"), ("senior", "3")])
def test_different_stage_or_math_never_auto_closes_adopted_cycle(store, stage, answer):
    _old, opened = archive(store)
    cycle = open_cycle(store, opened)
    _new, correct = archive(store, stage=stage, frozen=make_teaching_context(stage), result="correct", answer=answer)
    tracking._apply_attempt(store, correct)
    assert cycle.status == "pending"


def test_teacher_authorized_adopted_target_uses_same_effective_stage(store):
    _old, opened = archive(store)
    cycle = open_cycle(store, opened)
    _new, correct = archive(store, frozen=make_teaching_context("senior"), result="correct", answer="3")
    store.add(QuestionCorrectionRelation(teacher_id=1, target_cycle_id=cycle.id,
        source_question_identity=correct.question_identity,
        source_question_fingerprint=correct.question_fingerprint, authorized_by=1))
    store.flush()
    tracking._apply_attempt(store, correct)
    assert cycle.status == "corrected"


@pytest.mark.parametrize("initial,final", [("wrong", "correct"), ("correct", "wrong")])
def test_teacher_review_adopted_empty_context_preserves_contract_and_hashes(store, initial, final):
    from services.grading_persistence import synchronize_teacher_confirmation

    row, attempt = archive(store, frozen={}, result=initial)
    if initial == "wrong":
        open_cycle(store, attempt)
    profile = StudentProfile(student_id=1, grading_result_id=row.id,
        education_stage="senior", catalog_version=catalog_version("senior"),
        details_json=row.grading_details)
    event = KnowledgeEvent(student_id=1, class_id=1, teacher_id=1, grading_result_id=row.id,
        education_stage="senior", catalog_version=catalog_version("senior"), result=initial)
    store.add_all([profile, event])
    store.commit()
    identity = attempt.question_identity, attempt.question_fingerprint
    previous = row.get_details()
    revised = copy.deepcopy(previous)
    revised[0]["result"] = final
    row.correct_count = int(final == "correct")
    row.wrong_count = int(final == "wrong")
    row.set_details(revised)
    with use_teaching_context(make_teaching_context("junior")):
        synchronize_teacher_confirmation(store, student=store.get(Student, 1), teacher_id=1,
            grading_result=row, previous_details=previous, details=revised, detail_index=0)
    store.commit()
    assert attempt.final_result == final
    assert row.get_details()[0]["teaching_context"] == {}
    assert profile.get_details()[0]["teaching_context"] == {}
    assert attempt.question_snapshot()["teaching_context"] == {}
    assert "education_stage" not in row.get_details()[0]
    assert identity == (attempt.question_identity, attempt.question_fingerprint)
    assert row.grading_contract_hash == "unchanged-contract"
    assert profile.education_stage == event.education_stage == "senior"


def test_ambiguous_adopted_originals_are_not_merged_without_authorization(store):
    _old, first = archive(store)
    first_cycle = open_cycle(store, first)
    _other, second = archive(store)
    # A second historical correction cycle can be distinct despite identical
    # source math. Adoption must not collapse teacher history implicitly.
    second.question_identity += ":historical-distinct"
    second_cycle = open_cycle(store, second)
    _new, correct = archive(store, frozen=make_teaching_context("senior"), result="correct")
    tracking._apply_attempt(store, correct)
    assert first_cycle.status == second_cycle.status == "pending"


def test_new_wrong_attempt_keeps_own_cycle_for_later_review(store):
    _old, opened = archive(store)
    old_cycle = open_cycle(store, opened)
    _new, wrong = archive(store, frozen=make_teaching_context("senior"), result="wrong")
    tracking._apply_attempt(store, wrong)
    store.flush()
    new_cycle = store.query(WrongQuestionCycle).filter_by(question_identity=wrong.question_identity).one()
    assert new_cycle.id != old_cycle.id
    assert old_cycle.wrong_count == new_cycle.wrong_count == 1
    wrong.final_result = "correct"
    tracking._apply_attempt(store, wrong)
    assert new_cycle.status == "corrected"
    assert old_cycle.status == "pending"
    assert old_cycle.wrong_count == 1
