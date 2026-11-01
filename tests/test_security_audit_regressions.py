import threading
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def test_failed_imports_are_isolated_by_teacher(tmp_path, monkeypatch):
    from routers import teacher

    failed_root = tmp_path / "failed"
    monkeypatch.setattr(teacher, "FAILED_DIR", str(failed_root))

    source_a = tmp_path / "a.png"
    source_b = tmp_path / "b.png"
    source_a.write_bytes(b"teacher-a")
    source_b.write_bytes(b"teacher-b")

    for teacher_id, source, reason in ((101, source_a, "A reason"), (202, source_b, "B reason")):
        teacher_dir = failed_root / str(teacher_id)
        teacher_dir.mkdir(parents=True)
        dest = teacher_dir / f"20260101_000000_000000_{source.name}"
        dest.write_bytes(source.read_bytes())
        (teacher_dir / f"{dest.name}.txt").write_text(f"失败原因: {reason}\n", encoding="utf-8")

    listed_a = teacher.get_failed_imports(page=1, page_size=50, user=SimpleNamespace(id=101))
    listed_b = teacher.get_failed_imports(page=1, page_size=50, user=SimpleNamespace(id=202))
    assert [item["filename"].endswith("a.png") for item in listed_a["items"]] == [True]
    assert [item["filename"].endswith("b.png") for item in listed_b["items"]] == [True]
    assert "B reason" not in repr(listed_a)
    assert "A reason" not in repr(listed_b)

    foreign_path = listed_b["items"][0]["image_path"]
    with pytest.raises(HTTPException) as exc:
        teacher.delete_failed_import(
            teacher.ImagePathRequest(image_path=foreign_path),
            user=SimpleNamespace(id=101),
        )
    assert exc.value.status_code in {403, 404}

    with pytest.raises(HTTPException) as upload_exc:
        teacher.delete_failed_import(
            teacher.ImagePathRequest(image_path="/uploads/active-paper.png"),
            user=SimpleNamespace(id=101),
        )
    assert upload_exc.value.status_code == 404

    deleted = teacher.delete_all_failed_imports(user=SimpleNamespace(id=101))
    assert deleted["deleted"] == 2
    assert not any((failed_root / "101").iterdir())
    assert any((failed_root / "202").iterdir())


def test_class_wide_parent_link_is_disabled_fail_closed():
    from routers import parent_letter

    with pytest.raises(HTTPException) as create_exc:
        parent_letter.create_class_parent_link(
            1,
            db=None,
            user=SimpleNamespace(id=101),
        )
    assert create_exc.value.status_code == 410

    with pytest.raises(HTTPException) as exc:
        parent_letter.class_letter_verify(
            "any-token",
            parent_letter.VerifyRequest(last4="0000"),
            db=None,
        )
    assert exc.value.status_code == 410


def test_password_failure_increment_is_atomic(tmp_path):
    from services.auth_service import _PasswordFailureStore

    store = _PasswordFailureStore(str(tmp_path / "password_failures.db"))
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            store.record_failure("13800000000", max_attempts=50, lockout_duration=900)
        except BaseException as exc:  # pragma: no cover - reported by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert store.get("13800000000")["attempts"] == 8


def test_ip_failure_increment_is_atomic(tmp_path, monkeypatch):
    from services import ip_rate_limit

    db_path = str(tmp_path / "ip_failures.db")
    monkeypatch.setattr(ip_rate_limit, "DB_PATH", db_path)
    monkeypatch.setattr(ip_rate_limit, "IP_LOCK_THRESHOLD", 50)
    monkeypatch.setattr(ip_rate_limit, "IP_BAN_THRESHOLD", 100)
    ip_rate_limit._init_db()
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            ip_rate_limit.record_ip_failure("203.0.113.7")
        except BaseException as exc:  # pragma: no cover - reported by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with sqlite3.connect(db_path) as conn:
        attempts = conn.execute(
            "SELECT attempts FROM ip_failures WHERE ip=?",
            ("203.0.113.7",),
        ).fetchone()[0]
    assert errors == []
    assert attempts == 8
