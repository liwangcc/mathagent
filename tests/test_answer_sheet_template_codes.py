import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

import services.answer_sheet_template_registry as template_registry_module
from services.answer_sheet_session import AnswerSheetSession
from services.answer_sheet_template_registry import (
    AnswerSheetTemplateIncompatible,
    AnswerSheetTemplateRegistry,
)
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_evidence import build_template_artifact


def _question(number: str = "1", question_type: str = "choice") -> dict:
    return {
        "question_no": number,
        "question_text": f"第{number}题",
        "question_type": question_type,
        "choice_mode": "single" if question_type == "choice" else "unknown",
        "standard_answer": "A" if question_type == "choice" else "1",
        "max_score": 5,
        "grading_method": "exact" if question_type == "choice" else "equivalent",
        "solution_steps": [],
    }


def _answer_spec_hash(specs: list[dict]) -> str:
    raw = json.dumps(specs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _frozen_session(
    tmp_path: Path,
    session_id: str,
    *,
    teacher_id: int = 1,
    bubble_grids: list[dict] | None = None,
) -> AnswerSheetSession:
    upload_dir = tmp_path / "uploads"
    session = AnswerSheetSession(session_id, str(upload_dir), teacher_id=teacher_id, class_id=10)
    specs = [_question()]
    session.answer_spec_snapshot = specs
    session.answer_spec_hash = _answer_spec_hash(specs)
    session.status = "answers_confirmed"
    page_path = Path(session.session_dir) / "layout" / "page.png"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 1200), "white").save(page_path)
    digest = hashlib.sha256(page_path.read_bytes()).hexdigest()
    session.answer_sheet_pages = [{
        "page_id": "page_1",
        "index": 0,
        "path": str(page_path),
        "working_path": str(page_path),
        "sha256": digest,
        "working_sha256": digest,
    }]
    session.save_layout(
        [{
            "region_id": "answer_1",
            "page_id": "page_1",
            "question_no": "1",
            "bbox": [80, 300, 920, 520],
            "region_type": "choice_mark",
            "teacher_confirmed": True,
        }],
        [{
            "region_id": "identity_1",
            "page_id": "page_1",
            "bbox": [80, 60, 420, 150],
            "identity_type": "name",
            "teacher_confirmed": True,
        }],
    )
    if bubble_grids:
        # Grid rows only become real through the teacher's explicit confirm.
        session.confirm_bubble_grids(bubble_grids, teacher_id=teacher_id)
    artifact = build_template_artifact(
        page_path,
        session.session_dir,
        0,
        [{
            "question_number": "answer_1",
            "question_type": "choice",
            "answer_bbox": [80, 300, 920, 520],
        }],
    )
    artifact["question_crop_plan"] = build_anchor_crop_plan(
        {
            "column_layout": {
                "column_count": 1,
                "columns": [{
                    "column_index": 1,
                    "x1_1000": 0,
                    "x2_1000": 1000,
                }],
            }
        },
        artifact_sha256=artifact["sha256"],
        expected_question_numbers=["1"],
    )
    session.layout_artifacts = {"page_1": artifact}
    session.freeze_layout()
    return session


def _compatible_session(tmp_path: Path, session_id: str, *, teacher_id: int) -> AnswerSheetSession:
    session = AnswerSheetSession(
        session_id,
        str(tmp_path / "uploads"),
        teacher_id=teacher_id,
        class_id=20,
    )
    specs = [_question()]
    session.answer_spec_snapshot = specs
    session.answer_spec_hash = _answer_spec_hash(specs)
    session.status = "answers_confirmed"
    session.save()
    return session


def test_shared_template_code_is_immutable_reusable_and_contains_no_exam_answers(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _frozen_session(tmp_path, "a" * 16)

    published = registry.publish(source, creator_teacher_id=1)

    assert published["template_code"].startswith("AS-")
    manifest_path = (
        tmp_path / "registry" / "templates" / published["template_code"] / "manifest.json"
    )
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "standard_answer" not in manifest_text
    assert "question_text" not in manifest_text
    assert "student_pages" not in manifest_text

    reused = _compatible_session(tmp_path, "b" * 16, teacher_id=2)
    summary = registry.apply_to_session(reused, published["template_code"])

    assert summary["template_code"] == published["template_code"]
    assert reused.status == "frozen"
    assert reused.shared_template_code == published["template_code"]
    assert reused.layout_localization["provider_route"] == "none"
    assert "shared_template_hash" not in reused.to_dict(public=True)
    assert Path(reused.answer_sheet_pages[0]["working_path"]).is_file()
    assert str(tmp_path / "registry") not in reused.answer_sheet_pages[0]["working_path"]


def test_template_history_is_private_per_teacher_and_updates_after_use(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _frozen_session(tmp_path, "c" * 16)
    published = registry.publish(source, creator_teacher_id=1)

    registry.remember(1, published["template_code"])

    assert [item["template_code"] for item in registry.history(1)] == [
        published["template_code"]
    ]
    assert registry.history(2) == []

    registry.remember(2, published["template_code"])
    assert registry.history(2)[0]["template_code"] == published["template_code"]


def test_template_code_rejects_a_different_question_contract(tmp_path):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _frozen_session(tmp_path, "d" * 16)
    published = registry.publish(source, creator_teacher_id=1)
    incompatible = _compatible_session(tmp_path, "e" * 16, teacher_id=2)
    incompatible.answer_spec_snapshot[0]["question_type"] = "fill"
    incompatible.answer_spec_snapshot[0]["choice_mode"] = "unknown"

    with pytest.raises(AnswerSheetTemplateIncompatible):
        registry.apply_to_session(incompatible, published["template_code"])

    assert incompatible.status == "answers_confirmed"
    assert incompatible.answer_sheet_pages == []


def test_template_publish_retries_a_transient_windows_permission_error(tmp_path, monkeypatch):
    registry = AnswerSheetTemplateRegistry(tmp_path / "registry")
    source = _frozen_session(tmp_path, "f" * 16)
    real_replace = template_registry_module.os.replace
    directory_replace_attempts = 0

    def flaky_replace(source_path, destination_path):
        nonlocal directory_replace_attempts
        if Path(source_path).is_dir() and directory_replace_attempts == 0:
            directory_replace_attempts += 1
            raise PermissionError(5, "temporary scanner lock")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(template_registry_module.os, "replace", flaky_replace)
    published = registry.publish(source, creator_teacher_id=1)

    assert directory_replace_attempts == 1
    assert published["template_code"].startswith("AS-")


def test_qwen_locator_forces_fallback_route_high_reasoning_and_teacher_context(
    tmp_path,
    monkeypatch,
):
    from services import answer_sheet_template_locator as locator
    from services import ocr_service

    image_path = tmp_path / "blank.png"
    Image.new("RGB", (800, 1200), "white").save(image_path)
    calls = []
    response = json.dumps({
        "coordinate_space": "normalized_1000_v1",
        "column_count": 1,
        "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "answer_regions": [{
            "question_no": "1",
            "bbox": [80, 300, 920, 520],
            "confidence": 0.9,
        }],
        "identity_regions": [{
            "identity_type": "name",
            "bbox": [80, 60, 420, 150],
            "confidence": 0.95,
        }],
    })

    async def fake_call(_path, _prompt, **kwargs):
        calls.append({**kwargs, "teacher_id": ocr_service._vision_teacher_id.get()})
        kwargs["response_validator"](response)
        return response

    monkeypatch.setattr(locator, "call_gpt_vision", fake_call)
    result = asyncio.run(locator.locate_answer_sheet_template(
        [{"page_id": "page_1", "working_path": str(image_path)}],
        [_question()],
        teacher_id=27,
    ))

    assert result["provider_route"] == "qwen3.8max"
    assert result["reasoning_effort"] == "high"
    assert result["missing_questions"] == []
    assert calls == [{
        "max_tokens": 4096,
        "pool_route": "fallback",
        "reasoning_effort": "high",
        "response_validator": calls[0]["response_validator"],
        "prompt_first": True,
        "teacher_id": "27",
    }]


@pytest.mark.parametrize('size', [(2064, 1564), (1564, 2064), (1800, 600)])
def test_locator_grid_preserves_page_geometry_and_returned_coordinates(tmp_path, monkeypatch, size):
    from services import answer_sheet_template_locator as locator

    path = tmp_path / 'template.png'
    Image.new('RGB', size, 'white').save(path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    bbox = [120, 840, 910, 950]
    raw = json.dumps({
        'coordinate_space': 'normalized_1000_v1',
        'column_count': 1,
        'columns': [{'column_index': 1, 'x1_1000': 0, 'x2_1000': 1000}],
        'answer_regions': [{'question_no': '1', 'bbox': bbox}],
        'identity_regions': [],
    })
    request_paths = []

    async def fake_call(request_path, prompt, **kwargs):
        request_paths.append(Path(request_path))
        assert request_path != str(path)
        with Image.open(request_path) as request_image:
            assert request_image.size == size
            # y=500 ruler follows actual height, independently of width.
            r, g, b = request_image.getpixel((int(size[0] * .55), round(size[1] * .5)))
            assert r > g and r > b
        assert f'宽={size[0]}，高={size[1]}' in prompt
        assert 'y像素/图片高度*1000' in prompt
        assert '涂卡式框住该题全部选项涂卡格' in prompt
        assert '填写式框住该题完整作答括号及内部书写空间' in prompt
        assert '[A][B][C][D]' in prompt
        assert '不识别答案，不推断学生信息，不输出题干内容' not in prompt
        kwargs['response_validator'](raw)
        return raw

    monkeypatch.setattr(locator, 'call_gpt_vision', fake_call)
    result = asyncio.run(locator.locate_answer_sheet_template(
        [{'page_id': 'page_1', 'working_path': str(path)}], [_question()], teacher_id=27,
    ))
    assert result['pages'][0]['answer_regions'][0]['bbox'] == bbox
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert all(not candidate.exists() for candidate in request_paths)


def test_locator_removes_temporary_grid_after_provider_failure(tmp_path, monkeypatch):
    from services import answer_sheet_template_locator as locator

    path = tmp_path / 'template.png'
    Image.new('RGB', (800, 600), 'white').save(path)
    request_paths = []

    async def failed_call(request_path, prompt, **kwargs):
        request_paths.append(Path(request_path))
        raise RuntimeError('provider failure')

    monkeypatch.setattr(locator, 'call_gpt_vision', failed_call)
    with pytest.raises(RuntimeError, match='provider failure'):
        asyncio.run(locator.locate_answer_sheet_template(
            [{'page_id': 'page_1', 'working_path': str(path)}], [_question()], teacher_id=27,
        ))
    assert request_paths and all(not candidate.exists() for candidate in request_paths)
