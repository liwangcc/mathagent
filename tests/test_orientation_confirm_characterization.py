"""Golden characterization of ``POST /batch-grade/{id}/paper/{pid}/orientation-confirm``.

``batch_grade_confirm_orientation`` was one 247-line handler mixing the batch
lock, idempotent replays by ``action_id``, source/template evidence checks,
the OCR job ledger and a single-page OCR retry.  Before it was split this
file pinned, per scenario: the HTTP status and body, the raw ``meta.json``,
the ordered checkpoint/lock ledger, the context of every OCR call and the
files left in the batch directory.  It reuses the recognition-stream harness
(same boundaries, same normalization).

    MATHAGENT_UPDATE_GOLDEN=1 pytest tests/test_orientation_confirm_characterization.py
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from services.quick_grader import QuickBatch
from tests.test_batch_calibration_flow import _setup
from tests.test_recognition_stream_characterization import (  # noqa: F401 - autouse fixtures
    _FINAL_FAILURE,
    _LEDGER,
    _RETRYABLE_FAILURE,
    _Boundary,
    _Normalizer,
    _answer,
    _busy_lock,
    _lock_released,
    _raw_meta,
    _template,
    deterministic_runtime,
    record_ledger,
)


GOLDEN_DIR = Path(__file__).parent / "golden" / "orientation_confirm"
UPDATE_GOLDEN = os.environ.get("MATHAGENT_UPDATE_GOLDEN") == "1"
BATCH_ID = "orientation-characterization"
ACTION_ID = "action-0001"


def _assert_golden(name: str, observed: dict) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(observed, ensure_ascii=False, indent=1) + "\n"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"missing golden {path}; generate it with MATHAGENT_UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    assert json.loads(rendered) == json.loads(expected)
    assert rendered == expected, "same values, different key order"


def _batch(tmp_path, *, status="ocr_partial_failed", review_page=1, pages=3, review_attempts=1):
    """Pages succeed except ``review_page``, whose direction is still unconfirmed."""
    batch = QuickBatch(BATCH_ID, str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.identity_mode = "anonymous"
    batch.grade_mode = "auto"
    batch.category = "daily"
    batch.source_mode = "template"
    batch.status = status
    batch.template_id = _template(tmp_path).template_id
    directory = tmp_path / f"quick_batch_{BATCH_ID}"
    directory.mkdir()
    for index in range(pages):
        image = directory / f"paper-{index}.png"
        image.write_bytes(f"synthetic-image-{index}".encode())
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        ready = index != review_page
        upload = {
            "paper_id": f"paper-{index}", "filename": image.name,
            "image_path": str(image), "original_image_path": str(image),
            "source_sha256": digest, "working_sha256": digest,
            "manual_quarter_turns": 0, "orientation_history": [],
            "orientation_status": "auto_passed" if ready else "needs_review",
            "orientation_reason": "normal_angle" if ready else "orientation_confidence_too_low",
            "orientation_confidence": 1.0 if ready else 0.2,
            "detected_tilt_degrees": 0.0,
        }
        batch.uploads.append(upload)
        if ready:
            batch.papers.append({
                **upload, "student_name": "", "student_no": "",
                "identity": {"status": "anonymous"},
                "questions": [{"question_no": "1", "question_type": "fill", "student_answer": "2"}],
            })
    batch.questions = [{"question_no": "1", "question_text": "1+1=?", "question_type": "fill"}]
    batch.ensure_ocr_jobs(batch.uploads)
    for index, job in enumerate(batch.ocr_jobs):
        if index == review_page:
            job.update(status="failed", attempts=review_attempts, last_error="试卷方向异常或未确认")
        else:
            job.update(status="succeeded", attempts=1)
    batch.save_meta()
    return batch


def _confirm(client, *, paper_id="paper-1", action_id=ACTION_ID, turns=1, filename="paper-1.ocr-ready-v1.jpg",
             content=b"synthetic-rotated-1", with_file=True):
    files = {"file": (filename, content, "image/jpeg")} if with_file else None
    return client.post(
        f"/api/batch-grade/{BATCH_ID}/paper/{paper_id}/orientation-confirm",
        data={"action_id": action_id, "quarter_turns_clockwise": str(turns)},
        files=files,
    )


def _observe(tmp_path, responses, boundary) -> dict:
    directory = tmp_path / f"quick_batch_{BATCH_ID}"
    return _Normalizer(tmp_path)({
        "responses": [[response.status_code, response.json()] for response in responses],
        "meta": _raw_meta(BATCH_ID, tmp_path),
        "batch_files": sorted(
            path.name for path in directory.iterdir()
            if path.name not in {"meta.json", ".review-operation.lock"}
        ) if directory.is_dir() else [],
        "lock_released": _lock_released(BATCH_ID, tmp_path),
        "ledger": list(_LEDGER),
        **boundary.record(),
    })


def test_orientation_confirm_retries_page_and_finishes_batch(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    _batch(tmp_path)
    boundary = _Boundary(monkeypatch)

    response = _confirm(client)

    _assert_golden("retries_page_and_finishes_batch", _observe(tmp_path, [response], boundary))


@pytest.mark.parametrize("outcome", ["provider_raises", "retryable_failure", "final_failure"])
def test_orientation_confirm_retry_fails(monkeypatch, tmp_path, outcome):
    client, _Session = _setup(monkeypatch, tmp_path)
    _batch(tmp_path)
    answer = {
        "provider_raises": RuntimeError("provider exploded"),
        "retryable_failure": _RETRYABLE_FAILURE,
        "final_failure": _FINAL_FAILURE,
    }[outcome]
    boundary = _Boundary(monkeypatch, answers={"page-1": answer})

    response = _confirm(client)

    _assert_golden(f"retry_fails_{outcome}", _observe(tmp_path, [response], boundary))


def test_orientation_confirm_replays(monkeypatch, tmp_path):
    """Same action again: completed replay; other turns refused; a new action re-runs.

    The first retry fails retryably, so the teacher may rotate the page again.
    """
    client, _Session = _setup(monkeypatch, tmp_path)
    _batch(tmp_path, review_attempts=0)
    boundary = _Boundary(monkeypatch, answers={"page-1": _RETRYABLE_FAILURE})

    responses = [
        _confirm(client),
        _confirm(client),
        _confirm(client, turns=2),
        _confirm(client, with_file=False),
    ]
    boundary.answers["page-1"] = _answer("2")
    responses.append(_confirm(client, action_id="action-0002", turns=3, content=b"synthetic-rotated-again-1"))
    responses.append(_confirm(client, action_id="action-0002", turns=3, content=b"synthetic-rotated-again-1"))

    _assert_golden("replays", _observe(tmp_path, responses, boundary))


def test_orientation_confirm_with_another_page_still_unoriented(monkeypatch, tmp_path):
    """A gradable ledger stays partial while any page still awaits its direction."""
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = _batch(tmp_path)
    batch.uploads[2].update(orientation_status="needs_review", orientation_reason="orientation_confidence_too_low")
    batch.ocr_jobs[2].update(status="needs_review")
    batch.save_meta()
    boundary = _Boundary(monkeypatch)

    response = _confirm(client)

    _assert_golden("another_page_still_unoriented", _observe(tmp_path, [response], boundary))


def test_orientation_confirm_incomplete_action_is_rerun(monkeypatch, tmp_path):
    """An action recorded as still running is re-run with the same turns."""
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = _batch(tmp_path)
    batch.uploads[1]["orientation_history"] = [{
        "action_id": ACTION_ID, "quarter_turns_clockwise": 1, "teacher_id": 1,
        "created_at": "2026-01-01T00:00:00+00:00", "status": "ocr_running",
    }]
    batch.save_meta()
    boundary = _Boundary(monkeypatch)

    response = _confirm(client)

    _assert_golden("incomplete_action_is_rerun", _observe(tmp_path, [response], boundary))


def _break(case: str, tmp_path, batch: QuickBatch, monkeypatch) -> dict:
    """Prepare one refusal; returns the request overrides."""
    upload = batch.uploads[1]
    if case == "bad_action_id":
        return {"action_id": "short"}
    if case == "bad_action_characters":
        return {"action_id": "action/0001"}
    if case == "bad_turns":
        return {"turns": 4}
    if case == "review_started":
        batch.review_results = [{"paper_id": "paper-0"}]
    elif case == "grading":
        batch.status = "grading"
    elif case == "still_recognizing":
        batch.status = "recognizing_remaining"
    elif case == "unknown_paper":
        return {"paper_id": "paper-9"}
    elif case == "no_file":
        return {"with_file": False}
    elif case == "already_ready":
        return {"paper_id": "paper-0", "filename": "paper-0.ocr-ready-v1.jpg", "content": b"synthetic-rotated-0"}
    elif case == "not_template_batch":
        batch.source_mode = "legacy"
    elif case == "original_missing":
        Path(upload["original_image_path"]).unlink()
    elif case == "original_outside_batch":
        outside = tmp_path / "outside-1.png"
        outside.write_bytes(b"synthetic-image-1")
        upload["original_image_path"] = str(outside)
    elif case == "original_changed":
        Path(upload["original_image_path"]).write_bytes(b"synthetic-image-1-edited")
    elif case == "tilted_page":
        upload["orientation_reason"] = "page_tilt_exceeds_limit"
    elif case == "busy":
        _busy_lock(monkeypatch)
    elif case == "not_client_rendered":
        return {"filename": "paper-1.jpg"}
    elif case == "empty_file":
        return {"content": b""}
    elif case == "foreign_batch":
        batch.teacher_id = 2
    elif case == "already_ready_beats_not_template":
        batch.source_mode = "legacy"
        batch.save_meta()
        return {"paper_id": "paper-0", "filename": "paper-0.ocr-ready-v1.jpg", "content": b"synthetic-rotated-0"}
    batch.save_meta()
    return {}


@pytest.mark.parametrize("case", [
    "bad_action_id", "bad_action_characters", "bad_turns", "review_started", "grading",
    "still_recognizing", "unknown_paper", "no_file", "already_ready", "not_template_batch",
    "original_missing", "original_outside_batch", "original_changed", "tilted_page", "busy",
    "not_client_rendered", "empty_file", "foreign_batch", "already_ready_beats_not_template",
])
def test_orientation_confirm_refusals(monkeypatch, tmp_path, case):
    client, _Session = _setup(monkeypatch, tmp_path)
    batch = _batch(tmp_path)
    boundary = _Boundary(monkeypatch)
    overrides = _break(case, tmp_path, batch, monkeypatch)

    response = _confirm(client, **overrides)

    _assert_golden(f"refusal_{case}", _observe(tmp_path, [response], boundary))
