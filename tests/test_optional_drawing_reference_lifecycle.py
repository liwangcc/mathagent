"""标准图可选生命周期：保存即确认、重开回读、删除后仍走 custom 轨。

合成模板 + 内存 DB + mock 掉的 Flash，零网络、零业务数据、零付费。
不重复 test_drawing_flow / test_slot_http_e2e / test_drawing_grading 里已有的
几何矩阵与像素×模型双门大矩阵，只覆盖它们没串起来的"确认/保存/重开"生命周期。
"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from services import drawing_grading as drawing
from services.drawing_reference import normalize_reference
from services.grader import grade_paper_unified
from services.template_session import TemplateSession
from tests.test_template_directory_routes import _session, directory_client  # noqa: F401

CONFIRM_URL = "/api/batch-grade/template/confirm"
DRAWING_REGIONS = [[700, 350, 900, 550]]


def _reference_payload(*, point=(800, 450), mode="fixed"):
    """Raw model payload: an unverified flag plus a hash the model made up."""
    payload = {
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": mode, "context_regions": [],
        "variants": [{"strokes": [{"kind": "point", "point": list(point)}]}] if mode == "fixed" else [],
    }
    payload.update(teacher_confirmed=True, reference_hash="forged-model-hash")
    return payload


def _staged_reference(*, point=(800, 450), mode="fixed"):
    """What the teacher's draft holds: content-true hash, never yet confirmed."""
    reference = normalize_reference(_reference_payload(point=point, mode=mode), trusted_confirmed=False)
    assert reference["teacher_confirmed"] is False
    assert reference["reference_hash"] != "forged-model-hash"
    return reference


def _confirm(client, session, pages):
    return client.post(CONFIRM_URL, json={
        "template_id": session.template_id, "category": "daily", "pages": pages,
    })


def _reopen(session, tmp_path):
    """Reopen the persisted template metadata, as the next request would."""
    return TemplateSession.load_meta(session.template_id, str(tmp_path))


def _recognition_question(client, session):
    response = client.get(f"/api/batch-grade/template/{session.template_id}/recognition")
    assert response.status_code == 200, response.text
    return response.json()["pages"][0]["questions"][1]


def _question_no(question):
    return str(question.get("question_no") or question.get("question_number"))


def _student_page(tmp_path):
    path = tmp_path / "student-answer.png"
    Image.new("RGB", (100, 100), "white").save(path)
    return str(path)


def _custom_route_item(monkeypatch, tmp_path, question):
    """Judge the saved geometry-only drawing question through the custom route.

    Flash is mocked, so this asserts routing/contract, not model quality.
    """
    path = _student_page(tmp_path)
    call = AsyncMock(return_value={
        "content": json.dumps({"result": "correct", "p_correct": 95, "reason": "合成证据"}),
        "model": "qwen3.8-flash", "parameters": {},
    })
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    number = _question_no(question)
    result = asyncio.run(drawing.assess_page(
        source_path=path, page_type_id="page_1", questions=[question],
        transcriptions=[{"question_no": number, "student_answer": ""}],
        evidence_paths=(path,),
        diagnostics={"template_width": 100, "template_height": 100,
                     "answer_source_complete": True, "evidence_columns": [{}]},
    ))
    assert number in result, result
    return result[number], path


@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_confirm_without_reference_keeps_legal_drawing_regions(directory_client, tmp_path, monkeypatch, mode):
    """No standard figure at all: the saved drawing region alone is a valid contract."""
    session = _session(tmp_path, mode=mode)
    assert "drawing_reference" not in session.pages[0]["questions"][1]

    response = _confirm(directory_client, session, copy.deepcopy(session.pages))
    assert response.status_code == 200, response.text

    reopened = _reopen(session, tmp_path)
    question = reopened.pages[0]["questions"][1]
    assert reopened.status == "confirmed"
    assert question["drawing_regions"] == DRAWING_REGIONS
    assert "drawing_reference" not in question
    assert "drawing_reference" not in reopened.build_answer_map()["2.3"]
    assert "drawing_reference" not in _recognition_question(directory_client, session)

    item, _ = _custom_route_item(monkeypatch, tmp_path, question)
    assert item["comparison_mode"] == "custom"
    assert item["decision"] == "correct"


def test_forged_reference_hash_cannot_enter_the_template(directory_client, tmp_path):
    """A model-invented hash/flag is rejected before it can be confirmed."""
    session = _session(tmp_path)
    pages = copy.deepcopy(session.pages)
    pages[0]["questions"][1]["drawing_reference"] = _reference_payload()

    response = _confirm(directory_client, session, pages)
    assert response.status_code == 400, response.text
    assert "drawing reference" in response.json()["detail"]
    saved = _reopen(session, tmp_path)
    assert saved.status == "ocr_done"
    assert "drawing_reference" not in saved.pages[0]["questions"][1]


@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_teacher_fixed_reference_is_stamped_by_save_and_survives_reopen(directory_client, tmp_path, mode):
    """Saving the template is the teacher confirmation of a fixed reference."""
    session = _session(tmp_path, mode=mode)
    pages = copy.deepcopy(session.pages)
    pages[0]["questions"][1]["drawing_reference"] = _staged_reference()

    response = _confirm(directory_client, session, pages)
    assert response.status_code == 200, response.text

    reopened = _reopen(session, tmp_path)
    question = reopened.pages[0]["questions"][1]
    reference = question["drawing_reference"]
    assert reference["teacher_confirmed"] is True
    assert reference["reference_hash"] == _staged_reference()["reference_hash"]
    assert reference["comparison_mode"] == "fixed"
    assert reference["variants"][0]["strokes"][0]["point"] == [800, 450]
    assert question["drawing_regions"] == DRAWING_REGIONS
    # The stamped reference is what every later grading run reads back.
    assert reopened.build_answer_map()["2.3"]["drawing_reference"] == reference
    assert drawing.validate_confirmed_reference(reference)["reference_hash"] == reference["reference_hash"]

    readback = _recognition_question(directory_client, session)["drawing_reference"]
    assert readback["reference_hash"] == reference["reference_hash"]
    assert readback["teacher_confirmed"] is True
    assert readback["variants"][0]["strokes"][0]["point"] == [800, 450]
    assert _reopen(session, tmp_path).build_answer_map()["2.3"]["drawing_reference"] == reference


def test_removing_the_reference_on_reconfirm_clears_map_and_stays_custom(
    directory_client, tmp_path, monkeypatch,
):
    """Teachers may delete a bad standard figure; the region keeps judging by Flash."""
    session = _session(tmp_path)
    pages = copy.deepcopy(session.pages)
    pages[0]["questions"][1]["drawing_reference"] = _staged_reference()
    assert _confirm(directory_client, session, pages).status_code == 200

    saved = _reopen(session, tmp_path)
    assert saved.build_answer_map()["2.3"]["drawing_reference"]["teacher_confirmed"] is True

    removed = copy.deepcopy(saved.pages)
    removed[0]["questions"][1].pop("drawing_reference")
    response = _confirm(directory_client, session, removed)
    assert response.status_code == 200, response.text

    reopened = _reopen(session, tmp_path)
    question = reopened.pages[0]["questions"][1]
    assert reopened.status == "confirmed"
    assert "drawing_reference" not in question
    assert "drawing_reference" not in reopened.build_answer_map()["2.3"]
    assert question["drawing_regions"] == DRAWING_REGIONS
    assert "drawing_reference" not in _recognition_question(directory_client, session)

    item, _ = _custom_route_item(monkeypatch, tmp_path, question)
    assert item["comparison_mode"] == "custom"
    assert item["decision"] == "correct"


def test_pure_drawing_without_reference_never_enters_the_text_track(
    directory_client, tmp_path, monkeypatch,
):
    """A drawing-only question is judged by the drawing assessment, not by text."""
    session = _session(tmp_path)
    assert _confirm(directory_client, session, copy.deepcopy(session.pages)).status_code == 200

    saved = _reopen(session, tmp_path)
    question = saved.pages[0]["questions"][1]
    number = _question_no(question)
    item, path = _custom_route_item(monkeypatch, tmp_path, question)

    paper = {
        "image_path": path, "page_type_id": "page_1",
        "questions": [{"question_no": number, "question_type": "solve", "student_answer": ""}],
        "drawing_assessments": {number: item},
    }
    monkeypatch.setattr("services.grader.judge_paper",
                        AsyncMock(side_effect=AssertionError("纯作图题不得进入文字判分轨")))
    result = asyncio.run(grade_paper_unified(paper, saved.build_answer_map(), "演示", 1))

    detail = result["details"][0]
    assert detail["match_method"] == "drawing"
    assert detail["drawing_assessment"]["comparison_mode"] == "custom"
    assert detail["drawing_assessment"]["threshold"] == drawing.ACCEPT_THRESHOLD
    assert detail["result"] == "correct"
    assert result["correct_count"] == 1
