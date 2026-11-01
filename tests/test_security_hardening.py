"""安全加固测试：LIKE 转义 / 导入大小限制 / CORS 配置。"""

import io
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from services.security import (
    escape_like,
    read_upload_capped,
    stream_to_disk_capped,
    validate_upload_file,
    validate_upload_path,
)


# ─────────────────────────────────────────────────────────
# escape_like：SQL LIKE 通配符转义
# ─────────────────────────────────────────────────────────

class TestEscapeLike:
    def test_escapes_percent_and_underscore(self):
        assert escape_like("100%") == "100\\%"
        assert escape_like("a_b") == "a\\_b"

    def test_escapes_backslash_first(self):
        # 反斜杠必须先转义，否则会破坏后续转义序列
        assert escape_like("a\\b") == "a\\\\b"

    def test_empty(self):
        assert escape_like("") == ""
        assert escape_like(None) == ""

    def test_plain_text_unchanged(self):
        assert escape_like("张三") == "张三"


# ─────────────────────────────────────────────────────────
# read_upload_capped：分块读取 + 超限 413
# ─────────────────────────────────────────────────────────

class _FakeUpload:
    """最小 UploadFile 替身：按块吐 data。"""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


def test_read_capped_under_limit():
    import asyncio
    up = _FakeUpload(b"x" * 1000)
    out = asyncio.run(read_upload_capped(up, max_bytes=2000))
    assert out == b"x" * 1000


def test_read_capped_over_limit_raises_413():
    import asyncio
    from fastapi import HTTPException

    up = _FakeUpload(b"x" * (3 * 1024 * 1024))  # 3MB
    with pytest.raises(HTTPException) as ei:
        asyncio.run(read_upload_capped(up, max_bytes=1 * 1024 * 1024))  # 1MB 上限
    assert ei.value.status_code == 413


def test_stream_to_disk_capped_under_limit(tmp_path):
    import asyncio

    target = tmp_path / "upload.bin"
    up = _FakeUpload(b"x" * 1000)

    size = asyncio.run(stream_to_disk_capped(up, str(target), max_bytes=2000))

    assert size == 1000
    assert target.read_bytes() == b"x" * 1000


def test_stream_to_disk_capped_over_limit_returns_minus_one(tmp_path):
    import asyncio

    target = tmp_path / "upload.bin"
    up = _FakeUpload(b"x" * (2 * 1024 * 1024))

    size = asyncio.run(stream_to_disk_capped(up, str(target), max_bytes=1024))

    assert size == -1
    assert target.exists()


def test_image_upload_rejects_total_pixel_bomb_before_decode(monkeypatch):
    from PIL import Image

    class HeaderOnlyImage:
        size = (5_001, 5_000)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(Image, "open", lambda *_args, **_kwargs: HeaderOnlyImage())

    result = validate_upload_file("paper.png", b"\x89PNG\r\n\x1a\nheader")

    assert result["safe"] is False
    assert "像素" in result["reason"]


def test_image_upload_rejects_malformed_decoder_input():
    result = validate_upload_file("paper.png", b"\x89PNG\r\n\x1a\nbroken")

    assert result["safe"] is False
    assert "损坏" in result["reason"]


def test_path_image_validation_matches_in_memory_contract(tmp_path):
    from PIL import Image

    path = tmp_path / "paper.jpg"
    Image.new("RGB", (640, 480), "white").save(path, "JPEG")

    from_memory = validate_upload_file("paper.jpg", path.read_bytes())
    from_path = validate_upload_path("paper.jpg", path)

    assert from_path == from_memory


def test_path_image_validation_rejects_magic_extension_mismatch(tmp_path):
    path = tmp_path / "paper.png"
    path.write_bytes(b"\xff\xd8\xff" + b"not-a-png")

    result = validate_upload_path("paper.png", path)

    assert result["safe"] is False
    assert "不匹配" in result["reason"]


# ─────────────────────────────────────────────────────────
# 导入端点大小限制（answer.py excel/json）
# ─────────────────────────────────────────────────────────

@pytest.fixture
def import_client(monkeypatch):
    monkeypatch.setattr(config, "MAX_IMPORT_FILE_SIZE", 1024)  # 1KB 便于触发
    import routers.answer as ans
    monkeypatch.setattr(ans, "MAX_IMPORT_FILE_SIZE", 1024)

    from routers.answer import router
    from routers.auth import _require_user
    from models_user import User

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="t", role="teacher", education_stage="senior")
    return TestClient(app, raise_server_exceptions=False)


class TestImportSizeLimit:
    def test_excel_oversize_rejected(self, import_client):
        big = b"PK" + b"\x00" * 4096  # 4KB > 1KB 上限
        r = import_client.post(
            "/api/answer/import/excel",
            files=[("file", ("big.xlsx", big, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        )
        assert r.status_code == 413
        assert "限制" in r.json()["detail"]

    def test_json_oversize_rejected(self, import_client):
        big = b"[" + b"0," * 4096 + b"0]"
        r = import_client.post(
            "/api/answer/import/json",
            files=[("file", ("big.json", big, "application/json"))],
        )
        assert r.status_code == 413


# ─────────────────────────────────────────────────────────
# CORS 配置
# ─────────────────────────────────────────────────────────

class TestCorsConfig:
    """直接构造两种模式的中间件配置，断言收紧效果（不 reload 全局 app，避免污染其他用例）。"""

    def _build(self, allow_all: bool, origins: list[str]):
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware

        app = FastAPI()
        if allow_all:
            app.add_middleware(
                CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            )
        else:
            app.add_middleware(
                CORSMiddleware, allow_origins=origins, allow_credentials=True,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
            )

        @app.get("/api/auth/me")
        def _me():
            return {"ok": True}

        return TestClient(app)

    def test_prod_whitelist_origin_allowed(self):
        client = self._build(False, ["https://prod.example.com"])
        r = client.options(
            "/api/auth/me",
            headers={"Origin": "https://prod.example.com",
                     "Access-Control-Request-Method": "GET"},
        )
        assert r.headers.get("access-control-allow-origin") == "https://prod.example.com"
        allow_methods = r.headers.get("access-control-allow-methods", "")
        assert "*" not in allow_methods and "GET" in allow_methods

    def test_prod_unknown_origin_rejected(self):
        client = self._build(False, ["https://prod.example.com"])
        r = client.options(
            "/api/auth/me",
            headers={"Origin": "https://evil.example.com",
                     "Access-Control-Request-Method": "GET"},
        )
        assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_dev_mode_no_credentials_with_wildcard(self):
        """开发模式通配 origin 时不得带 credentials（否则浏览器拒绝）。"""
        client = self._build(True, [])
        r = client.get("/api/auth/me", headers={"Origin": "https://anything.example.com"})
        assert r.headers.get("access-control-allow-origin") == "*"
        # 通配下不应回 allow-credentials: true
        assert r.headers.get("access-control-allow-credentials") != "true"
        r = client.options(
            "/api/auth/me",
            headers={"Origin": "https://anything.example.com",
                     "Access-Control-Request-Method": "GET"},
        )
        assert "*" not in r.headers.get("access-control-allow-methods", "")

    def test_actual_app_prod_config(self):
        """冒烟：实际 app（测试环境 CORS_ALLOW_ALL 未设=生产模式）方法非通配。"""
        from app import app as real_app
        cors = next(
            (m for m in real_app.user_middleware if "CORSMiddleware" in str(m.cls)),
            None,
        )
        assert cors is not None
        methods = cors.kwargs.get("allow_methods", [])
        headers = cors.kwargs.get("allow_headers", [])
        exposed = cors.kwargs.get("expose_headers", [])
        assert "*" not in methods
        assert "*" not in headers
        assert "X-Template-ID" in exposed

    def test_actual_app_allows_only_fixed_tauri_desktop_origin(self):
        from app import app as real_app

        client = TestClient(real_app)
        allowed = client.options(
            "/api/auth/me",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == "http://tauri.localhost"

        rejected = client.options(
            "/api/auth/me",
            headers={
                "Origin": "http://evil.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert rejected.headers.get("access-control-allow-origin") is None


# ─────────────────────────────────────────────────────────
# HTTP 安全头 / Host 校验 / SPA 静态文件白名单
# ─────────────────────────────────────────────────────────

class TestNetworkHardening:
    def test_actual_app_sets_security_headers(self):
        from app import app as real_app

        client = TestClient(real_app)
        r = client.get("/health", headers={"host": "127.0.0.1"})
        assert r.status_code == 200
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")
        assert r.headers.get("permissions-policy")

    def test_actual_app_rejects_untrusted_host(self):
        from app import app as real_app

        client = TestClient(real_app, raise_server_exceptions=False)
        r = client.get("/health", headers={"host": "evil.example.com"})
        assert r.status_code == 400

    def test_safe_static_path_allows_assets_but_blocks_maps_and_hidden_files(self):
        from app import _is_safe_static_path

        assert _is_safe_static_path("assets/index-abc.js")
        assert _is_safe_static_path("favicon.ico")
        assert _is_safe_static_path("media/welcome-canopy.mp4")
        assert not _is_safe_static_path("assets/index-abc.js.map")
        assert not _is_safe_static_path(".env")
        assert not _is_safe_static_path("../mathagent.db")


# ─────────────────────────────────────────────────────────
# 生成文件扩展名白名单
# ─────────────────────────────────────────────────────────

def test_register_generated_file_rejects_executable_extension(tmp_path, monkeypatch):
    import services.generated_files as gf

    monkeypatch.setattr(gf, "OUTPUT_DIR", str(tmp_path))

    class _Db:
        def add(self, obj):
            raise AssertionError("unsafe file should be rejected before db.add")

        def flush(self):
            raise AssertionError("unsafe file should be rejected before db.flush")

    with pytest.raises(ValueError):
        gf.register_generated_file(_Db(), 1, b"payload", "evil.html")

    assert os.listdir(tmp_path) == []


def test_cleanup_generated_files_removes_expired_registered_files(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    import services.generated_files as gf
    from database import Base
    from models import GeneratedFile

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(gf, "OUTPUT_DIR", str(output_dir))

    old_name = "old.docx"
    new_name = "new.docx"
    (output_dir / old_name).write_bytes(b"old")
    (output_dir / new_name).write_bytes(b"new")

    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionLocal()
    try:
        db.add_all([
            GeneratedFile(
                owner_id=1,
                stored_name=old_name,
                display_name="old.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                created_at=datetime.now() - timedelta(days=40),
            ),
            GeneratedFile(
                owner_id=1,
                stored_name=new_name,
                display_name="new.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                created_at=datetime.now(),
            ),
        ])
        db.commit()

        result = gf.cleanup_generated_files(db, older_than_days=30)
        db.commit()

        assert result["deleted_rows"] == 1
        assert result["deleted_files"] == 1
        assert not (output_dir / old_name).exists()
        assert (output_dir / new_name).exists()
        assert db.query(GeneratedFile).count() == 1
    finally:
        db.close()


def test_classify_answer_hides_non_owned_questions(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base, get_db
    from models import QuestionBank
    from models_user import User
    from routers.answer import router
    from routers.auth import _require_user

    engine = create_engine(f"sqlite:///{tmp_path / 'answer.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionLocal()
    try:
        db.add(QuestionBank(teacher_id=2, is_public=False, question="1+1", answer="2"))
        db.commit()
    finally:
        db.close()

    def override_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="t1", role="teacher")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.put("/api/answer/1/classify", params={"chapter": "代数", "topic": "方程"})

    assert r.status_code == 404
