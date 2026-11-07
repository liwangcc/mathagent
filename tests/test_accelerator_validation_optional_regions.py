from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from services import accelerator_validation as validation
from routers.answer_sheet import _required_answer_source_complete
from routers import answer_sheet_recognize_page


def _job(layout: dict) -> SimpleNamespace:
    return SimpleNamespace(
        teacher_id=1,
        layout_snapshot_json=json.dumps(layout),
    )


def _candidate(**overrides) -> dict:
    value = {
        "worker_metrics": {"backend": "windows_nvidia_gpu"},
        "answer_source_complete": False,
        "incomplete_answer_regions": ["I001"],
        "good_matches": 100,
        "inliers": 90,
        "inlier_ratio": 0.9,
        "reprojection_rmse": 1.0,
        "coverage": 0.95,
    }
    value.update(overrides)
    return value


def test_local_first_allows_incomplete_optional_identity_region(monkeypatch) -> None:
    node = SimpleNamespace(
        access_scope="platform",
        owner_user_id=1,
        capabilities_json=json.dumps({"backend": "windows_nvidia_gpu"}),
    )
    monkeypatch.setattr(validation, "LOCAL_FIRST_VALIDATION_ENABLED", True)
    monkeypatch.setattr(validation, "_node_for_job", lambda _db, _job: node)
    layout = {
        "answer_regions": [
            {"question_number": "I001", "answer_source_required": False},
            {"question_number": "A001"},
        ]
    }

    assert validation._eligible_for_local_first(
        object(),
        _job(layout),
        _candidate(),
    ) is True


def test_local_first_rejects_incomplete_required_answer_region(monkeypatch) -> None:
    node = SimpleNamespace(
        access_scope="platform",
        owner_user_id=1,
        capabilities_json=json.dumps({"backend": "windows_nvidia_gpu"}),
    )
    monkeypatch.setattr(validation, "LOCAL_FIRST_VALIDATION_ENABLED", True)
    monkeypatch.setattr(validation, "_node_for_job", lambda _db, _job: node)
    layout = {
        "answer_regions": [
            {"question_number": "I001", "answer_source_required": False},
            {"question_number": "A001"},
        ]
    }

    assert validation._eligible_for_local_first(
        object(),
        _job(layout),
        _candidate(incomplete_answer_regions=["A001"]),
    ) is False


def test_page_registration_ignores_only_optional_identity_coverage() -> None:
    plan = [
        {"question_number": "I001", "kind": "identity"},
        {"question_number": "A001", "kind": "answer"},
    ]

    assert _required_answer_source_complete(
        {"answer_source_complete": False, "incomplete_answer_regions": ["I001"]},
        plan,
    ) is True
    assert _required_answer_source_complete(
        {"answer_source_complete": False, "incomplete_answer_regions": ["A001"]},
        plan,
    ) is False
    assert _required_answer_source_complete(
        {"answer_source_complete": False, "incomplete_answer_regions": ["unknown"]},
        plan,
    ) is False


def test_accelerated_recognition_passes_every_evidence_image(monkeypatch) -> None:
    from routers import answer_sheet

    plan = [{"question_number": "A001", "kind": "answer"}]
    evidence = SimpleNamespace(
        page_type_id="page_1",
        evidence_path="legacy.png",
        evidence_paths=("left.png", "right.png"),
        question_numbers=["A001"],
        diagnostics={
            "ownership": "confirmed_bbox_focus_sheets",
            "answer_source_complete": True,
            "evidence_locations": [{
                "question_number": "A001",
                "image_index": 2,
                "answer_bbox_1000": [100, 200, 900, 800],
            }],
        },
    )
    handle = SimpleNamespace(
        state="ready",
        evidence=evidence,
        job_ids=(),
        reason_code=None,
    )
    captured = {}

    async def fake_try_evidence(**_kwargs):
        return handle

    async def fake_transcribe(paths, region_plan, *, teacher_id):
        captured.update(
            paths=paths,
            region_plan=region_plan,
            teacher_id=teacher_id,
        )
        return [{"question_number": "A001", "kind": "answer"}]

    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "_student_recognition_signature",
        lambda *_: "sig",
    )
    monkeypatch.setattr(answer_sheet_recognize_page, "orientation_is_ready", lambda _page: True)
    monkeypatch.setattr(answer_sheet, "orientation_is_ready", lambda _page: True)
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "build_answer_sheet_acceleration_plan",
        lambda *_: ({"page_1": {}}, {"page_1": plan}),
    )
    monkeypatch.setattr(
        answer_sheet,
        "build_answer_sheet_acceleration_plan",
        lambda *_: ({"page_1": {}}, {"page_1": plan}),
    )
    monkeypatch.setattr(answer_sheet_recognize_page, "try_accelerated_evidence", fake_try_evidence)
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "transcribe_answer_sheet_evidence",
        fake_transcribe,
    )
    monkeypatch.setattr(
        answer_sheet_recognize_page,
        "_answer_row_for_transcription",
        lambda _item: ({"question_no": "1", "answer": "A"}, None),
    )

    session = SimpleNamespace(
        teacher_id=7,
        session_id="session-1",
        session_dir="session-dir",
        layout_artifacts={},
        identity_regions=[],
        answer_regions=[],
    )
    page = {"paper_id": "paper-1", "path": "paper.png"}

    result = asyncio.run(
        answer_sheet._recognize_one_student_page_accelerated(session, page, [])
    )

    assert result == {"accelerator_handle": handle}
    assert captured == {
        "paths": ("left.png", "right.png"),
        "region_plan": [{
            "question_number": "A001",
            "kind": "answer",
            "image_index": 2,
            "answer_bbox_1000": [100, 200, 900, 800],
            "evidence_format": "focus_sheet",
        }],
        "teacher_id": 7,
    }
