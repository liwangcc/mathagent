"""Empty review ``image_path`` rows are the retention scrub, not a broken chain.

Four production batches (8f2ae52e / cc94236c / 0bd1276c / dd21b900) returned
every review row with ``image_path == ""`` and only ``image_content_hash``.
The mirrored 172211 record (outputs/recognize-fail-20260929/ocr-evidence.json)
also has ``source_relative_path`` and ``original_image_path`` blank with
``source_sha256`` kept, which is exactly what scripts/cleanup_files.py leaves
after it deletes an anonymous review_ready batch's images 36 h later.
``image_content_hash`` is an archive idempotency key, not a content address.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from models_user import User
from scripts import cleanup_files as cleanup
from services.quick_grader import QuickBatch

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=40)).isoformat()
PRIVACY = "原卷已按隐私保留策略清理"


def _batch_with_image(uploads: Path, *, review_ready_at: str = OLD) -> tuple[QuickBatch, Path]:
    batch = QuickBatch("dd21b9006282", str(uploads))
    Path(batch.batch_dir).mkdir(parents=True)
    image = Path(batch.batch_dir) / "a1b2c3.ocr-ready-v1.jpg"
    image.write_bytes(b"student page")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    source = {
        "image_path": str(image),
        "original_image_path": str(image),
        "source_relative_path": "测试素材.rar/测试素材/IMG_20260913_172211.jpg",
        "source_sha256": digest,
        "working_sha256": digest,
    }
    batch.teacher_id = 1
    batch.identity_mode = "anonymous"
    batch.status = "review_ready"
    batch.review_ready_at = review_ready_at
    batch.uploads = [{"paper_id": "9fa9cd6a194e", "filename": "IMG_20260913_172211.jpg", **source}]
    batch.papers = [{"paper_id": "9fa9cd6a194e", "filename": "IMG_20260913_172211.jpg", **source,
                     "questions": [{"question_no": "1", "student_answer": "[无法识别]"}]}]
    result = {"total": 1, "needs_review_count": 1, "details": [{"question_no": "1", "result": "needs_review"}]}
    batch.review_results = [{"review_id": "9fa9cd6a194e", "paper_id": "9fa9cd6a194e",
                             "filename": "IMG_20260913_172211.jpg", "student_name": "匿名页 20",
                             "image_path": str(image), "image_content_hash": digest,
                             "disposition": "active", "result": result}]
    batch.results = [{"review_id": "9fa9cd6a194e", "paper_id": "9fa9cd6a194e", "details": result["details"]}]
    batch.save_meta()
    return batch, image


def test_retention_scrub_produces_the_production_empty_path_shape(tmp_path):
    uploads = tmp_path / "uploads"
    batch, image = _batch_with_image(uploads)
    digest = batch.review_results[0]["image_content_hash"]

    summary = cleanup.cleanup_quick_batch_images(36, False, upload_dir=str(uploads), now=NOW)

    assert summary["expired_groups"] == 1 and not image.exists()
    meta = json.loads(Path(batch.meta_path).read_text(encoding="utf-8"))
    paper, review = meta["papers"][0], meta["review_results"][0]
    # Identical to the mirrored 172211 record: every *_path blank, hashes kept.
    assert paper["image_path"] == paper["original_image_path"] == paper["source_relative_path"] == ""
    assert paper["source_sha256"] == paper["working_sha256"] == digest
    assert review["image_path"] == "" and review["image_content_hash"] == digest
    assert review["result"]["needs_review_count"] == 1
    assert meta["asset_lifecycle"]["status"] == "expired"


def _client(monkeypatch, uploads):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.grade as grade_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    from routers.auth import _require_user

    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(grade_router, "UPLOAD_DIR", str(uploads))
    app = FastAPI()
    app.include_router(grade_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher")
    return TestClient(app)


def test_expired_empty_path_rows_keep_results_and_explain_the_missing_image(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    _batch_with_image(uploads)
    cleanup.cleanup_quick_batch_images(36, False, upload_dir=str(uploads), now=NOW)
    client = _client(monkeypatch, uploads)

    state = client.get("/api/batch-grade/review/dd21b9006282")
    assert state.status_code == 200, state.text
    assert state.json()["asset_lifecycle"]["status"] == "expired"
    assert state.json()["results"][0]["needs_review"] == 1

    # Student window (active/original/review variants) and review-image export.
    for variant in ("active", "original", "review"):
        response = client.get(f"/api/batch-grade/dd21b9006282/paper/9fa9cd6a194e/image?variant={variant}")
        assert response.status_code == 410, (variant, response.text)
        assert PRIVACY in response.json()["detail"]
    export = client.post("/api/batch-grade/review/export/images", json={"batch_id": "dd21b9006282"})
    assert export.status_code == 410
    assert PRIVACY in export.json()["detail"]


def test_blank_path_without_expiry_is_reported_missing_not_served(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    batch, _image = _batch_with_image(uploads, review_ready_at=datetime.now(timezone.utc).isoformat())
    batch.papers[0]["image_path"] = batch.papers[0]["original_image_path"] = ""
    batch.uploads[0]["image_path"] = batch.uploads[0]["original_image_path"] = ""
    batch.save_meta()
    client = _client(monkeypatch, uploads)

    response = client.get("/api/batch-grade/dd21b9006282/paper/9fa9cd6a194e/image?variant=review")

    assert response.status_code == 404
    assert response.json()["detail"] == "试卷不存在或已被清理"


def test_grading_key_never_collapses_to_a_shared_anonymous_label(monkeypatch):
    from services import grader

    keys = []

    class CapturingQueue:
        async def submit(self, coro_factory, **kwargs):
            keys.append(kwargs["idempotency_key"])
            return await coro_factory()

    async def judge(questions, **_kwargs):
        return {q["question_no"]: {"result": "correct", "explanation": ""} for q in questions}

    monkeypatch.setattr(grader, "get_grading_queue", lambda: CapturingQueue())
    monkeypatch.setattr(grader, "judge_paper", judge)
    answer_map = {"1": {"question_type": "solve", "standard_answer": "3", "matched": True}}
    for working_sha256 in ("a" * 64, "b" * 64):
        paper = {"paper_id": working_sha256[:12], "image_path": "", "working_sha256": working_sha256,
                 "questions": [{"question_no": "1", "student_answer": "3", "answer_state": "visible",
                                "question_type": "solve"}]}
        asyncio.run(grader.grade_paper_unified(paper, answer_map, "匿名页 1", 1, category="daily"))

    # Two different pages with the same label and no image file on disk.
    assert len(keys) == 2 and keys[0] != keys[1]
