"""核心流程冒烟测试 — 上传/批改主链路。

目的：保证反复批阅时主流程不会因代码改动悄悄崩掉。
策略：用 TestClient + 独立测试DB + dependency_overrides，mock 掉 OCR/批改的外部
API 调用，只验证端点契约、认证、归属隔离、参数校验等"骨架"逻辑。
不消耗任何真实 API 额度。
"""

import io
import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401


# ─────────────────────────────────────────────────────────
# 公共夹具
# ─────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient + 独立SQLite + 覆盖 get_db / _require_user。"""
    from fastapi.testclient import TestClient
    from app import app
    from routers import grade as grade_router
    from routers.auth import _require_user
    from models import Student
    from models_user import User

    db_url = f"sqlite:///{tmp_path / 'smoke.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # 预置一个老师 + 一个学生
    setup = TestSession()
    teacher = User(phone="13800138000", name="王老师", status="active")
    setup.add(teacher)
    setup.commit()
    setup.refresh(teacher)
    student = Student(teacher_id=teacher.id, name="张三")
    setup.add(student)
    setup.commit()
    setup.refresh(student)
    teacher_id, student_id = teacher.id, student.id
    setup.close()

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    def override_require_user():
        db = TestSession()
        try:
            return db.query(User).filter(User.id == teacher_id).first()
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[_require_user] = override_require_user

    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}

    monkeypatch.setattr(grade_router, "batch_grade_availability", available)

    async def infrastructure_ready(_upload_dir, _request_bytes):
        return {"ready": True, "status": "available", "code": "test-ready"}

    # The multipart guard runs before FastAPI parses form fields. Keep these
    # contract-only tests focused on endpoint validation instead of the real
    # storage/model readiness state of the host process.
    monkeypatch.setattr(
        "services.system_readiness.get_grading_infrastructure_readiness",
        infrastructure_ready,
    )

    c = TestClient(app)
    c.teacher_id = teacher_id
    c.student_id = student_id
    c.TestSession = TestSession
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _png_bytes() -> bytes:
    """最小合法 PNG（1x1），用于通过 magic bytes 校验。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def upload_image():
    """在 UPLOAD_DIR 内创建一张测试图片并返回其绝对路径，测试结束清理。

    grade/paper 与 profiles/import 的 image_path 现在被限制必须落在 uploads/ 内
    （H6 路径遍历防护），所以测试图片必须写进 UPLOAD_DIR，不能用 tmp_path。
    """
    import uuid
    from config import UPLOAD_DIR
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"_smoke_{uuid.uuid4().hex}.png")
    with open(path, "wb") as f:
        f.write(_png_bytes())
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


# ─────────────────────────────────────────────────────────
# 健康检查
# ─────────────────────────────────────────────────────────

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_api_health_includes_db(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"


# ─────────────────────────────────────────────────────────
# 上传链路
# ─────────────────────────────────────────────────────────

class TestUpload:
    def test_upload_valid_png(self, client):
        files = [("files", ("a.png", _png_bytes(), "image/png"))]
        r = client.post("/api/upload/batch", files=files)
        assert r.status_code == 200
        body = r.json()
        assert len(body["saved"]) == 1
        assert body["failed"] == []
        # 清理落盘文件
        try:
            os.remove(body["saved"][0]["path"])
        except OSError:
            pass

    def test_upload_rejects_fake_extension(self, client):
        # 内容是文本，但扩展名伪装成 .png → magic bytes 校验失败
        files = [("files", ("evil.png", b"<?php system($_GET[0]); ?>", "image/png"))]
        r = client.post("/api/upload/batch", files=files)
        assert r.status_code == 200
        body = r.json()
        assert len(body["saved"]) == 0
        assert len(body["failed"]) == 1

    def test_upload_rejects_disallowed_type(self, client):
        files = [("files", ("a.exe", b"MZ\x90\x00", "application/octet-stream"))]
        r = client.post("/api/upload/batch", files=files)
        assert r.status_code == 200
        assert len(r.json()["saved"]) == 0


# ─────────────────────────────────────────────────────────
# 批量批改流式端点（只验证入参校验，不触发流水线）
# ─────────────────────────────────────────────────────────

class TestBatchGradeStream:
    @pytest.mark.parametrize("category", ["garbage", "review"])
    def test_batch_rejects_unsupported_category(self, client, category):
        files = [("files", ("a.png", _png_bytes(), "image/png"))]
        r = client.post(
            "/api/batch-grade/recognize",
            files=files,
            data={"category": category},
        )
        assert r.status_code == 400

    def test_batch_exam_requires_name(self, client):
        files = [("files", ("a.png", _png_bytes(), "image/png"))]
        r = client.post(
            "/api/batch-grade/recognize",
            files=files,
            data={"category": "exam"},
        )
        assert r.status_code == 400


class TestReportContracts:
    def test_summary_report_rejects_removed_review_type(self):
        from pydantic import ValidationError
        from routers.report import SummaryReportRequest

        with pytest.raises(ValidationError):
            SummaryReportRequest(type="review")

    def test_profiles_list_rejects_removed_review_type(self, client):
        response = client.get(
            "/api/profiles/list",
            params={"category": "review"},
        )
        assert response.status_code == 400


# ─────────────────────────────────────────────────────────
# 认证强制（不覆盖 _require_user 时应拒绝）
# ─────────────────────────────────────────────────────────

class TestAuthEnforced:
    def test_grade_requires_auth(self, client):
        """移除 _require_user 覆盖后，无 token 应 401。"""
        from app import app
        from routers.auth import _require_user

        # 临时取消 user 覆盖，模拟未登录
        app.dependency_overrides.pop(_require_user, None)
        try:
            r = client.get(
                "/api/grade/results",
                params={"student_id": client.student_id},
            )
            assert r.status_code == 401
        finally:
            # client 夹具的 finally 会 clear，这里无需恢复
            pass


# ─────────────────────────────────────────────────────────
# uploads 鉴权（M2：试卷照片含未成年人 PII，不可匿名访问）
# ─────────────────────────────────────────────────────────

class TestUploadsAuth:
    def test_uploads_served_to_authed_user(self, client, upload_image):
        """老师可取到自己名下批改记录引用的 uploads/ 文件（P0-1 归属校验）。"""
        from models import GradingResult
        name = os.path.basename(upload_image)
        # 关联一条当前老师名下学生的批改记录，使该图片归属本老师
        db = client.TestSession()
        try:
            gr = GradingResult(student_id=client.student_id, exam_name="smoke")
            gr.set_details([{"index": 0, "image_path": upload_image}])
            db.add(gr)
            db.commit()
        finally:
            db.close()

        r = client.get(f"/uploads/{name}")
        assert r.status_code == 200
        assert r.content == _png_bytes() or len(r.content) > 0

    def test_uploads_owned_by_other_teacher_404(self, client, upload_image):
        """已登录但文件不属于当前老师 → 404（归属隔离，P0-1）。

        文件存在于 uploads/ 内、路径合法，但没有任何本老师的记录引用它。
        """
        name = os.path.basename(upload_image)
        r = client.get(f"/uploads/{name}")
        assert r.status_code == 404

    def test_uploads_requires_auth(self, client):
        """未登录访问 /uploads/* 应 401（原 StaticFiles 公开 mount 的修复）。"""
        from app import app
        from routers.auth import _require_user

        app.dependency_overrides.pop(_require_user, None)
        r = client.get("/uploads/anything.png")
        assert r.status_code == 401

    def test_uploads_blocks_traversal(self, client):
        """已登录但路径越界 → 403（复用 H6 路径防护）。

        注意：明文 ../ 会被 HTTP 客户端在发送前规范化掉，根本到不了后端；
        真正能打到端点的是编码形式，这里用编码 payload 验证防护生效。
        """
        for payload in ("/uploads/%2e%2e/config.py", "/uploads/..%2fconfig.py"):
            r = client.get(payload)
            assert r.status_code == 403, f"{payload} -> {r.status_code}"


class TestGradingResultSoftDelete:
    def test_delete_hides_record_but_keeps_analytics_history(self, client):
        """Grade-record deletion is a teacher-visible hide, not a cascading purge."""
        from models import GradingResult, KnowledgeEvent, StudentProfile

        db = client.TestSession()
        try:
            result = GradingResult(
                student_id=client.student_id,
                category="daily",
                image_content_hash="smoke-soft-delete",
                total_questions=1,
                wrong_count=1,
                score=0.0,
            )
            result.set_details([{"needs_name_confirm": True}])
            db.add(result)
            db.commit()
            profile = StudentProfile(
                student_id=client.student_id,
                grading_result_id=result.id,
                score=0.0,
            )
            event = KnowledgeEvent(
                student_id=client.student_id,
                teacher_id=client.teacher_id,
                grading_result_id=result.id,
                chapter="代数",
                topic="加法",
                result="wrong",
            )
            db.add_all([profile, event])
            db.commit()
            result_id, profile_id, event_id = result.id, profile.id, event.id
        finally:
            db.close()

        deleted = client.request("DELETE", "/api/grade/results", json=[result_id])
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] == 1
        assert client.get("/api/grade/results").json() == []
        assert client.get("/api/student/pending-confirm").json() == []

        db = client.TestSession()
        try:
            assert db.get(GradingResult, result_id).deleted_at is not None
            assert db.get(StudentProfile, profile_id) is not None
            assert db.get(KnowledgeEvent, event_id) is not None
        finally:
            db.close()
