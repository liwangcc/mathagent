from services.quick_grader import QuickBatch


def test_orientation_metadata_and_batch_contract_survive_reload(tmp_path):
    batch = QuickBatch("batch-1", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 9
    batch.source_mode = "template"
    batch.template_id = "template-1"
    batch.category = "exam"
    batch.exam_name = "期中测试"
    batch.grade_mode = "precise"
    batch.confirmation_questions = [{"question_no": "1", "answer": "A"}]
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(tmp_path / "original.png"),
        "original_image_path": str(tmp_path / "original.png"),
        "source_sha256": "a" * 64,
        "working_sha256": "a" * 64,
        "orientation_status": "needs_review",
        "orientation_history": [],
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.save_meta()

    restored = QuickBatch.load_meta("batch-1", str(tmp_path))

    assert restored.template_id == "template-1"
    assert restored.category == "exam"
    assert restored.exam_name == "期中测试"
    assert restored.grade_mode == "precise"
    assert restored.confirmation_questions == [{"question_no": "1", "answer": "A"}]
    assert restored.ocr_jobs[0]["paper_id"] == "paper-1"
    assert restored.uploads[0]["source_sha256"] == "a" * 64


def test_replacing_orientation_working_path_keeps_one_ocr_job(tmp_path):
    original = str(tmp_path / "original.png")
    working = str(tmp_path / "working.png")
    batch = QuickBatch("batch-2", str(tmp_path))
    upload = {
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": original,
    }
    batch.ensure_ocr_jobs([upload])
    batch.mark_ocr_job(original, "failed", "方向未确认")

    upload["image_path"] = working
    batch.replace_ocr_job_path(original, upload)

    assert len(batch.ocr_jobs) == 1
    assert batch.ocr_jobs[0]["paper_id"] == "paper-1"
    assert batch.ocr_jobs[0]["image_path"] == working
    assert batch.ocr_jobs[0]["status"] == "queued"
    assert batch.ocr_jobs[0]["last_error"] == ""
