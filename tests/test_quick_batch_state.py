import json

from services.quick_grader import QuickBatch, batch_operation_lock


def test_quick_batch_round_trips_safety_state(tmp_path):
    batch = QuickBatch("safe1", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 3
    batch.source_mode = "template"
    batch.template_id = "template-1"
    batch.uploads = [{"paper_id": "p1", "filename": "a.png", "image_path": "a.png"}]
    batch.pending_uploads = [{"paper_id": "p2", "filename": "b.png", "image_path": "b.png"}]
    batch.review_revision = 4
    batch.save_meta()

    loaded = QuickBatch.load_meta("safe1", str(tmp_path))

    assert loaded.teacher_id == 7
    assert loaded.class_id == 3
    assert loaded.source_mode == "template"
    assert loaded.template_id == "template-1"
    assert loaded.uploads == batch.uploads
    assert loaded.pending_uploads == batch.pending_uploads
    assert loaded.review_revision == 4


def test_quick_batch_loads_legacy_meta_with_safe_defaults(tmp_path):
    batch = QuickBatch("legacy", str(tmp_path))
    batch_dir = tmp_path / "quick_batch_legacy"
    batch_dir.mkdir()
    # Fields of the retired no-template flow may still be on disk; they are
    # ignored rather than breaking the load.
    (batch_dir / "meta.json").write_text(json.dumps({
        "batch_id": "legacy",
        "status": "ocr_done",
        "questions": [],
        "papers": [],
        "answer_map": {},
        "results": [],
        "expected_num_types": 2,
        "baseline_candidates": {"page_1": {"sample_count": 5}},
        "confirmed_baselines": {},
        "baseline_resume_generation": 3,
    }), encoding="utf-8")

    loaded = QuickBatch.load_meta("legacy", str(tmp_path))

    assert loaded.teacher_id is None
    assert loaded.class_id is None
    assert loaded.source_mode == ""
    assert loaded.uploads == []
    assert loaded.pending_uploads == []
    assert "expected_num_types" not in loaded.to_dict()
    assert loaded.review_revision == 0
    assert loaded.ocr_jobs == []


def test_quick_batch_persists_and_recovers_ocr_job_states(tmp_path):
    batch = QuickBatch("jobs", str(tmp_path))
    batch.ocr_jobs = [
        {"job_id": "p1", "filename": "a.png", "status": "succeeded", "attempts": 1},
        {"job_id": "p2", "filename": "b.png", "status": "running", "attempts": 1},
    ]
    batch.save_meta()

    restored = QuickBatch.load_meta("jobs", str(tmp_path))

    assert restored.ocr_jobs[0]["status"] == "succeeded"
    assert restored.ocr_jobs[1]["status"] == "running"
    with batch_operation_lock(batch.batch_id, str(tmp_path)):
        restored.recover_interrupted_ocr_jobs()
    assert restored.ocr_jobs[1]["status"] == "queued"
    assert restored.ocr_jobs[1]["last_error"] == "服务重启后等待恢复"
