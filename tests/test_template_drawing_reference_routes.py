"""Teacher-only reference save contract with synthetic templates and no model work."""

import copy

import pytest

from routers import grade
from routers import grade_pipeline
from routers import grade_template
from tests.test_template_directory_routes import _session
from tests.test_template_session import _client, _make_app_and_session
from tests.grade_family import set_grade_global
from routers import grade_recognition
from routers import grade_template_import
from routers import grade_template_confirm


def _reference(*, mode="fixed", point=(800, 450)):
    return {
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": mode,
        "variants": [{"strokes": [{"kind": "point", "point": list(point)}]}] if mode == "fixed" else [],
        "context_regions": [], "teacher_confirmed": False,
    }


@pytest.fixture
def reference_client(tmp_path, monkeypatch):
    engine, Session = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade_recognition, "SessionLocal", Session)

    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("unexpected model work")

    async def no_classifier(*_args, **_kwargs):
        return None

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", forbidden_model)
    monkeypatch.setattr(grade_template_confirm, "_classify_confirmed_template_knowledge", no_classifier)
    with _client(Session, monkeypatch, raise_server_exceptions=True) as client:
        yield client
    engine.dispose()


def test_model_reference_generation_endpoint_is_retired(reference_client, tmp_path):
    session = _session(tmp_path)
    response = reference_client.post(f"/api/batch-grade/template/{session.template_id}/drawing-reference", json={"page_index": 0})
    assert response.status_code == 404


@pytest.mark.parametrize("mode", ["fixed", "manual"])
@pytest.mark.parametrize("answer_kind", ["drawing", "mixed"])
def test_reference_candidate_final_confirmation_and_reopen(reference_client, tmp_path, monkeypatch, mode, answer_kind):
    session = _session(tmp_path)
    session.pages[0]["questions"][1]["answer_kind"] = answer_kind
    session.page_artifacts["page_1"]["question_schema"][1]["answer_kind"] = answer_kind
    for run in session.layout_cut_runs.values():
        run["pages"]["page_1"]["template_ocr_diagnostics"] = {"model_stages": {"template-fullsolve-v1": {"status": "completed"}}}
    session.save_meta()

    reference = _reference(mode=mode)
    pages = copy.deepcopy(session.pages)
    pages[0]["questions"][1].update(
        question_text="教师核对题干", standard_answer="" if answer_kind == "drawing" else "教师标准答案",
        drawing_reference=reference,
    )
    confirmation = {"template_id": session.template_id, "category": "daily", "pages": pages}
    # 保存模板即确认：固定参考不再要求教师二次勾选（也不再 422）。
    saved = reference_client.post("/api/batch-grade/template/confirm", json=confirmation)
    assert saved.status_code == 200, saved.text
    reopened = reference_client.get(f"/api/batch-grade/template/{session.template_id}/recognition")
    assert reopened.status_code == 200
    question = reopened.json()["pages"][0]["questions"][1]
    assert question["question_no"] == "2.3" and question["drawing_reference"]["comparison_mode"] == mode
    # Saving the template is the teacher confirmation for both fixed and
    # custom/manual drawing comparison modes.
    assert bool(question["drawing_reference"]["teacher_confirmed"]) is True
    assert question["question_text"] == "教师核对题干"
    assert question["standard_answer"] == ("" if answer_kind == "drawing" else "教师标准答案")
    assert question["directory_node_id"] == "n003"
    assert question["printed_path"] == ["二", "3"]
    assert question["answer_kind"] == answer_kind


def test_out_of_box_teacher_reference_cannot_be_confirmed(reference_client, tmp_path, monkeypatch):
    session = _session(tmp_path)

    pages = copy.deepcopy(session.pages)
    pages[0]["questions"][1]["drawing_reference"] = _reference(point=(100, 100))
    response = reference_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": pages,
    })
    assert response.status_code == 422, response.text


def _flagged_session(tmp_path, flag):
    session = _session(tmp_path)
    session.pages[0]["questions"][0]["max_flags"] = [flag]
    session.save_meta()
    return session


def test_max_report_survives_draft_reopen_and_blocks_until_kept(reference_client, tmp_path):
    flag = {"scope": "question", "status": "extra", "reason": "该编号不是独立题目", "source": "solver"}
    session = _flagged_session(tmp_path, flag)
    draft = reference_client.get(f"/api/batch-grade/template/{session.template_id}/recognition")
    assert draft.status_code == 200
    assert draft.json()["pages"][0]["questions"][0]["max_flags"] == [flag]

    pages = copy.deepcopy(session.pages)
    blocked = reference_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": pages})
    assert blocked.status_code == 422 and "Max" in blocked.text and "确认保留" in blocked.text

    pages[0]["questions"][0]["max_flags_kept"] = ["*"]
    kept = reference_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": pages})
    assert kept.status_code == 200, kept.text
    reopened = reference_client.get(f"/api/batch-grade/template/{session.template_id}/recognition")
    question = reopened.json()["pages"][0]["questions"][0]
    assert "max_flags" not in question and "max_flags_kept" not in question


def test_deleting_a_reported_slot_resolves_its_max_review(reference_client, tmp_path):
    flag = {"scope": "slot", "slot_id": "2.2[2]", "status": "refused", "reason": "看不清", "source": "locator"}
    session = _flagged_session(tmp_path, flag)
    response = reference_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": copy.deepcopy(session.pages)})
    assert response.status_code == 200, response.text
