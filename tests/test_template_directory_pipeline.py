"""Legacy import fixtures and preflight contracts; active v4 has separate tests."""
import asyncio
import json
from pathlib import Path

from PIL import Image

from services import template_directory_pipeline as pipeline
from services import template_recognition
from services.merged_template_prompt import build_merged_template_prompt
from services.merged_template_receive import receive_merged_template_output
from services.teaching_context import make_teaching_context, use_teaching_context


def leaf(number, *, question_type="solve", y=100, x=300, regions=None, exemptions=None,
         requires_drawing=False):
    """Legacy fixture builder retained for template-session integration tests."""
    return {
        "printed_label": str(number), "kind": "question", "context_summary": "fixture",
        "question_text": f"题干_Q{number}", "number_bbox": [x, y, x + 40, y + 30],
        "question_type": question_type, "choice_mode": "single" if question_type == "choice" else "unknown",
        "requires_drawing": requires_drawing, "answer_kind": "drawing" if requires_drawing else "non_graphic",
        "regions": regions or [[x + 80, y, x + 220, y + 45]], "drawing_regions": regions or [],
        "label_anchor": [x + 70, y + 20] if requires_drawing else None,
        "standard_answer": f"答案_Q{number}", "solution_steps": ["fixture"], "children": [],
        "exemptions": exemptions or [],
    }


class ModelFixture:
    """Small transport double compatible with both old session tests and merged output."""
    def __init__(self, _phase, tree):
        self.tree = tree
        self.calls = [{"phase": phase} for phase in ("directory", "solve", "drawing")]
        self.block_phases = set()
        self.cancelled = set()
        self.response_edit = {}

    async def __call__(self, _images, _prompt, **_kwargs):
        self.cancelled.update(self.block_phases)
        payload = {"coordinate_space": "normalized_1000_v1", "needs_review": [],
                   "nodes": json.loads(json.dumps(self.tree["nodes"]))}
        leaves = []
        def visit(nodes):
            for node in nodes:
                if node.get("children"):
                    visit(node["children"])
                elif node.get("kind") in {"question", "answer_item"}:
                    leaves.append(node)
        visit(payload["nodes"])
        payload["questions"] = [dict(question_no=n["printed_label"], question_text=n.get("question_text", ""),
                                      standard_answer=n.get("standard_answer", ""), answer_kind=n.get("answer_kind"),
                                      drawing_regions=n.get("drawing_regions", []), label_anchor=n.get("label_anchor"))
                            for n in leaves]
        payload["answers"] = [{"q_id": "Q" + n["printed_label"], "standard_answer": n.get("standard_answer", "")}
                              for n in leaves]
        if "solve" in self.block_phases:
            for n in leaves: n["standard_answer"] = ""
            for row in payload["answers"]: row["standard_answer"] = ""
        if "drawing" in self.block_phases:
            for n in leaves:
                n.pop("drawing_regions", None); n.pop("label_anchor", None)
            for row in payload["questions"]:
                row["drawing_regions"] = []; row["label_anchor"] = None
        for phase, edit in self.response_edit.items():
            if phase in self.block_phases:
                continue
            edit(payload)
        for answer in payload["answers"]:
            number = str(answer.get("q_id", "")).removeprefix("Q")
            for row in payload["questions"]:
                if str(row.get("question_no")) == number and "standard_answer" in answer:
                    row["standard_answer"] = answer["standard_answer"]
        for node, row in zip(leaves, payload["questions"]):
            for key in ("question_text", "standard_answer", "answer_kind", "drawing_regions", "label_anchor"):
                if key in row: node[key] = row[key]
        return json.dumps(payload, ensure_ascii=False)


def _payload():
    return {"coordinate_space": "normalized_1000_v1", "needs_review": [], "nodes": [
        {"printed_label": "一", "kind": "section", "context_summary": "基础题",
         "number_bbox": [20, 20, 60, 50], "question_type": None, "choice_mode": "unknown",
         "regions": [], "children": [{
             "printed_label": "1", "kind": "question", "context_summary": "选择题",
             "question_text": "在图中选择正确选项。", "number_bbox": [80, 100, 120, 130],
             "question_type": "choice", "choice_mode": "single", "requires_drawing": False,
             "answer_kind": "non_graphic", "regions": [[250, 100, 450, 150]],
             "drawing_regions": [], "label_anchor": None, "standard_answer": "C",
             "solution_steps": ["核对选项"], "children": []}]},
        {"printed_label": "二", "kind": "question", "context_summary": "作图题",
         "question_text": "在坐标图中画出线段。", "number_bbox": [20, 300, 60, 330],
         "question_type": "solve", "choice_mode": "unknown", "requires_drawing": True,
         "answer_kind": "drawing", "regions": [],
         "drawing_regions": [[300, 350, 700, 650]], "label_anchor": [285, 500],
         "standard_answer": "连接指定两点", "solution_steps": ["按坐标定位"], "children": []}]}


def _layout():
    return {"column_layout": {"column_count": 1, "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}]}}


def _harness(tmp_path, monkeypatch):
    source = tmp_path / "template.png"
    Image.new("RGB", (1000, 800), "white").save(source)
    calls = []

    async def transport(images, prompt, **kwargs):
        paths = [images] if isinstance(images, str) else list(images)
        calls.append((paths, prompt, kwargs))
        assert len(paths) == 2
        return json.dumps(_payload(), ensure_ascii=False)

    monkeypatch.setattr(pipeline, "call_gpt_vision", transport)
    return source, calls


def test_missing_stage_fails_closed_without_model_call(tmp_path, monkeypatch):
    source, calls = _harness(tmp_path, monkeypatch)
    result = asyncio.run(template_recognition.recognize_template_pipeline(str(source), layout_result=_layout()))
    assert result["error"] and not result["questions"]
    assert calls == []


def test_missing_layout_fails_before_model(tmp_path, monkeypatch):
    source, calls = _harness(tmp_path, monkeypatch)
    result = asyncio.run(template_recognition.recognize_template_pipeline(str(source), layout_result=None))
    assert result["error"] and not result["questions"]
    assert calls == []


def test_merged_receiver_rejects_contradictory_explicit_kind():
    payload = _payload()
    payload["nodes"][1]["answer_kind"] = "non_graphic"
    try:
        receive_merged_template_output(payload)
    except ValueError as exc:
        assert "non_graphic" in str(exc)
    else:
        raise AssertionError("contradictory drawing kind was accepted")


def test_model_drawing_generation_phase_is_retired():
    assert "template-drawing-reference-v1" not in pipeline._REQUEST_PHASES
    assert "本次学段：初中" in build_merged_template_prompt(1000, 800, education_stage="junior")
