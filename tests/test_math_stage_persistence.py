"""Frozen-stage persistence/classification tests; no real data or upstream calls."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_accelerator  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, KnowledgeEvent, QuestionBank, Student, StudentProfile, Weakness
from models_correction import QuestionAttemptEvent, WrongQuestionCycle
from models_user import User
from services import grading_persistence as persistence
from services import question_classification as classification
from services.correction_tracking import (
    grading_result_teaching_context, question_fingerprint, question_identity,
    repair_grading_result_projection, wrong_question_summary,
)
from services.math_curriculum import catalog_version, get_knowledge_point, get_math_catalog
from services.teaching_context import make_teaching_context, use_teaching_context, current_teaching_context


POINTS = {
    "junior": ("有理数与实数", "有理数与数轴"),
    "senior": ("函数的概念与性质", "函数的定义域"),
}


@pytest.fixture
def storage(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(classification, "SessionLocal", sessions)
    monkeypatch.setattr(persistence, "ARCHIVE_DIR", str(tmp_path / "archives"))
    db = sessions()
    db.add(User(id=1, name="测试教师", education_stage="junior"))
    student = Student(id=1, teacher_id=1, name="虚拟学生")
    db.add(student)
    db.commit()
    yield db, student, sessions
    db.close()
    engine.dispose()


def detail(stage=None, *, classified=True):
    row = {"question_no": "1", "question": "测试题 x+1=2", "question_type": "fill",
           "standard_answer": "1", "student_answer": "2", "result": "wrong"}
    if stage and classified:
        row["chapter"], row["topic"] = POINTS[stage]
    return row


def persist(db, student, stage=None, *, details=None, image_hash="same", **kwargs):
    options = dict(student=student, teacher_id=1, category="daily", exam_name="",
                   image_path="", image_hash=image_hash, total_questions=1,
                   correct_count=0, partial_count=0, wrong_count=1, needs_review_count=0,
                   score=None, details=details if details is not None else [detail(stage)], **kwargs)
    if stage:
        options["teaching_context"] = make_teaching_context(stage)
    return persistence.persist_grading_result(db, **options)


def test_legacy_hash_is_byte_compatible_and_does_not_use_ambient_context():
    rows = [detail()]
    old_units = [{key: value for key, value in rows[0].items() if key in persistence._CONTRACT_FIELDS}]
    payload = {"category": "daily", "scoring_version": 2, "units": old_units}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), default=str).encode()).hexdigest()
    with use_teaching_context(make_teaching_context("junior")):
        assert persistence.build_grading_contract_hash(rows, "daily", 2) == expected
    assert persistence.build_grading_contract_hash(rows, "daily", 2, teaching_context={}) == expected


def test_new_hash_and_question_identity_bind_frozen_catalog():
    rows = [detail()]
    hashes = {persistence.build_grading_contract_hash(rows, "daily", 2)}
    fingerprints = {question_fingerprint(rows[0])}
    identities = {question_identity({**rows[0], "template_question_id": "shared:1"})[0]}
    for stage in ("junior", "senior"):
        context = make_teaching_context(stage)
        explicit = persistence.build_grading_contract_hash(rows, "daily", 2, teaching_context=context)
        stamped = {**rows[0], "teaching_context": context, "template_question_id": "shared:1"}
        assert explicit == persistence.build_grading_contract_hash([stamped], "daily", 2)
        hashes.add(explicit)
        fingerprints.add(question_fingerprint(stamped))
        identities.add(question_identity(stamped)[0])
    assert len(hashes) == len(fingerprints) == len(identities) == 3


def test_same_paper_has_three_isolated_archives_and_weaknesses(storage):
    db, student, _ = storage
    results = [persist(db, student, stage)[0] for stage in (None, "junior", "senior")]
    assert len({row.id for row in results}) == 3
    for cls in (GradingResult, StudentProfile, KnowledgeEvent, Weakness):
        assert {row.education_stage for row in db.query(cls).all()} == {"", "junior", "senior"}
    attempts = db.query(QuestionAttemptEvent).all()
    assert len({row.question_fingerprint for row in attempts}) == 3
    assert db.query(WrongQuestionCycle).count() == 3
    for stage in ("junior", "senior"):
        repeated, _, created = persist(db, student, stage)
        assert not created
        assert repeated.education_stage == stage
        attempt = db.query(QuestionAttemptEvent).filter_by(grading_result_id=repeated.id).one()
        snapshot = attempt.question_snapshot()
        assert snapshot["teaching_context"] == make_teaching_context(stage)
        assert snapshot["knowledge_point_id"] == get_knowledge_point(stage, *POINTS[stage])["id"]
        event = db.query(KnowledgeEvent).filter_by(grading_result_id=repeated.id).one()
        assert event.knowledge_point_id == snapshot["knowledge_point_id"]
        with use_teaching_context(make_teaching_context(stage)):
            assert len(wrong_question_summary(db, student.id)) == 1
            assert len(wrong_question_summary(db, student.id, teaching_context={})) == 3


def test_task_snapshot_not_current_teacher_and_input_not_mutated(storage):
    db, student, _ = storage
    rows = [detail("junior")]
    rows[0]["teaching_context"] = make_teaching_context("junior")
    original = copy.deepcopy(rows)
    db.get(User, 1).education_stage = "senior"
    with use_teaching_context(make_teaching_context("senior")):
        result, profile, _ = persist(db, student, details=rows)
        legacy, _, _ = persist(db, student, image_hash="legacy")
    assert result.education_stage == profile.education_stage == "junior"
    assert legacy.education_stage == ""
    assert rows == original


@pytest.mark.parametrize("bad", [
    {"education_stage": "primary"},
    {**make_teaching_context("junior"), "curriculum_version": "unknown"},
    {**make_teaching_context("junior"), "rules_version": "unknown"},
])
def test_unknown_context_fails_before_write(storage, bad):
    db, student, _ = storage
    with pytest.raises(ValueError):
        persist(db, student, teaching_context=bad)
    assert db.query(GradingResult).count() == 0


def test_conflicting_detail_context_fails_before_write(storage):
    db, student, _ = storage
    rows = [{**detail(), "teaching_context": make_teaching_context("senior")}]
    with pytest.raises(ValueError):
        persist(db, student, "junior", details=rows)
    assert db.query(GradingResult).count() == 0


def test_stale_supplied_hash_cannot_merge_stages(storage):
    db, student, _ = storage
    junior = persist(db, student, "junior", grading_contract_hash="caller-legacy-hash")[0]
    senior = persist(db, student, "senior", grading_contract_hash="caller-legacy-hash")[0]
    assert junior.id != senior.id
    assert junior.grading_contract_hash != senior.grading_contract_hash


def test_teacher_revision_preserves_archived_stage(storage):
    db, student, _ = storage
    result, profile, _ = persist(db, student, "junior")
    previous = result.get_details()
    revised = copy.deepcopy(previous)
    revised[0]["result"] = "correct"
    result.correct_count, result.wrong_count = 1, 0
    result.set_details(revised)
    db.get(User, 1).education_stage = "senior"
    with use_teaching_context(make_teaching_context("senior")):
        persistence.synchronize_teacher_confirmation(db, student=student, teacher_id=1,
            grading_result=result, previous_details=previous, details=revised, detail_index=0)
    db.commit()
    attempt = db.query(QuestionAttemptEvent).one()
    assert attempt.final_result == "correct"
    assert attempt.question_snapshot()["education_stage"] == profile.education_stage == "junior"
    assert db.query(KnowledgeEvent).one().education_stage == "junior"


def test_projection_repair_uses_result_not_missing_detail_context(storage):
    db, student, _ = storage
    result = GradingResult(student_id=student.id, category="daily", total_questions=1,
                           wrong_count=1, analytics_included=True,
                           education_stage="junior", catalog_version=catalog_version("junior"))
    result.set_details([detail("junior")])
    db.add(result)
    db.commit()
    with use_teaching_context(make_teaching_context("senior")):
        repair_grading_result_projection(db, grading_result=result, student=student, teacher_id=1)
    assert db.query(QuestionAttemptEvent).one().question_snapshot()["education_stage"] == "junior"
    result.catalog_version = "unavailable"
    with pytest.raises(ValueError):
        grading_result_teaching_context(result)


def test_matchers_filter_at_insert_and_after_record_mutation():
    from services.matchers.hash_matcher import HashMatcher
    from services.matchers.trie_matcher import TrieMatcher
    junior = make_teaching_context("junior")
    good = SimpleNamespace(education_stage="junior", catalog_version=junior["curriculum_version"])
    bad = SimpleNamespace(education_stage="senior", catalog_version=catalog_version("senior"))
    exact, fuzzy = HashMatcher(teaching_context=junior), TrieMatcher(teaching_context=junior)
    for key, value in (("same", good), ("same", bad), ("old", {})):
        exact.put(key, value)
        fuzzy.insert(key, value)
    assert exact.get("same") is fuzzy.search_exact("same") is good
    assert fuzzy.search_fuzzy("same") == [good]
    assert exact.get("old") is None
    good.education_stage = "senior"
    assert exact.get("same") is fuzzy.search_exact("same") is None
    assert fuzzy.search_fuzzy("same") == []
    legacy = HashMatcher(teaching_context={})
    legacy.put("same", bad)
    assert legacy.get("same") is bad


def test_classification_batches_by_stage_and_rejects_unknown_without_request(monkeypatch):
    from services import analyzer
    calls = []
    async def classify(payload, *, per_question=False):
        assert per_question is True
        calls.append((current_teaching_context(), payload))
        return [{"id": item["id"], "status": "suggested"} for item in payload]
    monkeypatch.setattr(analyzer, "classify_questions_knowledge", classify)
    claimed = []
    for index, stage in enumerate(("junior", "junior", "senior", "")):
        claimed.append({"question_id": index, "question": f"题干{index}", "education_stage": stage,
                        "catalog_version": catalog_version(stage) if stage else ""})
    claimed.append({"question_id": 9, "question": "不得发送", "education_stage": "junior", "catalog_version": "missing"})
    with use_teaching_context(make_teaching_context("senior")):
        results = asyncio.run(classification._classify_stage_groups(claimed, "question_id"))
        assert current_teaching_context()["education_stage"] == "senior"
    assert [len(payload) for _, payload in calls] == [2, 1, 1]
    assert [ctx.get("education_stage", "legacy") for ctx, _ in calls] == ["junior", "senior", "legacy"]
    assert next(item for item in results if item["id"] == "9")["status"] == "manual"


@pytest.mark.parametrize("stage", ["junior", "senior"])
def test_bank_classification_stable_id_and_teacher_edit_preserved(storage, stage):
    db, _, _ = storage
    question = QuestionBank(question="待分类", answer="", education_stage=stage, catalog_version=catalog_version(stage))
    db.add(question)
    db.commit()
    classification.reconcile_classification_jobs_once()
    claimed = classification.claim_classification_batch("worker", 10)
    chapter, topic = POINTS[stage]
    question.chapter, question.topic = chapter, topic  # Teacher edits while model runs.
    db.commit()
    classification.finish_classification_batch(claimed, [{"id": str(question.id), "status": "suggested",
                                                         "chapter": "错误建议", "topic": "错误建议"}])
    db.expire_all()
    assert (question.chapter, question.topic) == (chapter, topic)
    assert question.classification_source == "teacher"
    assert question.knowledge_point_id == get_knowledge_point(stage, chapter, topic)["id"]


def test_wrong_stage_classification_result_is_manual(storage):
    db, _, _ = storage
    question = QuestionBank(question="待分类", answer="", education_stage="junior", catalog_version=catalog_version("junior"))
    db.add(question)
    db.commit()
    classification.reconcile_classification_jobs_once()
    claimed = classification.claim_classification_batch("worker", 10)
    classification.finish_classification_batch(claimed, [{"id": str(question.id), "status": "suggested",
        "chapter": POINTS["senior"][0], "topic": POINTS["senior"][1]}])
    db.expire_all()
    assert question.classification_status == "manual"
    assert not question.knowledge_point_id


def test_reconcile_unknown_version_manual_and_valid_import_backfills_id(storage):
    db, _, _ = storage
    unknown = QuestionBank(question="未知目录", answer="", education_stage="junior", catalog_version="unknown")
    good = QuestionBank(question="教师已有分类", answer="", education_stage="junior", catalog_version=catalog_version("junior"),
                        chapter=POINTS["junior"][0], topic=POINTS["junior"][1])
    db.add_all([unknown, good])
    db.commit()
    classification.reconcile_classification_jobs_once()
    db.expire_all()
    assert unknown.classification_status == "manual"
    assert "目录" in unknown.classification_error
    assert good.knowledge_point_id == get_knowledge_point("junior", *POINTS["junior"])["id"]
    assert classification.claim_classification_batch("worker", 10) == []


def test_historical_classification_updates_only_matching_stage_projections(storage):
    db, student, _ = storage
    results = {stage: persist(db, student, stage, details=[detail(stage, classified=False)])[0]
               for stage in ("junior", "senior")}
    assert classification.reconcile_knowledge_event_classification_jobs_once() == 2
    for _ in range(2):
        claimed = classification.claim_knowledge_event_classification("worker")
        stage = claimed[0]["education_stage"]
        chapter, topic = POINTS[stage]
        classification.finish_knowledge_event_classification(claimed, [{"id": str(claimed[0]["attempt_id"]),
            "status": "suggested", "chapter": chapter, "topic": topic}])
        db.expire_all()
        result = results[stage]
        event = db.query(KnowledgeEvent).filter_by(grading_result_id=result.id).one()
        snapshot = db.query(QuestionAttemptEvent).filter_by(grading_result_id=result.id).one().question_snapshot()
        point_id = get_knowledge_point(stage, chapter, topic)["id"]
        assert event.knowledge_point_id == snapshot["knowledge_point_id"] == point_id
        assert result.get_details()[0]["knowledge_point_id"] == point_id
        assert db.query(StudentProfile).filter_by(grading_result_id=result.id).one().get_details()[0]["knowledge_point_id"] == point_id


def test_analyzer_returns_stable_id_only_for_valid_frozen_catalog():
    from services.analyzer import _validate_knowledge_result
    raw = [{"id": "1", "chapter": POINTS["junior"][0], "topic": POINTS["junior"][1]}]
    with use_teaching_context(make_teaching_context("junior")):
        result = _validate_knowledge_result(raw, ["1"], get_math_catalog("junior"), catalog_version("junior"))[0]
        assert result["education_stage"] == "junior"
        assert result["knowledge_point_id"] == get_knowledge_point("junior", *POINTS["junior"])["id"]
        assert result["knowledge_points"][0]["knowledge_point_id"] == result["knowledge_point_id"]
        with pytest.raises(ValueError):
            _validate_knowledge_result(raw, ["1"], get_math_catalog("junior"), "unknown")


def test_historical_batch_claim_retains_global_lane_and_groups_by_stage(storage, monkeypatch):
    from services import analyzer
    db, student, _ = storage
    for index, stage in enumerate(("junior", "junior", "senior")):
        row = detail(stage, classified=False)
        row["question"] += str(index)
        persist(db, student, stage, details=[row], image_hash=str(index))
    assert classification.reconcile_knowledge_event_classification_jobs_once() == 3
    claimed = classification.claim_knowledge_event_classification("worker-a", batch_size=10)
    assert len(claimed) == 3
    assert classification.claim_knowledge_event_classification("worker-b", batch_size=10) == []
    assert classification.claim_classification_batch("worker-b", batch_size=10) == []
    calls = []
    async def classify(payload, *, per_question=False):
        assert per_question is True
        calls.append((current_teaching_context()["education_stage"], len(payload)))
        return []
    monkeypatch.setattr(analyzer, "classify_questions_knowledge", classify)
    asyncio.run(classification._classify_stage_groups(claimed, "attempt_id"))
    assert calls == [("junior", 2), ("senior", 1)]


def test_manual_bank_assignment_checks_frozen_catalog(storage):
    db, _, _ = storage
    question = QuestionBank(question="测试", answer="", education_stage="junior", catalog_version=catalog_version("junior"))
    db.add(question)
    db.commit()
    question.chapter, question.topic = POINTS["senior"]
    with pytest.raises(ValueError):
        classification.mark_question_classified(db, question)
    question.chapter, question.topic = POINTS["junior"]
    classification.mark_question_classified(db, question)
    assert question.knowledge_point_id == get_knowledge_point("junior", *POINTS["junior"])["id"]
    classification.queue_question_classification(db, question)
    assert question.knowledge_point_id == ""
