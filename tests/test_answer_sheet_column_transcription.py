"""Slice 5: column-split transcription contract for answer sheets.

Every page image is synthetic and generated in-test (white page, printed box
frames, bubble cells, dark "handwriting" strokes).  The model is replaced by a
recording fake transport that returns fixture JSON: these tests check the
request builder, strict parser, merger, verification stage, evidence and
gates -- never a real transcription.
"""
import ast
import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import services.answer_sheet_column_transcription as contract
from services.answer_sheet_column_transcription import (
    MAX_IMAGES_PER_REQUEST,
    ColumnTranscriptionError,
    TranscriptionEvidence,
    apply_verification,
    build_page_column_plan,
    build_transcription_request,
    build_verification_requests,
    masked_page,
    merge_page_transcription,
    page_transcription_gate,
    parse_transcription_response,
    parse_verification_response,
    read_evidence_file,
    render_column_images,
    render_verification_images,
    transcribe_page_columns,
)
from services.answer_sheet_layout_contract import resolve_bubble_grid_pixels
from services.template_anchor_crops import build_anchor_crop_plan

WIDTH, HEIGHT = 900, 1200
PAGE = "page_1"
ARTIFACT_SHA = hashlib.sha256(b"synthetic answer-sheet page").hexdigest()
THREE_COLUMNS = [(0, 333), (333, 667), (667, 1000)]
Q1_BUBBLES = [40, 100, 300, 140]


def _region(region_id, question_no, bbox, kind, order, sub="", **extra):
    return {
        "region_id": region_id, "page_id": PAGE, "question_no": question_no,
        "subquestion_no": sub, "bbox": list(bbox), "region_type": kind,
        "reading_order": order, "required": True, "teacher_confirmed": True,
        "choice_options": ["A", "B", "C", "D"], "allow_multiple": False, **extra,
    }


def _regions():
    """Q1 bubbles, Q3 fill, Q4(1) solve and Q4(2) solve continuing from the
    tail of column 1 to the head of column 2, Q5 fill, Q6 handwritten choice,
    Q7 solve in column 3."""
    return [
        _region("answer_1", "1", Q1_BUBBLES, "choice_mark", 1),
        _region("answer_3", "3", [40, 200, 300, 260], "fill", 2),
        _region("answer_4a", "4", [40, 300, 300, 700], "solve", 3, sub="1"),
        _region("answer_4b", "4", [40, 720, 300, 990], "solve", 4, sub="2"),
        _region("answer_4c", "4", [370, 20, 630, 400], "solve", 5, sub="2"),
        _region("answer_5", "5", [370, 450, 630, 520], "fill", 6),
        _region("answer_6", "6", [370, 560, 630, 620], "choice_handwritten", 7),
        _region("answer_7", "7", [700, 40, 960, 900], "solve", 8),
    ]


def _bubble_rows(bbox=Q1_BUBBLES, *, question_no="1", region_id="answer_1", size=(WIDTH, HEIGHT)):
    x1, y1, x2, y2 = bbox
    width = (x2 - x1) // 4
    row = {
        "question_no": question_no, "subquestion_no": "", "page_id": PAGE,
        "region_id": region_id, "allow_multiple": False, "grid_order": "left_to_right",
        "grid_bbox": [x1, y1, x1 + 4 * width, y2],
        "cells": [
            {"option": option, "bbox": [x1 + i * width, y1, x1 + (i + 1) * width, y2],
             "cell_size": [width, y2 - y1]}
            for i, option in enumerate("ABCD")
        ],
        "teacher_confirmed": True,
    }
    return resolve_bubble_grid_pixels([row], {PAGE: size})


def _artifact(columns=THREE_COLUMNS):
    return {
        "sha256": ARTIFACT_SHA, "width": WIDTH, "height": HEIGHT,
        "question_crop_plan": build_anchor_crop_plan(
            {"column_layout": {
                "column_count": len(columns),
                "columns": [
                    {"column_index": index, "x1_1000": left, "x2_1000": right}
                    for index, (left, right) in enumerate(columns, 1)
                ],
            }},
            artifact_sha256=ARTIFACT_SHA,
        ),
    }


def _plan(regions=None, *, columns=THREE_COLUMNS, rows=None):
    return build_page_column_plan(
        page_id=PAGE,
        artifact=_artifact(columns),
        answer_regions=_regions() if regions is None else regions,
        bubble_rows=_bubble_rows() if rows is None else rows,
    )


def _px(bbox):
    from services.answer_sheet_layout_contract import normalized_bbox_to_pixels
    return normalized_bbox_to_pixels(bbox, WIDTH, HEIGHT)


def _page(regions=None, rows=None, *, filled="C"):
    """White page, printed frames, bubble outlines, one filled bubble and a
    dark stroke inside every text box."""
    image = np.full((HEIGHT, WIDTH, 3), 255, np.uint8)
    for region in _regions() if regions is None else regions:
        x1, y1, x2, y2 = _px(region["bbox"])
        image[[y1, y2 - 1], x1:x2] = 150
        image[y1:y2, [x1, x2 - 1]] = 150
        if region["region_type"] != "choice_mark":
            mid = (y1 + y2) // 2
            image[mid - 2:mid + 2, x1 + 10:x2 - 10] = 30
    for row in (_bubble_rows() if rows is None else rows).values():
        for cell in row["cells"]:
            x1, y1, x2, y2 = cell["pixel_box"]
            image[[y1, y2 - 1], x1:x2] = 120
            image[y1:y2, [x1, x2 - 1]] = 120
            if cell["option"] == filled:
                image[y1 + 2:y2 - 2, x1 + 2:x2 - 2] = 40
    return image


def _mask(plan):
    mask = np.zeros((HEIGHT, WIDTH), bool)
    for x1, y1, x2, y2 in plan["mask_boxes"]:
        mask[y1:y2, x1:x2] = True
    return mask


def _reading(entry_id, text="", state="visible", confidence=0.97, *, key="entry_id"):
    return {key: entry_id, "student_answer": text, "answer_state": state, "confidence": confidence}


PRIMARY = {
    "c1-01": ("1/2", "visible", 0.97),          # Q3 fill
    "c1-02": ("x=2", "visible", 0.95),          # Q4(1) solve
    "c1-03": ("2x+1=5", "visible", 0.96),       # Q4(2) solve, column 1 part
    "c2-01": ("x=2", "visible", 0.93),          # Q4(2) solve, column 2 part
    "c2-02": ("", "blank", 0.99),               # Q5 fill
    "c2-03": ("C", "visible", 0.99),            # Q6 handwritten choice
    "c3-01": ("y=3x", "visible", 0.98),         # Q7 solve
}


def _primary(**overrides):
    values = {**PRIMARY, **overrides}
    return json.dumps({"entries": [_reading(key, *values[key]) for key in PRIMARY]}, ensure_ascii=False)


def _checks(*readings):
    return json.dumps({"checks": [_reading(*item, key="check_id") for item in readings]}, ensure_ascii=False)


class FakeVision:
    """Records each request (paths, prompt, decoded images) and replays fixtures."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, paths, prompt, *, max_tokens):
        images = []
        for path in paths:
            with Image.open(path) as source:
                images.append(np.asarray(source.convert("RGB")))
        self.calls.append({"paths": list(paths), "prompt": prompt, "max_tokens": max_tokens, "images": images})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _run(image, plan, evidence, transport):
    return asyncio.run(transcribe_page_columns(image, plan, evidence, transport))


def _fields(plan, raw=None):
    request = build_transcription_request(plan)
    return merge_page_transcription(plan, request, parse_transcription_response(raw or _primary(), request))


# --- plan: columns, order, continuity ---------------------------------------------

def test_plan_orders_entries_column_major_and_joins_a_cross_column_question():
    plan = _plan()
    assert [column["bbox_1000"] for column in plan["columns"]] == [
        [0, 0, 333, 1000], [333, 0, 667, 1000], [667, 0, 1000, 1000],
    ]
    assert [(entry["entry_id"], entry["answer_key"]) for entry in plan["entries"]] == [
        ("c1-01", "3"), ("c1-02", "4(1)"), ("c1-03", "4(2)"),
        ("c2-01", "4(2)"), ("c2-02", "5"), ("c2-03", "6"), ("c3-01", "7"),
    ]
    fields = {field["answer_key"]: field for field in plan["fields"]}
    assert list(fields) == ["1", "3", "4(1)", "4(2)", "5", "6", "7"]
    assert fields["4(2)"]["columns"] == [1, 2]
    assert fields["4(2)"]["entry_ids"] == ["c1-03", "c2-01"]
    assert fields["1"]["source"] == "bubble_pixel_recognizer"
    assert fields["1"]["answer_state_ref"]["answer_key"] == "1"
    assert "entry_ids" not in fields["1"]
    assert fields["6"]["choice_options"] == ["A", "B", "C", "D"]
    # Boxes are given inside their own column image.
    q5 = next(entry for entry in plan["entries"] if entry["answer_key"] == "5")
    assert q5["regions"][0]["column_box_1000"] == [111, 450, 889, 520]


def test_every_known_bubble_cell_is_in_the_mask_and_nothing_else():
    plan = _plan()
    cells = [cell["pixel_box"] for cell in _bubble_rows()["1"]["cells"]]
    assert plan["mask_boxes"] == sorted(cells)


def _moved(index, **changes):
    regions = _regions()
    regions[index] = {**regions[index], **changes}
    return regions


@pytest.mark.parametrize("regions, rows, message", [
    (_moved(1, bbox=[300, 200, 400, 260]), None, "跨越分栏边界"),
    # Q4(2) continues from column 1 straight into column 3.
    (_moved(4, bbox=[700, 920, 960, 990], reading_order=9), None, "跨栏不连续"),
    # Q4(2) part in column 2 is no longer at the head of the column.
    (_moved(4, reading_order=7, bbox=[370, 630, 630, 700]), None, "跨栏不连续"),
    # Q4(1) split around Q4(2) inside column 1.
    (_regions() + [_region("answer_4d", "4", [40, 995, 300, 999], "solve", 4, sub="1")],
     None, "被其他题隔开"),
    # Q5 printed below Q6 in reading order.
    (_moved(5, reading_order=9, bbox=[370, 700, 630, 760]), None, "题号顺序"),
    (_moved(4, region_type="fill"), None, "题型不一致"),
    (_regions(), {}, "没有已确认的涂卡网格"),
    (_moved(1, bbox=[40, 130, 300, 260]), None, "与涂卡格重叠"),
    (_moved(1, region_type="identity"), None, "类型无效"),
])
def test_plan_fails_closed_on_broken_layout(regions, rows, message):
    with pytest.raises(ValueError, match=message):
        _plan(regions, rows=rows)


def test_plan_rejects_foreign_or_tampered_bubble_geometry():
    rows = _bubble_rows()
    rows["1"]["cells"][0]["pixel_box"] = [0, 0, 1, 1]
    with pytest.raises(ValueError, match="像素框与模板几何不一致"):
        _plan(rows=rows)
    with pytest.raises(ValueError, match="页面尺寸"):
        _plan(rows=_bubble_rows(size=(WIDTH + 1, HEIGHT)))
    with pytest.raises(ValueError, match="不属于本页"):
        _plan(rows=_bubble_rows(region_id="answer_3"))


def test_plan_needs_a_valid_frozen_column_plan():
    artifact = _artifact()
    artifact["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="分栏计划无效"):
        build_page_column_plan(page_id=PAGE, artifact=artifact, answer_regions=_regions(),
                               bubble_rows=_bubble_rows())
    plan = _plan()
    tampered = {**plan, "mask_boxes": []}
    with pytest.raises(ValueError, match="已被改动"):
        build_transcription_request(tampered)


# --- request: one call per page carries all N columns ---------------------------------

@pytest.mark.parametrize("columns", [
    [(0, 1000)],
    [(0, 500), (500, 1000)],
    THREE_COLUMNS,
])
def test_one_request_carries_every_column_image_of_the_page(columns):
    regions = [
        _region(f"answer_{index}", str(index + 1), [left + 5, 100, right - 5, 300], "solve", index + 1)
        for index, (left, right) in enumerate(columns)
    ]
    plan = _plan(regions, columns=columns, rows={})
    request = build_transcription_request(plan)
    assert request["image_count"] == len(columns) == len(render_column_images(_page(regions, {}), plan))
    assert [image["column"] for image in request["images"]] == list(range(1, len(columns) + 1))
    assert request["entry_ids"] == [f"c{index}-01" for index in range(1, len(columns) + 1)]


def test_prompt_carries_the_exclusion_contract_and_no_grading_material():
    request = build_transcription_request(_plan())
    prompt = request["prompt"]
    for clause in ("只抄写作答框内学生本人手写的内容", "印刷内容", "涂卡格", "教师批注",
                   "考生身份信息", "[无法识别]", "不补全", "不推断", "只转录，不解题、不判分"):
        assert clause in prompt
    for forbidden in ("标准答案", "参考答案", "评分", "得分", "判错", "正确答案"):
        assert forbidden not in prompt
    assert "共 3 张图片" in prompt
    assert json.dumps(request["entry_ids"], separators=(",", ":")) in prompt


def test_a_page_of_bubbles_only_needs_no_transcription_call(tmp_path):
    regions = [_regions()[0]]
    plan = _plan(regions)
    assert build_transcription_request(plan) is None
    transport = FakeVision()
    record = _run(_page(regions), plan, TranscriptionEvidence(tmp_path, "paper1", PAGE), transport)
    assert transport.calls == []
    assert record["status"] == "no_transcription_required"
    assert [field["source"] for field in record["fields"]] == ["bubble_pixel_recognizer"]
    assert page_transcription_gate(record)["ready_for_grading"] is True


# --- images: deterministic bubble mask, no differencing ---------------------------------

def test_column_images_whiten_bubble_cells_and_keep_every_other_pixel():
    plan = _plan()
    page = _page()
    mask = _mask(plan)
    assert (page[mask] < 255).any()  # the filled bubble exists on the page
    images = render_column_images(page, plan)
    for column, image in zip(plan["columns"], images):
        x1, y1, x2, y2 = column["pixel_box"]
        assert image.shape == (y2 - y1, x2 - x1, 3)
        column_mask = mask[y1:y2, x1:x2]
        assert (image[column_mask] == 255).all()
        np.testing.assert_array_equal(image[~column_mask], page[y1:y2, x1:x2][~column_mask])
    # No filled bubble pixel survives anywhere in what the model will see.
    for cell in _bubble_rows()["1"]["cells"]:
        x1, y1, x2, y2 = cell["pixel_box"]
        assert (images[0][y1:y2, x1:x2] == 255).all()
    # The input page is never modified.
    np.testing.assert_array_equal(page, _page())


def test_images_must_be_registered_to_the_template_frame():
    plan = _plan()
    with pytest.raises(ValueError, match="配准"):
        render_column_images(np.full((HEIGHT, WIDTH + 2, 3), 255, np.uint8), plan)
    with pytest.raises(ValueError, match="RGB"):
        render_column_images(np.full((HEIGHT, WIDTH), 255, np.uint8), plan)


# --- strict response parsing ------------------------------------------------------------

def test_valid_response_is_read_exactly_including_fences_and_latex():
    request = build_transcription_request(_plan())
    raw = _primary(**{"c3-01": ("\\frac{1}{2}+\\beta", "visible", 1)})
    raw = raw.replace("\\\\frac", "\\frac").replace("\\\\beta", "\\beta")  # single-backslash LaTeX
    readings = parse_transcription_response("```json\n" + raw + "\n```", request)
    assert [reading["id"] for reading in readings] == request["entry_ids"]
    assert readings[-1]["student_answer"] == "\\frac{1}{2}+\\beta"
    assert readings[-1]["confidence"] == 1.0
    multiline = _primary(**{"c1-02": ("2x=4\nx=2", "visible", 0.95)}).replace("\\n", "\n")
    assert parse_transcription_response(multiline, request)[1]["student_answer"] == "2x=4\nx=2"


def _entries_raw(mutate):
    payload = json.loads(_primary())
    mutate(payload)
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("raw, reason", [
    (None, "response_not_text"),
    ("以下是转录结果：" + _primary(), "response_not_json"),
    (_primary() + "\n备注：第5题空白", "response_not_json"),
    ("[]", "response_not_object"),
    (_entries_raw(lambda p: p.update(page="1")), "response_shape_invalid"),
    (_entries_raw(lambda p: p["entries"].pop()), "entry_count_mismatch"),
    (_entries_raw(lambda p: p["entries"].append(_reading("c3-02", "x"))), "entry_count_mismatch"),
    (_entries_raw(lambda p: p["entries"].reverse()), "entry_order_mismatch"),
    (_entries_raw(lambda p: p["entries"].__setitem__(1, _reading("c1-01", "x"))), "entry_order_mismatch"),
    (_entries_raw(lambda p: p["entries"][0].update(score=5)), "entry_fields_invalid"),
    (_entries_raw(lambda p: p["entries"][0].pop("confidence")), "entry_fields_invalid"),
    (_entries_raw(lambda p: p["entries"][0].update(answer_state="partial")), "entry_value_invalid"),
    (_entries_raw(lambda p: p["entries"][0].update(student_answer=None)), "entry_value_invalid"),
    (_entries_raw(lambda p: p["entries"][0].update(confidence=1.5)), "confidence_invalid"),
    (_entries_raw(lambda p: p["entries"][0].update(confidence=True)), "confidence_invalid"),
    (_entries_raw(lambda p: p["entries"][0].update(confidence="0.9")), "confidence_invalid"),
    (_primary().replace('"confidence": 0.97', '"confidence": NaN', 1), "confidence_invalid"),
    (_entries_raw(lambda p: p["entries"][4].update(student_answer="x")), "state_text_conflict"),
    (_entries_raw(lambda p: p["entries"][0].update(answer_state="unreadable")), "state_text_conflict"),
    (_entries_raw(lambda p: p["entries"][0].update(student_answer="  ")), "state_text_conflict"),
    (_entries_raw(lambda p: p["entries"][0].update(student_answer="[无法识别]")), "state_text_conflict"),
    ('{"entries": [], "entries": []}', "duplicate_json_key"),
])
def test_malformed_responses_fail_closed_with_a_reason(raw, reason):
    request = build_transcription_request(_plan())
    with pytest.raises(ColumnTranscriptionError) as caught:
        parse_transcription_response(raw, request)
    assert caught.value.reason_code == reason


# --- merge: per question number + small question -------------------------------------------

def test_merge_joins_fragments_in_column_order_and_derives_review_and_risk():
    plan = _plan()
    fields = {field["answer_key"]: field for field in _fields(plan)}
    assert list(fields) == ["1", "3", "4(1)", "4(2)", "5", "6", "7"]
    joined = fields["4(2)"]
    assert (joined["state"], joined["student_answer"]) == ("visible", "2x+1=5\nx=2")
    assert [fragment["column"] for fragment in joined["fragments"]] == [1, 2]
    assert joined["confidence"] == 0.93
    assert fields["1"] == {
        key: plan["fields"][0][key] for key in (
            "answer_key", "question_no", "subquestion_no", "answer_kind",
            "source", "columns", "region_ids", "answer_state_ref",
        )
    }
    assert fields["6"]["choice_letters"] == "C"
    assert fields["5"]["review_reasons"] == ["blank_requires_teacher_confirmation"]
    assert fields["5"]["verification"] == {"status": "not_required"}
    risk = {key: field["high_risk_reasons"] for key, field in fields.items() if key != "1"}
    assert risk == {
        "3": ["decisive_short_answer"],
        "4(1)": [],
        "4(2)": ["cross_column_join"],
        "5": [],
        "6": ["decisive_short_answer"],
        "7": [],
    }
    assert [key for key, field in fields.items() if field.get("verification", {}).get("status") == "pending"] == [
        "3", "4(2)", "6",
    ]


@pytest.mark.parametrize("overrides, state, text, reasons", [
    ({"c1-03": ("", "blank", 0.99), "c2-01": ("", "blank", 0.99)}, "blank", "",
     ["blank_requires_teacher_confirmation"]),
    ({"c1-03": ("", "blank", 0.99)}, "visible", "x=2", []),
    ({"c2-01": ("[无法识别]", "unreadable", 0.2)}, "unreadable", "[无法识别]",
     ["unreadable_requires_teacher_confirmation"]),
    ({"c2-01": ("x=[无法识别]", "visible", 0.6)}, "visible", "2x+1=5\nx=[无法识别]", ["partially_unreadable"]),
])
def test_cross_column_fragments_merge_conservatively(overrides, state, text, reasons):
    field = next(f for f in _fields(_plan(), _primary(**overrides)) if f["answer_key"] == "4(2)")
    assert (field["state"], field["student_answer"], field["review_reasons"]) == (state, text, reasons)
    assert len(field["fragments"]) == 2


def test_low_confidence_and_invalid_choice_are_flagged():
    fields = {f["answer_key"]: f for f in _fields(_plan(), _primary(**{
        "c3-01": ("y=3x", "visible", 0.89), "c2-03": ("CD", "visible", 0.99),
    }))}
    assert fields["7"]["high_risk_reasons"] == ["low_confidence"]
    assert fields["6"]["review_reasons"] == ["choice_transcription_invalid"]
    assert "choice_letters" not in fields["6"]
    assert fields["6"]["verification"] == {"status": "not_required"}


def test_merge_refuses_readings_from_another_plan():
    plan = _plan()
    request = build_transcription_request(plan)
    readings = parse_transcription_response(_primary(), request)
    other = _plan(_moved(7, bbox=[700, 40, 960, 910]))
    with pytest.raises(ValueError, match="不一致"):
        merge_page_transcription(other, request, readings)
    with pytest.raises(ValueError, match="不一致"):
        merge_page_transcription(plan, request, readings[:-1])


# --- verification stage ------------------------------------------------------------------------

def test_verification_rereads_high_risk_boxes_blind_from_the_masked_page():
    plan = _plan()
    fields = _fields(plan)
    [request] = build_verification_requests(plan, fields)
    assert [(check["check_id"], check["answer_key"], check["image_indexes"]) for check in request["checks"]] == [
        ("v01", "3", [1]), ("v02", "4(2)", [2, 3]), ("v03", "6", [4]),
    ]
    assert request["image_count"] == 4
    prompt = request["prompt"]
    for first_reading in ("1/2", "2x+1=5", "x=2"):
        assert first_reading not in prompt
    for clause in ("印刷内容", "涂卡格", "教师批注", "[无法识别]"):
        assert clause in prompt
    page = _page()
    crops = render_verification_images(page, plan, request)
    masked = masked_page(page, plan)
    for crop, image in zip(crops, request["images"]):
        x1, y1, x2, y2 = image["pixel_box"]
        np.testing.assert_array_equal(crop, masked[y1:y2, x1:x2])
    assert [image["region_id"] for image in request["images"]] == [
        "answer_3", "answer_4b", "answer_4c", "answer_6",
    ]


def test_verification_requests_never_exceed_the_image_limit_or_split_a_field():
    regions = [
        _region(f"answer_{n}", str(n), [10, 30 + n * 60, 320, 70 + n * 60], "fill", n)
        for n in range(1, 15)
    ]
    plan = _plan(regions, rows={})
    fields = _fields(plan, json.dumps({"entries": [
        _reading(entry["entry_id"], str(index)) for index, entry in enumerate(plan["entries"])
    ]}))
    requests = build_verification_requests(plan, fields)
    assert [request["image_count"] for request in requests] == [MAX_IMAGES_PER_REQUEST, 2]
    assert [check["check_id"] for request in requests for check in request["checks"]] == [
        f"v{n:02d}" for n in range(1, 15)
    ]


def test_agreement_confirms_and_disagreement_goes_to_the_teacher_with_both_readings():
    plan = _plan()
    fields = _fields(plan)
    [request] = build_verification_requests(plan, fields)
    readings = parse_verification_response(_checks(
        ("v01", "1 / 2"), ("v02", "2x+1=5 x=2", "visible", 0.9), ("v03", "B"),
    ), request)
    settled = {f["answer_key"]: f for f in apply_verification(fields, request, readings)}
    assert settled["3"]["verification"]["status"] == "confirmed"
    assert settled["4(2)"]["verification"]["status"] == "confirmed"
    disputed = settled["6"]
    assert disputed["verification"]["status"] == "disputed"
    assert disputed["verification"]["mismatch"] == "text_mismatch"
    assert disputed["verification"]["second_reading"]["student_answer"] == "B"
    assert (disputed["student_answer"], disputed["choice_letters"]) == ("C", "C")  # first reading kept
    assert disputed["review_reasons"] == ["verification_disputed"]
    assert settled["3"]["review_reasons"] == []
    # Inputs are not mutated.
    assert all(f.get("verification", {}).get("status") != "confirmed" for f in fields)


def test_state_disagreement_and_failed_checks_are_never_passed():
    plan = _plan()
    fields = _fields(plan)
    [request] = build_verification_requests(plan, fields)
    blank_second = parse_verification_response(_checks(
        ("v01", "", "blank"), ("v02", "2x+1=5x=2"), ("v03", "C"),
    ), request)
    settled = {f["answer_key"]: f for f in apply_verification(fields, request, blank_second)}
    assert settled["3"]["verification"]["mismatch"] == "state_mismatch"
    assert settled["3"]["student_answer"] == "1/2"
    failed = {f["answer_key"]: f for f in apply_verification(fields, request, None, failure_reason="call_failed")}
    assert {failed[key]["verification"]["status"] for key in ("3", "4(2)", "6")} == {"failed"}
    assert failed["3"]["review_reasons"] == ["verification_failed"]
    with pytest.raises(ValueError, match="没有待核对"):
        apply_verification(apply_verification(fields, request, None), request, None)
    with pytest.raises(ColumnTranscriptionError):
        parse_verification_response(_checks(("v01", "1/2"), ("v03", "C"), ("v02", "x")), request)


# --- gates --------------------------------------------------------------------------------------

def test_pending_verification_blocks_grading_until_settled():
    plan = _plan()
    fields = _fields(plan)
    record = {"status": "transcribed", "page_id": PAGE, "plan_sha256": plan["plan_sha256"], "fields": fields}
    gate = page_transcription_gate(record)
    assert gate["ready_for_grading"] is False
    assert gate["blocking_reasons"] == ["verification_incomplete"]
    assert gate["pending_verification"] == ["3", "4(2)", "6"]
    [request] = build_verification_requests(plan, fields)
    record["fields"] = apply_verification(fields, request, parse_verification_response(
        _checks(("v01", "1/2"), ("v02", "2x+1=5 x=2"), ("v03", "B")), request))
    gate = page_transcription_gate(record)
    assert gate["ready_for_grading"] is True
    assert gate["teacher_review_keys"] == ["5", "6"]
    assert page_transcription_gate({"status": "failed", "fields": []})["blocking_reasons"] == [
        "transcription_failed",
    ]


# --- orchestration and evidence ------------------------------------------------------------------

def test_page_transcription_end_to_end_keeps_all_evidence(tmp_path):
    plan = _plan()
    page = _page()
    transport = FakeVision(
        _primary(),
        _checks(("v01", "1/2"), ("v02", "2x+1=5 x=2"), ("v03", "B")),
    )
    evidence = TranscriptionEvidence(tmp_path, "paper1", PAGE, "attempt1")
    record = _run(page, plan, evidence, transport)

    # Exactly one primary call with the three masked column images, then one blind check.
    assert len(transport.calls) == 2
    primary, check = transport.calls
    assert len(primary["images"]) == 3
    for sent, expected in zip(primary["images"], render_column_images(page, plan)):
        np.testing.assert_array_equal(sent, expected)
    assert len(check["images"]) == 4 and "1/2" not in check["prompt"]

    assert record["status"] == "transcribed"
    assert page_transcription_gate(record) == {
        "ready_for_grading": True, "blocking_reasons": [], "pending_verification": [],
        "teacher_review_keys": ["5", "6"],
    }
    fields = {f["answer_key"]: f for f in record["fields"]}
    assert fields["6"]["verification"]["status"] == "disputed"

    names = [item["file"] for item in record["evidence"]["files"]]
    assert names == [
        "column-01.png", "column-02.png", "column-03.png", "request-primary.json",
        "response-primary.txt", "verify-01-01.png", "verify-01-02.png", "verify-01-03.png",
        "verify-01-04.png", "request-verify-01.json", "response-verify-01.txt",
    ]
    assert record["evidence"]["directory"] == "transcription_evidence/paper1/page_1/attempt1"
    assert read_evidence_file(tmp_path, record, "response-primary.txt").decode("utf-8") == _primary()
    stored = json.loads((evidence.root / "record.json").read_text(encoding="utf-8"))
    assert stored == record
    serialized = json.dumps(record, ensure_ascii=False)
    assert str(tmp_path) not in serialized and str(tmp_path).replace("\\", "/") not in serialized
    for identity_key in ("student_id", "student_name", "identity"):
        assert identity_key not in serialized
    with Image.open(evidence.path("column-01.png")) as sent_png:
        np.testing.assert_array_equal(np.asarray(sent_png.convert("RGB")), render_column_images(page, plan)[0])


def test_evidence_is_write_once_and_integrity_checked(tmp_path):
    plan = _plan()
    evidence = TranscriptionEvidence(tmp_path, "paper1", PAGE, "attempt1")
    record = _run(_page(), plan, evidence, FakeVision(
        _primary(), _checks(("v01", "1/2"), ("v02", "2x+1=5 x=2"), ("v03", "C"))))
    with pytest.raises(FileExistsError):
        evidence.write_text("response-primary.txt", "x", kind="primary_response")
    for name in ("../meta.json", "record.json", "column-01.png/../../x"):
        with pytest.raises(ValueError):
            read_evidence_file(tmp_path, record, name)
    escaped = {**record, "evidence": {**record["evidence"], "directory": "../../outside/a/b"}}
    with pytest.raises(ValueError, match="路径无效"):
        read_evidence_file(tmp_path, escaped, "column-01.png")
    evidence.path("column-02.png").write_bytes(b"not the image that was sent")
    with pytest.raises(ValueError, match="完整性"):
        read_evidence_file(tmp_path, record, "column-02.png")
    evidence.path("column-03.png").unlink()
    with pytest.raises(ValueError, match="清理"):
        read_evidence_file(tmp_path, record, "column-03.png")
    for segment in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            TranscriptionEvidence(tmp_path, segment, PAGE)


@pytest.mark.parametrize("response, reason, has_response_file", [
    (TimeoutError("vision timed out"), "call_failed", False),
    ("我无法完成转录", "response_not_json", True),
    (json.dumps({"entries": []}), "entry_count_mismatch", True),
])
def test_failed_primary_transcription_produces_no_fields_only_evidence(tmp_path, response, reason, has_response_file):
    plan = _plan()
    transport = FakeVision(response)
    record = _run(_page(), plan, TranscriptionEvidence(tmp_path, "paper1", PAGE), transport)
    assert len(transport.calls) == 1
    assert record["status"] == "failed"
    assert record["fields"] == []
    assert record["failure"]["stage"] == "primary"
    assert record["failure"]["reason_code"] == reason
    names = [item["file"] for item in record["evidence"]["files"]]
    assert "request-primary.json" in names
    assert ("response-primary.txt" in names) is has_response_file
    assert page_transcription_gate(record)["ready_for_grading"] is False


@pytest.mark.parametrize("second", [RuntimeError("down"), "not json", _checks(("v01", "1/2"))])
def test_failed_verification_sends_high_risk_fields_to_the_teacher(tmp_path, second):
    record = _run(_page(), _plan(), TranscriptionEvidence(tmp_path, "paper1", PAGE), FakeVision(_primary(), second))
    fields = {f["answer_key"]: f for f in record["fields"]}
    assert record["status"] == "transcribed"
    for key in ("3", "4(2)", "6"):
        assert fields[key]["verification"]["status"] == "failed"
        assert "verification_failed" in fields[key]["review_reasons"]
    assert fields["3"]["student_answer"] == "1/2"
    gate = page_transcription_gate(record)
    assert gate["ready_for_grading"] is True
    assert set(gate["teacher_review_keys"]) == {"3", "4(2)", "5", "6"}


def test_blank_sheet_transcribes_blank_without_invention_or_verification_calls(tmp_path):
    plan = _plan()
    blank_page = _page(filled="")
    transport = FakeVision(json.dumps({"entries": [
        _reading(entry_id, "", "blank", 0.99) for entry_id in PRIMARY
    ]}))
    record = _run(blank_page, plan, TranscriptionEvidence(tmp_path, "paper1", PAGE), transport)
    assert len(transport.calls) == 1
    transcribed = [f for f in record["fields"] if f["source"] == "column_transcription"]
    assert {(f["state"], f["student_answer"]) for f in transcribed} == {("blank", "")}
    assert all(f["verification"]["status"] == "not_required" for f in transcribed)


# --- through the frozen answer-sheet template ----------------------------------------------------


# --- separation ------------------------------------------------------------------------------------

def test_transcription_module_imports_no_grading_code():
    tree = ast.parse(Path(contract.__file__).read_text(encoding="utf-8"))
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update({alias.name: None for alias in node.names})
        elif isinstance(node, ast.ImportFrom):
            imported.setdefault(node.module, set()).update(alias.name for alias in node.names)
    services = {name for name in imported if name and name.startswith(("services", "routers"))}
    assert services == {
        "services.answer_sheet_bubble_recognizer",
        "services.answer_sheet_layout_contract",
        "services.template_anchor_crops",
        "services.analyzer",
    }
    # Only the JSON backslash normalizer is taken from the analyzer module.
    assert imported["services.analyzer"] == {"_sanitize_json_backslashes"}
    assert imported["services.answer_sheet_bubble_recognizer"] == {"RECOGNIZER_VERSION"}
