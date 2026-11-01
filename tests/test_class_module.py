"""班级管理模块端到端联调测试。

用内存库造种子(老师+班级+学生+knowledge_events+月考)，串测契约第七/十节的全部端点，
验证：响应字段名一致、归属隔离、topic_key URL 编码、报告生成落库。
"""

from datetime import datetime, timedelta
import os
import zipfile
from io import BytesIO

from openpyxl import Workbook

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db
from models import (
    Student,
    KnowledgeEvent,
    QuestionBank,
    ExamScore,
    GradingResult,
    StudentProfile,
    Weakness,
)
from models_correction import StudentObservationPeriod
from models_user import User
from models_rate_limit import TeacherClass

T1, T2 = 1, 2  # 两个老师 id（验证越权隔离）


def _seed(session):
    """老师1：班级A(2学生，含批改事件+月考)；老师2：另一班(验证隔离)。"""
    db = session()
    db.add(User(id=T1, name="师一", role="teacher"))
    db.add(User(id=T2, name="师二", role="teacher"))
    cls = TeacherClass(id=1, teacher_id=T1, class_name="24级数控1班")
    other = TeacherClass(id=2, teacher_id=T2, class_name="别人的班")
    db.add_all([cls, other])
    db.flush()

    s1 = Student(id=10, teacher_id=T1, class_id=1, name="张三", student_no="001", exam_no="00010001")
    s2 = Student(id=11, teacher_id=T1, class_id=1, name="李四", student_no="002")
    db.add_all([s1, s2])
    db.flush()

    base = datetime(2026, 1, 1)
    # 张三：二次函数 错→对→对（应判已纠正）
    seq = [("wrong", "计算"), ("correct", ""), ("correct", "")]
    for i, (res, err) in enumerate(seq):
        e = KnowledgeEvent(student_id=10, class_id=1, teacher_id=T1, grading_result_id=i + 1,
                           chapter="代数与函数", topic="二次函数", result=res,
                           error_type=err, difficulty=3)
        e.created_at = base + timedelta(days=i)
        db.add(e)
    # 李四：三角函数 错→对→错（未纠正，概念错因）
    seq2 = [("wrong", "概念"), ("correct", ""), ("wrong", "概念")]
    for i, (res, err) in enumerate(seq2):
        e = KnowledgeEvent(student_id=11, class_id=1, teacher_id=T1, grading_result_id=i + 10,
                           chapter="三角函数", topic="正弦", result=res,
                           error_type=err, difficulty=3)
        e.created_at = base + timedelta(days=i)
        db.add(e)
    db.add(Weakness(
        student_id=11,
        chapter="三角函数",
        topic="正弦",
        subtopic="正弦",
        error_type="概念",
        occurrences=2,
        status="pending",
    ))
    # 月考成绩
    db.add(ExamScore(student_id=10, class_id=1, teacher_id=T1, exam_name="月考1", score=78))
    db.add(ExamScore(student_id=10, class_id=1, teacher_id=T1, exam_name="期中", score=86))
    db.commit()
    db.close()


@pytest.fixture
def client_as(tmp_path):
    """返回 (make_client, session)。make_client(teacher_id) 给出以该老师登录的 client。

    报告生成会落盘，重定向 generated_files.OUTPUT_DIR 到临时目录，避免污染真实 output/。
    """
    import services.generated_files as gf
    _orig_output = gf.OUTPUT_DIR
    gf.OUTPUT_DIR = str(tmp_path / "output")

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)  # 所有连接共享同一内存库
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _seed(Session)

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    from routers import class_mgmt, parent_letter, report, stats, student
    from routers.auth import _require_user

    app = FastAPI()
    app.include_router(student.router, prefix="/api")
    app.include_router(class_mgmt.router, prefix="/api")
    app.include_router(report.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(parent_letter.router, prefix="/api")
    app.dependency_overrides[get_db] = _override_db

    def make_client(teacher_id):
        app.dependency_overrides[_require_user] = lambda: User(id=teacher_id, name="t", role="teacher", education_stage="senior")
        return TestClient(app)

    yield make_client, Session

    gf.OUTPUT_DIR = _orig_output  # 还原，避免影响其他测试


def test_class_table_fields(client_as):
    """班级表格：字段名 + 指标正确性。"""
    mk, _ = client_as
    c = mk(T1)
    r = c.get("/api/class/1/students")
    assert r.status_code == 200
    data = r.json()
    assert data["class_name"] == "24级数控1班"
    rows = {s["name"]: s for s in data["students"]}
    assert rows["张三"]["class_id"] == 1
    assert rows["张三"]["class_name"] == "24级数控1班"
    # 字段名锁定（契约 7.2 + risk 后端化）
    for key in ("id", "student_no", "exam_no", "name", "wrong_rate", "correction_rate",
                "weakness_count", "latest_exam_score", "latest_exam_name",
                "risk_level", "risk_reason"):
        assert key in rows["张三"], f"缺字段 {key}"
    # 张三二次函数已纠正 → 纠错率 1.0，无未纠正薄弱
    assert rows["张三"]["correction_rate"] == 1.0
    assert rows["张三"]["weakness_count"] == 0
    assert rows["张三"]["latest_exam_score"] == 86  # 最近月考
    # 李四未纠正 → 纠错率 0，薄弱 1
    assert rows["李四"]["correction_rate"] == 0.0
    assert rows["李四"]["weakness_count"] == 1


def test_student_stats_returns_complete_current_page_contract(client_as):
    """统计页应拿到真实份数、题数和题目汇总，而不是因字段错位显示 0。"""
    mk, Session = client_as
    db = Session()
    db.add_all([
        GradingResult(
            student_id=10,
            category="daily",
            total_questions=4,
            correct_count=3,
            wrong_count=1,
            correct_rate=0.75,
            scoring_version=2,
            analytics_included=True,
        ),
        GradingResult(
            student_id=10,
            category="exam",
            exam_name="期末",
            total_questions=6,
            correct_count=3,
            partial_count=1,
            wrong_count=2,
            score=60,
            max_score=100,
            correct_rate=0.5,
            scoring_version=2,
            analytics_included=True,
        ),
        GradingResult(
            student_id=10,
            category="daily",
            total_questions=5,
            correct_count=5,
            correct_rate=1.0,
            scoring_version=2,
            analytics_included=False,
            inclusion_reason="observation_mode",
        ),
        GradingResult(
            student_id=10,
            category="daily",
            total_questions=3,
            correct_count=2,
            needs_review_count=1,
            scoring_version=2,
            analytics_included=True,
        ),
    ])
    db.commit()
    db.close()

    response = mk(T1).get("/api/stats/10")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["student"]["class_id"] == 1
    assert body["student"]["class_name"] == "24级数控1班"
    assert body["total_gradings"] == body["total_exams"] == 2
    assert body["total_questions"] == 10
    assert body["correct_count"] == 6
    assert body["partial_count"] == 1
    assert body["wrong_count"] == 3
    assert body["incorrect_count"] == 4
    assert body["accuracy"] == 0.6
    assert body["accuracy_percent"] == 60.0
    assert body["avg_score"] == 62.5
    assert body["average_correct_rate"] == 0.625
    assert body["error_distribution"] == []
    assert body["knowledge_distribution"][0]["topic"] == "二次函数"
    assert "error_distribution" not in body["knowledge_distribution"][0]
    assert "question_classification" in body


def test_stats_resolves_legacy_unknown_event_from_classified_question_bank(client_as):
    mk, Session = client_as
    db = Session()
    question = QuestionBank(
        teacher_id=T1,
        question="求函数定义域",
        answer="x>0",
        chapter="函数",
        topic="定义域",
    )
    db.add(question)
    db.flush()
    db.add(KnowledgeEvent(
        student_id=10,
        class_id=1,
        teacher_id=T1,
        question_bank_id=question.id,
        chapter="未知",
        topic="未知",
        result="wrong",
        error_type="审题",
    ))
    db.commit()
    db.close()

    distribution = mk(T1).get("/api/stats/10").json()["knowledge_distribution"]
    definition_domain = next(item for item in distribution if item["topic"] == "定义域")
    assert definition_domain["chapter"] == "函数"
    assert "error_distribution" not in definition_domain


def test_student_class_name_is_shared_across_list_stats_and_detail(client_as):
    """class_id 关系存在时，所有学生身份入口都使用同一个班级名称。"""
    mk, _ = client_as
    client = mk(T1)

    listed = client.get("/api/student/list").json()["students"]
    listed_student = next(item for item in listed if item["id"] == 10)
    filtered = client.get("/api/student/list", params={"class_name": "24级数控1班"}).json()
    assert {item["id"] for item in filtered["students"]} == {10, 11}
    stats_student = client.get("/api/stats/10").json()["student"]
    detail_student = client.get("/api/student/10/detail").json()["student"]
    table_student = next(
        item for item in client.get("/api/class/1/students").json()["students"]
        if item["id"] == 10
    )

    for item in (listed_student, stats_student, detail_student, table_student):
        assert item["class_id"] == 1
        assert item["class_name"] == "24级数控1班"


def test_unbinding_student_clears_legacy_class_name(client_as):
    """解绑后不能因旧兼容字段残留而继续显示原班级。"""
    mk, Session = client_as
    client = mk(T1)
    response = client.delete("/api/class/1/students/10")
    assert response.status_code == 200

    listed = client.get("/api/student/list").json()["students"]
    student = next(item for item in listed if item["id"] == 10)
    assert student["class_id"] is None
    assert student["class_name"] == ""

    db = Session()
    stored = db.get(Student, 10)
    assert stored.class_id is None
    assert stored.class_name == ""
    db.close()


def test_roster_image_uses_teacher_fairness_identity(client_as, monkeypatch):
    from services import ocr_service

    observed = {}

    async def fake_read_image(image, *_args, **kwargs):
        observed["image"] = image
        observed.update(kwargs)
        return '[{"student_no":"003","name":"test","phone":""}]'

    monkeypatch.setattr(ocr_service, "read_image", fake_read_image)
    make_client, _ = client_as

    response = make_client(T1).post(
        "/api/class/1/students/import-image",
        files={"file": ("roster.png", b"image", "image/png")},
    )

    assert response.status_code == 200
    assert observed["image"] == b"image"
    assert observed["teacher_id"] == str(T1)


def test_active_observation_student_is_excluded_from_risk_and_heatmap(client_as):
    mk, Session = client_as
    db = Session()
    db.add(StudentObservationPeriod(
        student_id=11,
        class_id=1,
        teacher_id=T1,
        reason="观察",
        started_at=datetime.now() - timedelta(minutes=1),
        created_by=T1,
    ))
    db.commit()
    db.close()

    client = mk(T1)
    table = client.get("/api/class/1/students").json()["students"]
    observed = next(row for row in table if row["id"] == 11)
    assert observed["observation_mode"] is True
    assert observed["risk_level"] == "observation"
    assert observed["wrong_rate"] is None
    assert observed["weakness_count"] is None

    heatmap = client.get("/api/class/1/heatmap").json()["tree"]
    assert all(
        "李四" not in topic["students"]
        for chapter in heatmap
        for topic in chapter["topics"]
    )


def test_reimporting_exam_score_does_not_reclassify_frozen_observation_snapshot(client_as):
    mk, Session = client_as
    db = Session()
    period = StudentObservationPeriod(
        student_id=10,
        class_id=1,
        teacher_id=T1,
        reason="观察",
        started_at=datetime.now() - timedelta(minutes=1),
        created_by=T1,
    )
    db.add(period)
    db.commit()
    period_id = period.id
    db.close()

    def workbook(score: float) -> BytesIO:
        wb = Workbook()
        ws = wb.active
        ws.append(["学号", "成绩"])
        ws.append(["001", score])
        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        return stream

    client = mk(T1)
    first = client.post(
        "/api/class/1/exam-scores/import",
        data={"exam_name": "观察月考"},
        files={"file": ("scores.xlsx", workbook(60), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert first.status_code == 200, first.text

    db = Session()
    period = db.get(StudentObservationPeriod, period_id)
    period.ended_at = datetime.now()
    period.ended_by = T1
    db.commit()
    db.close()

    second = client.post(
        "/api/class/1/exam-scores/import",
        data={"exam_name": "观察月考"},
        files={"file": ("scores.xlsx", workbook(88), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert second.status_code == 200, second.text
    db = Session()
    score = db.query(ExamScore).filter(ExamScore.exam_name == "观察月考").one()
    assert score.score == 88
    assert score.analytics_included is False
    assert score.inclusion_reason == "observation_mode"
    assert score.observation_period_id == period_id
    db.close()


def test_class_isolation(client_as):
    """越权隔离：师二不能看师一的班级。"""
    mk, _ = client_as
    assert mk(T2).get("/api/class/1/students").status_code == 404


def test_student_detail(client_as):
    """学生详情：薄弱点/月考/纠错率 + 新增 delta 字段。"""
    mk, _ = client_as
    data = mk(T1).get("/api/student/10/detail").json()
    assert data["student"]["name"] == "张三"
    # class_name 从 teacher_classes 取，不能是空（学生用 class_id 建，旧 class_name 字段为空）
    assert data["student"]["class_name"] == "24级数控1班"
    assert data["student"]["class_id"] == 1
    assert data["correction_rate"] == 1.0
    assert len(data["exam_scores"]) == 2          # 月考折线图数据
    assert data["exam_scores"][0]["exam_name"] == "月考1"
    # 新增：后端化 exam delta
    assert "latest_exam_delta" in data
    assert "latest_exam_rank_delta" in data
    # 张三薄弱点应已 corrected，带 topic_key/知识点和状态字段
    w = data["weaknesses"][0]
    for key in ("chapter", "topic", "topic_key", "status",
                "manual", "suggested_action"):
        assert key in w
    assert w["topic_key"] == "代数与函数||二次函数"
    assert isinstance(w["suggested_action"], str) and len(w["suggested_action"]) > 0


def test_student_detail_isolation(client_as):
    mk, _ = client_as
    assert mk(T2).get("/api/student/10/detail").status_code == 404


def test_manual_correct_url_encoded_key(client_as):
    """手动纠正：topic_key 走查询参数 + URL 编码，能正确识别。"""
    from urllib.parse import quote
    mk, _ = client_as
    key = quote("三角函数||正弦")  # 李四的薄弱点
    r = mk(T1).post(f"/api/student/11/weakness/correct?topic_key={key}",
                    json={"status": "corrected"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["correction_rate"] == 1.0  # 唯一薄弱点被标已纠正 → 100%
    # 撤销 → 恢复自动判定（未纠正）
    r2 = mk(T1).post(f"/api/student/11/weakness/correct?topic_key={key}",
                     json={"status": "pending"})
    assert r2.json()["correction_rate"] == 0.0


def test_legacy_weakness_routes_operate_on_canonical_topic(client_as):
    """旧列表返回兼容 id；旧解决接口更新同一知识点的新状态。"""
    mk, _ = client_as
    client = mk(T1)

    pending = client.get("/api/student/11/weaknesses?status=pending")
    assert pending.status_code == 200
    item = pending.json()[0]
    assert isinstance(item["id"], int)
    assert item["topic_key"] == "三角函数||正弦"

    resolved = client.post(f"/api/student/11/resolve/{item['id']}")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "corrected"
    assert client.get("/api/student/11/weaknesses?status=pending").json() == []


def test_legacy_weakness_delete_keeps_audit_but_removes_pending_state(client_as):
    mk, Session = client_as
    client = mk(T1)
    weakness_id = client.get("/api/student/11/weaknesses?status=pending").json()[0]["id"]

    deleted = client.delete(f"/api/student/11/weakness/{weakness_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": weakness_id, "status": "corrected"}
    assert client.get("/api/student/11/weaknesses?status=pending").json() == []

    db = Session()
    assert db.query(KnowledgeEvent).filter(KnowledgeEvent.student_id == 11).count() == 3
    assert db.get(Weakness, weakness_id) is None
    db.close()


def test_heatmap_structure(client_as):
    """热力图只按章、知识点和学生展示，并包含讲评备课字段。"""
    mk, _ = client_as
    tree = mk(T1).get("/api/class/1/heatmap").json()["tree"]
    # 只有李四未纠正(三角函数/正弦/概念)；张三已纠正不计入
    assert len(tree) == 1
    ch = tree[0]
    assert ch["chapter"] == "三角函数"
    assert ch["wrong_students"] == 1
    # 讲评备课面板新增字段
    assert "teaching_suggestion" in ch and isinstance(ch["teaching_suggestion"], str)
    assert "priority" in ch and ch["priority"] in ("high", "medium", "low")
    topic = ch["topics"][0]
    assert topic["students"] == ["李四"]
    assert "errors" not in topic
    assert "错因" not in ch["teaching_suggestion"]


def test_add_students(client_as):
    """手动加学生 + 学号去重。"""
    mk, _ = client_as
    c = mk(T1)
    r = c.post("/api/class/1/students",
               json={"students": [{"student_no": "003", "name": "王五"},
                                  {"student_no": "001", "name": "重复号"}]})
    body = r.json()
    assert body["added"] == 1
    assert body["skipped"] == 1
    assert isinstance(body["errors"], list) and len(body["errors"]) == 1  # 全量字符串


def test_exam_no_is_independent_scoped_identity(client_as):
    mk, Session = client_as
    c = mk(T1)
    response = c.post("/api/class/1/students", json={"students": [
        {"student_no": "003", "exam_no": "00010003", "name": "王五"},
        {"student_no": "004", "exam_no": "00010001", "name": "重复考号"},
    ]})
    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert response.json()["skipped"] == 1
    db = Session()
    added = db.query(Student).filter(Student.student_no == "003").one()
    assert added.exam_no == "00010003"
    db.close()

    duplicate_edit = c.put("/api/class/1/students/11", json={"exam_no": "00010001"})
    assert duplicate_edit.status_code == 409
    cleared = c.put("/api/class/1/students/10", json={"exam_no": ""})
    assert cleared.status_code == 200
    assert cleared.json()["student"]["exam_no"] is None


def test_reports_generate(client_as):
    """三种报告生成 → file_id + download_url 完整路径。"""
    mk, _ = client_as
    c = mk(T1)
    # 班级报告
    r1 = c.post("/api/class/1/report").json()
    assert "file_id" in r1 and r1["download_url"] == f"/api/download/{r1['file_id']}"
    # 个人报告
    r2 = c.post("/api/student/10/report", json={"type": "student"}).json()
    assert r2["download_url"].startswith("/api/download/")
    # 家长信
    r3 = c.post("/api/student/10/report", json={"type": "parent_letter"}).json()
    assert "file_id" in r3
    # 批量 ZIP
    r4 = c.post("/api/class/1/report/batch", json={"type": "student"}).json()
    assert r4["generated"] == 2
    assert isinstance(r4["skipped"], list)  # 字符串数组


def test_summary_report_uses_current_class_period_and_no_model(client_as, monkeypatch):
    """成绩概览按当前班级和时间范围统计，读取页面不调用模型。"""
    from routers import report

    def fail_model_call(*_args, **_kwargs):
        pytest.fail("deterministic report summary must not call a model")

    monkeypatch.setattr(report, "generate_report_text", fail_model_call)
    mk, Session = client_as
    db = Session()
    recent = StudentProfile(
        student_id=10,
        category="daily",
        total_questions=4,
        correct_count=2,
        partial_count=1,
        wrong_count=1,
        analytics_included=True,
        created_at=datetime.now() - timedelta(days=1),
    )
    recent.set_details([
        {"result": "partial", "error_type": "计算", "chapter": "函数", "topic": "定义域"},
        {"result": "wrong", "error_type": "概念", "chapter": "函数", "topic": "定义域"},
    ])
    older = StudentProfile(
        student_id=11,
        category="exam",
        total_questions=2,
        correct_count=0,
        wrong_count=2,
        analytics_included=True,
        created_at=datetime.now() - timedelta(days=45),
    )
    db.add_all([recent, older])
    db.commit()
    db.close()

    client = mk(T1)
    weekly = client.post("/api/reports/summary", json={
        "period": "week",
        "class_id": 1,
        "type": "all",
    })
    assert weekly.status_code == 200, weekly.text
    body = weekly.json()
    assert body["total"] == 1
    assert body["total_questions"] == 4
    assert body["accuracy"] == 50.0
    assert body["top_students"][0]["name"] == "张三"
    assert body["error_distribution"] == []
    assert body["knowledge_distribution"] == [{
        "education_stage": "",
        "catalog_version": "",
        "chapter": "函数",
        "topic": "定义域",
        "label": "定义域",
        "count": 2,
        "student_count": 1,
        "pct": 1.0,
    }]
    assert "近7天共完成 1 份正式批改" in body["summary"]

    all_time = client.post("/api/reports/summary", json={
        "period": "all",
        "class_id": 1,
        "type": "all",
    }).json()
    assert all_time["total"] == 2
    assert mk(T2).post("/api/reports/summary", json={
        "period": "all", "class_id": 1, "type": "all",
    }).status_code == 404


def test_report_docx_style_contract(client_as):
    """报告模板样式契约：极简素雅班级报告 + 信封信纸家长信的关键样式必须写入 docx。"""
    import services.generated_files as gf
    from models import GeneratedFile

    mk, Session = client_as
    c = mk(T1)

    class_res = c.post("/api/class/1/report").json()
    db = Session()
    class_rec = db.query(GeneratedFile).filter(GeneratedFile.id == class_res["file_id"]).one()
    class_path = os.path.join(gf.OUTPUT_DIR, class_rec.stored_name)
    with zipfile.ZipFile(class_path) as zf:
        document_xml = zf.read("word/document.xml").decode("utf-8")
    assert 'w:top="1134"' in document_xml  # A4 上下 2cm
    assert 'w:right="1417"' in document_xml  # A4 左右 2.5cm
    # 极简素雅风：主色海军蓝 + 浅灰表头/斑马纹，无旧版黑板绿/橙配色
    assert 'w:fill="1A237E"' not in document_xml  # 班级报告正文不铺主色块
    assert 'w:fill="F5F5F5"' in document_xml  # 表头浅灰底
    assert 'w:fill="FAFAFA"' in document_xml  # 斑马纹交替行
    assert "1A237E" in document_xml  # 标题/主色文字
    assert "班级学情报告" in document_xml
    assert "学生概览" in document_xml
    assert "薄弱点分析" in document_xml

    letter_res = c.post("/api/student/10/report", json={"type": "parent_letter"}).json()
    letter_rec = db.query(GeneratedFile).filter(GeneratedFile.id == letter_res["file_id"]).one()
    letter_path = os.path.join(gf.OUTPUT_DIR, letter_rec.stored_name)
    db.close()
    with zipfile.ZipFile(letter_path) as zf:
        letter_xml = zf.read("word/document.xml").decode("utf-8")
    assert "学校名称" in letter_xml  # 未传校名时的占位
    assert 'w:color="C5A55A"' in letter_xml  # 信纸金色边框
    assert '<w:pgBorders w:offsetFrom="page">' in letter_xml  # 信封页边框
    assert '<w:top w:val="double"' in letter_xml  # 标题双线分隔
    assert "致 家 长 的 一 封 信" in letter_xml  # 封面标题


def test_report_isolation(client_as):
    mk, _ = client_as
    assert mk(T2).post("/api/class/1/report").status_code == 404


def _set_phone(Session, student_id, phone):
    db = Session()
    db.query(Student).filter(Student.id == student_id).update({"phone": phone})
    db.commit()
    db.close()


def test_letter_share_batch_includes_no_phone_without_password(client_as):
    """无手机号也生成链接，但不设置后四位校验。"""
    mk, Session = client_as
    c = mk(T1)
    _set_phone(Session, 10, "13800000001")  # 张三有手机号
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "token"}).json()
    assert r["mode"] == "share"
    assert r["generated"] == 2
    assert r["skipped"] == []
    assert r["unprotected"] == 1
    shares = {share["student_name"]: share for share in r["shares"]}
    assert shares["张三"]["password_required"] is True
    assert shares["李四"]["password_required"] is False
    assert shares["李四"]["url"].startswith("/letter/")

    token = shares["李四"]["url"].split("/letter/")[1]
    meta = c.get(f"/api/letter/{token}/meta")
    assert meta.status_code == 200
    assert meta.json() == {"need_verify": False, "hint": "该链接无需密码即可查看"}
    view = c.get(f"/api/letter/{token}/view")
    assert view.status_code == 200
    assert "致家长的一封信" in view.text


def test_letter_share_batch_reuses_until_teacher_refreshes(client_as):
    """普通生成复用有效链接；显式刷新才撤销旧链接并生成新内容。"""
    from models import ParentLetterShare

    mk, Session = client_as
    client = mk(T1)
    _set_phone(Session, 10, "13800000001")

    first = client.post("/api/class/1/report/batch", json={
        "type": "parent_letter", "lock": "token",
    }).json()
    second = client.post("/api/class/1/report/batch", json={
        "type": "parent_letter", "lock": "token",
    }).json()
    assert second["created"] == 0
    assert second["reused"] == 2
    assert second["shares"][0]["url"] == first["shares"][0]["url"]

    refreshed = client.post("/api/class/1/report/batch", json={
        "type": "parent_letter", "lock": "token", "refresh": True,
    }).json()
    assert refreshed["created"] == 2
    assert refreshed["reused"] == 0
    assert refreshed["shares"][0]["url"] != first["shares"][0]["url"]

    db = Session()
    rows = db.query(ParentLetterShare).filter(ParentLetterShare.student_id == 10).all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.is_valid) == 1
    db.close()

    listed = client.get("/api/class/1/letter-shares").json()["shares"]
    assert {row["student_name"] for row in listed} == {"张三", "李四"}
    assert {row["status"] for row in listed} == {"有效", "已失效"}


def test_letter_verify_flow(client_as):
    """家长校验：错误后4位 403，正确返回 HTML。"""
    mk, Session = client_as
    c = mk(T1)
    _set_phone(Session, 10, "13800005678")
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "token"}).json()
    token = r["shares"][0]["url"].split("/letter/")[1]

    # meta 不泄露 PII
    meta = c.get(f"/api/letter/{token}/meta").json()
    assert meta["need_verify"] is True
    assert "张三" not in str(meta)

    # 错误后4位
    bad = c.post(f"/api/letter/{token}/verify", json={"last4": "0000"})
    assert bad.status_code == 403
    # 正确后4位 → 返回信件 HTML
    ok = c.post(f"/api/letter/{token}/verify", json={"last4": "5678"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert "致家长的一封信" in body["html"]
    assert "张三" in body["html"]


def test_letter_verify_lock_after_max_attempts(client_as):
    """连续校验失败超过阈值 → token 锁死，统一 404。"""
    from models import ParentLetterShare
    mk, Session = client_as
    c = mk(T1)
    _set_phone(Session, 10, "13800009999")
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "token"}).json()
    token = r["shares"][0]["url"].split("/letter/")[1]

    for _ in range(ParentLetterShare.MAX_FAIL_ATTEMPTS):
        c.post(f"/api/letter/{token}/verify", json={"last4": "0000"})
    # 锁死后即使输对也是 404（统一侧信道）
    locked = c.post(f"/api/letter/{token}/verify", json={"last4": "9999"})
    assert locked.status_code == 404
    assert c.get(f"/api/letter/{token}/meta").status_code == 404


def test_letter_revoke(client_as):
    """老师手动失效 → 链接 404；列表可见状态。"""
    mk, Session = client_as
    c = mk(T1)
    _set_phone(Session, 10, "13811112222")
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "token"}).json()
    token = r["shares"][0]["url"].split("/letter/")[1]

    shares = c.get("/api/class/1/letter-shares").json()["shares"]
    assert len(shares) == 2
    share = next(row for row in shares if row["student_id"] == 10)
    share_id = share["id"]
    assert share["status"] == "有效"

    assert c.post(f"/api/letter-share/{share_id}/revoke").json()["ok"] is True
    # 撤销后家长访问 404
    assert c.get(f"/api/letter/{token}/meta").status_code == 404
    assert c.post(f"/api/letter/{token}/verify", json={"last4": "2222"}).status_code == 404
    # 越权撤销 404
    assert mk(T2).post(f"/api/letter-share/{share_id}/revoke").status_code == 404


def test_letter_expired_token(client_as):
    """过期 token → 统一 404。"""
    from datetime import datetime, timedelta
    from models import ParentLetterShare
    mk, Session = client_as
    c = mk(T1)
    _set_phone(Session, 10, "13833334444")
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "token"}).json()
    token = r["shares"][0]["url"].split("/letter/")[1]
    # 手动把过期时间提前
    db = Session()
    db.query(ParentLetterShare).filter(ParentLetterShare.token == token).update(
        {"expires_at": datetime.now() - timedelta(days=1)})
    db.commit()
    db.close()
    assert c.get(f"/api/letter/{token}/meta").status_code == 404


def test_letter_batch_docx_still_zips(client_as):
    """lock=none 时致家长信仍走打包 ZIP（不破坏旧路径）。"""
    mk, _ = client_as
    c = mk(T1)
    r = c.post("/api/class/1/report/batch",
               json={"type": "parent_letter", "lock": "none"}).json()
    assert "file_id" in r
    assert r["download_url"].startswith("/api/download/")
    assert r["generated"] == 2  # docx 模式不要求手机号
