"""安全报告回归：IP 封禁限时、微信 OAuth state、上传文件归属精确匹配。

对应 Claude 安全报告 2.9 / 2.10 / 2.11。
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base


# ---------- 2.9 IP 封禁必须限时 ----------

@pytest.fixture
def ip_module(tmp_path, monkeypatch):
    import services.ip_rate_limit as m

    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "ip_failures.db"), raising=False)
    m._init_db()
    return m


def test_ban_is_permanent_within_window(ip_module, monkeypatch):
    monkeypatch.setattr(ip_module, "IP_BAN_THRESHOLD", 2)
    monkeypatch.setattr(ip_module, "IP_BAN_DURATION", 3600)

    ip_module.record_ip_failure("198.51.100.7")
    ip_module.record_ip_failure("198.51.100.7")

    assert ip_module.get_ip_lock("198.51.100.7") == -1


def test_ban_expires_and_clears_itself(ip_module, monkeypatch):
    """封禁到期后必须自动解封，否则共用出口的整所学校会被永久锁死。"""
    monkeypatch.setattr(ip_module, "IP_BAN_THRESHOLD", 2)
    monkeypatch.setattr(ip_module, "IP_BAN_DURATION", 3600)

    ip_module.record_ip_failure("198.51.100.8")
    ip_module.record_ip_failure("198.51.100.8")
    assert ip_module.get_ip_lock("198.51.100.8") == -1

    with ip_module._get_conn() as conn:
        conn.execute(
            "UPDATE ip_failures SET locked_until=? WHERE ip=?",
            (time.time() - 1, "198.51.100.8"),
        )

    assert ip_module.get_ip_lock("198.51.100.8") == 0


def test_unban_ip_clears_record(ip_module, monkeypatch):
    monkeypatch.setattr(ip_module, "IP_BAN_THRESHOLD", 2)
    monkeypatch.setattr(ip_module, "IP_BAN_DURATION", 3600)

    ip_module.record_ip_failure("198.51.100.9")
    ip_module.record_ip_failure("198.51.100.9")
    assert ip_module.get_ip_lock("198.51.100.9") == -1

    assert ip_module.unban_ip("198.51.100.9") is True
    assert ip_module.get_ip_lock("198.51.100.9") == 0
    assert ip_module.unban_ip("198.51.100.9") is False


# ---------- 2.10 微信 OAuth state ----------

def test_wechat_state_is_one_time():
    from routers import auth as auth_mod

    state = auth_mod._issue_wechat_state()
    assert len(state) > 16
    assert auth_mod._consume_wechat_state(state) is True
    assert auth_mod._consume_wechat_state(state) is False


def test_wechat_callback_rejects_missing_state():
    from routers.auth import router as auth_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    client = TestClient(app)

    resp = client.get("/api/auth/wechat/callback", params={"code": "attacker-code"})
    assert resp.status_code == 400
    assert "状态" in resp.json()["detail"]


# ---------- 2.11 上传文件归属：完整文件名匹配 ----------

@pytest.fixture
def upload_db(tmp_path):
    import models  # noqa: F401
    import models_user  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'upload.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_upload_ownership_requires_exact_filename(upload_db):
    from models import Student, StudentProfile
    from app import _teacher_owns_upload

    db = upload_db
    student = Student(teacher_id=1, name="测试学生", class_name="高一1班", student_no="001")
    db.add(student)
    db.commit()
    db.refresh(student)
    db.add(StudentProfile(student_id=student.id, image_path="/uploads/t1/abc123.jpg"))
    db.commit()

    assert _teacher_owns_upload(db, 1, "/uploads/t1/abc123.jpg") is True
    # 子串不得命中：xabc123.jpg 不属于该老师
    assert _teacher_owns_upload(db, 1, "/uploads/t1/xabc123.jpg") is False


def test_upload_ownership_isolated_between_teachers(upload_db):
    from models import Student, StudentProfile
    from app import _teacher_owns_upload

    db = upload_db
    student = Student(teacher_id=1, name="甲", class_name="高一1班", student_no="001")
    db.add(student)
    db.commit()
    db.refresh(student)
    db.add(StudentProfile(student_id=student.id, image_path="/uploads/t1/only-mine.jpg"))
    db.commit()

    assert _teacher_owns_upload(db, 1, "/uploads/t1/only-mine.jpg") is True
    assert _teacher_owns_upload(db, 2, "/uploads/t1/only-mine.jpg") is False