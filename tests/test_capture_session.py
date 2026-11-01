"""Phone capture sessions: token scope, teacher ownership, limits and cleanup."""

import io
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import config
from models_user import User
from routers.auth import _require_user
from routers.capture import router as capture_router
from services import capture_session


def _jpeg(width=640, height=900, color=(250, 250, 250)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


@pytest.fixture
def capture_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MAX_UPLOAD_FILES", 200)
    current = {"user": User(id=1, name="t1", role="teacher", education_stage="senior")}

    app = FastAPI()
    app.include_router(capture_router, prefix="/api")
    app.dependency_overrides[_require_user] = lambda: current["user"]
    return TestClient(app), current, tmp_path


def _open(client):
    response = client.post("/api/capture/sessions")
    assert response.status_code == 200, response.text
    body = response.json()
    return body["session_id"], body["token"], body


def _upload(client, session_id, token, data=None, client_id=""):
    return client.post(
        f"/api/capture/phone/{session_id}/images",
        files={"file": ("shot.jpg", data if data is not None else _jpeg(), "image/jpeg")},
        data={"client_id": client_id} if client_id else {},
        headers={"X-Capture-Token": token} if token is not None else {},
    )


def test_phone_upload_then_teacher_imports_in_order(capture_env):
    client, _, _ = capture_env
    session_id, token, body = _open(client)
    assert body["images"] == [] and body["max_images"] == 200
    assert "token_sha256" not in body

    state = client.get(f"/api/capture/phone/{session_id}", headers={"X-Capture-Token": token})
    assert state.status_code == 200
    assert set(state.json()) == {"session_id", "status", "expires_at", "max_images", "images"}

    for index in range(3):
        response = _upload(client, session_id, token, _jpeg(600 + index, 900), client_id=f"shot-{index:04d}")
        assert response.status_code == 200, response.text
        assert response.json()["image"]["seq"] == index + 1

    session = client.get(f"/api/capture/sessions/{session_id}").json()
    assert [image["seq"] for image in session["images"]] == [1, 2, 3]
    assert session["phone_seen_at"]

    image = client.get(f"/api/capture/sessions/{session_id}/images/2")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "no-store"
    assert Image.open(io.BytesIO(image.content)).size == (601, 900)

    assert client.delete(f"/api/capture/sessions/{session_id}").status_code == 200
    assert client.get(f"/api/capture/sessions/{session_id}").status_code == 404
    assert _upload(client, session_id, token).status_code == 403


def test_repeated_client_id_is_idempotent(capture_env):
    client, _, _ = capture_env
    session_id, token, _ = _open(client)
    first = _upload(client, session_id, token, client_id="retry-abcdef01").json()
    second = _upload(client, session_id, token, client_id="retry-abcdef01").json()
    assert first["image"]["seq"] == second["image"]["seq"] == 1
    assert second["duplicate"] is True
    assert len(client.get(f"/api/capture/sessions/{session_id}").json()["images"]) == 1


def test_phone_token_is_required_and_scoped(capture_env):
    client, current, _ = capture_env
    session_id, token, _ = _open(client)
    assert _upload(client, session_id, None).status_code == 403
    assert _upload(client, session_id, "wrong-token").status_code == 403

    current["user"] = User(id=2, name="t2", role="teacher", education_stage="senior")
    other_id, other_token, _ = _open(client)
    assert other_id != session_id
    # One teacher's phone token never opens another teacher's session.
    assert _upload(client, other_id, token).status_code == 403
    assert _upload(client, session_id, other_token).status_code == 403
    # A login token is not a capture token and the phone cannot read images.
    assert client.get(f"/api/capture/phone/{session_id}/images/1").status_code in {404, 405}


def test_other_teacher_cannot_read_or_close(capture_env):
    client, current, _ = capture_env
    session_id, token, _ = _open(client)
    assert _upload(client, session_id, token).status_code == 200

    current["user"] = User(id=2, name="t2", role="teacher", education_stage="senior")
    assert client.get(f"/api/capture/sessions/{session_id}").status_code == 404
    assert client.get(f"/api/capture/sessions/{session_id}/images/1").status_code == 404
    assert client.delete(f"/api/capture/sessions/{session_id}/images/1").status_code == 404
    assert client.delete(f"/api/capture/sessions/{session_id}").status_code == 404

    current["user"] = User(id=1, name="t1", role="teacher", education_stage="senior")
    assert len(client.get(f"/api/capture/sessions/{session_id}").json()["images"]) == 1


def test_reopen_resumes_photos_and_rotates_token(capture_env):
    client, _, _ = capture_env
    session_id, old_token, _ = _open(client)
    assert _upload(client, session_id, old_token).status_code == 200

    same_id, new_token, body = _open(client)
    assert same_id == session_id and new_token != old_token
    assert len(body["images"]) == 1
    assert _upload(client, session_id, old_token).status_code == 403
    assert _upload(client, session_id, new_token).status_code == 200


def test_delete_by_phone_and_teacher(capture_env):
    client, _, _ = capture_env
    session_id, token, _ = _open(client)
    for _ in range(3):
        assert _upload(client, session_id, token).status_code == 200
    headers = {"X-Capture-Token": token}
    phone = client.delete(f"/api/capture/phone/{session_id}/images/2", headers=headers)
    assert phone.status_code == 200
    assert [image["seq"] for image in phone.json()["images"]] == [1, 3]
    teacher = client.delete(f"/api/capture/sessions/{session_id}/images/1")
    assert [image["seq"] for image in teacher.json()["images"]] == [3]
    # Sequence numbers are never reused, so the desktop import order stays stable.
    assert _upload(client, session_id, token).json()["image"]["seq"] == 4
    assert client.delete(f"/api/capture/phone/{session_id}/images/2", headers=headers).status_code == 410


def test_rejects_non_images_and_respects_limit(capture_env, monkeypatch):
    client, _, _ = capture_env
    session_id, token, _ = _open(client)
    assert _upload(client, session_id, token, b"not an image").status_code == 400
    assert _upload(client, session_id, token, _jpeg(100, 100)).status_code == 400
    gif = io.BytesIO()
    Image.new("RGB", (400, 400)).save(gif, format="GIF")
    assert _upload(client, session_id, token, gif.getvalue()).status_code == 400

    monkeypatch.setattr(config, "MAX_UPLOAD_FILES", 2)
    assert _upload(client, session_id, token).status_code == 200
    assert _upload(client, session_id, token).status_code == 200
    full = _upload(client, session_id, token)
    assert full.status_code == 409
    assert "上限" in full.json()["detail"]


def test_expired_sessions_are_removed(capture_env, monkeypatch):
    client, _, tmp_path = capture_env
    session_id, token, _ = _open(client)
    assert _upload(client, session_id, token).status_code == 200
    session_dir = os.path.join(str(tmp_path), "capture_sessions", session_id)
    assert os.path.isdir(session_dir)

    real_time = time.time
    monkeypatch.setattr(capture_session.time, "time", lambda: real_time() + capture_session.SESSION_TTL_SECONDS + 1)
    assert _upload(client, session_id, token).status_code == 410
    assert not os.path.exists(session_dir)


def test_sweep_removes_only_expired_sessions(capture_env, monkeypatch):
    client, current, tmp_path = capture_env
    old_id, _, _ = _open(client)
    current["user"] = User(id=2, name="t2", role="teacher", education_stage="senior")
    later = time.time() + capture_session.SESSION_TTL_SECONDS / 2
    monkeypatch.setattr(capture_session.time, "time", lambda: later)
    new_id, _, _ = _open(client)

    removed = capture_session.sweep_expired_sessions(later + capture_session.SESSION_TTL_SECONDS / 2 + 1)
    root = os.path.join(str(tmp_path), "capture_sessions")
    assert removed == 1
    assert not os.path.exists(os.path.join(root, old_id))
    assert os.path.isdir(os.path.join(root, new_id))


def test_invalid_session_ids_never_touch_the_filesystem(capture_env):
    client, _, _ = capture_env
    _open(client)
    for bad in ("..", "abc", "../capture_sessions", "A" * 24):
        assert client.get(f"/api/capture/sessions/{bad}").status_code == 404
        assert _upload(client, bad, "token").status_code in {403, 404}


def test_open_requires_teaching_stage(capture_env):
    client, current, _ = capture_env
    current["user"] = User(id=3, name="t3", role="teacher", education_stage=None)
    assert client.post("/api/capture/sessions").status_code == 428


def test_capture_page_alone_may_use_the_camera():
    from app import _permissions_policy

    assert _permissions_policy("/capture/0123456789abcdef01234567").startswith("camera=(self)")
    assert _permissions_policy("/upload").startswith("camera=()")
    assert _permissions_policy("/api/capture/sessions").startswith("camera=()")
    assert _permissions_policy("/captured").startswith("camera=()")
