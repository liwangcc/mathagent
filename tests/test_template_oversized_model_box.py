"""A model box larger than a quarter of the page opens the editor for review.

Ruling (2026-10-06): an oversized model box must not fail the template import.
The page opens with the model geometry as editable candidates, the editor
names the box, and the teacher shrinks or deletes it before saving.
"""
from __future__ import annotations

from services.template_session import TemplateSession
from tests.test_template_session import (  # noqa: F401 - autouse readiness fixture
    _client,
    _make_app_and_session,
    _parse_sse,
    _patch_template_layout,
    _ready_batch_grade_chain,
    _template_png_bytes,
)


def _one_question_pipeline(box):
    async def fake_pipeline(_path, **kwargs):
        return {
            # Like the real pipeline, hand back the column layout it was given.
            "layout_result": kwargs.get("layout_result"),
            "questions": [{
                "question_number": "1", "question_no": "1", "q_id": "Q1",
                "question_text": "合成题干", "question_type": "fill",
                "standard_answer": "1", "answer_slots": ["1"], "slot_ids": ["1[1]"],
                "printed_path": ["1."], "directory_node_id": "n001", "directory_parent_id": None,
            }],
            "template_answer_regions": [{"question_number": "1", "regions": [box], "slot_ids": ["1[1]"]}],
            "question_anchors": [{"question_number": "1", "question_number_bbox": [60, 220, 100, 250]}],
        }

    return fake_pipeline


def _import(monkeypatch, tmp_path, box):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    monkeypatch.setattr(gr_router, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(gr_router, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    monkeypatch.setattr(grade_template, "recognize_template_pipeline", _one_question_pipeline(box))
    response = _client(Session).post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "daily"},
        files=[("files", ("page-1.png", _template_png_bytes("P1"), "image/png"))],
    )
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"], events
    template_id = next(payload for event, payload in events if event == "done")["template_id"]
    sess = TemplateSession.load_meta(template_id, str(tmp_path))
    page_run = next(iter(sess.layout_cut_runs.values()))["pages"]["page_1"]
    return sess, page_run


def test_oversized_model_box_opens_for_teacher_review(monkeypatch, tmp_path):
    box = [40, 60, 900, 700]  # 860 x 640, well over a quarter of the page
    sess, page_run = _import(monkeypatch, tmp_path, box)
    assert page_run["geometry_review_pending"] is True
    assert page_run["template_answer_region_candidates"][0]["regions"] == [box]
    # A candidate never becomes trusted student-side geometry before the teacher saves.
    assert not (sess.page_artifacts["page_1"] or {}).get("template_answer_regions")


def test_valid_model_boxes_stay_trusted(monkeypatch, tmp_path):
    box = [500, 220, 850, 280]
    sess, page_run = _import(monkeypatch, tmp_path, box)
    assert not page_run.get("geometry_review_pending")
    assert sess.page_artifacts["page_1"]["template_answer_regions"][0]["regions"] == [box]
