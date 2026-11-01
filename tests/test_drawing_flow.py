"""Real confirmation routes, frozen contracts, rendering and scoring boundaries."""
import copy

import pytest

from services.drawing_reference import normalize_reference
from services.template_session import TemplateSession
from tests.test_template_directory_routes import directory_client, _session, _layout_payload


def _reference(confirmed=True):
    return normalize_reference({"version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
                                "comparison_mode": "fixed", "context_regions": [],
                                "variants": [{"strokes": [{"kind": "point", "point": [800, 450]}]}]},
                               trusted_confirmed=confirmed)


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_reference_confirm_roundtrip_with_existing_geometry(directory_client, tmp_path, mode):
    session = _session(tmp_path, mode=mode)
    submitted = copy.deepcopy(session.pages)
    submitted[0]["questions"][1]["drawing_reference"] = _reference()
    response = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": submitted,
    })
    assert response.status_code == 200, response.text
    stored = TemplateSession.load_meta(session.template_id, str(tmp_path))
    reference = stored.build_answer_map()["2.3"]["drawing_reference"]
    assert reference == _reference()
    assert stored.pages[0]["questions"][1]["drawing_regions"] == [[700, 350, 900, 550]]
    assert stored.pages[0]["questions"][1]["label_anchor"] == [690, 400]
    assert "drawing_reference" not in stored.build_answer_map()["2.2"]
    layout_payload = _layout_payload(stored)
    if mode == "blank":
        # The real blank-template editor sends one empty exemption entry per Q.
        layout_payload["template_handwriting_regions"] = [
            {"question_number": question["question_no"], "regions": []}
            for question in stored.pages[0]["questions"]
        ]
    response = directory_client.put(f"/api/batch-grade/template/{session.template_id}/layout-overrides", json=layout_payload)
    assert response.status_code == 200, response.text
    reopened = TemplateSession.load_meta(session.template_id, str(tmp_path))
    assert reopened.pages[0]["questions"][1]["drawing_reference"]["teacher_confirmed"] is False


@pytest.mark.parametrize("kind", ["outside", "ordinary_question"])
def test_invalid_reference_never_enters_confirmed_template(directory_client, tmp_path, kind):
    session = _session(tmp_path)
    submitted = copy.deepcopy(session.pages)
    index = 0 if kind == "ordinary_question" else 1
    # 保存模板即确认：未勾选二次确认的参考不再是"无效"（not_confirmed 用例已随
    # 口径取消）；本用例仅覆盖几何越界与题目类型不符两类仍然无效的参考。
    reference = _reference(confirmed=True)
    if kind == "outside":
        reference["variants"][0]["strokes"][0]["point"] = [200, 200]
    submitted[0]["questions"][index]["drawing_reference"] = reference
    response = directory_client.post("/api/batch-grade/template/confirm", json={
        "template_id": session.template_id, "category": "daily", "pages": submitted,
    })
    assert response.status_code in {400, 422}, response.text
    assert TemplateSession.load_meta(session.template_id, str(tmp_path)).status == "ocr_done"
