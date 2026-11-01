from __future__ import annotations

import json
import copy
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_user  # noqa: F401
from database import Base
from models import GradingResult, KnowledgeEvent, Student, StudentProfile, WeaknessOverride
from models_correction import (
    CorrectionEvidence,
    QuestionAttemptEvent,
    QuestionCorrectionRelation,
    StudentObservationPeriod,
    TemplateQuestionAuthorization,
    WrongQuestionCycle,
)
from models_rate_limit import TeacherClass
from models_user import User
from services.correction_tracking import (
    _combined_snapshot,
    audit_grading_result_projection,
    question_fingerprint,
    repair_grading_result_projection,
    repair_missing_question_projections,
)
from services.scoring_engine import aggregate_grading_result, build_grading_units
from services.review_marks import render_review_marks
from services.correction_tracking import template_candidates
from services.class_analytics import error_distribution, weakness_list
from services.grading_persistence import persist_grading_result
from services.grading_persistence import synchronize_teacher_confirmation
from services.correction_tracking import active_observation_period


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    user = User(id=1, phone="13800000000", name="老师", current_class_id=1)
    klass = TeacherClass(id=1, teacher_id=1, class_name="一班")
    student = Student(id=1, teacher_id=1, class_id=1, name="学生", student_no="001")
    db.add_all([user, klass, student])
    db.commit()
    return db, student


def _persist(
    db,
    student,
    *,
    image_hash: str,
    details: list[dict],
    purpose="standard",
    knowledge_items: list[dict] | None = None,
    teaching_context: dict | None = None,
):
    total = len(details)
    correct = sum(1 for item in details if item["result"] == "correct")
    partial = sum(1 for item in details if item["result"] == "partial")
    wrong = sum(1 for item in details if item["result"] in {"wrong", "blank"})
    review = sum(1 for item in details if item["result"] == "needs_review")
    return persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="daily",
        exam_name="",
        image_path="",
        image_hash=image_hash,
        total_questions=total,
        correct_count=correct,
        partial_count=partial,
        wrong_count=wrong,
        needs_review_count=review,
        score=None,
        details=details,
        knowledge_items=knowledge_items,
        assessment_purpose=purpose,
        teaching_context=teaching_context,
    )[0]


def _parts(*results):
    return [
        {
            "parent_question_no": "17",
            "question_no": f"17({index})",
            "subquestion_no": str(index),
            "question": "已知函数 f(x)=x^2，求下列各式",
            "question_type": "solve",
            "standard_answer": "完整解答",
            "student_answer": f"作答{index}",
            "result": result,
        }
        for index, result in enumerate(results, start=1)
    ]


def test_top_level_question_is_pending_until_every_subquestion_is_correct():
    db, student = _session()
    _persist(db, student, image_hash="a" * 64, details=_parts("correct", "wrong"))

    attempts = db.query(QuestionAttemptEvent).all()
    cycles = db.query(WrongQuestionCycle).all()
    assert len(attempts) == 1
    assert attempts[0].question_no == "17"
    assert attempts[0].final_result == "partial"
    assert len(cycles) == 1 and cycles[0].status == "pending"

    _persist(db, student, image_hash="b" * 64, details=_parts("correct", "correct"), purpose="correction")
    db.refresh(cycles[0])
    assert cycles[0].status == "corrected"
    assert cycles[0].close_method == "fingerprint"
    assert db.query(CorrectionEvidence).count() == 1


def test_same_question_wrong_again_opens_a_new_cycle_and_duplicate_is_idempotent():
    db, student = _session()
    first = _persist(db, student, image_hash="c" * 64, details=_parts("wrong", "wrong"))
    duplicate = _persist(db, student, image_hash="c" * 64, details=_parts("wrong", "wrong"))
    assert duplicate.id == first.id
    assert db.query(QuestionAttemptEvent).count() == 1

    _persist(db, student, image_hash="d" * 64, details=_parts("correct", "correct"))
    _persist(db, student, image_hash="e" * 64, details=_parts("wrong", "correct"))
    cycles = db.query(WrongQuestionCycle).order_by(WrongQuestionCycle.cycle_no).all()
    assert [(cycle.cycle_no, cycle.status) for cycle in cycles] == [(1, "corrected"), (2, "pending")]


def test_new_wrong_attempt_clears_stale_manual_knowledge_correction():
    db, student = _session()
    knowledge = [{"chapter": "函数", "topic": "单调性"}] * 2
    _persist(
        db,
        student,
        image_hash="ca" * 32,
        details=_parts("wrong", "wrong"),
        knowledge_items=knowledge,
    )
    _persist(
        db,
        student,
        image_hash="cb" * 32,
        details=_parts("correct", "correct"),
        knowledge_items=knowledge,
    )
    db.add(WeaknessOverride(
        student_id=student.id,
        teacher_id=1,
        topic_key="函数||单调性",
        status="corrected",
    ))
    db.commit()

    _persist(
        db,
        student,
        image_hash="cc" * 32,
        details=_parts("wrong", "correct"),
        knowledge_items=knowledge,
    )

    assert db.query(WeaknessOverride).count() == 0
    assert weakness_list(db, student.id)[0]["status"] == "pending"
    cycles = db.query(WrongQuestionCycle).order_by(WrongQuestionCycle.cycle_no).all()
    assert [(cycle.cycle_no, cycle.status) for cycle in cycles] == [
        (1, "corrected"),
        (2, "pending"),
    ]


def test_needs_review_does_not_change_pending_cycle():
    db, student = _session()
    _persist(db, student, image_hash="f" * 64, details=_parts("wrong", "wrong"))
    cycle = db.query(WrongQuestionCycle).one()
    _persist(db, student, image_hash="1" * 64, details=_parts("needs_review", "correct"))
    db.refresh(cycle)
    assert cycle.status == "pending"
    assert cycle.wrong_count == 1


def test_observation_snapshot_keeps_result_but_emits_no_analytics_or_correction():
    db, student = _session()
    period = StudentObservationPeriod(
        student_id=student.id,
        class_id=student.class_id,
        teacher_id=1,
        started_at=datetime.now() - timedelta(minutes=1),
        reason="个别观察",
        created_by=1,
    )
    db.add(period)
    db.commit()

    result = _persist(db, student, image_hash="2" * 64, details=_parts("wrong", "wrong"))
    assert result.analytics_included is False
    assert result.inclusion_reason == "observation_mode"
    assert result.observation_period_id == period.id
    assert db.query(StudentProfile).one().analytics_included is False
    assert db.query(KnowledgeEvent).count() == 0
    assert db.query(WrongQuestionCycle).count() == 0
    attempt = db.query(QuestionAttemptEvent).one()
    assert attempt.analytics_included is False

    period.ended_at = datetime.now()
    period.ended_by = 1
    db.commit()
    next_result = _persist(db, student, image_hash="3" * 64, details=_parts("wrong", "wrong"))
    assert next_result.analytics_included is True
    assert db.query(WrongQuestionCycle).count() == 1


def test_observation_interval_lookup_does_not_reclassify_old_or_future_results():
    db, student = _session()
    start = datetime.now()
    period = StudentObservationPeriod(
        student_id=student.id,
        class_id=student.class_id,
        teacher_id=1,
        started_at=start,
        ended_at=start + timedelta(hours=1),
        reason="区间观察",
        created_by=1,
        ended_by=1,
    )
    db.add(period)
    db.commit()
    assert active_observation_period(db, student.id, start - timedelta(seconds=1)) is None
    assert active_observation_period(db, student.id, start + timedelta(minutes=30)).id == period.id
    assert active_observation_period(db, student.id, start + timedelta(hours=1)) is None


def test_fingerprint_ignores_numbering_but_not_math_changes():
    base = {
        "question_no": "1",
        "question_text": "求 x+1=0 的解。",
        "question_type": "solve",
        "standard_answer": "x=-1",
    }
    renumbered = {**base, "question_no": "19"}
    changed = {**base, "question_text": "求 x-1=0 的解。", "standard_answer": "x=1"}
    assert question_fingerprint(base) == question_fingerprint(renumbered)
    assert question_fingerprint(base) != question_fingerprint(changed)


def test_template_fill_fingerprint_survives_grading_projection():
    source = {
        "question_no": "8",
        "question_text": "分别填写两个值____；____",
        "question_type": "fill",
        "standard_answer": "2；3",
        "blank_count": 2,
        "answer_slots": ["2", "3"],
    }
    units = build_grading_units(
        [{"question_no": "8", "student_answer": "2；3"}],
        {"8": source},
        "daily",
    )
    judgments = {
        unit["key"]: {"result": "correct", "explanation": "", "hint": "", "error_type": ""}
        for unit in units
    }
    detail = aggregate_grading_result(units, judgments, "daily", "学生")["details"][0]
    snapshot, _answer = _combined_snapshot("8", [detail])
    assert question_fingerprint(source) == question_fingerprint(snapshot)


def test_solve_question_uses_one_grading_unit_per_answer_box():
    boxes = [[index * 100, 100, index * 100 + 80, 150] for index in range(8)]
    units = build_grading_units(
        [{"question_no": "1", "student_answer": "；".join(str(i) for i in range(1, 9))}],
        {"1": {
            "question_type": "solve",
            "question_text": "计算（共8个）",
            "standard_answer": "；".join(str(i) for i in range(1, 9)),
            "answer_bboxes": boxes,
            "answer_slots": [str(i) for i in range(1, 9)],
            "directory_node_id": "n001",
            "subquestion_count": 1,
        }},
        "daily",
    )

    assert len(units) == 8
    assert [unit["key"] for unit in units] == [f"1({i})" for i in range(1, 9)]
    assert [unit["answer_bboxes"] for unit in units] == [[box] for box in boxes]
    judgments = {
        unit["key"]: {"result": "wrong" if unit["key"] == "1(3)" else "correct"}
        for unit in units
    }
    result = aggregate_grading_result(units, judgments, "daily", "学生")
    assert result["total"] == 8
    assert result["correct_count"] == 7
    assert result["wrong_count"] == 1


def test_grading_detail_keeps_answer_box_for_review_mark_export(tmp_path):
    units = build_grading_units(
        [{"question_no": "1", "student_answer": "A", "answer_bbox": [100, 200, 300, 400]}],
        {"1": {"question_type": "choice", "question_text": "选择", "standard_answer": "A", "max_score": 1}},
        "exam",
    )
    details = aggregate_grading_result(
        units, {"1": {"result": "correct", "explanation": "", "hint": ""}}, "exam", "学生"
    )["details"]
    assert details[0]["answer_bboxes"] == [[100.0, 200.0, 300.0, 400.0]]

    from PIL import Image
    source = tmp_path / "paper.png"
    output = tmp_path / "review.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    assert render_review_marks(str(source), details, str(output)) == 1
    assert output.exists() and output.stat().st_size > source.stat().st_size


def test_folded_fill_marks_follow_each_grading_unit(tmp_path):
    boxes = [[100, 100, 180, 140], [220, 100, 300, 140]]
    units = build_grading_units(
        [{"question_no": "2", "student_answer": "1；9"}],
        {"2": {
            "question_type": "fill", "question_text": "填两个空：____；____",
            "standard_answer": "1；2", "answer_slots": ["1", "2"],
            "answer_bboxes": boxes,
        }},
        "daily",
    )
    details = aggregate_grading_result(
        units,
        {"2[1]": {"result": "correct"}, "2[2]": {"result": "wrong"}},
        "daily", "学生",
    )["details"]
    assert details[0]["result"] == "partial"
    assert [item["question_no"] for item in details[0]["grading_units"]] == ["2[1]", "2[2]"]
    from PIL import Image
    source = tmp_path / "fill.png"
    output = tmp_path / "fill-review.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    assert render_review_marks(str(source), details, str(output)) == 2


def test_review_mark_anchor_uses_bottom_edge_not_box_height(tmp_path):
    from PIL import Image

    source = tmp_path / "anchor.png"
    output = tmp_path / "anchor-review.png"
    Image.new("RGB", (1000, 1000), "white").save(source)
    details = [
        {"result": "wrong", "answer_bboxes": [[100, 100, 300, 600]]},
        {"result": "wrong", "answer_bboxes": [[500, 550, 700, 600]]},
    ]
    assert render_review_marks(str(source), details, str(output)) == 2

    image = Image.open(output).convert("RGB")

    def red_rows(left, right):
        rows = []
        for y in range(image.height):
            if any(
                image.getpixel((x, y))[0] > 150
                and image.getpixel((x, y))[1] < 110
                and image.getpixel((x, y))[2] < 110
                for x in range(left, right)
            ):
                rows.append(y)
        return min(rows), max(rows)

    # Equal bottom-edge lengths and equal bottoms produce equal marks even
    # though the first locator is much taller than the second one.
    assert red_rows(50, 350) == red_rows(450, 750)


def test_review_mark_keeps_student_coordinates_when_template_has_its_own_boxes():
    units = build_grading_units(
        [{"question_no": "1", "student_answer": "A", "answer_bbox": [300, 400, 350, 420]}],
        {"1": {"question_type": "choice", "standard_answer": "A", "max_score": 1,
               "answer_bboxes": [[100, 200, 150, 220]], "answer_box_source": "template"}},
        "exam",
    )
    details = aggregate_grading_result(units, {"1": {"result": "correct"}}, "exam", "学生")["details"]
    assert details[0]["answer_bboxes"] == [[300, 400, 350, 420]]
    assert details[0]["answer_box_source"] == "student"


def test_teacher_regrade_attempt_is_audited_but_does_not_close_cycle():
    db, student = _session()
    _persist(db, student, image_hash="a1" * 32, details=_parts("wrong", "wrong"))
    cycle = db.query(WrongQuestionCycle).one()

    details = _parts("correct", "correct")
    persist_grading_result(
        db,
        student=student,
        teacher_id=1,
        category="daily",
        exam_name="",
        image_path="",
        image_hash="b2" * 32,
        total_questions=2,
        correct_count=2,
        partial_count=0,
        wrong_count=0,
        needs_review_count=0,
        score=None,
        details=details,
        correction_evidence_allowed=False,
    )
    db.refresh(cycle)
    assert cycle.status == "pending"
    assert db.query(QuestionAttemptEvent).count() == 2
    assert db.query(CorrectionEvidence).count() == 0


def test_teacher_authorized_similar_only_closes_target_after_correct_attempt():
    db, student = _session()
    _persist(db, student, image_hash="4" * 64, details=_parts("wrong", "wrong"))
    cycle = db.query(WrongQuestionCycle).one()
    source = _parts("wrong", "wrong")
    for row in source:
        row["question"] = "相似但参数不同：已知函数 f(x)=x^2+1"
        row["standard_answer"] = "另一完整解答"
    fingerprint = question_fingerprint({**source[0], "question_text": source[0]["question"], "subquestion_count": 2})
    db.add(TemplateQuestionAuthorization(
        teacher_id=1,
        template_id="template-1",
        source_question_fingerprint=fingerprint,
        target_cycle_id=cycle.id,
        authorized_by=1,
    ))
    db.add(QuestionCorrectionRelation(
        teacher_id=1,
        target_cycle_id=cycle.id,
        source_question_identity="template:template-1:17",
        source_question_fingerprint=fingerprint,
        relation_type="teacher_authorized_similar",
        authorized_by=1,
    ))
    db.commit()

    _persist(db, student, image_hash="5" * 64, details=source)
    db.refresh(cycle)
    assert cycle.status == "pending"

    for row in source:
        row["result"] = "correct"
    _persist(db, student, image_hash="6" * 64, details=source)
    db.refresh(cycle)
    assert cycle.status == "corrected"
    assert cycle.close_method == "teacher_authorized_similar"
    relation = db.query(QuestionCorrectionRelation).filter(
        QuestionCorrectionRelation.target_cycle_id == cycle.id,
    ).one()
    assert relation.authorized_by == 1


def test_teacher_authorized_similar_cannot_close_another_students_cycle():
    db, student = _session()
    other = Student(id=2, teacher_id=1, class_id=1, name="另一学生", student_no="002")
    db.add(other)
    db.commit()
    _persist(db, other, image_hash="7" * 64, details=_parts("wrong", "wrong"))
    target = db.query(WrongQuestionCycle).filter(WrongQuestionCycle.student_id == other.id).one()

    source = _parts("correct", "correct")
    for row in source:
        row["question"] = "教师授权来源题"
        row["standard_answer"] = "授权来源答案"
    fingerprint = question_fingerprint({**source[0], "question_text": source[0]["question"], "subquestion_count": 2})
    db.add(TemplateQuestionAuthorization(
        teacher_id=1,
        template_id="template-2",
        source_question_fingerprint=fingerprint,
        target_cycle_id=target.id,
        authorized_by=1,
    ))
    db.commit()

    _persist(db, student, image_hash="8" * 64, details=source)
    db.refresh(target)
    assert target.status == "pending"


def test_template_candidates_restore_saved_authorization():
    db, student = _session()
    _persist(db, student, image_hash="9" * 64, details=_parts("wrong", "wrong"))
    cycle = db.query(WrongQuestionCycle).one()
    question = {
        "question_no": "17",
        "question_text": "已知函数 f(x)=x^2，求下列各式",
        "question_type": "solve",
        "standard_answer": "完整解答",
        "subquestion_count": 2,
    }
    fingerprint = question_fingerprint(question)
    assert fingerprint == cycle.question_fingerprint
    db.add(TemplateQuestionAuthorization(
        teacher_id=1,
        template_id="template-restore",
        source_question_fingerprint=fingerprint,
        target_cycle_id=cycle.id,
        authorized_by=1,
    ))
    db.commit()

    rows = template_candidates(
        db,
        teacher_id=1,
        class_id=1,
        questions=[question],
    )
    assert rows[0]["candidates"][0]["already_authorized"] is True


def test_blank_opens_cycle_and_is_visible_as_uncategorized_knowledge_weakness():
    db, student = _session()
    details = [{
        "question_no": "1",
        "question": "求函数的定义域",
        "question_type": "solve",
        "standard_answer": "x > 0",
        "student_answer": "",
        "result": "blank",
    }]
    _persist(
        db,
        student,
        image_hash="ab" * 32,
        details=details,
        knowledge_items=[{"chapter": "函数", "topic": "定义域", "difficulty": 2}],
    )

    attempt = db.query(QuestionAttemptEvent).one()
    assert attempt.question_snapshot()["knowledge_point"] == "定义域"
    assert db.query(WrongQuestionCycle).one().status == "pending"
    assert weakness_list(db, student.id)[0]["topic"] == "定义域"
    assert weakness_list(db, student.id)[0]["status"] == "pending"
    assert error_distribution(db, student.id) == []


def test_template_candidates_only_summarize_current_class_knowledge_point():
    db, first_student = _session()
    second_student = Student(
        id=2, teacher_id=1, class_id=1, name="乙同学", student_no="002"
    )
    other_class = TeacherClass(id=2, teacher_id=1, class_name="二班")
    outside_student = Student(
        id=3, teacher_id=1, class_id=2, name="外班同学", student_no="003"
    )
    db.add_all([second_student, other_class, outside_student])
    db.commit()

    for index, target_student in enumerate(
        (first_student, second_student, outside_student), start=1
    ):
        _persist(
            db,
            target_student,
            image_hash=f"{index + 2:02x}" * 32,
            details=[{
                "question_no": str(index),
                "question": f"历史题目 {index}，题干完全不同",
                "question_type": "solve",
                "standard_answer": f"答案 {index}",
                "student_answer": "错误",
                "result": "wrong",
            }],
            knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
        )

    rows = template_candidates(
        db,
        teacher_id=1,
        class_id=1,
        questions=[{
            "question_no": "9",
            "question_text": "判断一个新函数在给定区间上的增减趋势",
            "question_type": "solve",
            "standard_answer": "递增",
            "chapter": "函数",
            "topic": "单调性",
        }],
    )
    # A different question on the same point is not a retest of any one record.
    assert rows[0]["candidates"] == []
    assert rows[0]["knowledge_summary"] == {
        "chapter": "函数", "topic": "单调性", "student_count": 2, "record_count": 2,
    }


def test_template_candidates_list_each_students_same_and_similar_records():
    db, first_student = _session()
    second_student = Student(id=2, teacher_id=1, class_id=1, name="乙同学", student_no="002")
    other_class = TeacherClass(id=2, teacher_id=1, class_name="二班")
    outside_student = Student(id=3, teacher_id=1, class_id=2, name="外班同学", student_no="003")
    db.add_all([second_student, other_class, outside_student])
    db.commit()
    original = {
        "question_no": "5",
        "question": "已知函数 f(x)=2x+1，求 f(3) 的值",
        "question_type": "fill",
        "standard_answer": "7",
        "student_answer": "6",
        "result": "wrong",
    }
    for index, target_student in enumerate((first_student, second_student, outside_student), start=1):
        _persist(db, target_student, image_hash=f"e{index}" * 32, details=[original])
    # Same wording, different answer: similar, not the same original.
    variant = {**original, "question_no": "6", "standard_answer": "9"}
    _persist(db, first_student, image_hash="e4" * 32, details=[variant])

    rows = template_candidates(
        db,
        teacher_id=1,
        class_id=1,
        questions=[{
            "question_no": "2",
            "question_text": original["question"],
            "question_type": "fill",
            "standard_answer": "7",
        }],
    )
    candidates = rows[0]["candidates"]
    assert [(item["student_id"], item["relation"]) for item in candidates] == [
        (1, "same_original"), (2, "same_original"), (1, "similar_candidate"),
    ]
    assert {item["student_name"] for item in candidates} == {"学生", "乙同学"}
    assert all(item["historical_grading_result_id"] for item in candidates)
    assert candidates[-1]["similarity_score"] == 1.0


def test_one_authorized_correct_attempt_closes_only_that_students_cycle():
    db, first_student = _session()
    second_student = Student(
        id=2, teacher_id=1, class_id=1, name="乙同学", student_no="002"
    )
    db.add(second_student)
    db.commit()
    history = [{
        "question_no": "1",
        "question": "历史单调性错题",
        "question_type": "solve",
        "standard_answer": "递增",
        "student_answer": "递减",
        "result": "wrong",
    }]
    for index, target_student in enumerate((first_student, second_student), start=1):
        _persist(
            db,
            target_student,
            image_hash=f"c{index}" * 32,
            details=history,
            knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
        )
    cycles = db.query(WrongQuestionCycle).order_by(WrongQuestionCycle.student_id).all()

    current = [{
        "question_no": "8",
        "question": "新的单调性检验题",
        "question_type": "solve",
        "standard_answer": "递增",
        "student_answer": "递增",
        "result": "correct",
    }]
    fingerprint = question_fingerprint(current[0])
    for cycle in cycles:
        db.add(QuestionCorrectionRelation(
            teacher_id=1,
            target_cycle_id=cycle.id,
            source_question_identity="template:new:8",
            source_question_fingerprint=fingerprint,
            relation_type="teacher_authorized_similar",
            authorized_by=1,
        ))
    db.commit()

    _persist(
        db,
        first_student,
        image_hash="d1" * 32,
        details=current,
        knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
    )
    db.refresh(cycles[0])
    db.refresh(cycles[1])
    assert cycles[0].status == "corrected"
    assert cycles[0].close_method == "teacher_authorized_similar"
    assert cycles[1].status == "pending"
    assert weakness_list(db, first_student.id)[0]["status"] == "corrected"
    assert weakness_list(db, second_student.id)[0]["status"] == "pending"


def test_archiving_correct_answers_loads_none_of_the_students_open_records():
    from services.teaching_context import make_teaching_context

    senior = make_teaching_context("senior")
    db, student = _session()
    history = [{
        "question_no": str(index),
        "question": f"历史错题 {index}",
        "question_type": "solve",
        "standard_answer": "答案",
        "student_answer": "错",
        "result": "wrong",
    } for index in range(1, 31)]
    _persist(db, student, image_hash="a1" * 32, details=history, teaching_context=senior)
    assert db.query(WrongQuestionCycle).filter_by(status="pending").count() == 30

    fresh = sessionmaker(bind=db.get_bind())()
    loaded = []

    def count_load(target, _context):
        loaded.append(target.id)

    event.listen(WrongQuestionCycle, "load", count_load)
    try:
        _persist(fresh, fresh.get(Student, student.id), image_hash="a2" * 32, details=[{
            "question_no": str(index),
            "question": f"新题 {index}",
            "question_type": "solve",
            "standard_answer": "答案",
            "student_answer": "答案",
            "result": "correct",
        } for index in range(1, 4)], teaching_context=senior)
    finally:
        event.remove(WrongQuestionCycle, "load", count_load)
    assert loaded == []


def _discovery_cost(student_count: int) -> tuple[int, int, int]:
    """(SQL statements, snapshots loaded, candidates) for one template question."""
    db, _first = _session()
    db.add_all([
        Student(id=index, teacher_id=1, class_id=1, name=f"学生{index}", student_no=f"{index:03d}")
        for index in range(2, student_count + 1)
    ])
    db.commit()
    history = [
        {"question_no": "1", "question": "已知函数 f(x)=x+1，求 f(2)", "question_type": "fill",
         "standard_answer": "3", "student_answer": "2", "result": "wrong"},
        {"question_no": "2", "question": "解方程 2x=6", "question_type": "fill",
         "standard_answer": "3", "student_answer": "2", "result": "wrong"},
    ]
    knowledge = [{"chapter": "函数", "topic": "函数值"}, {"chapter": "方程", "topic": "一元一次方程"}]
    for student in db.query(Student).all():
        _persist(db, student, image_hash=f"{student.id:064x}", details=history, knowledge_items=knowledge)

    fresh = sessionmaker(bind=db.get_bind())()
    statements, snapshots = [], []

    def count_statement(*_args):
        statements.append(1)

    def count_snapshot(target, _context):
        snapshots.append(target.id)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count_statement)
    event.listen(QuestionAttemptEvent, "load", count_snapshot)
    try:
        rows = template_candidates(fresh, teacher_id=1, class_id=1, questions=[{
            "question_no": "9",
            "question_text": history[0]["question"],
            "question_type": "fill",
            "standard_answer": "3",
        }])
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
        event.remove(QuestionAttemptEvent, "load", count_snapshot)
    return len(statements), len(snapshots), len(rows[0]["candidates"])


def test_template_discovery_cost_does_not_grow_with_class_size():
    small, large = _discovery_cost(3), _discovery_cost(12)
    assert small[0] == large[0]
    # One snapshot per distinct question, however many students got it wrong.
    assert small[1] == large[1] == 2
    assert (small[2], large[2]) == (3, 12)


def test_older_knowledge_point_link_no_longer_closes_a_record():
    db, student = _session()
    _persist(db, student, image_hash="f1" * 32, details=[{
        "question_no": "1",
        "question": "历史单调性错题",
        "question_type": "solve",
        "standard_answer": "递增",
        "student_answer": "递减",
        "result": "wrong",
    }])
    cycle = db.query(WrongQuestionCycle).one()
    current = [{
        "question_no": "8",
        "question": "另一道单调性题",
        "question_type": "solve",
        "standard_answer": "递减",
        "student_answer": "递减",
        "result": "correct",
    }]
    db.add(QuestionCorrectionRelation(
        teacher_id=1,
        target_cycle_id=cycle.id,
        source_question_identity="template:old:8",
        source_question_fingerprint=question_fingerprint(current[0]),
        relation_type="teacher_authorized_knowledge",
        authorized_by=1,
    ))
    db.commit()

    _persist(db, student, image_hash="f2" * 32, details=current)
    db.refresh(cycle)
    assert cycle.status == "pending"


def _legacy_result(db, student, details, *, analytics_included=True, total_questions=None):
    result = GradingResult(
        student_id=student.id,
        category="daily",
        exam_name="历史批改",
        total_questions=total_questions if total_questions is not None else len(details),
        wrong_count=sum(1 for row in details if row.get("result") in {"wrong", "错误", "blank", "空白"}),
        grading_details=json.dumps(details, ensure_ascii=False),
        analytics_included=analytics_included,
        inclusion_reason="included" if analytics_included else "observation_mode",
    )
    db.add(result)
    db.commit()
    return result


def _simple_details(*results):
    return [
        {
            "question_no": str(index),
            "question": f"题目 {index}",
            "question_type": "solve",
            "standard_answer": "答案",
            "student_answer": "作答",
            "result": result,
        }
        for index, result in enumerate(results, start=1)
    ]


def test_repair_normalizes_legacy_statuses_and_is_idempotent():
    db, student = _session()
    result = _legacy_result(db, student, _simple_details("错误", "正确", "空白"))

    first = repair_grading_result_projection(db, grading_result=result)
    assert first["status"] == "repaired"
    assert first["created_attempts"] == 3
    assert first["declared_total_questions"] == 3
    assert first["declared_wrong_count"] == 2
    assert first["cycle_statuses"] == ["pending"]
    assert [row.final_result for row in db.query(QuestionAttemptEvent).order_by(QuestionAttemptEvent.id)] == [
        "wrong", "correct", "blank",
    ]
    assert db.query(WrongQuestionCycle).count() == 2

    second = repair_grading_result_projection(db, grading_result=result)
    assert second["status"] == "already_complete"
    assert second["created_attempts"] == 0
    assert db.query(QuestionAttemptEvent).count() == 3
    assert db.query(WrongQuestionCycle).count() == 2


def test_repair_fills_partial_attempt_projection_and_missing_cycle():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-partial",
        details=_simple_details("wrong", "correct", "blank"),
    )
    # Simulate the old path: one event remains, but all derived cycles and the
    # other question events were lost.
    db.query(WrongQuestionCycle).delete(synchronize_session=False)
    db.query(QuestionAttemptEvent).filter(QuestionAttemptEvent.question_no != "1").delete(
        synchronize_session=False,
    )
    db.commit()

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "repaired"
    assert report["created_attempts"] == 2
    assert db.query(QuestionAttemptEvent).count() == 3
    assert db.query(WrongQuestionCycle).count() == 2

    audit = audit_grading_result_projection(db, grading_result=result)
    assert audit["projection_complete"] is True
    assert audit["attempt_count"] == 3


def test_audit_detects_missing_cycles_even_when_every_attempt_exists():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-no-cycles",
        details=_simple_details("wrong", "wrong", "wrong"),
    )
    db.query(WrongQuestionCycle).delete(synchronize_session=False)
    db.commit()

    audit = audit_grading_result_projection(db, grading_result=result)
    assert audit["attempt_count"] == 3
    assert audit["cycle_count"] == 0
    assert audit["cycle_statuses"] == []
    assert audit["wrong_attempts_missing_cycle"] == 3
    assert audit["projection_complete"] is False

    dry_run = repair_grading_result_projection(db, grading_result=result, dry_run=True)
    assert dry_run["status"] == "would_repair"
    assert dry_run["would_create_attempts"] == 0
    assert dry_run["would_repair_cycles"] == 3

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "repaired"
    assert db.query(WrongQuestionCycle).count() == 3


def test_all_correct_attempts_are_complete_without_wrong_cycles():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-all-correct",
        details=_simple_details("correct", "correct"),
    )
    audit = audit_grading_result_projection(db, grading_result=result)
    assert audit["projection_complete"] is True
    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "already_complete"


def test_repair_does_not_resurrect_a_teacher_voided_cycle():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-voided-cycle",
        details=_simple_details("wrong"),
    )
    cycle = db.query(WrongQuestionCycle).one()
    cycle.status = "voided"
    cycle.wrong_count = 0
    db.commit()

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "already_complete"
    db.refresh(cycle)
    assert cycle.status == "voided"
    assert db.query(WrongQuestionCycle).count() == 1


def test_repair_restores_inconsistent_excluded_event_for_formal_result():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-excluded-event",
        details=_simple_details("wrong"),
    )
    attempt = db.query(QuestionAttemptEvent).one()
    cycle = db.query(WrongQuestionCycle).one()
    db.delete(cycle)
    attempt.analytics_included = False
    attempt.inclusion_reason = "legacy_path"
    db.commit()

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "repaired"
    db.refresh(attempt)
    assert attempt.analytics_included is True
    assert attempt.inclusion_reason == "included"
    assert db.query(WrongQuestionCycle).count() == 1


def test_repair_reconciles_attempt_result_with_grading_details():
    db, student = _session()
    result = _persist(
        db,
        student,
        image_hash="legacy-result-mismatch",
        details=_simple_details("wrong"),
    )
    attempt = db.query(QuestionAttemptEvent).one()
    db.query(WrongQuestionCycle).delete(synchronize_session=False)
    attempt.final_result = "correct"
    db.commit()

    audit = audit_grading_result_projection(db, grading_result=result)
    assert audit["result_mismatches"] == 1
    assert audit["projection_complete"] is False

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "repaired"
    db.refresh(attempt)
    assert attempt.final_result == "wrong"
    assert attempt.question_snapshot().get("teacher_regraded") is not True
    assert db.query(WrongQuestionCycle).count() == 1


def test_repair_flushes_repeated_question_fingerprints_between_existing_events():
    db, student = _session()
    details = [
        {
            "question_no": str(index),
            "question": "同一指纹题",
            "question_type": "solve",
            "standard_answer": "同一答案",
            "student_answer": "同一作答",
            "result": "wrong",
        }
        for index in range(1, 4)
    ]
    result = _persist(db, student, image_hash="legacy-repeated-fingerprint", details=details)
    db.query(WrongQuestionCycle).delete(synchronize_session=False)
    db.commit()

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "repaired"
    cycles = db.query(WrongQuestionCycle).all()
    assert len(cycles) == 1
    assert cycles[0].wrong_count == 3


def test_repair_does_not_fabricate_questions_from_summary_only():
    db, student = _session()
    result = _legacy_result(db, student, [], total_questions=25)
    result.wrong_count = 25
    db.commit()

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_details"
    assert db.query(QuestionAttemptEvent).count() == 0
    assert db.query(WrongQuestionCycle).count() == 0

    summary = repair_missing_question_projections(db, student_id=student.id, dry_run=True)
    assert summary["scanned"] == 1
    assert summary["skipped"] == 1


def test_repair_does_not_infer_question_numbers_from_row_position():
    db, student = _session()
    result = _legacy_result(
        db,
        student,
        [{"question": "没有题号", "result": "错误"}],
        total_questions=1,
    )

    audit = audit_grading_result_projection(db, grading_result=result)
    assert audit["projection_complete"] is False
    assert audit["repairable"] is False
    assert audit["skip_reason"] == "missing_question_number"

    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_question_number"
    assert db.query(QuestionAttemptEvent).count() == 0
    assert db.query(WrongQuestionCycle).count() == 0


def test_repair_skips_observation_results():
    db, student = _session()
    result = _legacy_result(
        db,
        student,
        _simple_details("错误"),
        analytics_included=False,
    )
    report = repair_grading_result_projection(db, grading_result=result)
    assert report["status"] == "skipped"
    assert report["skip_reason"] == "analytics_excluded"
    assert db.query(QuestionAttemptEvent).count() == 0


def test_teacher_regrade_keeps_old_verdict_until_cycle_reversal():
    db, student = _session()
    _persist(db, student, image_hash="teacher-old", details=_simple_details("wrong"))
    result = _persist(db, student, image_hash="teacher-new", details=_simple_details("correct"))
    previous = copy.deepcopy(result.get_details())
    details = result.get_details()
    details[0]["result"] = "wrong"
    result.set_details(details)

    synchronize_teacher_confirmation(
        db,
        student=student,
        teacher_id=1,
        grading_result=result,
        previous_details=previous,
        details=details,
        detail_index=0,
    )
    db.commit()

    attempt = db.query(QuestionAttemptEvent).filter(
        QuestionAttemptEvent.grading_result_id == result.id,
    ).one()
    assert attempt.final_result == "wrong"
    assert db.query(WrongQuestionCycle).filter(WrongQuestionCycle.status == "pending").count() == 1
