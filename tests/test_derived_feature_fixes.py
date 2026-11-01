"""Regression tests for derived (non-grading) features: class roster/score
imports, risk labels, class report, summary ranking, parent letters."""

import io
from datetime import datetime, timedelta

from openpyxl import Workbook

from models import ExamScore, KnowledgeEvent, ParentLetterShare, Student, StudentProfile
from routers.class_mgmt import _find_cols, _ROSTER_COLUMNS, _ROSTER_EXCLUDE, _SCORE_COLUMNS, _SCORE_EXCLUDE
from services.report_templates import _letter_body_lines
from tests.test_class_module import T1, client_as  # noqa: F401


def _xlsx(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _upload(client, path, rows, **data):
    return client.post(path, files={"file": ("x.xlsx", _xlsx(rows))}, data=data)


# ── column matching ──

def test_parent_name_column_is_not_taken_as_phone_or_student_name():
    cols = _find_cols(["家长姓名", "学号", "姓名", "家长电话"], _ROSTER_COLUMNS, _ROSTER_EXCLUDE)
    assert cols == {"no": 1, "name": 2, "phone": 3}


def test_exam_score_header_is_only_the_score_column():
    cols = _find_cols(["学号", "姓名", "考试成绩"], _SCORE_COLUMNS, _SCORE_EXCLUDE)
    assert cols == {"no": 0, "name": 1, "score": 2}


# ── roster import ──

def test_duplicate_student_number_inside_one_file_is_reported_not_crashing(client_as):
    mk, Session = client_as
    response = _upload(mk(T1), "/api/class/1/students/import-excel",
                       [["学号", "姓名"], ["100", "甲"], ["100", "乙"]])
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 1 and body["skipped"] == 1
    assert "与第2行重复" in body["errors"][0]
    db = Session()
    try:
        imported = db.query(Student).filter(Student.student_no == "100").one()
        assert imported.name == "甲"
        assert imported.class_name == "24级数控1班"
    finally:
        db.close()


def test_roster_dry_run_previews_without_writing(client_as):
    mk, Session = client_as
    response = _upload(mk(T1), "/api/class/1/students/import-excel",
                       [["学号", "姓名", "家长姓名", "家长电话"], ["101", "丙", "丙父", "13800000000"],
                        ["001", "重名学号", "", ""]],
                       dry_run="true")
    body = response.json()
    assert body["dry_run"] is True and body["added"] == 1 and body["skipped"] == 1
    assert body["columns"] == {"no": "学号", "name": "姓名", "phone": "家长电话"}
    assert [row["status"] for row in body["preview"]] == ["add", "skip"]
    assert body["preview"][0]["phone"] == "13800000000"
    db = Session()
    try:
        assert db.query(Student).filter(Student.student_no == "101").count() == 0
    finally:
        db.close()


def test_manual_batch_with_repeated_number_keeps_first(client_as):
    mk, _Session = client_as
    response = mk(T1).post("/api/class/1/students", json={"students": [
        {"student_no": "300", "name": "甲"}, {"student_no": "300", "name": "乙"},
    ]})
    assert response.status_code == 200
    assert response.json()["added"] == 1 and response.json()["skipped"] == 1


def test_editing_to_an_existing_student_number_is_a_conflict(client_as):
    mk, _Session = client_as
    client = mk(T1)
    response = client.put("/api/class/1/students/11", json={"student_no": "001"})
    assert response.status_code == 409
    assert client.put("/api/class/1/students/11", json={"name": "   "}).status_code == 422


# ── score import ──

def test_score_import_refuses_ambiguous_names_and_reports_unreadable_scores(client_as):
    mk, Session = client_as
    db = Session()
    db.add(Student(id=60, teacher_id=T1, class_id=1, name="张三", student_no="060"))
    db.commit()
    db.close()
    body = _upload(mk(T1), "/api/class/1/exam-scores/import",
                   [["姓名", "成绩"], ["张三", 99], ["李四", "缺考"]],
                   exam_name="测验X", exam_date="2026-09-15").json()
    assert body["matched"] == 0
    assert "同名" in body["unmatched"][0]
    assert "缺考" in body["skipped"][0]


def test_score_import_merges_repeated_rows_and_checks_number_name_agreement(client_as):
    mk, Session = client_as
    body = _upload(mk(T1), "/api/class/1/exam-scores/import",
                   [["学号", "姓名", "成绩"], ["002", "李四", 70], ["002", "李四", "75分"], ["001", "李四", 88]],
                   exam_name="测验Y", exam_date="2026-09-15").json()
    assert body["matched"] == 1
    assert len(body["duplicates"]) == 1
    assert "不一致" in body["unmatched"][0]
    db = Session()
    try:
        rows = db.query(ExamScore).filter(ExamScore.exam_name == "测验Y").all()
        assert [(row.student_id, row.score, str(row.exam_date)) for row in rows] == [(11, 75.0, "2026-09-15")]
    finally:
        db.close()


def test_score_dry_run_does_not_write(client_as):
    mk, Session = client_as
    body = _upload(mk(T1), "/api/class/1/exam-scores/import",
                   [["学号", "成绩"], ["001", 90]], exam_name="预览", dry_run="true").json()
    assert body["dry_run"] is True and body["matched"] == 1
    db = Session()
    try:
        assert db.query(ExamScore).filter(ExamScore.exam_name == "预览").count() == 0
    finally:
        db.close()


# ── risk, weakness override, class report ──

def test_student_without_records_is_no_data_not_low_correction(client_as):
    mk, Session = client_as
    db = Session()
    db.add(Student(id=50, teacher_id=T1, class_id=1, name="新生", student_no="050"))
    db.add(Student(id=51, teacher_id=T1, class_id=1, name="全对", student_no="051"))
    event = KnowledgeEvent(student_id=51, class_id=1, teacher_id=T1, grading_result_id=900,
                           chapter="函数", topic="定义域", result="correct")
    event.created_at = datetime(2026, 9, 1)
    db.add(event)
    db.commit()
    db.close()
    rows = {row["name"]: row for row in mk(T1).get("/api/class/1/students").json()["students"]}
    assert rows["新生"]["risk_level"] == "no_data"
    assert rows["新生"]["has_records"] is False
    assert rows["全对"]["risk_level"] == "stable"
    assert rows["全对"]["has_mistakes"] is False


def test_teacher_can_reopen_an_auto_corrected_weakness(client_as):
    mk, _Session = client_as
    client = mk(T1)
    [weakness] = client.get("/api/student/10/detail").json()["weaknesses"]
    assert weakness["status"] == "corrected" and weakness["manual"] is False
    key = weakness["topic_key"]

    reopened = client.post("/api/student/10/weakness/correct", params={"topic_key": key}, json={"status": "pending"})
    assert reopened.json()["manual"] is True
    [weakness] = client.get("/api/student/10/detail").json()["weaknesses"]
    assert weakness["status"] == "pending" and weakness["manual"] is True

    client.post("/api/student/10/weakness/correct", params={"topic_key": key}, json={"status": "corrected"})
    [weakness] = client.get("/api/student/10/detail").json()["weaknesses"]
    assert weakness["status"] == "corrected" and weakness["manual"] is False


def test_class_report_survives_observation_students(client_as):
    mk, _Session = client_as
    client = mk(T1)
    assert client.post("/api/class/1/students/11/observation",
                       json={"enabled": True, "reason": "转学过渡"}).status_code == 200
    response = client.post("/api/class/1/report")
    assert response.status_code == 200, response.text
    assert response.json()["download_url"].startswith("/api/download/")


# ── summary, letters ──

def test_summary_top_and_attention_lists_never_overlap(client_as):
    mk, Session = client_as
    db = Session()
    for student_id, correct in ((10, 9), (11, 3)):
        db.add(StudentProfile(student_id=student_id, category="daily", total_questions=10,
                              correct_count=correct, wrong_count=10 - correct,
                              analytics_included=True, created_at=datetime.now()))
    db.commit()
    db.close()
    body = mk(T1).post("/api/reports/summary", json={"class_id": 1, "period": "week"}).json()
    assert [row["name"] for row in body["top_students"]] == ["张三"]
    assert [row["name"] for row in body["weak_students"]] == ["李四"]
    assert body["summary"].startswith("近7天")


def test_parent_letter_never_names_placeholder_topics(client_as):
    _mk, Session = client_as
    db = Session()
    try:
        db.add(Student(id=70, teacher_id=T1, class_id=1, name="王五", student_no="070"))
        for index, result in enumerate(["wrong", "correct"]):
            event = KnowledgeEvent(student_id=70, class_id=1, teacher_id=T1, grading_result_id=700 + index,
                                   chapter="", topic="", result=result)
            event.created_at = datetime(2026, 9, 1) + timedelta(days=index)
            db.add(event)
        db.commit()
        paragraphs, rows = _letter_body_lines(db, db.get(Student, 70))
        assert not any("未分类" in paragraph for paragraph in paragraphs)
        assert any("暂未出现需要重点巩固" in paragraph for paragraph in paragraphs)
        assert rows == []
    finally:
        db.close()


def test_reused_letter_link_gets_the_new_validity_and_flags_empty_letters(client_as):
    mk, Session = client_as
    client = mk(T1)
    db = Session()
    db.add(Student(id=80, teacher_id=T1, class_id=1, name="空白", student_no="080"))
    db.add(KnowledgeEvent(student_id=10, class_id=1, teacher_id=T1, grading_result_id=990,
                          chapter="函数", topic="定义域", result="correct", education_stage="senior"))
    db.commit()
    db.close()
    first = client.post("/api/class/1/report/batch", json={
        "type": "parent_letter", "lock": "token", "expire_days": 1,
    }).json()
    # Fixture events for 李四 are unlabeled legacy rows, so his senior-scope letter is empty too.
    assert first["empty_content"] == ["李四", "空白"]
    second = client.post("/api/class/1/report/batch", json={
        "type": "parent_letter", "lock": "token", "expire_days": 30,
    }).json()
    assert second["reused"] == 3 and second["extended"] == 3
    db = Session()
    try:
        share = db.query(ParentLetterShare).filter(ParentLetterShare.student_id == 10).one()
        assert share.expires_at > datetime.now() + timedelta(days=29)
    finally:
        db.close()


def test_grade_records_page_reports_the_real_total(client_as):
    from models import GradingResult
    from models_user import User
    from routers.grading_records import list_grading_results

    _mk, Session = client_as
    db = Session()
    try:
        for category in ("daily", "daily", "daily", "exam"):
            db.add(GradingResult(student_id=10, category=category, total_questions=1))
        db.commit()
        teacher = User(id=T1, name="t", role="teacher")
        page = list_grading_results(student_id=None, page=2, page_size=2, category="daily", db=db, user=teacher)
        assert page["total"] == 3 and page["page"] == 2 and len(page["items"]) == 1
        assert isinstance(list_grading_results(student_id=None, db=db, user=teacher), list)
    finally:
        db.close()


def test_photo_import_dedupe_compares_the_whole_question():
    from routers.bank_import import _normalize_question

    stem = "如图，在平面直角坐标系中，" * 10
    assert _normalize_question(stem + "求点A的坐标。") != _normalize_question(stem + "求三角形面积。")
    assert _normalize_question("【例】1. 求 x （5分）") == _normalize_question("求x")


def test_long_chat_history_is_clipped_instead_of_rejected():
    import asyncio

    from routers.chat import ChatRequest
    from services.hermes_config import compress_history

    # A persisted reply longer than the per-item cap used to make every later
    # request fail validation (422) until the teacher cleared history.
    request = ChatRequest(message="继续", history=[{"role": "assistant", "content": "长" * 5000}])
    assert len(request.history[0].content) == 4000
    returned = asyncio.run(compress_history([{"role": "assistant", "content": "长" * 9000}]))
    assert len(returned[0]["content"]) == 4000
    assert ChatRequest(message="继续", history=returned).history[0].content == returned[0]["content"]
