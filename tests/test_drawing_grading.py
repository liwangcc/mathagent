import asyncio
import copy
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest
from PIL import Image

from services import drawing_grading as drawing
from services.drawing_reference import normalize_reference
from services.grading_persistence import image_content_hash, build_grading_contract_hash


@pytest.fixture
def reference():
    return normalize_reference({
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": "fixed", "variants": [{"strokes": [
            {"kind": "polyline", "points": [[200, 300], [500, 500], [650, 500]]},
        ]}], "context_regions": [],
    }, trusted_confirmed=True)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "student.png"
    Image.new("RGB", (1000, 820), "white").save(path)
    return str(path)


def verdict(result="correct", probability=95):
    return {"result": result, "p_correct": probability, "reason": "观察到图形符合标准"}


@pytest.mark.parametrize("probability", [True, False, -1, 101, float("nan"), float("inf"), "95"])
def test_probability_is_finite_number_not_boolean(probability):
    with pytest.raises(ValueError):
        drawing.validate_verdict(verdict(probability=probability))


def test_probability_gate_has_one_meaning():
    assert drawing.accepted(verdict())
    assert drawing.ACCEPT_THRESHOLD == 90
    assert drawing.PIXEL_THRESHOLD == 80
    assert drawing.accepted(verdict(probability=90))
    assert drawing.accepted(verdict(probability=92))
    assert not drawing.accepted(verdict(probability=89.99))
    assert not drawing.accepted(verdict("wrong", 99))
    assert not drawing.accepted(verdict("needs_review", None))
    with pytest.raises(ValueError):
        drawing.validate_verdict(verdict("correct", None))


def test_overlay_preserves_input_and_uses_complete_alternatives(reference):
    canvas = np.full((820, 1000, 3), 255, np.uint8)
    canvas[400:415, 490:510] = 0
    saved = canvas.copy()
    reference["variants"].append({"strokes": [{"kind": "point", "point": [300, 300]}]})
    inputs = drawing.build_inputs(canvas, {"drawing_regions": [[100, 100, 900, 900]]}, reference)
    assert np.array_equal(canvas, saved)
    assert len(inputs["overlays"]) == 2
    assert inputs["overlays"][0] != inputs["overlays"][1]
    assert inputs["student_image"] not in inputs["overlays"]
    assert inputs["variant_source_images"] == [None, inputs["student_image"]]
    assert inputs["variant_prompts"][0] == drawing.PROMPT
    assert "仅有1个点，不要求连线" in inputs["variant_prompts"][1]
    assert "检查位置、方向、连接、长度" not in inputs["variant_prompts"][1]
    assert "300" not in inputs["variant_prompts"][1]


def test_point_prompt_counts_only_confirmed_reference_not_question_text(monkeypatch):
    variant = {"strokes": [{"kind": "point", "point": [200, 200]}, {"kind": "point", "point": [400, 400]}]}
    prompt = drawing._reference_prompt(variant)
    assert "仅有2个点" in prompt
    assert "不要求连线" in prompt
    assert "p_correct" in prompt and "0到100" in prompt
    assert "200" not in prompt and "400" not in prompt
    call = AsyncMock(return_value={"content": json.dumps(verdict()), "model": "qwen3.8-flash", "parameters": {}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    asyncio.run(drawing.assess_inputs({"overlays": ["image"], "variant_prompts": [prompt],
                                      "variant_source_images": ["unmodified-student"]}))
    assert call.await_args.args[0][0]["content"] == [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "unmodified-student"}},
        {"type": "image_url", "image_url": {"url": "image"}},
    ]


def test_outside_scale_requires_review_not_missing_context(reference):
    reference["context_regions"] = [[901, 901, 999, 999]]
    with pytest.raises(ValueError, match="context"):
        drawing.build_inputs(np.full((820, 1000, 3), 255, np.uint8),
                             {"drawing_regions": [[100, 100, 900, 900]]}, reference)


def test_column_reconstruction_requires_actual_endpoints(tmp_path):
    left, right = tmp_path / "left.png", tmp_path / "right.png"
    Image.new("RGB", (3, 5), "red").save(left)
    Image.new("RGB", (7, 5), "blue").save(right)
    diagnostics = {"template_width": 10, "template_height": 5, "answer_source_complete": True,
                   "evidence_columns": [{"crop_left_px": 0, "crop_right_px": 3},
                                        {"crop_left_px": 3, "crop_right_px": 10}]}
    canvas = drawing.reconstruct_canvas((str(left), str(right)), diagnostics)
    assert list(canvas[0, 2]) == [255, 0, 0]
    assert list(canvas[0, 3]) == [0, 0, 255]
    diagnostics["evidence_columns"] = [{}, {}]
    with pytest.raises(ValueError, match="endpoints"):
        drawing.reconstruct_canvas((str(left), str(right)), diagnostics)


def test_small_flash_view_enlarges_exact_pixels_without_inventing_ink():
    import base64
    import io
    canvas = np.full((286, 272, 3), 255, dtype=np.uint8)
    canvas[100:103, 199:203] = 143
    uri = drawing._png_uri(canvas)
    rendered = np.array(Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))))
    assert rendered.shape == (1144, 1088, 3)
    assert np.array_equal(rendered, np.repeat(np.repeat(canvas, 4, axis=0), 4, axis=1))


def test_plus_mask_scales_non_square_page_and_preserves_flash_source(tmp_path):
    canvas = np.full((1811, 1280, 3), [30, 80, 120], dtype=np.uint8)
    left, right = tmp_path / "left.png", tmp_path / "right.png"
    Image.fromarray(canvas[:, :470]).save(left)
    Image.fromarray(canvas[:, 470:]).save(right)
    paths = (str(left), str(right))
    originals = [p.read_bytes() for p in (left, right)]
    diagnostics = {"template_width": 1280, "template_height": 1811, "answer_source_complete": True,
                   "evidence_columns": [{"crop_left_px": 0, "crop_right_px": 470},
                                        {"crop_left_px": 470, "crop_right_px": 1280}]}
    questions = [{"answer_kind": "drawing", "drawing_regions": [[715, 348, 920, 502]]},
                 {"answer_kind": "mixed", "drawing_regions": [[116, 778, 530, 895],
                                                                 [548, 851, 608, 879]]}]
    text_paths = drawing.build_text_evidence(paths, diagnostics, questions, str(tmp_path / "plus"))
    plus = drawing.reconstruct_canvas(text_paths, diagnostics)
    expected = canvas.copy()
    # Independently computed floor/ceil bounds on a 1280 x 1811 page.
    expected[630:910, 915:1178] = 255
    expected[1408:1621, 148:679] = 255
    expected[1541:1592, 701:779] = 255
    assert np.array_equal(plus, expected)
    assert np.array_equal(drawing.reconstruct_canvas(paths, diagnostics), canvas)
    assert [p.read_bytes() for p in (left, right)] == originals
    assert list(plus[1550, 900]) == [30, 80, 120]  # mixed text remains visible


def test_plus_mask_requires_saved_drawing_geometry(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (10, 20), "black").save(source)
    diagnostics = {"template_width": 10, "template_height": 20, "answer_source_complete": True,
                   "evidence_columns": [{}]}
    with pytest.raises(ValueError, match="drawing regions"):
        drawing.build_text_evidence((str(source),), diagnostics,
                                    [{"answer_kind": "drawing"}], str(tmp_path / "plus"))


def test_cache_sends_masked_copy_to_plus_and_original_evidence_to_flash(tmp_path, monkeypatch):
    from services.paper_cache import PaperCache
    from services.template_evidence import EvidenceResult
    from types import SimpleNamespace

    path = tmp_path / "source.png"
    Image.new("RGB", (200, 300), "black").save(path)
    question = {"question_no": "2.3", "question_text": "画图", "question_type": "solve",
                "answer_kind": "drawing", "drawing_regions": [[250, 200, 800, 900]],
                "label_anchor": [200, 500]}
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": [question]}])
    diagnostics = {"template_width": 200, "template_height": 300, "answer_source_complete": True,
                   "evidence_columns": [{}]}
    evidence = EvidenceResult(page_type_id="page_1", evidence_path=str(path),
                              question_numbers=["2.3"], blank_question_numbers=[], diagnostics=diagnostics)
    cache._template_evidence_matcher = SimpleNamespace(match_and_build=lambda *a, **kw: evidence)
    cache._require_local_evidence = True
    seen = []

    async def plus(image_path, label, **kwargs):
        pixels = np.array(Image.open(image_path).convert("RGB"))
        assert (pixels[60:270, 50:160] == 255).all()
        assert (pixels[20, 20] == 0).all()
        seen.append("plus")
        return {"questions": [{"question_number": "2.3", "student_answer": "", "answer_state": "blank"}]}

    async def flash(**kwargs):
        assert kwargs["evidence_paths"] == (str(path),)
        assert (np.array(Image.open(path)) == 0).all()
        seen.append("flash")
        return {"2.3": {"result": "needs_review"}}

    cache._recognize_answers_only = plus
    monkeypatch.setattr(drawing, "assess_page", flash)
    result = asyncio.run(cache._recognize_with_local_evidence(str(path)))
    assert not result.get("error"), result
    assert seen == ["plus", "flash"]
    assert result["_drawing_assessments"]["2.3"]["result"] == "needs_review"


def test_visual_request_contains_no_question_or_solution(monkeypatch):
    from services import flash_grading
    call = AsyncMock(return_value={"content": json.dumps(verdict()), "model": "qwen3.8-flash",
                                  "parameters": {"reasoning_effort": "high"}})
    monkeypatch.setattr(flash_grading, "call_flash", call)
    result = asyncio.run(drawing.assess_inputs({"overlays": ["data:image/png;base64,eA=="]}, teacher_id="t1"))
    messages = call.await_args.args[0]
    assert messages == [{"role": "user", "content": [
        {"type": "text", "text": drawing.PROMPT},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
    ]}]
    assert call.await_args.kwargs["phase"] == "drawing"
    assert drawing.accepted(result[0])


def test_failed_call_never_awards_zero(monkeypatch):
    from services import flash_grading
    monkeypatch.setattr(flash_grading, "call_flash", AsyncMock(side_effect=RuntimeError("upstream")))
    result = asyncio.run(drawing.assess_inputs({"overlays": ["x"]}))
    assert result[0]["result"] == "needs_review"
    assert result[0]["p_correct"] is None


@pytest.mark.parametrize("model_result,probability,expected", [
    ("correct", 90, "correct"), ("correct", 89.9, "needs_review"),
    ("wrong", 99, "wrong"), ("needs_review", None, "needs_review"),
])
def test_manual_custom_drawing_uses_question_text_and_confidence_gate(
    monkeypatch, model_result, probability, expected,
):
    call = AsyncMock(return_value={"content": json.dumps(verdict(model_result, probability)),
                                   "model": "qwen3.8-flash", "parameters": {}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    canvas = np.full((100, 100, 3), 255, np.uint8)
    question = {"question_text": "在坐标图上画出从A到B的路线",
                "drawing_regions": [[100, 100, 900, 900]]}
    check = asyncio.run(drawing.assess_manual_input(canvas, question))
    assert drawing._manual_outcome(check)[0] == expected
    content = call.await_args.args[0][0]["content"]
    assert content[0]["type"] == "text" and "路线" in content[0]["text"]
    assert len(content) == 2 and content[1]["type"] == "image_url"


def test_missing_reference_uses_custom_flash_route_with_saved_region(tmp_path, monkeypatch):
    source = tmp_path / "student.png"
    Image.new("RGB", (100, 100), "white").save(source)
    call = AsyncMock(return_value={"content": json.dumps(verdict()), "model": "qwen3.8-flash",
                                  "parameters": {}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    result = asyncio.run(drawing.assess_page(
        source_path=str(source), page_type_id="page_1",
        questions=[{"question_no": "3", "question_text": "画出路线",
                    "answer_kind": "drawing", "drawing_regions": [[100, 100, 900, 900]]}],
        transcriptions=[], evidence_paths=(str(source),),
        diagnostics={"template_width": 100, "template_height": 100,
                     "answer_source_complete": True, "evidence_columns": [{}]},
    ))
    item = result["3"]
    assert item["comparison_mode"] == "custom"
    assert item["decision"] == "correct"
    content = call.await_args.args[0][0]["content"]
    assert content[0]["type"] == "text" and "画出路线" in content[0]["text"]
    assert len(content) == 2 and content[1]["type"] == "image_url"


def test_custom_assessment_binds_without_a_reference(reference, source):
    unit = {"parent_question_no": "2", "student_answer": "绘图",
            "entry": {"question_text": "按题干画图", "answer_kind": "drawing",
                      "drawing_regions": [[100, 100, 900, 900]]}}
    source_hash = drawing.image_content_hash(source)
    raw = {"question_number": "2", "policy": drawing.POLICY,
           "threshold": drawing.ACCEPT_THRESHOLD, "source_sha256": source_hash,
           "page_type_id": "page_1", "transcript_sha256": drawing.transcript_digest("绘图"),
           "rubric_sha256": drawing.rubric_digest(unit["entry"]), "comparison_mode": "custom",
           "checks": [{**verdict(), "variant_index": 0, "model_called": True}]}
    paper = {"image_path": source, "ocr_page_type": "page_1", "drawing_assessments": {"2": raw}}
    result = drawing.bound_assessment(paper, unit)
    assert result["comparison_mode"] == "custom"
    assert result["decision"] == "correct"


def _bound(reference, source, answer=""):
    assessment = {"question_number": "2", "policy": drawing.POLICY,
                  "threshold": drawing.ACCEPT_THRESHOLD,
                  "reference_hash": reference["reference_hash"],
                  "source_sha256": image_content_hash(source), "page_type_id": "page_1",
                  "transcript_sha256": drawing.transcript_digest(answer),
                  "reason": "符合标准", "checks": [{**verdict(), "variant_index": 0, "model_called": True}]}
    assessment["pixel_gate"] = {
        "version": drawing.PIXEL_VERSION, "threshold": drawing.PIXEL_THRESHOLD,
        "reference_hash": reference["reference_hash"], "source_sha256": assessment["source_sha256"],
        "variants": [{"variant_index": 0, "status": "pass", "score": 100, "reason": "像素通过"}],
    }
    unit = {"parent_question_no": "2", "student_answer": answer, "entry": {"drawing_reference": reference}}
    assessment["rubric_sha256"] = drawing.rubric_digest(unit["entry"])
    paper = {"image_path": source, "ocr_page_type": "page_1", "drawing_assessments": {"2": assessment}}
    return unit, paper


@pytest.mark.parametrize("change", ["reference", "page", "source", "transcript", "variant", "policy", "threshold"])
def test_stale_graphic_evidence_is_not_reused(reference, source, change):
    unit, paper = _bound(reference, source)
    assert drawing.bound_assessment(paper, unit)["decision"] == "correct"
    check = paper["drawing_assessments"]["2"]
    if change == "reference":
        reference["variants"][0]["strokes"][0]["points"][0][0] += 1
    elif change == "page":
        paper["ocr_page_type"] = "page_2"
    elif change == "source":
        check["source_sha256"] = "other"
    elif change == "transcript":
        unit["student_answer"] = "edited"
    elif change == "variant":
        check["checks"][0]["variant_index"] = 1
    elif change == "threshold":
        check["threshold"] = 95
    else:
        check["policy"] = "drawing-pixel-model-v4"
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"


def test_page_without_drawings_has_no_new_work(monkeypatch):
    monkeypatch.setattr(drawing, "image_content_hash", lambda _: pytest.fail("ordinary image hashed"))
    result = asyncio.run(drawing.assess_page(source_path="unused", page_type_id="page_1", questions=[],
                                           transcriptions=[], evidence_paths=(), diagnostics={}))
    assert result == {}


def test_changed_confirmed_reference_is_not_resigned(reference, source, monkeypatch):
    reference["variants"][0]["strokes"][0]["points"][0][0] += 10
    call = AsyncMock(side_effect=AssertionError("unconfirmed geometry must not call model"))
    monkeypatch.setattr(drawing, "assess_inputs", call)
    result = asyncio.run(drawing.assess_page(
        source_path=source, page_type_id="page_1",
        questions=[{"question_no": "2", "answer_kind": "drawing", "drawing_reference": reference}],
        transcriptions=[], evidence_paths=(), diagnostics={},
    ))
    assert result["2"]["decision"] == "needs_review"
    call.assert_not_awaited()
    from services.grading_contract import AnswerSpec
    with pytest.raises(ValueError, match="hash mismatch"):
        AnswerSpec.model_validate({"question_no": "2", "question_type": "solve", "answer_kind": "drawing",
                                   "drawing_reference": reference})


@pytest.mark.parametrize("bad_check", [None, "bad", [], {"variant_index": True}, {"variant_index": "0"}])
def test_malformed_drawing_check_is_local_review(reference, source, bad_check):
    unit, paper = _bound(reference, source)
    paper["drawing_assessments"]["2"]["checks"] = [bad_check]
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"


@pytest.mark.parametrize("probability,expected", [(89.99, "needs_review"), (90, "correct"), (92, "correct")])
def test_pure_drawing_bypasses_blank_transcription(reference, source, monkeypatch, probability, expected):
    from services.grader import grade_paper_unified
    monkeypatch.setattr("services.grader.judge_paper", AsyncMock(side_effect=AssertionError("text called")))
    _unit, paper = _bound(reference, source)
    paper["drawing_assessments"]["2"]["checks"][0]["p_correct"] = probability
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": ""}]
    answer_map = {"2": {"question_type": "solve", "answer_kind": "drawing", "standard_answer": "标准图",
                        "question_text": "画图", "drawing_reference": reference, "matched": True}}
    paper["drawing_assessments"]["2"]["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    result = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1))
    assert result["details"][0]["result"] == expected
    assert result["details"][0]["drawing_assessment"]["threshold"] == 90
    assert result["details"][0]["match_method"] == "drawing"


@pytest.mark.parametrize("text_result, drawing_result, expected", [
    ("correct", "correct", "correct"), ("wrong", "correct", "partial"),
    ("partial", "correct", "partial"), ("needs_review", "correct", "needs_review"),
    ("correct", "wrong", "needs_review"),
])
def test_mixed_never_passes_on_drawing_alone(reference, source, monkeypatch, text_result, drawing_result, expected):
    from services.grader import grade_paper_unified
    _unit, paper = _bound(reference, source, "240秒")
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": "240秒"}]
    paper["drawing_assessments"]["2"]["checks"][0].update(verdict(drawing_result, 95 if drawing_result == "correct" else 10))
    answer_map = {"2": {"question_type": "solve", "answer_kind": "mixed", "standard_answer": "240秒及标准图",
                        "question_text": "按图作答", "drawing_reference": reference, "matched": True}}
    paper["drawing_assessments"]["2"]["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    class Queue:
        async def submit(self, *, coro_factory, **_kwargs):
            return await coro_factory()
    call = AsyncMock(return_value={"2": {"result": text_result, "explanation": "文字结果"}})
    monkeypatch.setattr("services.grader.get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr("services.grader.get_grading_queue", lambda: Queue())
    monkeypatch.setattr("services.grader.judge_paper", call)
    result = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1))
    assert call.await_args.args[0][0]["scope_hint"] == "text_only"
    assert "drawing_reference" not in call.await_args.args[0][0]
    assert result["details"][0]["result"] == expected


def test_mixed_unreadable_text_requires_review_even_when_drawing_is_correct(reference, source, monkeypatch):
    from services.grader import grade_paper_unified
    _unit, paper = _bound(reference, source)
    paper["questions"] = [{"question_no": "2", "question_type": "solve",
                           "student_answer": "", "answer_state": "unreadable"}]
    paper["drawing_assessments"]["2"]["checks"][0].update(verdict("correct", 95))
    answer_map = {"2": {"question_type": "solve", "answer_kind": "mixed",
                         "standard_answer": "240秒及标准图", "question_text": "按图作答",
                         "drawing_reference": reference, "matched": True}}
    paper["drawing_assessments"]["2"]["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    monkeypatch.setattr("services.grader.judge_paper", AsyncMock(side_effect=AssertionError("unreadable text must not call grader")))
    result = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1))
    assert result["details"][0]["result"] == "needs_review"
    assert result["details"][0]["error_type"] == "drawing_mixed_review"


def test_contract_hash_uses_reference_not_model_probability():
    base = [{"question_no": "2", "drawing_reference_hash": "a", "drawing_policy": drawing.POLICY,
             "drawing_assessment": {"p_correct": 95}}]
    original = build_grading_contract_hash(base, "daily", 1)
    base[0]["drawing_assessment"]["p_correct"] = 96
    assert build_grading_contract_hash(base, "daily", 1) == original
    base[0]["drawing_policy"] = "drawing-pixel-model-v4"
    assert build_grading_contract_hash(base, "daily", 1) != original
    base[0]["drawing_policy"] = drawing.POLICY
    base[0]["drawing_reference_hash"] = "b"
    assert build_grading_contract_hash(base, "daily", 1) != original


def _set_pixel(assessment, status, score, index=0):
    row = assessment["pixel_gate"]["variants"][index]
    row.update(status=status, score=score)
    if status != "pass":
        assessment["checks"][index] = {
            "variant_index": index, "model_called": False, "p_correct": None,
            "result": "pixel_rejected" if status == "fail" else "needs_review", "reason": "像素检查",
        }


@pytest.mark.parametrize("status,score,expected", [("fail", 79.99, "wrong"),
    ("pass", 80, "correct"), ("unavailable", None, "needs_review")])
def test_pixel_rejection_is_direct_wrong_but_errors_are_not(reference, source, status, score, expected):
    unit, paper = _bound(reference, source)
    _set_pixel(paper["drawing_assessments"]["2"], status, score)
    assert drawing.bound_assessment(paper, unit)["decision"] == expected


@pytest.mark.parametrize("probability,expected", [(89.99, "needs_review"), (90, "correct"), (92, "correct")])
def test_dual_gate_keeps_pixel_80_with_model_90(reference, source, probability, expected):
    unit, paper = _bound(reference, source)
    raw = paper["drawing_assessments"]["2"]
    _set_pixel(raw, "pass", 80)
    raw["checks"][0]["p_correct"] = probability
    assert drawing.bound_assessment(paper, unit)["decision"] == expected
    _set_pixel(raw, "fail", 79.99)
    assert drawing.bound_assessment(paper, unit)["decision"] == "wrong"


@pytest.mark.parametrize("change", ["missing", "version", "source", "reference", "boolean", "nan", "contradiction"])
def test_stale_or_malformed_pixels_never_award_credit_or_zero(reference, source, change):
    unit, paper = _bound(reference, source)
    raw = paper["drawing_assessments"]["2"]
    _set_pixel(raw, "fail", 0)
    gate = raw["pixel_gate"]
    if change == "missing":
        raw.pop("pixel_gate")
    elif change in {"version", "source", "reference"}:
        gate[{"version": "version", "source": "source_sha256", "reference": "reference_hash"}[change]] = "old"
    else:
        gate["variants"][0]["score"] = {"boolean": False, "nan": float("nan"), "contradiction": 100}[change]
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"


def test_two_candidates_cannot_share_pixel_and_model_pass(reference, source):
    raw_reference = copy.deepcopy(reference)
    raw_reference["variants"].append({"strokes": [{"kind": "point", "point": [350, 400]}]})
    reference = normalize_reference(raw_reference, trusted_confirmed=True)
    unit, paper = _bound(reference, source)
    raw = paper["drawing_assessments"]["2"]
    raw["pixel_gate"]["variants"].append({"variant_index": 1, "status": "pass", "score": 100, "reason": "像素"})
    raw["checks"].append({**verdict("wrong", 0), "variant_index": 1, "model_called": True})
    _set_pixel(raw, "fail", 70, 0)
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"
    # A prior model saying A is correct cannot override A's pixel rejection.
    raw["checks"][0] = {**verdict(), "variant_index": 0, "model_called": True}
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"
    _set_pixel(raw, "fail", 70, 0)
    raw["checks"][1].update(verdict())
    raw["checks"].reverse()
    assert drawing.bound_assessment(paper, unit)["decision"] == "correct"


def test_only_computed_pixel_failures_skip_the_model(monkeypatch):
    call = AsyncMock(return_value={"content": json.dumps(verdict()), "model": "qwen3.8-flash",
                                  "parameters": {"reasoning_effort": "high"}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    gate = {"variants": [{"variant_index": i, "status": status, "reason": "像素"}
                          for i, status in enumerate(["fail", "pass", "unavailable"])]}
    result = asyncio.run(drawing.assess_inputs(
        {"overlays": ["a", "b", "c"], "student_image": "plain"}, pixel_gate=gate))
    assert call.await_count == 2
    contents = {tuple(part["image_url"]["url"] for part in args.args[0][0]["content"] if part["type"] == "image_url"):
                args.args[0][0]["content"][0]["text"] for args in call.await_args_list}
    # A pass keeps the single overlay; an unmeasurable shape also shows the
    # uncovered student crop so coloured ink is not read as the standard layer.
    assert set(contents) == {("b",), ("plain", "c")}
    assert contents[("plain", "c")].startswith(drawing.FALLBACK_PREFIX)
    assert [row["model_called"] for row in result] == [False, True, True]
    assert result[0]["result"] == "pixel_rejected"
    assert result[2]["pixel_fallback"] is True and "pixel_fallback" not in result[1]


@pytest.mark.parametrize("model,probability,expected", [
    ("correct", 95, "correct"), ("correct", 89.99, "needs_review"),
    ("wrong", 99, "needs_review"), ("needs_review", None, "needs_review")])
def test_unmeasurable_shape_falls_back_to_flash_overlay_without_automatic_zero(
        reference, source, model, probability, expected):
    unit, paper = _bound(reference, source)
    raw = paper["drawing_assessments"]["2"]
    raw["pixel_gate"]["variants"][0].update(status="unavailable", score=None)
    raw["checks"][0] = {**verdict(model, probability), "variant_index": 0, "model_called": True, "pixel_fallback": True}
    result = drawing.bound_assessment(paper, unit)
    assert result["decision"] == expected
    if expected == "correct":
        assert result["reason"].startswith("像素门无法测量该图形")


def test_teacher_move_requires_reconfirmation_and_fresh_evidence(reference, source):
    unit, paper = _bound(reference, source)
    edited = copy.deepcopy(reference)
    edited["variants"][0]["strokes"][0]["points"][0][0] += 2
    # The frontend drops confirmation on edit; server confirmation creates a new hash.
    edited = normalize_reference(edited, trusted_confirmed=False)
    assert not edited["teacher_confirmed"]
    unit["entry"]["drawing_reference"] = edited
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"
    edited = normalize_reference(edited, trusted_confirmed=True)
    assert edited["reference_hash"] != reference["reference_hash"]
    unit["entry"]["drawing_reference"] = edited
    assert drawing.bound_assessment(paper, unit)["decision"] == "needs_review"


def test_manual_reference_keeps_verified_binding_and_explanation(reference, source):
    reference = normalize_reference({**reference, "comparison_mode": "manual"}, trusted_confirmed=True)
    unit, paper = _bound(reference, source)
    raw = paper["drawing_assessments"]["2"]
    raw.pop("pixel_gate")
    raw["checks"] = []
    result = drawing.bound_assessment(paper, unit)
    assert result["decision"] == "needs_review"
    assert result["comparison_mode"] == "manual"
    assert result["reference_hash"] == reference["reference_hash"]
    assert "自选比例尺" in result["reason"]
    raw["source_sha256"] = "stale"
    assert "reference_hash" not in drawing.bound_assessment(paper, unit)


@pytest.mark.parametrize("kind", ["drawing", "mixed"])
def test_pixel_wrong_survives_grader_and_preserves_text_result(reference, source, monkeypatch, kind):
    from services.grader import grade_paper_unified
    _unit, paper = _bound(reference, source, "240秒")
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": "240秒"}]
    answer_map = {"2": {"question_type": "solve", "answer_kind": kind, "standard_answer": "240秒及标准图",
                        "question_text": "按图作答", "drawing_reference": reference, "matched": True}}
    raw = paper["drawing_assessments"]["2"]
    raw["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    _set_pixel(raw, "fail", 10)
    class Queue:
        async def submit(self, *, coro_factory, **_kwargs):
            return await coro_factory()
    call = AsyncMock(return_value={"2": {"result": "correct", "explanation": "文字正确"}})
    monkeypatch.setattr("services.grader.get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr("services.grader.get_grading_queue", lambda: Queue())
    monkeypatch.setattr("services.grader.judge_paper", call)
    result = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1))["details"][0]
    assert result["result"] == ("wrong" if kind == "drawing" else "partial")
    assert result["error_type"] == "drawing_pixel_mismatch"
    if kind == "mixed":
        assert result["text_assessment"]["result"] == "correct"
        assert "半对" in result["explanation"]
    else:
        call.assert_not_awaited()


@pytest.mark.parametrize("pixel_rejected, text_result, expected", [
    (True, "wrong", "wrong"), (True, "partial", "partial"), (True, "correct", "partial"),
    (False, "wrong", "partial"),
])
def test_mixed_one_sided_conflicts_fold_to_half_credit(reference, source, monkeypatch, pixel_rejected, text_result, expected):
    from services.grader import grade_paper_unified
    _unit, paper = _bound(reference, source, "240秒")
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": "240秒"}]
    raw = paper["drawing_assessments"]["2"]
    if pixel_rejected:
        _set_pixel(raw, "fail", 10)
    answer_map = {"2": {"question_type": "solve", "answer_kind": "mixed", "standard_answer": "240秒及标准图",
                        "question_text": "按图作答", "drawing_reference": reference, "matched": True}}
    raw["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    class Queue:
        async def submit(self, *, coro_factory, **_kwargs):
            return await coro_factory()
    call = AsyncMock(return_value={"2": {"result": text_result, "explanation": "文字结果", "error_type": ""}})
    monkeypatch.setattr("services.grader.get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr("services.grader.get_grading_queue", lambda: Queue())
    monkeypatch.setattr("services.grader.judge_paper", call)
    detail = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1))["details"][0]
    assert detail["drawing_assessment"]["decision"] == ("wrong" if pixel_rejected else "correct")
    assert detail["result"] == expected
    assert detail["text_assessment"]["result"] == text_result
    if expected == "wrong":
        assert detail["error_type"] == "drawing_pixel_mismatch"
    if expected == "partial":
        assert "半对" in detail["explanation"]


@pytest.mark.parametrize("pixel_rejected, text_result, expected_result, expected_points", [
    (True, "correct", "partial", 2.5), (False, "wrong", "partial", 2.5),
    (True, "wrong", "wrong", 0.0), (False, "correct", "correct", 5.0),
])
def test_mixed_fold_awards_half_credit_in_exam_mode(reference, source, monkeypatch, pixel_rejected, text_result, expected_result, expected_points):
    from services.grader import grade_paper_unified
    _unit, paper = _bound(reference, source, "240秒")
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": "240秒"}]
    raw = paper["drawing_assessments"]["2"]
    if pixel_rejected:
        _set_pixel(raw, "fail", 10)
    answer_map = {"2": {"question_type": "solve", "answer_kind": "mixed", "standard_answer": "240秒及标准图",
                        "question_text": "按图作答", "drawing_reference": reference, "matched": True,
                        "max_score": 5}}
    raw["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    class Queue:
        async def submit(self, *, coro_factory, **_kwargs):
            return await coro_factory()
    call = AsyncMock(return_value={"2": {"result": text_result, "explanation": "文字结果", "error_type": ""}})
    monkeypatch.setattr("services.grader.get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr("services.grader.get_grading_queue", lambda: Queue())
    monkeypatch.setattr("services.grader.judge_paper", call)
    result = asyncio.run(grade_paper_unified(paper, answer_map, "演示", 1, category="exam"))
    detail = result["details"][0]
    assert detail["result"] == expected_result
    assert detail["awarded_score"] == expected_points
    assert result["score"] == expected_points


@pytest.mark.parametrize("case,expected,model_calls", [
    ("standard", "correct", 1), ("empty", "wrong", 0),
    # No blank baseline: the gate cannot measure lines, so Flash (two images)
    # decides alone and may pass, but could never award an automatic zero.
    ("missing_segment", "wrong", 0), ("missing_baseline", "correct", 1),
])
def test_real_pixel_assess_page_chain(reference, tmp_path, monkeypatch, case, expected, model_calls):
    from PIL import ImageDraw
    path = tmp_path / "actual-student.png"
    page = Image.new("RGB", (1000, 820), "white")
    points = [(200, 246), (500, 410), (650, 410)]
    if case != "empty":
        ImageDraw.Draw(page).line(points[:2] if case == "missing_segment" else points, fill="black", width=5)
    page.save(path)
    question = {"question_no": "2", "answer_kind": "drawing", "drawing_reference": reference,
                "drawing_regions": [[100, 100, 900, 900]]}
    call = AsyncMock(return_value={"content": json.dumps(verdict()), "model": "qwen3.8-flash",
                                  "parameters": {"reasoning_effort": "high"}})
    monkeypatch.setattr("services.flash_grading.call_flash", call)
    result = asyncio.run(drawing.assess_page(source_path=str(path), page_type_id="page_1",
        questions=[question], transcriptions=[], evidence_paths=(str(path),), template_mode="blank",
        blank_template=None if case == "missing_baseline" else np.full((820, 1000, 3), 255, np.uint8),
        diagnostics={"template_width": 1000, "template_height": 820, "answer_source_complete": True,
                     "evidence_columns": [{"crop_left_px": 0, "crop_right_px": 1000}]}))["2"]
    assert result["decision"] == expected
    assert call.await_count == model_calls
    assert result["pixel_gate"]["source_sha256"] == image_content_hash(path)
    assert result["pixel_gate"]["reference_hash"] == reference["reference_hash"]
    if case == "missing_baseline":
        assert result["pixel_gate"]["variants"][0]["score"] is None
        images = [part for part in call.await_args.args[0][0]["content"] if part["type"] == "image_url"]
        assert len(images) == 2 and result["checks"][0]["pixel_fallback"] is True
    paper = {"image_path": str(path), "page_type_id": "page_1", "drawing_assessments": {"2": result}}
    unit = {"parent_question_no": "2", "student_answer": "", "entry": question}
    assert drawing.bound_assessment(paper, unit)["decision"] == expected


def test_pixel_merge_does_not_poison_real_text_queue_cache(reference, source, monkeypatch):
    from services.grader import grade_paper_unified
    from services.task_queue import TaskQueue
    _unit, paper = _bound(reference, source, "240秒")
    paper["questions"] = [{"question_no": "2", "question_type": "solve", "student_answer": "240秒"}]
    answer_map = {"2": {"question_type": "solve", "answer_kind": "mixed", "standard_answer": "240秒及标准图",
                        "question_text": "按图作答", "drawing_reference": reference, "matched": True}}
    raw = paper["drawing_assessments"]["2"]
    raw["rubric_sha256"] = drawing.rubric_digest(answer_map["2"])
    original_check = copy.deepcopy(raw["checks"][0])
    queue = TaskQueue(max_concurrent=1, per_teacher_limit=1)
    text = {"2": {"result": "correct", "explanation": "文字正确", "error_type": ""}}
    call = AsyncMock(return_value=text)
    monkeypatch.setattr("services.grader.get_grading_provider", lambda: "qwen3.8-flash")
    monkeypatch.setattr("services.grader.get_grading_queue", lambda: queue)
    monkeypatch.setattr("services.grader.judge_paper", call)
    async def run():
        _set_pixel(raw, "fail", 0)
        first = await grade_paper_unified(paper, answer_map, "演示", 1)
        _set_pixel(raw, "pass", 100)
        raw["checks"][0] = original_check
        second = await grade_paper_unified(paper, answer_map, "演示", 1)
        return first, second
    first, second = asyncio.run(run())
    assert first["details"][0]["result"] == "partial"
    assert second["details"][0]["result"] == "correct"
    assert second["details"][0]["text_assessment"]["result"] == "correct"
    assert call.await_count == 1, "unchanged text should reuse its valid cached result"
    assert text == {"2": {"result": "correct", "explanation": "文字正确", "error_type": ""}}
