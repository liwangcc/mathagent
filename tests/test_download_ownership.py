"""P0-1/P0-2 生成文件下载归属校验测试。

核心安全属性：只有文件 owner 能通过 /api/download/<id> 下载；
他人（即使是合法登录用户）请求一律 404，杜绝凭文件名/猜测越权。
"""

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db
from models_user import User

# download_file 内部固定从 routers/../output 读取物理文件
_REAL_OUTPUT = Path(__file__).resolve().parent.parent / "output"


@pytest.fixture
def setup(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'dl.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    _REAL_OUTPUT.mkdir(exist_ok=True)
    from models import GeneratedFile
    import uuid as _uuid
    stored = f"test_{_uuid.uuid4().hex}.docx"
    fpath = _REAL_OUTPUT / stored
    fpath.write_bytes(b"hello-docx")

    s = TestSession()
    rec = GeneratedFile(owner_id=1, stored_name=stored, display_name="报告.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    s.add(rec); s.commit()
    file_id = rec.id
    s.close()

    def _override_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    from routers.chat import download_file
    from routers.auth import _require_user

    app = FastAPI()
    app.add_api_route("/api/download/{file_id}", download_file, methods=["GET"])
    app.dependency_overrides[get_db] = _override_db

    yield app, _require_user, file_id

    # 清理物理文件
    try:
        os.remove(fpath)
    except OSError:
        pass


def test_owner_can_download(setup):
    app, require_user, file_id = setup
    app.dependency_overrides[require_user] = lambda: User(id=1, name="o", role="teacher")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(f"/api/download/{file_id}")
    assert r.status_code == 200
    assert r.content == b"hello-docx"
    # 友好中文名走 RFC5987 编码
    assert "filename*=UTF-8''" in r.headers.get("content-disposition", "")


def test_non_owner_gets_404(setup):
    app, require_user, file_id = setup
    app.dependency_overrides[require_user] = lambda: User(id=2, name="x", role="teacher")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get(f"/api/download/{file_id}")
    assert r.status_code == 404


def test_nonexistent_id_404(setup):
    app, require_user, file_id = setup
    app.dependency_overrides[require_user] = lambda: User(id=1, name="o", role="teacher")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/download/999999")
    assert r.status_code == 404
