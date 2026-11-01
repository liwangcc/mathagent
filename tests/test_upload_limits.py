"""P1-8 上传限制测试：流式落盘 + 数量/单文件/总大小硬限。"""

import asyncio
import io
import os

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

import config
from routers.upload import router as upload_router
from routers.auth import _require_user
from models_user import User


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    # 把上传目录指向临时目录，避免污染真实 uploads/
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    # 同时覆盖 upload 模块已 import 的 UPLOAD_DIR 名字
    import routers.upload as up
    monkeypatch.setattr(up, "UPLOAD_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(upload_router, prefix="/api")
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="t", role="teacher")
    return TestClient(app)


def _png_bytes(payload_size: int = 64) -> bytes:
    # 合法 PNG 头 + 填充
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * payload_size


def test_upload_count_limit(upload_client, monkeypatch):
    import routers.upload as up
    monkeypatch.setattr(up, "MAX_UPLOAD_FILES", 3)
    files = [("files", (f"{i}.png", _png_bytes(), "image/png")) for i in range(5)]
    r = upload_client.post("/api/upload/batch", files=files)
    assert r.status_code == 413
    assert "最多" in r.json()["detail"]


def test_upload_single_file_size_limit(upload_client, monkeypatch):
    import routers.upload as up
    # 把单文件上限压到 1KB，传一个 2KB 文件
    monkeypatch.setattr(up, "MAX_UPLOAD_FILE_SIZE", 1024)
    big = _png_bytes(2048)
    r = upload_client.post("/api/upload/batch", files=[("files", ("big.png", big, "image/png"))])
    assert r.status_code == 200
    body = r.json()
    assert len(body["saved"]) == 0
    assert len(body["failed"]) == 1
    assert "限制" in body["failed"][0]["error"]
    # 半成品文件应已被清理
    pending = os.path.join(up.UPLOAD_DIR, "待处理")
    leftover = os.listdir(pending) if os.path.isdir(pending) else []
    assert leftover == []


def test_upload_total_size_limit(upload_client, monkeypatch):
    import routers.upload as up
    monkeypatch.setattr(up, "MAX_UPLOAD_FILE_SIZE", 10 * 1024)
    monkeypatch.setattr(up, "MAX_UPLOAD_TOTAL_SIZE", 12 * 1024)
    # 两个 8KB 文件，单个不超限但合计 16KB > 12KB 总限
    f1 = _png_bytes(8 * 1024)
    f2 = _png_bytes(8 * 1024)
    r = upload_client.post("/api/upload/batch", files=[
        ("files", ("a.png", f1, "image/png")),
        ("files", ("b.png", f2, "image/png")),
    ])
    assert r.status_code == 413
    assert "总大小" in r.json()["detail"]
    pending = os.path.join(up.UPLOAD_DIR, "待处理")
    assert os.listdir(pending) == []


def test_upload_write_failure_removes_current_partial_file(upload_client, monkeypatch):
    import routers.upload as up

    def failing_open(path, mode):
        handle = io.open(path, mode)

        class PartialWriter:
            def write(self, chunk):
                handle.write(chunk[: max(1, len(chunk) // 2)])
                handle.flush()
                raise OSError("simulated disk write failure")

            def close(self):
                handle.close()

        return PartialWriter()

    monkeypatch.setattr(up, "open", failing_open, raising=False)
    response = upload_client.post(
        "/api/upload/batch",
        files=[("files", ("partial.png", _png_bytes(128), "image/png"))],
    )

    assert response.status_code == 200
    assert response.json()["saved"] == []
    assert len(response.json()["failed"]) == 1
    pending = os.path.join(up.UPLOAD_DIR, "待处理")
    assert os.listdir(pending) == []


def test_upload_cancellation_rolls_back_earlier_files(tmp_path, monkeypatch):
    import routers.upload as up

    class CancellingUpload:
        filename = "cancelled.png"

        def __init__(self):
            self.reads = 0

        async def read(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise asyncio.CancelledError

    monkeypatch.setattr(up, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(
        up,
        "validate_upload_path",
        lambda *_args: {"safe": True, "reason": ""},
    )
    first = UploadFile(filename="first.png", file=io.BytesIO(b"complete"))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            up.upload_batch(
                [first, CancellingUpload()],
                User(id=1, name="t", role="teacher"),
            )
        )

    pending = tmp_path / "待处理"
    assert list(pending.iterdir()) == []


def test_upload_valid_file_succeeds(upload_client, monkeypatch):
    # 合法小 PNG 应成功落盘（绕过尺寸检查：security 用 PIL，小 PNG 头会被判 unknown）
    # 这里主要验证流式落盘路径在限额内不报错并返回结构
    import routers.upload as up
    monkeypatch.setattr(up, "MAX_UPLOAD_FILE_SIZE", 5 * 1024 * 1024)
    r = upload_client.post("/api/upload/batch", files=[
        ("files", ("ok.png", _png_bytes(128), "image/png")),
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
