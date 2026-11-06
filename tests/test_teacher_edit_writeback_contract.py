"""Teacher edit write-back must never repersist contract-breaking rows.

Regression for the 01a7aa59 production class: a stale subquestion score layer
(subquestion_count=1 with subquestion_scores=[5, 0]) bricked template confirm
behind a misleading "模型返回结果不符合格式要求". Both echoes that can carry such
rows — the layout question_schema channel and the confirm question payload —
must fold the layer away at the boundary, and a genuine re-expansion attempt
must be rejected at save time with the question named (fail-closed, nothing
persisted).
"""
import copy
import json
from pathlib import Path

import pytest

from routers import grade
from routers import grade_pipeline
from routers import grade_review
from routers import grade_template
from services.grading_contract import AnswerSpec
from services.template_session import TemplateSession
from tests.test_template_directory_routes import _layout_payload, _session
from tests.test_template_session import _client, _make_app_and_session


@pytest.fixture
def edit_client(tmp_path, monkeypatch):
    engine, Session = _make_app_and_session()
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade, "SessionLocal", Session)

    async def no_background_model(*_args, **_kwargs):
        return None

    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", no_background_model)
    with _client(Session, raise_server_exceptions=True) as client:
        yield client
    engine.dispose()


def _stale_layer() -> dict:
    return {"subquestion_count": 1, "subquestion_scores": [5, 0]}


def test_layout_save_heals_stale_score_layer_echo(edit_client, tmp_path):
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1].update(_stale_layer())
    response = edit_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    # The on-disk row itself must be clean: the load-side heal must not mask a
    # dirty write-back (the previous version of this test was green either way).
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    assert raw["pages"][0]["questions"][1]["subquestion_scores"] == [5.0]
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    row = stored.pages[0]["questions"][1]
    assert row["subquestion_scores"] == [5.0]
    spec = AnswerSpec.model_validate(row)
    assert spec.subquestion_count == 1


def test_layout_save_heals_slot_edit_subquestion_layer_echo(edit_client, tmp_path):
    """A FE slot edit echoes subquestion_count = slot count (updateSlotStructure).

    On a directory leaf that legacy mirror layer is never a real hierarchy; it
    must fold into the root total at the save boundary instead of blocking the
    save (incident dfe229faafbd: consecutive bricks on the same edit).
    """
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1].update(subquestion_count=2, subquestion_scores=[5, 0])
    response = edit_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    row = raw["pages"][0]["questions"][1]
    assert row["subquestion_count"] == 1
    assert row["subquestion_scores"] == [5.0]
    AnswerSpec.model_validate(row)


def test_layout_save_passes_teacher_region_fix_over_stale_model_kind(edit_client, tmp_path):
    """Model misread a stem as a drawing question (fixture 2.3's natural state:
    answer_kind="drawing" + drawing regions + label anchor rides the wire as a
    stale echo); the teacher re-marks its region as a text answer region. The
    correction must pass through and the stale model drawing layer must clear —
    never block the correction on it (incident a5d95c2a, fourth occurrence).
    """
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1].update(
        region_kinds=["answer"],  # THE teacher's fix: the only field they changed
    )
    payload["template_answer_regions"][1]["regions"] = [[700.0, 350.0, 900.0, 550.0]]
    response = edit_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    row = raw["pages"][0]["questions"][1]
    assert row["region_kinds"] == ["answer"]
    assert row["answer_kind"] == "non_graphic"
    assert row["drawing_regions"] == []
    assert row["label_anchor"] is None
    AnswerSpec.model_validate(row)


def test_teacher_slot_edits_pass_through_confirm_and_bind(edit_client, tmp_path):
    """Teacher deletes a middle answer box (gap identity) and saves: the gap ids
    survive verbatim (never renumbered), confirm continues the flow, and
    student slot binding resolves the gap ids without slot_identity_error.
    """
    from services.template_slots import bind_student_slots

    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1].update(
        slot_ids=["2.3[1]", "2.3[3]"],      # 2.3[2] deleted by the teacher
        region_kinds=["answer", "answer"],
        answer_slots=["a", "b"], blank_count=2,
        blank_scores=[3.0, 2.0], subquestion_scores=[], max_score=5.0,
    )
    payload["template_answer_regions"][1]["regions"] = [
        [700.0, 350.0, 800.0, 550.0], [810.0, 350.0, 900.0, 550.0],
    ]
    response = edit_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    row = raw["pages"][0]["questions"][1]
    assert row["slot_ids"] == ["2.3[1]", "2.3[3]"]
    assert row["blank_count"] == 2
    assert row["answer_slots"] == ["a", "b"]
    AnswerSpec.model_validate(row)

    questions = raw["pages"][0]["questions"]
    confirm = edit_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": session.category,
        "exam_name": session.exam_name,
        "pages": [{"page_index": 0, "questions": questions}],
    })
    assert confirm.status_code == 200, confirm.text

    # the confirmed contract keeps the teacher's gap identities verbatim
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    confirmed = raw["pages"][0]["questions"][1]
    assert confirmed["slot_ids"] == ["2.3[1]", "2.3[3]"]
    assert confirmed["answer_slots"] == ["a", "b"]

    bound = bind_student_slots({"student_slots": [
        {"slot_id": "2.3[1]", "student_answer": "12", "answer_state": "visible"},
        {"slot_id": "2.3[3]", "student_answer": "34", "answer_state": "visible"},
    ]}, "2.3", confirmed)
    assert "slot_identity_error" not in bound
    assert [slot["slot_id"] for slot in bound["student_slots"]] == ["2.3[1]", "2.3[3]"]


def test_layout_geometry_pair_resolves_per_teacher_marks(edit_client, tmp_path):
    """Stale model drawing geometry clears per the teacher's kind marks.

    Product ruling (incident a5d95c2a, fourth occurrence): label anchors and
    stale region arrays are invisible model residue the teacher cannot delete
    in the UI, so blocking a correction on them is forbidden. The region marks
    are the source of truth; leftover geometry is deleted model noise. Only
    mark-less ambiguous producer data keeps failing closed.
    """
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][0].update(drawing_regions=[[10, 10, 100, 100]], label_anchor=[5, 5])
    response = edit_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 200, response.text
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    entry = raw["page_artifacts"]["page_1"]["question_schema"][0]
    assert entry["answer_kind"] == "non_graphic"
    assert entry["drawing_regions"] == []
    assert entry["label_anchor"] is None
    row = raw["pages"][0]["questions"][0]
    AnswerSpec.model_validate(row)


def test_confirm_heals_stale_score_layer_echo(edit_client, tmp_path):
    session = _session(tmp_path)
    submitted = copy.deepcopy(session.pages[0]["questions"])
    submitted[1].update(_stale_layer())
    response = edit_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id,
        "category": "daily",
        "pages": [{"page_index": 0, "questions": submitted}],
    })
    assert response.status_code == 200, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    row = stored.pages[0]["questions"][1]
    assert row["subquestion_scores"] == [5.0]
    AnswerSpec.model_validate(row)


@pytest.fixture
def edit_boundary_client(tmp_path, monkeypatch):
    """Same app as edit_client but with the production diagnostics boundary."""
    import tests.test_template_session as ts
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.processing_http import install_processing_errors

    engine, Session = _make_app_and_session()
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade, "SessionLocal", Session)

    async def no_background_model(*_args, **_kwargs):
        return None

    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", no_background_model)
    app = FastAPI()
    app.include_router(grade.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[ts.get_db] = _override_db
    app.dependency_overrides[ts._require_user] = lambda: ts.User(
        id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior",
    )
    install_processing_errors(app)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client
    engine.dispose()


def test_layout_save_rejection_reports_business_code_and_detail(edit_boundary_client, tmp_path):
    """A teacher-input contract rejection must not look like a model parse failure.

    Regression for diagnostic dfe229faafbd4509886baa2979307563: the rejection
    carried an exception chain, so the boundary classified it OUTPUT_PARSE_FAILED
    and the client rendered "模板处理：模型返回结果不符合格式要求" instead of the
    teacher-actionable per-question contract detail.
    """
    session = _session(tmp_path)
    payload = _layout_payload(session)
    payload["question_schema"][1].update(max_score=-5)
    response = edit_boundary_client.put(
        f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=payload,
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert "第 2.3 题的修改未通过合同校验，本次未保存" in body["detail"]
    assert body["error_info"]["code"] == "FORMAT_INVALID"
    assert "模型返回结果不符合格式要求" not in body["error_info"]["message"]


def test_confirm_400_for_legacy_breach_reports_business_code(edit_boundary_client, tmp_path):
    """The last-resort confirm guard keeps its business code too (no chain)."""
    session = _session(tmp_path)
    raw = json.loads(Path(session.meta_path).read_text(encoding="utf-8"))
    raw["pages"][0]["questions"][1].update(region_kinds=["drawing"], blank_count=2)
    Path(session.meta_path).write_bytes(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    submitted = copy.deepcopy(raw["pages"][0]["questions"])
    response = edit_boundary_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily",
        "pages": [{"page_index": 0, "questions": submitted}],
    })
    assert response.status_code == 400, response.text
    body = response.json()
    assert "已保存模板的题目合同无效，无法确认" in body["detail"]
    assert body["error_info"]["code"] == "FORMAT_INVALID"
    assert "模型返回结果不符合格式要求" not in body["error_info"]["message"]


def test_confirm_400_for_submitted_breach_names_question_with_business_code(edit_boundary_client, tmp_path):
    """Regression for diagnostic 2b5e7b509d74481aa71a931d450af93d: a submitted
    row that fails AnswerSpec (here a standard figure left on a question that is
    no longer graphic) must name the question and read as a business rejection,
    not "模型返回结果不符合格式要求" with the row dumped into the detail."""
    session = _session(tmp_path)
    submitted = copy.deepcopy(session.pages[0]["questions"])
    submitted[0]["drawing_reference"] = {
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": "manual", "variants": [],
    }
    response = edit_boundary_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily",
        "pages": [{"page_index": 0, "questions": submitted}],
    })
    assert response.status_code == 400, response.text
    body = response.json()
    assert "第 1 页第 2.2 题未通过答案合同校验，本次未保存：标准图只能用于作图题或图文混合题" in body["detail"]
    assert "input_value" not in body["detail"]
    assert body["error_info"]["code"] == "FORMAT_INVALID"
    assert "模型返回结果不符合格式要求" not in body["error_info"]["message"]


def test_normalize_still_rejects_geometry_without_explicit_kind():
    # Ambiguous producer data (geometry, no answer_kind) keeps failing closed;
    # only an explicit non-graphic mark resolves the contradiction pair.
    from services.template_evidence import normalize_question_schema
    with pytest.raises(ValueError, match="非作图题不能包含作图定位"):
        normalize_question_schema([{
            "question_number": "1", "question_type": "solve",
            "drawing_regions": [[0, 0, 10, 10]], "label_anchor": [1, 1],
        }])
