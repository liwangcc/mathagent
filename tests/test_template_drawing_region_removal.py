"""A grid wrongly marked as a drawing region, removed by the teacher before confirmation.

Synthetic page only. Question 1 has six text slots and a seventh, drawing,
slot on a printed grid that is not graded. With that region kept, the drawing
side has no evidence and sends the question to review even when all six text
slots are transcribed. The teacher deletes the one region and keeps the six
slots: the edit must survive save, reopen and confirm, and grading must make no
drawing call, apply no drawing penalty and raise no drawing review. The same
template with the region kept still gets the drawing check.
"""
import copy

import pytest
from PIL import Image, ImageDraw

from services import drawing_grading
from services.template_evidence import build_template_artifact
from services.template_session import TemplateSession
from tests.test_fill_slot_answer_contract import (
    BOXES, EXPECTED, NUMBER_BOX, SLOTS, STEM, _client_for, _grade, _grading_map,
)

GRID = [100, 500, 700, 800]
LABEL = [60, 650]
ALL_SLOTS = [*SLOTS, "1[7]"]
KINDS = ["answer"] * 6 + ["drawing"]


def _seed(tmp_path):
    question = {"question_no": "1", "question_type": "fill", "question_text": STEM,
                "standard_answer": "; ".join(EXPECTED), "answer_slots": [*EXPECTED, ""],
                "slot_ids": ALL_SLOTS, "region_kinds": KINDS, "blank_count": 7,
                "answer_kind": "mixed", "drawing_regions": [GRID], "label_anchor": LABEL}
    anchors = [{"question_number": "1", "question_number_bbox": NUMBER_BOX, "confidence": 1,
                "human_confirmed": True, "confidence_source": "teacher"}]
    regions = [{"question_number": "1", "regions": [*BOXES, GRID], "slot_ids": ALL_SLOTS,
                "region_kinds": KINDS, "confidence": 1, "human_confirmed": True, "confidence_source": "teacher"}]
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 300), "1.", fill="black")
    for x in range(GRID[0], GRID[2] + 1, 50):
        draw.line([(x, GRID[1]), (x, GRID[3])], fill="black")
    source = tmp_path / "grid.png"
    image.save(source)
    sess = TemplateSession("grid-region-removal", str(tmp_path), teacher_id=1)
    artifact = build_template_artifact(
        str(source), sess.template_dir, 0, [question], question_anchors=anchors,
        template_answer_regions=regions, template_handwriting_regions=copy.deepcopy(regions),
        template_mode="marked")
    sess.add_page(0, [question], artifact=artifact)
    sess.status, sess.category, sess.template_mode = "ocr_done", "daily", "marked"
    sess.save_meta()
    return sess


def _removal_payload():
    """What the editor saves after the teacher deletes slot 1[7], keeping 1[1]..1[6]."""
    return {
        "page_index": 0, "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
        "question_anchors": [{"question_number": "1", "question_number_bbox": NUMBER_BOX}],
        "template_answer_regions": [{"question_number": "1", "regions": BOXES, "slot_ids": SLOTS,
                                     "region_kinds": ["answer"] * 6}],
        "template_handwriting_regions": [{"question_number": "1", "regions": BOXES}],
        "question_schema": [{"question_number": "1", "question_type": "fill", "slot_ids": SLOTS,
                             "region_kinds": ["answer"] * 6, "answer_kind": "non_graphic",
                             "drawing_regions": [], "label_anchor": None, "blank_count": 6,
                             "answer_slots": EXPECTED, "blank_scores": [],
                             "subquestion_count": 1, "subquestion_scores": []}],
    }


@pytest.mark.parametrize("removed", [True, False])
def test_removed_grid_region_survives_reopen_and_grading_makes_no_drawing_call(monkeypatch, tmp_path, removed):
    sess = _seed(tmp_path)
    client = _client_for(monkeypatch, tmp_path)
    base = f"/api/batch-grade/template/{sess.template_id}"
    if removed:
        saved = client.put(f"{base}/layout-overrides", json=_removal_payload())
        assert saved.status_code == 200, saved.text
    pages = client.get(f"{base}/recognition").json()["pages"]
    question = pages[0]["questions"][0]
    if removed:
        assert (question["slot_ids"], question["region_kinds"], question["answer_slots"]) == (
            SLOTS, ["answer"] * 6, EXPECTED)
        assert (question["answer_kind"], question["drawing_regions"], question["label_anchor"]) == (
            "non_graphic", [], None)
        artifact = TemplateSession.load_meta(sess.template_id, str(tmp_path)).page_artifacts["page_1"]
        assert artifact["template_answer_regions"][0]["slot_ids"] == SLOTS
        # A non-graphic schema row carries no drawing fields at all.
        assert artifact["question_schema"][0].get("answer_kind", "non_graphic") == "non_graphic"
        assert not artifact["question_schema"][0].get("drawing_regions")

    confirmed = client.post("/api/batch-grade/template/confirm",
                            json={"template_id": sess.template_id, "category": "daily", "pages": pages})
    assert confirmed.status_code == 200, confirmed.text
    frozen = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    row = frozen.pages[0]["questions"][0]
    assert row["slot_ids"] == (SLOTS if removed else ALL_SLOTS)
    assert row["answer_kind"] == ("non_graphic" if removed else "mixed")

    calls = []
    real = drawing_grading.bound_assessment

    def counted(paper, unit):
        calls.append(unit["parent_question_no"])
        return real(paper, unit)

    monkeypatch.setattr(drawing_grading, "bound_assessment", counted)
    result, _messages = _grade(monkeypatch, _grading_map(frozen, tmp_path), {"1": dict(zip(SLOTS, EXPECTED))})
    detail = result["details"][0]
    if removed:
        assert calls == []
        assert detail["result"] == "correct" and result["needs_review_count"] == 0
        assert [unit["result"] for unit in detail["blank_results"]] == ["correct"] * 6
        assert "drawing" not in str(detail.get("error_type") or "")
        assert "drawing_assessment" not in detail
    else:
        # A genuine drawing region keeps its check and its review route.
        assert calls == ["1"]
        assert detail["result"] == "needs_review" and detail["error_type"] == "drawing_mixed_review"
