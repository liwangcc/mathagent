"""启动流程对管理员密码的处理。

回归目标（线上真实故障）：老师在界面上改过密码，服务一重启就被 .env 里的
ADMIN_PASSWORD 覆盖回去。现在的规则是：

- 管理员已有密码 → 启动流程一个字都不改，只记日志；
- 管理员没有密码（或还没有管理员账号）→ 写入初始值。
"""

import pytest

from database import Base, SessionLocal, engine
import models  # noqa: F401  确保所有表在 create_all 时注册
import models_rate_limit  # noqa: F401
import models_user
from models_user import User


@pytest.fixture
def admin_sandbox():
    """让 .first() 确定命中本用例创建的账号：先把已有管理员临时降级。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    demoted = []
    for user in db.query(User).filter(User.role == "admin").all():
        demoted.append((user.id, user.role))
        user.role = "teacher"
    db.commit()
    created_ids: list[int] = []
    try:
        yield db, created_ids
    finally:
        for user_id in created_ids:
            row = db.query(User).filter(User.id == user_id).first()
            if row is not None:
                db.delete(row)
        for user_id, role in demoted:
            row = db.query(User).filter(User.id == user_id).first()
            if row is not None:
                row.role = role
        db.commit()
        db.close()


def _make_admin(db, phone: str, password_hash: str | None, created_ids):
    user = User(
        phone=phone,
        name="测试管理员",
        role="admin",
        status="active",
        school="",
        password_hash=password_hash,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    created_ids.append(user.id)
    return user


def test_existing_password_is_never_overwritten(admin_sandbox, monkeypatch):
    from services.auth_service import hash_password, verify_password
    from database import _verify_admin_password

    db, created_ids = admin_sandbox
    original_hash = hash_password("TeacherOwn123")
    user = _make_admin(db, "13900004001", original_hash, created_ids)

    # .env 里还是老密码：旧实现会在这里把界面改过的新密码冲掉
    monkeypatch.setenv("ADMIN_PASSWORD", "LegacyEnvPass1")
    _verify_admin_password()

    db.expire_all()
    stored = db.query(User).filter(User.id == user.id).first().password_hash
    assert stored == original_hash
    assert verify_password("TeacherOwn123", stored) is True
    assert verify_password("LegacyEnvPass1", stored) is False


def test_empty_password_gets_bootstrap_value(admin_sandbox, monkeypatch):
    from services.auth_service import verify_password
    from database import _verify_admin_password

    db, created_ids = admin_sandbox
    user = _make_admin(db, "13900004002", None, created_ids)

    monkeypatch.setenv("ADMIN_PASSWORD", "Bootstrap123")
    _verify_admin_password()

    db.expire_all()
    stored = db.query(User).filter(User.id == user.id).first().password_hash
    assert stored
    assert verify_password("Bootstrap123", stored) is True


def test_missing_admin_is_created_from_env(admin_sandbox, monkeypatch):
    from services.auth_service import verify_password
    from database import _verify_admin_password

    db, created_ids = admin_sandbox
    monkeypatch.setenv("ADMIN_PASSWORD", "FirstRun123")
    monkeypatch.setenv("ADMIN_PHONE", "13900004003")
    monkeypatch.setenv("ADMIN_NAME", "首任管理员")
    _verify_admin_password()

    db.expire_all()
    user = db.query(User).filter(User.phone == "13900004003").first()
    assert user is not None
    created_ids.append(user.id)
    assert user.role == "admin"
    assert user.name == "首任管理员"
    assert verify_password("FirstRun123", user.password_hash) is True


def test_first_run_generates_password_when_env_missing(admin_sandbox, monkeypatch, tmp_path):
    """全新安装且 .env 没给密码：生成 16 位随机密码，写文件并标记必须改密。"""
    import config
    from database import _verify_admin_password

    db, created_ids = admin_sandbox
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_PHONE", "13900004009")

    result = _verify_admin_password()
    password = result["generated_password"]
    assert result["created"] is True
    assert password and len(password) == 16

    db.expire_all()
    user = db.query(User).filter(User.phone == "13900004009").first()
    assert user is not None
    created_ids.append(user.id)
    assert user.role == "admin"
    assert user.must_change_password is True

    from services.auth_service import verify_password

    assert verify_password(password, user.password_hash) is True

    # 密码同时落盘，便于 docker logs 之外二次查看
    assert (tmp_path / ".first-run-password").read_text(encoding="utf-8") == password


def test_first_run_password_can_log_in_and_flag_clears(admin_sandbox, monkeypatch, tmp_path):
    """生成的首启密码必须真的能登录；改密成功后标记清零。"""
    import config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.auth import router as auth_router
    from services.ip_rate_limit import clear_ip_failures

    from database import _verify_admin_password

    db, created_ids = admin_sandbox
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_PHONE", "13900004010")

    password = _verify_admin_password()["generated_password"]
    assert password
    db.expire_all()
    created_ids.append(
        db.query(User).filter(User.phone == "13900004010").first().id
    )

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    client = TestClient(app)
    clear_ip_failures("testclient")

    login = client.post(
        "/api/auth/password/login",
        json={"phone": "13900004010", "password": password},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["user"]["must_change_password"] is True

    changed = client.post(
        "/api/auth/password/set",
        json={"password": "Teacher123", "old_password": password},
        headers={"Authorization": f"Bearer {body['token']}"},
    )
    assert changed.status_code == 200, changed.text

    db.expire_all()
    user = db.query(User).filter(User.phone == "13900004010").first()
    assert user.must_change_password is False
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"}
    ).json()["user"]["must_change_password"] is False