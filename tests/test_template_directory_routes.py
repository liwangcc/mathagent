"""Teacher UI contract roundtrip: synthetic images, isolated files and memory DB only."""

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

import pytest
from PIL import Image, ImageDraw

from routers import grade
from routers import grade_template
from services.template_anchor_crops import build_anchor_crop_plan
from services.template_evidence import build_template_artifact
from services.template_session import (
    CURRENT_ARTIFACT_HASH_VERSION,
    LEGACY_GEOMETRY_CONTRACT_REASON,
    TemplateSession,
)
from tests.test_template_session import _client, _make_app_and_session


@pytest.fixture
def directory_client(tmp_path, monkeypatch):
    engine, Session = _make_app_and_session()
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "SessionLocal", Session)

    async def no_background_model(*_args, **_kwargs):
        return None

    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", no_background_model)
    with _client(Session, raise_server_exceptions=True) as client:
        yield client
    engine.dispose()


def _session(tmp_path, *, mode="marked", legacy=False):
    session = TemplateSession(f"directory-contract-{mode}-{'old' if legacy else 'new'}", str(tmp_path), teacher_id=1)
    session.template_mode = mode
    session.has_answers = mode == "marked"
    session.column_mode = "single"
    session.category = "daily"
    source = Path(session.template_dir) / "source.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((45, 120), "2. Explain the change.", fill="black")
    draw.text((45, 840), "3. Draw on the grid above.", fill="black")
    draw.rectangle((630, 420, 810, 660), outline="black", width=2)
    image.save(source)
    numbers = ["17", "18"] if legacy else ["2.2", "2.3"]
    questions = []
    for index, number in enumerate(numbers):
        question = {
            "question_number": number, "question_text": f"Synthetic question {number}",
            "standard_answer": "Synthetic reference", "question_type": "solve",
            "choice_mode": "unknown", "subquestion_count": 1, "blank_count": 1,
            "chapter": "Test", "topic": "Test",
        }
        if not legacy:
            question.update(
                directory_node_id=f"n00{index + 2}", directory_parent_id="n001",
                printed_path=["二", str(index + 2)],
                answer_kind="non_graphic" if index == 0 else "drawing",
                drawing_regions=[] if index == 0 else [[700, 350, 900, 550]],
                label_anchor=None if index == 0 else [690, 400],
            )
        questions.append(question)
    anchors = [{"question_number": number, "question_number_bbox": [100, 100 + index * 600, 170, 125 + index * 600]}
               for index, number in enumerate(numbers)]
    regions = [{"question_number": number, "regions": []} for number in numbers]
    artifact = build_template_artifact(
        source, session.template_dir, 0, questions,
        template_mode=mode, question_anchors=anchors,
        template_answer_regions=regions, template_handwriting_regions=regions,
    )
    layout = {"column_layout": {"column_count": 1, "columns": [
        {"column_index": 1, "x1_1000": 0, "x2_1000": 1000},
    ]}}
    artifact["question_crop_plan"] = build_anchor_crop_plan(
        layout, artifact_sha256=artifact["sha256"], expected_question_numbers=numbers,
    )
    session.add_page(0, questions, artifact)
    session.status = "ocr_done"
    session.layout_cut_runs["synthetic-layout"] = {
        "status": "complete", "action_id": "synthetic-layout", "cut_types": ["question"],
        "pages": {"page_1": {
            "page_index": 0, "artifact_sha256": artifact["sha256"],
            "anchor_crop_plan": artifact["question_crop_plan"], "modes": {"question": layout},
        }},
    }
    session.layout_candidates = {"active_action_id": "synthetic-layout"}
    session.save_meta()
    return session


def _layout_payload(session):
    artifact = session.page_artifacts["page_1"]
    return {
        "page_index": 0,
        "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "question_anchors": copy.deepcopy(artifact["question_anchors"]),
        "template_answer_regions": copy.deepcopy(artifact["template_answer_regions"]),
        "template_handwriting_regions": copy.deepcopy(artifact["template_handwriting_regions"]),
        "question_schema": copy.deepcopy(artifact["question_schema"]),
    }


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_directory_cut_response_exposes_schema_to_teacher_review(directory_client, tmp_path, mode):
    session = _session(tmp_path, mode=mode)
    response = directory_client.get(f"/api/batch-grade/template/{session.template_id}/cut-candidates")
    assert response.status_code == 200, response.text
    page = response.json()["pages"][0]
    assert page["question_schema"] == session.page_artifacts["page_1"]["question_schema"]
    assert page["question_schema"][1]["question_number"] == "2.3"
    assert page["question_schema"][1]["printed_path"] == ["二", "3"]
    assert page["question_schema"][1]["label_anchor"] == [690, 400]


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_directory_layout_updates_geometry_not_server_identity(directory_client, tmp_path, mode):
    session = _session(tmp_path, mode=mode)
    payload = _layout_payload(session)
    changed = payload["question_schema"][1]
    changed.update(directory_node_id="forged", directory_parent_id="other-parent", printed_path=["forged"])
    changed["label_anchor"] = [680, 390]
    changed["drawing_regions"] = [[695, 345, 900, 550]]
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 200, response.text
    schema = response.json()["question_schema"][1]
    assert schema["question_number"] == "2.3"
    assert schema["directory_node_id"] == "n003"
    assert schema["directory_parent_id"] == "n001"
    assert schema["printed_path"] == ["二", "3"]
    assert schema["label_anchor"] == [680, 390]
    assert schema["drawing_regions"] == [[695, 345, 900, 550]]
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.page_artifacts["page_1"]["question_schema"][1] == schema
    assert stored.pages[0]["questions"][1]["label_anchor"] == [680, 390]
    reopened = directory_client.get(f"/api/batch-grade/template/{session.template_id}/cut-candidates").json()
    assert reopened["pages"][0]["question_schema"][1] == schema


def test_directory_layout_resolves_model_geometry_residue_for_teacher(directory_client, tmp_path):
    # Product ruling (incident a5d95c2a, 4th occurrence): the stale geometry
    # (label anchors / region arrays) is invisible model residue the teacher
    # cannot delete in the UI — blocking the correction is forbidden. The
    # teacher's non-graphic mark wins and the residue clears. This supersedes
    # the earlier "surface the conflict as 422" semantics.
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][0].update(
        answer_kind="non_graphic",
        drawing_regions=[[120, 180, 720, 800]],
        label_anchor=[90, 220],
    )
    response = directory_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides",
        json=payload,
    )
    assert response.status_code == 200, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.page_artifacts["page_1"]["question_schema"][0]["answer_kind"] == "non_graphic"
    assert stored.page_artifacts["page_1"]["question_schema"][0]["drawing_regions"] == []
    assert stored.page_artifacts["page_1"]["question_schema"][0]["label_anchor"] is None


def test_directory_layout_rejects_unknown_q_without_persisting(directory_client, tmp_path):
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1]["question_number"] = "2.99"
    original = session.artifact_hash()
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 422, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.artifact_hash() == original


def test_directory_confirm_cannot_bypass_geometry_override(directory_client, tmp_path):
    session = _session(tmp_path)
    submitted = copy.deepcopy(session.pages[0]["questions"])
    submitted[1].update(directory_node_id="forged", directory_parent_id=None, printed_path=["forged"],
                        label_anchor=[680, 450], drawing_regions=[[700, 400, 900, 600]])
    response = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily",
        "pages": [{"page_index": 0, "questions": submitted}],
    })
    assert response.status_code == 200, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    schema = stored.page_artifacts["page_1"]["question_schema"][1]
    question = stored.pages[0]["questions"][1]
    for actual in (schema, question):
        assert actual["directory_node_id"] == "n003"
        assert actual["directory_parent_id"] == "n001"
        assert actual["printed_path"] == ["二", "3"]
        assert actual["label_anchor"] == [690, 400]
        assert actual["drawing_regions"] == [[700, 350, 900, 550]]
    assert set(stored.build_answer_map()) == {"2.2", "2.3"}
    assert stored.recognition_contract_error() == ""


def test_legacy_flat_template_layout_and_confirm_remain_compatible(directory_client, tmp_path):
    session = _session(tmp_path, legacy=True)
    payload = _layout_payload(session)
    payload.pop("question_schema")
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 200, response.text
    confirmed = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": session.pages,
    })
    assert confirmed.status_code == 200, confirmed.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert [item["question_no"] for item in stored.pages[0]["questions"]] == ["17", "18"]
    assert not any(item.get("directory_node_id") for item in stored.pages[0]["questions"])
    assert set(stored.build_answer_map()) == {"17", "18"}


@pytest.mark.parametrize("stage", ["primary", "junior", "senior"])
@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_first_reviewed_columns_keep_complete_answers_and_draft(
    directory_client, tmp_path, monkeypatch, stage, mode,
):
    """Establishing the first crop plan is not a change to prior solver input."""
    from services import template_directory_pipeline as pipeline
    from services.template_column_layout import _review_layout_result
    from services.teaching_context import make_teaching_context

    async def no_model(*_args, **_kwargs):
        raise AssertionError("First geometry confirmation must not re-solve answers")

    monkeypatch.setattr(pipeline, "_request", no_model)
    session = _session(tmp_path, mode=mode, legacy=True)
    session.teaching_context = make_teaching_context(stage)
    session.column_mode = "double"
    artifact = session.page_artifacts["page_1"]
    artifact.pop("question_crop_plan")
    page_run = session.layout_cut_runs["synthetic-layout"]["pages"]["page_1"]
    page_run["anchor_crop_plan"] = None
    page_run["layout_review_pending"] = True
    page_run["modes"]["question"] = _review_layout_result(
        {"sha256": artifact["sha256"], "width": artifact["width"],
         "height": artifact["height"], "size_bytes": 100, "image_format": "png"},
        mode="double", diagnostics={"strategy": "local_gutter_v1", "validation": "failed"},
        proposed_columns=None,
    )
    page_run["modes"]["question"]["ocr_input_strategy"] = "whole_page_fallback"
    answers_before = session.answers_hash()
    pages_before = copy.deepcopy(session.pages)
    session.save_meta()
    confirmation = {"template_id": session.template_id, "category": "daily", "pages": session.pages}

    refused = directory_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert refused.status_code == 409, refused.text
    assert "分栏仍需人工确认" in refused.json()["detail"]
    payload = _layout_payload(session)
    payload["columns"] = [
        {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
        {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
    ]
    response = directory_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["layout_changed"] is False
    saved = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert saved.pages == pages_before
    assert saved.answers_hash() == answers_before
    assert saved.status == "ocr_done"  # Geometry approval is not final confirmation.
    assert saved.recognition_contract_error()
    reopened = directory_client.get(
        f"/api/batch-grade/template/{session.template_id}/cut-candidates"
    ).json()
    assert reopened["geometry_review_pending_pages"] == []
    assert len(reopened["pages"][0]["anchor_crop_plan"]["parameters"]["columns"]) == 2
    confirmed = directory_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert confirmed.status_code == 200, confirmed.text
    final = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert final.recognition_contract_error() == ""
    assert final.teaching_context["education_stage"] == stage
    assert final.answers_hash() == answers_before


@pytest.mark.parametrize("changed_legacy_regions", [False, True])
def test_legacy_geometry_hash_requires_teacher_reconfirmation_without_recomputing_answers(
    directory_client, tmp_path, monkeypatch, changed_legacy_regions,
):
    from fastapi import HTTPException
    from services import template_directory_pipeline as pipeline

    async def no_model(*_args, **_kwargs):
        raise AssertionError("Geometry hash upgrade must not call a model")
    monkeypatch.setattr(pipeline, "_request", no_model)
    session = _session(tmp_path, legacy=True)
    confirmation = {"template_id": session.template_id, "category": "daily", "pages": session.pages}
    first = directory_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert first.status_code == 200, first.text
    saved = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert saved.confirmed_artifact_hash_version == CURRENT_ARTIFACT_HASH_VERSION
    original_answers = saved.answers_hash()
    pages_before = copy.deepcopy(saved.pages)
    artifacts = copy.deepcopy(saved.page_artifacts)
    # Reproduce the old algorithm: it omitted answer regions entirely.
    for artifact in saved.page_artifacts.values():
        artifact.pop("template_answer_regions", None)
    legacy_hash = saved.artifact_hash()
    saved.page_artifacts = artifacts
    legacy_meta = saved.to_dict()
    legacy_meta.pop("confirmed_artifact_hash_version")
    legacy_meta["confirmed_artifact_hash"] = legacy_hash
    if changed_legacy_regions:
        legacy_meta["page_artifacts"]["page_1"]["template_answer_regions"][0]["regions"] = [[300, 150, 600, 300]]
    Path(saved.meta_path).write_text(json.dumps(legacy_meta), encoding="utf-8")
    bytes_before = Path(saved.meta_path).read_bytes()

    restored = TemplateSession.load_meta(saved.template_id, str(tmp_path))
    assert restored.confirmed_artifact_hash_version == 0
    assert restored.recognition_contract_error() == LEGACY_GEOMETRY_CONTRACT_REASON
    assert restored.pages == pages_before
    assert restored.answers_hash() == original_answers
    assert Path(saved.meta_path).read_bytes() == bytes_before  # load never migrates/signs
    with pytest.raises(HTTPException) as blocked:
        grade._validate_template_for_recognition(restored, "daily", "", persist_reconfirmation=True)
    assert blocked.value.status_code == 409
    persisted = TemplateSession.load_meta(saved.template_id, str(tmp_path))
    assert persisted.reconfirmation_reason == LEGACY_GEOMETRY_CONTRACT_REASON
    assert persisted.answers_hash() == original_answers

    # Explicit teacher save writes the new signature; no recognition/solver rerun.
    confirmation["pages"] = persisted.pages
    response = directory_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert response.status_code == 200, response.text
    upgraded = TemplateSession.load_meta(saved.template_id, str(tmp_path))
    assert upgraded.confirmed_artifact_hash_version == CURRENT_ARTIFACT_HASH_VERSION
    assert upgraded.confirmed_artifact_hash == upgraded.artifact_hash()
    assert upgraded.recognition_contract_error() == ""
    assert upgraded.answers_hash() == original_answers
    assert upgraded.pages == pages_before
    grade._validate_template_for_recognition(upgraded, "daily", "")


def test_hierarchical_insert_moves_drawing_geometry_with_its_q(directory_client, tmp_path):
    session = _session(tmp_path)
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/questions", json={
        "page_index": 0, "action": "insert", "before_question_number": "2.3",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["renamed_question_numbers"] == {"2.3": "2.4"}
    assert [item["question_no"] for item in result["pages"][0]["questions"]] == ["2.2", "2.3", "2.4"]
    schema = result["page_artifacts"]["page_1"]["question_schema"]
    moved = next(item for item in schema if item["question_number"] == "2.4")
    assert moved["directory_node_id"] == "n003"
    assert moved["printed_path"] == ["二", "3"]
    assert moved["drawing_regions"] == [[700, 350, 900, 550]]
    assert moved["label_anchor"] == [690, 400]
    assert next(item for item in schema if item["question_number"] == "2.2")["directory_node_id"] == "n002"
    assert result["geometry_review_pending_pages"] == [0]
    assert result["page_artifacts"]["page_1"]["geometry_review_pending"] is True
    submitted = copy.deepcopy(result["pages"])
    for question in submitted[0]["questions"]:
        question["question_text"] = question["question_text"] or "Teacher supplied missing question"
        question["standard_answer"] = question["standard_answer"] or "Teacher supplied reference"
    blocked = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": submitted,
    })
    assert blocked.status_code == 409, blocked.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.status == "ocr_done"


def test_hierarchical_delete_does_not_renumber_remaining_q(directory_client, tmp_path):
    session = _session(tmp_path)
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/questions", json={
        "page_index": 0, "action": "delete", "question_number": "2.2",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["question_no"] for item in result["pages"][0]["questions"]] == ["2.3"]
    remaining = result["page_artifacts"]["page_1"]["question_schema"][0]
    assert remaining["question_number"] == "2.3"
    assert remaining["directory_node_id"] == "n003"
    assert remaining["label_anchor"] == [690, 400]


def test_rejected_drawing_and_exemption_edit_does_not_change_difference_file(directory_client, tmp_path):
    session = _session(tmp_path)
    initial = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=_layout_payload(session))
    assert initial.status_code == 200, initial.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    artifact = stored.page_artifacts["page_1"]
    difference_path = Path(stored.template_dir) / artifact["difference_template_image"]
    original_file_hash = hashlib.sha256(difference_path.read_bytes()).hexdigest()
    original_artifact_hash = stored.artifact_hash()
    assert original_file_hash == artifact["difference_template_sha256"]

    payload = _layout_payload(stored)
    # Label-label overlap is now safely routed to an external rail. Keep the
    # atomic-rejection guard using a genuinely invalid anchor INSIDE the graph.
    payload["question_schema"][1].update(drawing_regions=[[200, 100, 500, 300]], label_anchor=[250, 110])
    # If written before geometry rejection, this would erase real synthetic ink.
    payload["template_handwriting_regions"][0]["regions"] = [[30, 80, 350, 140]]
    rejected = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert rejected.status_code == 422, rejected.text
    assert hashlib.sha256(difference_path.read_bytes()).hexdigest() == original_file_hash
    unchanged = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert unchanged.artifact_hash() == original_artifact_hash
    assert unchanged.page_artifacts["page_1"]["difference_template_sha256"] == original_file_hash


def test_minimal_non_graphic_override_clears_drawing_but_keeps_identity(directory_client, tmp_path):
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"] = [{
        "question_number": number, "answer_kind": "non_graphic", "drawing_regions": [], "label_anchor": None,
    } for number in ("2.2", "2.3")]
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 200, response.text
    schema = response.json()["question_schema"][1]
    assert schema["question_type"] == "solve"
    assert schema["answer_kind"] == "non_graphic"
    assert schema["drawing_regions"] == []
    assert schema["label_anchor"] is None
    assert schema["directory_node_id"] == "n003"
    assert schema["printed_path"] == ["二", "3"]
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.pages[0]["questions"][1]["answer_kind"] == "non_graphic"


def test_marked_pending_answer_areas_survive_teacher_save_and_reopen(directory_client, tmp_path):
    session = _session(tmp_path)
    payload = _layout_payload(session)
    areas = [
        {"question_number": "2.2", "regions": [[300, 150, 600, 300]]},
        {"question_number": "2.3", "regions": [[700, 350, 900, 550]]},
    ]
    payload["template_answer_regions"] = copy.deepcopy(areas)
    artifact = session.page_artifacts["page_1"]
    artifact.pop("template_answer_regions")
    candidates = copy.deepcopy(artifact["question_schema"])
    artifact["question_schema"][1].update(answer_kind="non_graphic", drawing_regions=[], label_anchor=None)
    session.pages[0]["questions"][1].update(answer_kind="non_graphic", drawing_regions=[], label_anchor=None)
    run = session.layout_cut_runs["synthetic-layout"]["pages"]["page_1"]
    run.update(geometry_review_pending=True, question_schema_candidates=candidates,
               template_answer_region_candidates=copy.deepcopy(areas))
    session.save_meta()

    pending = directory_client.get(f"/api/batch-grade/template/{session.template_id}/cut-candidates")
    assert pending.status_code == 200, pending.text
    assert pending.json()["pages"][0]["template_answer_region_candidates"] == areas
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()["template_answer_regions"]
    assert [item["regions"] for item in saved] == [item["regions"] for item in areas]
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert stored.page_artifacts["page_1"]["template_answer_regions"] == saved
    assert stored.page_artifacts["page_1"]["question_schema"][1]["answer_kind"] == "drawing"
    reopened = directory_client.get(f"/api/batch-grade/template/{session.template_id}/cut-candidates").json()
    assert reopened["pages"][0]["template_answer_regions"] == saved
    assert not reopened["pages"][0].get("template_answer_region_candidates")
    assert not reopened["geometry_review_pending_pages"]


def test_legacy_marked_omitted_answer_areas_do_not_create_contract(directory_client, tmp_path):
    session = _session(tmp_path, legacy=True)
    payload = _layout_payload(session)
    payload.pop("template_answer_regions")
    payload.pop("question_schema")
    session.page_artifacts["page_1"].pop("template_answer_regions")
    session.save_meta()
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert response.status_code == 200, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert "template_answer_regions" not in stored.page_artifacts["page_1"]


@pytest.mark.parametrize("question_type", ["choice", "fill"])
@pytest.mark.parametrize("mode", ["marked", "blank"])
def test_graphic_geometry_edit_preserves_type_answers_and_reconfirmation_gate(directory_client, tmp_path, question_type, mode):
    from fastapi import HTTPException
    from services.drawing_reference import normalize_reference
    session = _session(tmp_path, mode=mode)
    question = session.pages[0]["questions"][1]
    question.update(question_type=question_type, choice_mode="single" if question_type == "choice" else "unknown",
                    grading_method="exact" if question_type == "choice" else "equivalent",
                    standard_answer="A" if question_type == "choice" else "标出坐标(2,3)",
                    drawing_reference=normalize_reference({
                        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
                        "comparison_mode": "manual", "variants": [], "context_regions": [],
                    }))
    schema = session.page_artifacts["page_1"]["question_schema"][1]
    schema.update(question_type=question_type, choice_mode=question["choice_mode"])
    session.save_meta()
    confirmation = {"template_id": session.template_id, "category": "daily", "pages": session.pages}
    confirmed = directory_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert confirmed.status_code == 200, confirmed.text
    saved = TemplateSession.load_meta(session.template_id, str(tmp_path))
    answer_hash = saved.answers_hash()
    answers_before = [row["standard_answer"] for row in saved.pages[0]["questions"]]
    saved.save_meta()
    payload = _layout_payload(saved)
    if mode == "blank":
        # Mirror Upload.vue serializeTemplateHandwritingRegions: even when
        # there are no ink boxes, the teacher sends one empty list per Q.
        payload["template_handwriting_regions"] = [
            {"question_number": anchor["question_number"], "regions": []}
            for anchor in payload["question_anchors"]]
    payload["question_schema"][1].update(label_anchor=[680, 390], drawing_regions=[[695, 345, 900, 550]])
    moved = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload)
    assert moved.status_code == 200, moved.text
    saved = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert saved.pages[0]["questions"][1]["question_type"] == question_type
    assert saved.pages[0]["questions"][1]["choice_mode"] == question["choice_mode"]
    assert saved.page_artifacts["page_1"]["question_schema"][1]["question_type"] == question_type
    assert saved.answers_hash() == answer_hash
    assert [row["standard_answer"] for row in saved.pages[0]["questions"]] == answers_before
    assert saved.requires_reconfirmation is True
    with pytest.raises(HTTPException) as blocked:
        grade._validate_template_for_recognition(saved, "daily", "")
    assert blocked.value.status_code == 409
    assert "定位框" in str(blocked.value.detail)
    reconfirmed = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": saved.template_id, "category": "daily", "pages": saved.pages,
    })
    assert reconfirmed.status_code == 200, reconfirmed.text
    reopened = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert reopened.recognition_contract_error() == ""
    assert reopened.answers_hash() == answer_hash
    assert reopened.pages[0]["questions"][1]["drawing_reference"]["comparison_mode"] == "manual"

def test_blank_refresh_keeps_unified_boxes_and_materializes_baseline(tmp_path):
    """A blank template keeps its unified boxes and gains a whiteout baseline.

    Regression: the blank branch zeroed template_handwriting_regions and
    dropped the difference image, so student diffs had no exemption and every
    written answer was whitened away.
    """
    session = _session(tmp_path, mode="blank")
    artifact = dict(session.page_artifacts["page_1"])
    artifact["template_answer_regions"] = [
        {"question_number": "2.3", "regions": [[680, 420, 900, 600]]},
        {"question_number": "2.2", "regions": [[120, 160, 520, 300]]},
    ]
    artifact["template_handwriting_regions"] = []

    refreshed = grade_template._refresh_template_difference_artifact(
        session.template_dir, artifact, template_mode="blank",
    )

    # The unified boxes return in question order and fill the exemption half.
    assert [item["question_number"] for item in refreshed["template_handwriting_regions"]] == ["2.2", "2.3"]
    assert refreshed["template_handwriting_regions"][1]["regions"] == [[680, 420, 900, 600]]
    # The hash-bound whiteout baseline exists and is registered.
    relative = refreshed["difference_template_image"]
    difference_path = Path(session.template_dir) / relative
    assert difference_path.is_file()
    assert refreshed["difference_template_sha256"] == hashlib.sha256(
        difference_path.read_bytes()
    ).hexdigest()
    # The printed rectangle inside the box is whitened; the template image
    # itself stays untouched for registration.
    difference = np.asarray(Image.open(difference_path).convert("RGB"))
    original = np.asarray(
        Image.open(Path(session.template_dir) / artifact["template_image"]).convert("RGB")
    )
    box = (slice(504, 720), slice(612, 810))
    assert np.any(np.any(original[box] < 200, axis=2))
    assert not np.any(np.any(difference[box] < 200, axis=2))


def test_legacy_blank_artifact_without_contract_stays_untouched(tmp_path):
    """Only artifacts with neither answer nor exemption metadata stay untouched."""
    session = _session(tmp_path, mode="blank")
    artifact = dict(session.page_artifacts["page_1"])
    artifact.pop("template_handwriting_regions", None)
    artifact.pop("template_answer_regions", None)
    artifact.pop("difference_template_image", None)
    artifact.pop("difference_template_sha256", None)

    refreshed = grade_template._refresh_template_difference_artifact(
        session.template_dir, artifact, template_mode="blank",
    )

    assert "template_handwriting_regions" not in refreshed
    assert "difference_template_image" not in refreshed
