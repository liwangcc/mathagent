import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageDraw

from routers import grade
from routers import grade_pipeline
from services.paper_orientation import sha256_file
from services.quick_grader import QuickBatch
from services.template_session import TemplateSession


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return []


class _Db:
    def query(self, *args, **kwargs):
        return _Query()


class _Limiter:
    calls = []

    def __init__(self, db):
        self.db = db

    def record_usage(self, teacher_id, count, **kwargs):
        self.calls.append((teacher_id, count, kwargs))


def _sideways_lined_page(path: Path) -> None:
    upright = Image.new("L", (500, 700), 255)
    draw = ImageDraw.Draw(upright)
    for y in range(90, 630, 38):
        draw.line((50, y, 450, y), fill=0, width=3)
    upright.rotate(90, expand=True, fillcolor=255).save(path)


def _client_rotated_upload(path: Path, turns: int) -> UploadFile:
    output = BytesIO()
    with Image.open(path) as image:
        image.convert("RGB").rotate(-90 * turns, expand=True, fillcolor="white").save(
            output,
            "JPEG",
        )
    output.seek(0)
    return UploadFile(
        filename=f"{path.stem}.ocr-ready-v1.jpg",
        file=output,
    )


def _frozen_template(tmp_path: Path, template_id: str) -> TemplateSession:
    template = TemplateSession(template_id, str(tmp_path), teacher_id=7)
    template.status = "frozen"
    template.category = "daily"
    template.add_page(0, [{
        "question_number": "1",
        "question_text": "1+1=？",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    template.confirm_current_contract(7)
    template.save_meta()
    return template


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_orientation_confirm_retries_once_and_preserves_original(tmp_path, monkeypatch):
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    batch = QuickBatch("batch-1", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 9
    batch.source_mode = "template"
    batch.template_id = _frozen_template(tmp_path, "orientation-template").template_id
    batch.category = "daily"
    batch.grade_mode = "precise"
    batch.status = "failed"
    batch_dir = Path(batch.batch_dir)
    batch_dir.mkdir(parents=True)
    original = batch_dir / "source.png"
    _sideways_lined_page(original)
    source_hash = sha256_file(original)
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "source.png",
        "image_path": str(original),
        "original_image_path": str(original),
        "source_sha256": source_hash,
        "working_sha256": source_hash,
        "manual_quarter_turns": 0,
        "orientation_status": "needs_review",
        "orientation_reason": "orientation_confidence_too_low",
        "orientation_history": [],
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(str(original), "failed", "试卷方向异常或未确认")
    batch.save_meta()

    calls = {"ocr": 0}
    phases = []

    async def fake_recognize(path, _batch_id, _paper_cache, **_kwargs):
        from services.ocr_service import _vision_phase_key

        calls["ocr"] += 1
        phases.append(_vision_phase_key.get())
        return {
            "student_name": "",
            "student_no": "",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=？",
                "student_answer": "2",
                "question_type": "fill",
            }],
        }

    def fake_initialize(target, *args, **kwargs):
        target.answer_map = {
            "1": {
                "matched": True,
                "standard_answer": "2",
                "question_text": "1+1=？",
                "question_type": "fill",
                "source": "test",
            }
        }

    monkeypatch.setattr(grade, "recognize_paper_with_cache", fake_recognize)
    monkeypatch.setattr(grade_pipeline, "_initialize_batch_answer_map", fake_initialize)
    monkeypatch.setattr(grade, "_initialize_batch_answer_map", fake_initialize)
    monkeypatch.setattr(grade, "RateLimiter", _Limiter)
    user = SimpleNamespace(id=7, current_class_id=9)
    client_upload = _client_rotated_upload(original, 1)

    first = await grade.batch_grade_confirm_orientation(
        "batch-1", "paper-1",
        action_id="orientation-action-1",
        quarter_turns_clockwise=1,
        file=client_upload,
        user=user,
        db=_Db(),
    )
    second = await grade.batch_grade_confirm_orientation(
        "batch-1", "paper-1",
        action_id="orientation-action-1",
        quarter_turns_clockwise=1,
        file=client_upload,
        user=user,
        db=_Db(),
    )

    assert calls["ocr"] == 1
    assert phases == ["orientation-retry:paper-1:orientation-action-1"]
    assert first["can_grade"] is True
    assert second["can_grade"] is True
    assert first["papers"][0]["orientation_status"] == "teacher_confirmed"
    restored = QuickBatch.load_meta("batch-1", str(tmp_path))
    upload = restored.uploads[0]
    assert sha256_file(original) == source_hash
    assert upload["original_image_path"] == str(original)
    assert upload["image_path"] != str(original)
    assert upload["manual_quarter_turns"] == 1
    assert upload["orientation_history"][-1]["status"] == "complete"
    assert len(restored.ocr_jobs) == 1
    assert restored.ocr_jobs[0]["status"] == "succeeded"
    assert restored.confirmation_questions[0]["answer"] == "2"

    state = grade.batch_grade_orientation_state("batch-1", user=user)
    assert state["papers"][0]["original_preserved"] is True
    assert state["questions"][0]["answer"] == "2"
    assert str(original) not in json.dumps(state, ensure_ascii=False)
    original_response = grade.batch_grade_paper_image(
        "batch-1", "paper-1", variant="original", user=user
    )
    assert Path(original_response.path) == original

    foreign_user = SimpleNamespace(id=8, current_class_id=9)
    with pytest.raises(HTTPException) as denied_state:
        grade.batch_grade_orientation_state("batch-1", user=foreign_user)
    assert denied_state.value.status_code == 404
    with pytest.raises(HTTPException) as denied_image:
        grade.batch_grade_paper_image(
            "batch-1", "paper-1", variant="original", user=foreign_user
        )
    assert denied_image.value.status_code == 404

    restored.papers.append({
        "paper_id": "paper-2",
        "filename": "blocked.png",
        "orientation_status": "needs_review",
    })
    with pytest.raises(HTTPException) as hard_gate:
        grade._require_orientation_ready(restored)
    assert hard_gate.value.status_code == 409


@pytest.mark.anyio
async def test_orientation_confirm_does_not_override_continuous_tilt(tmp_path, monkeypatch):
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    batch = QuickBatch("batch-tilted", str(tmp_path))
    batch.teacher_id = 7
    batch.class_id = 9
    batch.source_mode = "template"
    batch.template_id = _frozen_template(tmp_path, "tilted-template").template_id
    batch.status = "failed"
    batch_dir = Path(batch.batch_dir)
    batch_dir.mkdir(parents=True)
    original = batch_dir / "tilted.png"
    upright = Image.new("L", (500, 700), 255)
    draw = ImageDraw.Draw(upright)
    for y in range(90, 630, 38):
        draw.line((50, y, 450, y), fill=0, width=3)
    upright.rotate(30, expand=True, fillcolor=255).save(original)
    source_hash = sha256_file(original)
    batch.uploads = [{
        "paper_id": "paper-tilted",
        "filename": "tilted.png",
        "image_path": str(original),
        "original_image_path": str(original),
        "source_sha256": source_hash,
        "working_sha256": source_hash,
        "orientation_status": "invalid",
        "orientation_reason": "page_tilt_exceeds_limit",
        "orientation_history": [],
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(str(original), "failed", "试卷方向异常或未确认")
    batch.save_meta()

    async def forbidden_ocr(*args, **kwargs):
        raise AssertionError("tilted paper must not enter OCR")

    monkeypatch.setattr(grade, "recognize_paper_with_cache", forbidden_ocr)
    with pytest.raises(HTTPException) as rejected:
        await grade.batch_grade_confirm_orientation(
            "batch-tilted",
            "paper-tilted",
            action_id="orientation-tilt-action",
            quarter_turns_clockwise=0,
            file=_client_rotated_upload(original, 0),
            user=SimpleNamespace(id=7, current_class_id=9),
            db=_Db(),
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["error"] == "ORIENTATION_STILL_INVALID"
    restored = QuickBatch.load_meta("batch-tilted", str(tmp_path))
    assert restored.uploads[0]["image_path"] == str(original)
    assert restored.uploads[0]["orientation_history"] == []
    assert restored.ocr_jobs[0]["status"] == "failed"
    assert sha256_file(original) == source_hash
