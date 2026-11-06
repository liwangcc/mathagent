import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models import GradingResult, Student
from models_user import User
from scripts import cleanup_files as cleanup
from services.answer_sheet_session import AnswerSheetSession
from services.quick_grader import QuickBatch


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=40)).isoformat()
RECENT = (NOW - timedelta(hours=12)).isoformat()


def _write_meta(group_dir: Path, payload: dict) -> Path:
    group_dir.mkdir(parents=True, exist_ok=True)
    meta_path = group_dir / "meta.json"
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return meta_path


def _quick_meta(*, status: str, identity_mode: str, image_path: Path, timestamp: str) -> dict:
    return {
        "batch_id": "abc123def456",
        "status": status,
        "identity_mode": identity_mode,
        "created_at": OLD,
        "review_ready_at": timestamp if status == "review_ready" else "",
        "review_archived_at": timestamp if status == "archived" else "",
        "uploads": [{
            "paper_id": "paper-1",
            "filename": "student.png",
            "image_path": str(image_path),
            "original_image_path": str(image_path),
        }],
        "papers": [{
            "paper_id": "paper-1",
            "image_path": str(image_path),
            "questions": [{"question_no": "1", "student_answer": "A"}],
        }],
        "results": [{
            "paper_id": "paper-1",
            "wrong": 1,
            "correct_rate": 0.5,
            "details": [{"question_no": "1", "result": "wrong"}],
        }],
        "review_results": [{
            "paper_id": "paper-1",
            "disposition": "archived" if status == "archived" else "active",
            "archived_at": timestamp if status == "archived" else "",
            "result": {
                "wrong_count": 1,
                "correct_rate": 0.5,
                "details": [{"question_no": "1", "result": "wrong"}],
            },
        }],
        "asset_lifecycle": {},
    }


def test_identified_review_ready_batch_is_protected_even_when_old(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "quick_batch_abc123def456"
    image = group / "student.png"
    group.mkdir(parents=True)
    image.write_bytes(b"paper")
    _write_meta(
        group,
        _quick_meta(
            status="review_ready",
            identity_mode="identified",
            image_path=image,
            timestamp=OLD,
        ),
    )

    result = cleanup.cleanup_quick_batch_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )

    assert result["protected_active"] == 1
    assert result["deleted_files"] == 0
    assert image.is_file()


def test_identified_archived_batch_deletes_images_but_preserves_results(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "quick_batch_abc123def456"
    image = group / "student.png"
    derived = group / "derived" / "crop.jpg"
    derived.parent.mkdir(parents=True)
    image.write_bytes(b"paper")
    derived.write_bytes(b"crop")
    original = _quick_meta(
        status="archived",
        identity_mode="identified",
        image_path=image,
        timestamp=OLD,
    )
    expected_results = json.loads(json.dumps(original["results"], ensure_ascii=False))
    expected_review_result = original["review_results"][0]["result"]
    _write_meta(group, original)

    result = cleanup.cleanup_quick_batch_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )
    retained = json.loads((group / "meta.json").read_text(encoding="utf-8"))

    assert result["deleted_files"] == 2
    assert result["expired_groups"] == 1
    assert not image.exists()
    assert not derived.exists()
    assert retained["results"] == expected_results
    assert retained["review_results"][0]["result"] == expected_review_result
    assert retained["uploads"][0]["image_path"] == ""
    assert retained["asset_lifecycle"]["status"] == "expired"
    assert retained["asset_lifecycle"]["retention_hours"] == 36


def test_anonymous_completed_batch_retains_wrong_questions_and_correct_rate(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "quick_batch_abc123def456"
    image = group / "anonymous.webp"
    group.mkdir(parents=True)
    image.write_bytes(b"paper")
    meta = _quick_meta(
        status="review_ready",
        identity_mode="anonymous",
        image_path=image,
        timestamp=OLD,
    )
    _write_meta(group, meta)

    result = cleanup.cleanup_quick_batch_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )
    retained = json.loads((group / "meta.json").read_text(encoding="utf-8"))

    assert result["expired_groups"] == 1
    assert not image.exists()
    assert retained["results"][0]["wrong"] == 1
    assert retained["results"][0]["correct_rate"] == 0.5
    assert retained["results"][0]["details"][0]["result"] == "wrong"


def test_recent_terminal_batch_and_dry_run_do_not_mutate_files(tmp_path):
    uploads = tmp_path / "uploads"
    recent_group = uploads / "quick_batch_abc123def456"
    recent_image = recent_group / "recent.png"
    recent_group.mkdir(parents=True)
    recent_image.write_bytes(b"recent")
    _write_meta(
        recent_group,
        _quick_meta(
            status="archived",
            identity_mode="identified",
            image_path=recent_image,
            timestamp=RECENT,
        ),
    )

    recent = cleanup.cleanup_quick_batch_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )
    assert recent["recent"] == 1
    assert recent_image.is_file()

    dry_group = uploads / "quick_batch_def456abc123"
    dry_image = dry_group / "old.png"
    dry_group.mkdir(parents=True)
    dry_image.write_bytes(b"old")
    dry_meta = _quick_meta(
        status="archived",
        identity_mode="identified",
        image_path=dry_image,
        timestamp=OLD,
    )
    dry_meta["batch_id"] = "def456abc123"
    dry_meta_path = _write_meta(dry_group, dry_meta)
    before = dry_meta_path.read_bytes()

    dry = cleanup.cleanup_quick_batch_images(
        36,
        True,
        upload_dir=str(uploads),
        now=NOW,
    )
    assert dry["candidates"] == 1
    assert dry["would_delete_files"] == 1
    assert dry_image.is_file()
    assert dry_meta_path.read_bytes() == before
    assert not (dry_group / ".review-operation.lock").exists()


def test_archived_answer_sheet_deletes_images_and_keeps_structured_drafts(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "answer_sheet_abc123def456"
    image = group / "student.png"
    template = group / "layout" / "template.jpg"
    template.parent.mkdir(parents=True)
    image.write_bytes(b"student")
    template.write_bytes(b"template")
    decisions = [{
        "student_id": 10,
        "result": {
            "wrong_count": 1,
            "correct_rate": 0.5,
            "details": [{"question_no": "1", "result": "wrong"}],
        },
    }]
    _write_meta(group, {
        "session_id": "abc123def456",
        "teacher_id": 1,
        "status": "archived",
        "created_at": OLD,
        "updated_at": OLD,
        "student_pages": [{
            "paper_id": "paper-1",
            "filename": "student.png",
            "path": str(image),
        }],
        "layout_artifacts": {
            "page-1": {"template_image": "layout/template.jpg"},
        },
        "grouping_decisions": decisions,
        "archived_result_ids": [99],
        "asset_lifecycle": {
            "policy": "raw_images_expire_after_36h",
            "status": "retained_until_expiry",
            "retention_hours": 36,
            "archived_at": OLD,
            "result_ids": [99],
        },
    })

    result = cleanup.cleanup_answer_sheet_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )
    retained = json.loads((group / "meta.json").read_text(encoding="utf-8"))

    assert result["deleted_files"] == 2
    assert result["expired_groups"] == 1
    assert retained["grouping_decisions"] == decisions
    assert retained["archived_result_ids"] == [99]
    assert retained["student_pages"][0]["path"] == ""
    assert retained["asset_lifecycle"]["status"] == "expired"


def test_non_archived_answer_sheet_is_protected(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "answer_sheet_abc123def456"
    image = group / "student.png"
    group.mkdir(parents=True)
    image.write_bytes(b"student")
    _write_meta(group, {
        "session_id": "abc123def456",
        "status": "review",
        "created_at": OLD,
        "updated_at": OLD,
        "student_pages": [{"paper_id": "paper-1", "path": str(image)}],
        "asset_lifecycle": {},
    })

    result = cleanup.cleanup_answer_sheet_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )

    assert result["protected_active"] == 1
    assert image.is_file()


def test_cleanup_does_not_follow_image_symlink_outside_batch(tmp_path):
    uploads = tmp_path / "uploads"
    group = uploads / "quick_batch_abc123def456"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    group.mkdir(parents=True)
    link = group / "student.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _write_meta(
        group,
        _quick_meta(
            status="archived",
            identity_mode="identified",
            image_path=link,
            timestamp=OLD,
        ),
    )

    result = cleanup.cleanup_quick_batch_images(
        36,
        False,
        upload_dir=str(uploads),
        now=NOW,
    )

    assert result["skipped_unsafe"] >= 1
    assert outside.read_bytes() == b"outside"


def test_quick_batch_serializes_retention_lifecycle(tmp_path):
    batch = QuickBatch("abc123def456", str(tmp_path))
    batch.review_ready_at = OLD
    batch.asset_lifecycle = {"status": "expired", "retention_hours": 36}
    batch.save_meta()

    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))

    assert restored.review_ready_at == OLD
    assert restored.asset_lifecycle == {"status": "expired", "retention_hours": 36}


def test_expired_batch_image_endpoint_returns_privacy_message(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.grade as grade_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    from routers.auth import _require_user

    uploads = tmp_path / "uploads"
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(grade_router, "UPLOAD_DIR", str(uploads))
    batch = QuickBatch("abc123def456", str(uploads))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.asset_lifecycle = {"status": "expired"}
    batch.save_meta()

    app = FastAPI()
    app.include_router(grade_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher")
    client = TestClient(app)

    response = client.get("/api/batch-grade/abc123def456/paper/paper-1/image")

    assert response.status_code == 410
    assert "原卷已按隐私保留策略清理" in response.json()["detail"]
    assert "正确率记录仍保留" in response.json()["detail"]


def test_grading_detail_marks_expired_answer_sheet_unavailable(monkeypatch, tmp_path):
    from routers import grading_records

    uploads = tmp_path / "uploads"
    monkeypatch.setattr(grading_records, "UPLOAD_DIR", str(uploads))
    session = AnswerSheetSession("abc123def456", str(uploads), teacher_id=1, class_id=1)
    session.status = "archived"
    session.student_pages = [{
        "paper_id": "paper-1",
        "filename": "student.png",
        "path": "",
    }]
    session.asset_lifecycle = {
        "status": "expired",
        "expired_at": NOW.isoformat(),
    }
    session.save()

    result = GradingResult(
        id=99,
        student_id=10,
        category="daily",
        total_questions=2,
        correct_count=1,
        wrong_count=1,
        correct_rate=0.5,
        grading_details=json.dumps(
            [{"question_no": "2", "result": "wrong"}],
            ensure_ascii=False,
        ),
        ocr_raw_json=json.dumps({
            "source": "answer_sheet",
            "session_id": session.session_id,
            "pages": [{
                "paper_id": "paper-1",
                "page_index": 0,
                "filename": "student.png",
            }],
        }, ensure_ascii=False),
    )
    student = Student(id=10, teacher_id=1, name="学生")

    class _Query:
        def __init__(self, value):
            self.value = value

        def filter(self, *_args):
            return self

        def first(self):
            return self.value

    class _Db:
        def query(self, model):
            return _Query(result if model is GradingResult else student)

    payload = grading_records.get_grading_result(
        result_id=99,
        db=_Db(),
        user=User(id=1, name="老师", role="teacher"),
    )

    assert payload["wrong_count"] == 1
    assert payload["correct_rate"] == 0.5
    assert payload["grading_details"][0]["result"] == "wrong"
    assert payload["source_assets"]["available"] is False
    assert payload["source_assets"]["expired_at"] == NOW.isoformat()
