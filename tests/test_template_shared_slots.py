"""Shared-slot v4 template contract: one Flash-authored inventory, three prompts.

The first Flash call freezes question identity *and* the slot inventory
(``contract_version`` 4: ``questions[i].slots`` with exact sequential
``n[i]`` ids and a non-empty ``cue`` per slot). Slot length is the only
count authority: no separately generated count, no geometry and no
answer/drawing type at this stage. The locator Max then names each slot's
kind and box by ``slot_id``; the solver Max answers every inventory id by
``slot_id``; the text Flash is unchanged.

Offline only: every transport is intercepted, all images are local fixtures.
Run from the repository root:

    PYTHON_DOTENV_DISABLED=1 JWT_SECRET=isolated-shared-slots-test-only-32chars \\
        venv_local/Scripts/python.exe -m pytest tests/test_template_shared_slots.py -q
"""
from __future__ import annotations

import asyncio
import copy
import json
import re

import pytest
from PIL import Image

from services import template_directory_pipeline as pipeline
from services import template_max_owned as max_owned
from services import template_one_three as one_three
from services.teaching_context import make_teaching_context, use_teaching_context

SHARED_SLOT_SOURCE = "flash_slots_v4"
V3_SLOT_SOURCE = max_owned.SLOT_SOURCE
HIERARCHY_PHASE = "template-hierarchy-v1"
LOCATOR_PHASE = "template-directory-v1"

# ---------------------------------------------------------------------------
# Fixtures: page "B", printed Q3 (three ambiguous blanks) and printed Q6
# (one drawing slot plus three text slots). The three Q3 cues are the same
# printed text on purpose: under the old printed answer_key path they were
# indistinguishable, while shared slot ids make duplication impossible.
# ---------------------------------------------------------------------------
PAPER_B_WIRE = {
    "contract_version": 4,
    "questions": [
        {
            "question_number": "1",
            "printed_path": ["二、解答题", "3."],
            "slots": [
                {"slot_id": "1[1]", "cue": "( )"},
                {"slot_id": "1[2]", "cue": "( )"},
                {"slot_id": "1[3]", "cue": "( )"},
            ],
        },
        {
            "question_number": "2",
            "printed_path": ["二、解答题", "6."],
            "slots": [
                {"slot_id": "2[1]", "cue": "作图区：画出△ABC关于直线l的对称图形"},
                {"slot_id": "2[2]", "cue": "(1)求△ABC的周长"},
                {"slot_id": "2[3]", "cue": "(2)求△ABC的面积"},
                {"slot_id": "2[4]", "cue": "(3)写出你的结论"},
            ],
        },
    ],
}
PAPER_B_KINDS = {
    "Q1": ["answer", "answer", "answer"],
    "Q2": ["drawing", "answer", "answer", "answer"],
}
PAPER_B_VALUES = {
    "1[1]": "12",
    "1[2]": "8",
    "1[3]": "20",
    "2[1]": "",
    "2[2]": "24",
    "2[3]": "36",
    "2[4]": "周长24厘米",
}

PURE_DRAWING_WIRE = {
    "contract_version": 4,
    "questions": [
        {
            "question_number": "1",
            "printed_path": ["三、作图", "4."],
            "slots": [{"slot_id": "1[1]", "cue": "作图区：画出平移后的图形"}],
        }
    ],
}
CHOICE_WIRE = {
    "contract_version": 4,
    "questions": [
        {
            "question_number": "1",
            "printed_path": ["一、选择", "2."],
            "slots": [{"slot_id": "1[1]", "cue": "从A/B/C/D中选择一个结论"}],
        }
    ],
}
V3_WIRE = {
    "contract_version": 3,
    "questions": [{"question_number": "1", "printed_path": ["一、填空", "1."]}],
}


def paper_b_wire() -> dict:
    return copy.deepcopy(PAPER_B_WIRE)


def paper_b_hierarchy() -> dict:
    return one_three.receive_hierarchy(paper_b_wire())


def wire(
    *,
    slots,
    version=4,
    question_number="1",
    printed_path=("一、填空", "1."),
    question_extra=None,
    slot_extra=None,
) -> dict:
    """One-question payload builder so each malformed case is readable."""
    if isinstance(slots, list) and all(isinstance(slot, dict) for slot in slots):
        body = [dict({**slot, **(slot_extra or {})}) for slot in slots]
    else:
        # Malformed slot bodies (dict/str/None) pass through for rejection.
        body = slots
    question = {
        "question_number": question_number,
        "printed_path": list(printed_path),
        "slots": body,
    }
    question.update(question_extra or {})
    return {"contract_version": version, "questions": [question]}


def locator_payload(
    hierarchy: dict,
    *,
    kinds: dict | None = None,
    reverse: bool = False,
    reverse_questions: bool = False,
    question_type: str = "fill",
    choice_mode: str = "unknown",
    coordinate_space: str = "normalized_1000_v1",
) -> dict:
    rows = []
    for expected in one_three.question_contract(hierarchy):
        q_kinds = (kinds or {}).get(expected["q_id"], ["answer"] * len(expected["slots"]))
        assert len(q_kinds) == len(expected["slots"])
        regions = [
            {
                "slot_id": slot["slot_id"],
                "kind": q_kinds[index],
                "bbox": [20 + 40 * index, 100, 50 + 40 * index, 130],
            }
            for index, slot in enumerate(expected["slots"])
        ]
        if reverse:
            regions.reverse()
        drawing = [region["bbox"] for region in regions if region["kind"] == "drawing"]
        rows.append(
            {
                "question_number": expected["q_id"][1:],
                "question_number_bbox": [5, 90, 20, 105],
                "question_type": question_type,
                "choice_mode": choice_mode,
                "regions": regions,
                "label_anchor": [10, 100] if drawing else None,
            }
        )
    if reverse_questions:
        rows.reverse()
    return {
        "coordinate_space": coordinate_space,
        "questions": rows,
        "needs_review": [],
    }


def solver_payload(hierarchy: dict, values: dict | None = None, *, reverse: bool = False) -> dict:
    values = PAPER_B_VALUES if values is None else values
    answers = []
    for expected in one_three.question_contract(hierarchy):
        items = [
            {"slot_id": slot["slot_id"], "answer": values.get(slot["slot_id"], "")}
            for slot in expected["slots"]
        ]
        if reverse:
            items.reverse()
        answers.append(
            {
                "q_id": expected["q_id"],
                "standard_answer": "答案与作图要求",
                "answer_items": items,
                "solution_steps": [],
                "visual_facts": [],
                "uncertain": [],
            }
        )
    return {"answers": answers}


def transcription_payload(hierarchy: dict, texts: dict | None = None) -> dict:
    texts = texts or {}
    return {
        "questions": [
            {
                "q_id": row["q_id"],
                "question_text": texts.get(row["q_id"], f"印刷题干{row['q_id']}"),
                "uncertain": [],
            }
            for row in one_three.question_contract(hierarchy)
        ]
    }


def branches(hierarchy: dict, *, reverse: bool = False, values: dict | None = None, kinds: dict | None = None,
             question_type: str = "fill", choice_mode: str = "unknown", texts: dict | None = None):
    located = pipeline._validate_one_three_locator(
        locator_payload(hierarchy, kinds=kinds, reverse=reverse, question_type=question_type, choice_mode=choice_mode),
        hierarchy,
    )
    solver = one_three.receive_solver(solver_payload(hierarchy, values, reverse=reverse), hierarchy)
    transcription = one_three.receive_transcription(transcription_payload(hierarchy, texts), hierarchy)
    return located, solver, transcription


def mapping_entries(answer: dict) -> list[dict]:
    """Locate the slot_id -> solver item mapping whatever wrapper holds it."""
    binding = answer.get("slot_binding")
    if isinstance(binding, dict) and isinstance(binding.get("mapping"), list):
        return binding["mapping"]
    for value in answer.values():
        if (isinstance(value, list) and value
                and all(isinstance(entry, dict) and "slot_id" in entry and "method" in entry for entry in value)):
            return value
    return []


def run_pipeline(source, layout=None) -> dict:
    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            return await pipeline.recognize_directory_template(
                str(source), template_mode="marked", layout_result=layout or {})
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# 1. First Flash: shared inventory and count authority
# ---------------------------------------------------------------------------
def test_v4_hierarchy_freezes_shared_inventory():
    hierarchy = paper_b_hierarchy()
    assert hierarchy["question_identity_version"] == 4
    assert hierarchy["region_count_contract"] is True
    assert hierarchy["leaf_ids"] == ["1", "2"]
    expected = [
        {"q_id": "Q1", "printed_path": ["二、解答题", "3."],
         "slots": [{"slot_id": "1[1]", "cue": "( )"},
                   {"slot_id": "1[2]", "cue": "( )"},
                   {"slot_id": "1[3]", "cue": "( )"}]},
        {"q_id": "Q2", "printed_path": ["二、解答题", "6."],
         "slots": [{"slot_id": "2[1]", "cue": "作图区：画出△ABC关于直线l的对称图形"},
                   {"slot_id": "2[2]", "cue": "(1)求△ABC的周长"},
                   {"slot_id": "2[3]", "cue": "(2)求△ABC的面积"},
                   {"slot_id": "2[4]", "cue": "(3)写出你的结论"}]},
    ]
    assert one_three.question_contract(hierarchy) == expected
    # The locator-facing manifest is the same shared inventory, not a second one.
    assert one_three.slot_contract(hierarchy) == expected
    assert hierarchy["nodes"][0]["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert hierarchy["nodes"][1]["slot_ids"] == ["2[1]", "2[2]", "2[3]", "2[4]"]
    assert hierarchy["nodes"][1]["answer_slot_count"] == 4


def test_v4_accepts_missing_printed_leaf_number_as_trailing_empty_path_component():
    hierarchy = one_three.receive_hierarchy(wire(
        slots=[{"slot_id": "1[1]", "cue": "作答区"}],
        printed_path=["三、解决问题", ""],
    ))
    assert hierarchy["nodes"][0]["source_path"] == ["三、解决问题"]


def test_v4_rejects_non_trailing_empty_path_component():
    with pytest.raises(ValueError):
        one_three.receive_hierarchy(wire(
            slots=[{"slot_id": "1[1]", "cue": "作答区"}],
            printed_path=["三、解决问题", "", "6."],
        ))


def test_v4_slot_length_is_the_only_count_authority():
    three = one_three.receive_hierarchy(wire(slots=[{"slot_id": f"1[{index}]", "cue": f"空{index}"} for index in (1, 2, 3)]))
    one = one_three.receive_hierarchy(wire(slots=[{"slot_id": "1[1]", "cue": "空"}]))
    assert len(one_three.question_contract(three)[0]["slots"]) == 3
    assert len(one_three.question_contract(one)[0]["slots"]) == 1
    # No separately generated count may be smuggled into the payload.
    with pytest.raises(ValueError):
        one_three.receive_hierarchy(wire(slots=[{"slot_id": "1[1]", "cue": "空"}],
                                         question_extra={"answer_slot_count": 1}))
    with pytest.raises(ValueError):
        one_three.receive_hierarchy(wire(slots=[{"slot_id": "1[1]", "cue": "空"}],
                                         question_extra={"answer_regions": ["answer"]}))


def test_v4_contract_row_carries_no_geometry_or_type():
    serialized = json.dumps(one_three.question_contract(paper_b_hierarchy()), ensure_ascii=False)
    for forbidden in ("bbox", "kind", "answer_type", "drawing", "answer_slot_count", "regions", "answer_key"):
        assert forbidden not in serialized


def test_v4_repeated_cue_text_is_allowed_because_ids_are_the_identity():
    hierarchy = paper_b_hierarchy()
    slots = one_three.question_contract(hierarchy)[0]["slots"]
    assert [slot["cue"] for slot in slots] == ["( )", "( )", "( )"]
    assert len({"1[1]", "1[2]", "1[3]"}) == 3


@pytest.mark.parametrize(
    "payload",
    [
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], version=5),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], version="4"),
        {"contract_version": 4, "questions": [{"question_number": "1", "printed_path": ["1."]}]},
        {"contract_version": 4, "questions": []},
        wire(slots=[]),
        wire(slots={"slot_id": "1[1]", "cue": "空"}),
        wire(slots=[{"slot_id": "1[2]", "cue": "空"}]),
        wire(slots=[{"slot_id": "1[0]", "cue": "空"}]),
        wire(slots=[{"slot_id": "2[1]", "cue": "空"}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}, {"slot_id": "1[1]", "cue": "另一个空"}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}, {"slot_id": "1[3]", "cue": "空"}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "   "}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "长" * 2001}]),
        wire(slots=[{"slot_id": "1[1]", "cue": 7}]),
        wire(slots=[{"slot_id": "1[1]"}]),
        {"contract_version": 4, "questions": [{"question_number": "1",
                                               "slots": [{"slot_id": "1[1]", "cue": "空"}]}]},
        wire(slots=[{"slot_id": "1[1]", "cue": "空", "kind": "answer"}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空", "bbox": [0, 0, 1, 1]}]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], question_number="2"),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], printed_path=["", "1."]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], printed_path=["1.", 3]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], printed_path=["1.\n2."]),
        wire(slots=[{"slot_id": "1[1]", "cue": "空"}], printed_path=["长" * 129]),
        {"contract_version": 4, "questions": [{"question_number": "1", "printed_path": ["1."],
                                               "slots": [{"slot_id": f"1[{index}]", "cue": "空"} for index in range(1, 66)]}]},
    ],
)
def test_v4_hierarchy_fails_closed(payload):
    with pytest.raises(ValueError):
        one_three.receive_hierarchy(payload)


def test_hierarchy_prompt_declares_the_shared_slot_contract():
    prompt = pipeline._HIERARCHY_PROMPT
    assert '"contract_version":4' in prompt
    assert "slots" in prompt
    assert "印刷示意图、坐标图、几何图、统计图、观察图" in prompt
    assert "看图回答" in prompt
    assert "只有题干明确要求学生新增画图、补画、作图、描点、连线、涂色、标注或补全图形" in prompt
    assert "不明确时不要猜成作图题" in prompt
    assert '"answer_slot_count"' not in prompt
    assert '"answer_regions"' not in prompt
    assert '"kind"' not in prompt
    assert '"bbox"' not in prompt


# ---------------------------------------------------------------------------
# 2. One inventory, three prompts
# ---------------------------------------------------------------------------
def embedded_rows(prompt: str) -> list[dict]:
    """Extract the shared inventory rows embedded in a prompt (not the schema example)."""
    decoder = json.JSONDecoder()
    rows = []
    for match in re.finditer(r'\{"q_id"\s*:\s*"Q', prompt):
        try:
            value, _ = decoder.raw_decode(prompt, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and "printed_path" in value and "slots" in value:
            rows.append(value)
    return rows


@pytest.mark.parametrize("stage", ["locator", "solver", "transcription"])
def test_v4_inventory_is_identical_in_all_three_prompts(stage):
    hierarchy = paper_b_hierarchy()
    locator, _ = pipeline._parallel_template_prompts(
        hierarchy, width=1000, height=1400,
        columns=[{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}])
    solver, transcription = one_three.build_prompts(hierarchy, education_stage="primary")
    prompt = {"locator": locator, "solver": solver, "transcription": transcription}[stage]
    assert embedded_rows(prompt) == one_three.question_contract(hierarchy)
    for slot_id, cue in (("1[1]", "( )"), ("1[3]", "( )"), ("2[1]", "作图区：画出△ABC关于直线l的对称图形")):
        assert slot_id in prompt
        assert cue in prompt


def test_v4_solver_prompt_lists_every_slot_for_answer_items():
    hierarchy = paper_b_hierarchy()
    solver, _ = one_three.build_prompts(hierarchy, education_stage="primary")
    expected = [
        {"q_id": row["q_id"], "slot_ids": [slot["slot_id"] for slot in row["slots"]]}
        for row in one_three.question_contract(hierarchy)
    ]
    inventory = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert "这是v4共享作答位合同" in solver
    assert "每个slot_id恰好出现一次" in solver
    assert "每个answer_items元素包含slot_id、answer和status" in solver
    assert "extra和refused会转教师复核，处理前模板不能保存；正常drawing留空不会" in solver
    assert inventory in solver


# ---------------------------------------------------------------------------
# 3. Locator Max: slot_id/kind/bbox, no answer_key
# ---------------------------------------------------------------------------
def test_v4_locator_binds_kinds_to_shared_ids():
    hierarchy = paper_b_hierarchy()
    located = pipeline._validate_one_three_locator(
        locator_payload(hierarchy, kinds=PAPER_B_KINDS), hierarchy)
    assert located["slot_source"] == SHARED_SLOT_SOURCE
    assert located["coordinate_space"] == "normalized_1000_v1"
    first, second = located["questions"]
    assert first["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert first["region_kinds"] == ["answer", "answer", "answer"]
    assert first["regions"] == [[20, 100, 50, 130], [60, 100, 90, 130], [100, 100, 130, 130]]
    assert first["answer_slot_count"] == 3 and first["answer_area_count"] == 3
    assert first["answer_kind"] == "non_graphic" and first["requires_drawing"] is False
    assert first["drawing_regions"] == [] and first["label_anchor"] is None
    assert second["slot_ids"] == ["2[1]", "2[2]", "2[3]", "2[4]"]
    assert second["region_kinds"] == ["drawing", "answer", "answer", "answer"]
    assert second["answer_kind"] == "mixed" and second["requires_drawing"] is True
    assert second["drawing_regions"] == [[20, 100, 50, 130]]
    assert second["answer_slot_count"] == 4 and second["answer_area_count"] == 3
    assert second["label_anchor"] == [10, 100]


def test_v4_locator_accepts_redundant_slot_bbox_when_equal_to_bbox():
    hierarchy = paper_b_hierarchy()
    payload = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    for row in payload["questions"]:
        for region in row["regions"]:
            region["slot_bbox"] = list(region["bbox"])
    located = pipeline._validate_one_three_locator(payload, hierarchy)
    assert located["questions"][1]["regions"][0] == [20, 100, 50, 130]


@pytest.mark.parametrize("reverse_questions", [False, True])
def test_v4_locator_and_solver_order_is_canonicalized_to_inventory(reverse_questions):
    hierarchy = paper_b_hierarchy()
    payload = locator_payload(hierarchy, kinds=PAPER_B_KINDS, reverse=True,
                              reverse_questions=reverse_questions)
    located = pipeline._validate_one_three_locator(payload, hierarchy)
    assert [row["question_number"] for row in located["questions"]] == ["1", "2"]
    assert located["questions"][1]["slot_ids"] == ["2[1]", "2[2]", "2[3]", "2[4]"]
    assert located["questions"][1]["region_kinds"] == ["drawing", "answer", "answer", "answer"]
    assert located["questions"][1]["regions"] == [[20, 100, 50, 130], [60, 100, 90, 130],
                                                  [100, 100, 130, 130], [140, 100, 170, 130]]
    solver = one_three.receive_solver(solver_payload(hierarchy, reverse=True), hierarchy)
    transcription = one_three.receive_transcription(transcription_payload(hierarchy), hierarchy)
    merged = one_three.merge_branches(located, solver, transcription)
    assert merged["answers"][0]["answer_slots"] == ["12", "8", "20"]
    assert merged["answers"][1]["answer_slots"] == ["", "24", "36", "周长24厘米"]


@pytest.mark.parametrize(
    "fault",
    ["unknown_slot", "duplicate_slot", "missing_region", "foreign_question_slot",
     "answer_key", "bad_kind", "extra_region_field", "flat_box", "bad_bbox", "bad_question_type"],
)
def test_v4_locator_rejects_incomplete_or_foreign_bindings(fault):
    hierarchy = paper_b_hierarchy()
    payload = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    row = payload["questions"][1]
    if fault == "unknown_slot":
        row["regions"][0]["slot_id"] = "2[9]"
    elif fault == "duplicate_slot":
        row["regions"][1]["slot_id"] = row["regions"][0]["slot_id"]
    elif fault == "missing_region":
        row["regions"].pop()
    elif fault == "foreign_question_slot":
        row["regions"][0]["slot_id"] = "1[1]"
    elif fault == "answer_key":
        row["regions"][0]["answer_key"] = None
    elif fault == "bad_kind":
        row["regions"][0]["kind"] = "text"
    elif fault == "extra_region_field":
        row["regions"][0]["answer_type"] = "answer"
    elif fault == "flat_box":
        row["regions"][0] = row["regions"][0]["bbox"]
    elif fault == "bad_bbox":
        row["regions"][0]["bbox"] = [10, 10, 10, 20]
    elif fault == "bad_question_type":
        row["question_type"] = "reading"
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(payload, hierarchy)


@pytest.mark.parametrize(
    "fault",
    ["extra_question_field", "missing_question", "extra_question", "bad_coordinate_space",
     "missing_needs_review", "bad_needs_review"],
)
def test_v4_locator_envelope_fails_closed(fault):
    hierarchy = paper_b_hierarchy()
    payload = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    if fault == "extra_question_field":
        payload["questions"][0]["requires_drawing"] = False
    elif fault == "missing_question":
        payload["questions"].pop()
    elif fault == "extra_question":
        payload["questions"].append({**copy.deepcopy(payload["questions"][0]), "question_number": "3"})
    elif fault == "bad_coordinate_space":
        payload["coordinate_space"] = "normalized_500_v1"
    elif fault == "missing_needs_review":
        payload.pop("needs_review")
    elif fault == "bad_needs_review":
        payload["needs_review"] = [7]
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(payload, hierarchy)


def test_v4_locator_drawing_alias_and_pure_drawing_geometry():
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(PURE_DRAWING_WIRE))
    payload = locator_payload(hierarchy, kinds={"Q1": ["drawing"]}, question_type="drawing")
    located = pipeline._validate_one_three_locator(payload, hierarchy)["questions"][0]
    # drawing is an answer_kind alias for the internal solve route, kept as-is.
    assert located["question_type"] == "solve"
    assert located["region_kinds"] == ["drawing"]
    assert located["answer_kind"] == "drawing" and located["requires_drawing"] is True
    assert located["drawing_regions"] == [[20, 100, 50, 130]]
    assert located["label_anchor"] == [10, 100]
    payload["questions"][0]["label_anchor"] = None
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(payload, hierarchy)
    # Reception now shares the frozen schema contract: left boundary contact
    # is valid, an interior anchor is rejected without rewriting geometry.
    payload["questions"][0]["label_anchor"] = [30, 110]
    snapshot = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="第 1 题作图标签必须位于主作图区左侧"):
        pipeline._validate_one_three_locator(payload, hierarchy)
    assert payload == snapshot
    payload["questions"][0]["label_anchor"] = [20, 110]
    located = pipeline._validate_one_three_locator(payload, hierarchy)["questions"][0]
    assert located["label_anchor"] == [20, 110]
    assert located["drawing_regions"] == [[20, 100, 50, 130]]


# ---------------------------------------------------------------------------
# 4. Solver Max: every inventory id, by slot_id
# ---------------------------------------------------------------------------
def test_v4_solver_returns_every_inventory_id_without_mutating_input():
    hierarchy = paper_b_hierarchy()
    payload = solver_payload(hierarchy)
    snapshot = copy.deepcopy(payload)
    solver = one_three.receive_solver(payload, hierarchy)
    assert payload == snapshot
    assert [answer["q_id"] for answer in solver["answers"]] == ["Q1", "Q2"]


@pytest.mark.parametrize(
    "fault",
    ["missing_item", "missing_drawing_item", "duplicate_item", "unknown_item",
     "foreign_question_item", "answer_key_item", "extra_item_field", "missing_answers", "unknown_question"],
)
def test_v4_solver_rejects_incomplete_or_foreign_ids(fault):
    hierarchy = paper_b_hierarchy()
    payload = solver_payload(hierarchy)
    row = payload["answers"][1]
    if fault == "missing_item":
        row["answer_items"].pop()
    elif fault == "missing_drawing_item":
        row["answer_items"].pop(0)
    elif fault == "duplicate_item":
        row["answer_items"][1]["slot_id"] = row["answer_items"][0]["slot_id"]
    elif fault == "unknown_item":
        row["answer_items"][0]["slot_id"] = "2[9]"
    elif fault == "foreign_question_item":
        row["answer_items"][0]["slot_id"] = "1[1]"
    elif fault == "answer_key_item":
        row["answer_items"][0] = {"answer_key": {"part": "", "anchor": "", "blank_index": 0}, "answer": "24"}
    elif fault == "extra_item_field":
        row["answer_items"][0]["kind"] = "answer"
    elif fault == "missing_answers":
        payload["answers"] = []
    elif fault == "unknown_question":
        payload["answers"][1]["q_id"] = "Q3"
    with pytest.raises(ValueError):
        one_three.receive_solver(payload, hierarchy)


@pytest.mark.parametrize("status", ["extra", "refused"])
@pytest.mark.parametrize("level", ["question", "slot"])
@pytest.mark.parametrize("branch", ["locator", "solver"])
def test_v4_explicit_max_report_becomes_blocking_review_not_blank_or_failure(status, level, branch):
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    locator_raw = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    solver_raw = solver_payload(hierarchy)
    if branch == "solver":
        target = solver_raw["answers"][0]
        if level == "slot":
            target = target["answer_items"][0]
    else:
        target = locator_raw["questions"][0]
        if level == "slot":
            target = target["regions"][0]
    if level == "question":
        # A whole-question report need not carry the answer/geometry fields.
        for key in list(target):
            if key not in {"question_number", "q_id"}:
                target.pop(key)
    target.update(status=status, reason="原图没有该作答位置")
    locator = max_owned.receive_locator_candidates(locator_raw, manifest)
    solver = max_owned.receive_solver_candidates(solver_raw, manifest)
    flag = {"scope": level, **({"slot_id": "1[1]"} if level == "slot" else {}),
            "status": status, "reason": "原图没有该作答位置", "source": branch}
    assert (locator if branch == "locator" else solver)["max_flags"] == {"1": [flag]}
    located, solved = max_owned.reconcile_slot_candidates(locator, solver, manifest)
    geometry = located["questions"][0]
    # The reported position is retained, never deleted or answered by the model.
    assert geometry["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    if branch == "locator":
        expected_regions = 3 if level == "question" else 1
        assert sum(1 for box in geometry["regions"] if box == []) == expected_regions
    assert located["max_flags"] == {"1": [flag]}
    answer = max_owned.bind_answers(located, solved)["answers"][0]
    assert answer["answer_status"] == "needs_review"
    assert any("Max" in reason and "请教师删除或确认保留" in reason for reason in answer["answer_review_reasons"])
    if branch == "solver":
        assert answer["answer_slots"][0] == ""


def test_v4_both_branches_reporting_a_slot_never_silently_drop_it():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    locator_raw = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    solver_raw = solver_payload(hierarchy)
    locator_raw["questions"][0]["regions"][2] = {"slot_id": "1[3]", "status": "extra", "reason": "印刷示意图"}
    solver_raw["answers"][0]["answer_items"][2] = {"slot_id": "1[3]", "answer": "", "status": "extra", "reason": "印刷示意图"}
    located, solved = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(locator_raw, manifest),
        max_owned.receive_solver_candidates(solver_raw, manifest), manifest)
    geometry = located["questions"][0]
    assert geometry["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert geometry["regions"][2] == [] and geometry["region_kinds"][2] == "answer"
    assert [flag["source"] for flag in located["max_flags"]["1"]] == ["locator", "solver"]
    assert any("1[3]" in reason for reason in located["needs_review"])


def test_v4_reported_drawing_slot_keeps_drawing_type_when_solver_marked_it():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    locator_raw = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    locator_raw["questions"][1]["regions"][0] = {"slot_id": "2[1]", "status": "refused", "reason": "看不清"}
    solver_raw = solver_payload(hierarchy)
    solver_raw["answers"][1]["answer_items"][0].update(status="drawing")
    located, solved = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(locator_raw, manifest),
        max_owned.receive_solver_candidates(solver_raw, manifest), manifest)
    geometry = located["questions"][1]
    assert geometry["region_kinds"] == ["drawing", "answer", "answer", "answer"]
    assert geometry["regions"][0] == [] and geometry["drawing_regions"] == []


@pytest.mark.parametrize("branch", ["locator", "solver"])
def test_v4_response_level_refusal_still_stops_without_a_review_draft(branch):
    hierarchy = paper_b_hierarchy()
    receive = max_owned.receive_locator_candidates if branch == "locator" else max_owned.receive_solver_candidates
    with pytest.raises(max_owned.MaxTemplateRejection, match="阻断"):
        receive({"refusal": "policy"}, one_three.question_contract(hierarchy))


def test_v4_drawing_status_is_advisory_and_never_rebuys_the_response():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw_solver = solver_payload(hierarchy)
    row = raw_solver["answers"][0]
    row.update(status="drawing", standard_answer="画出图形", solution_steps=["先找对称点"])
    row["answer_items"][0].update(status="drawing", answer="对称图形")
    row["answer_items"][1].update(status="answered", reason="普通填空")
    solver = max_owned.receive_solver_candidates(raw_solver, manifest)
    locator = max_owned.receive_locator_candidates(
        locator_payload(hierarchy, kinds={**PAPER_B_KINDS, "Q1": ["drawing"] * 3}), manifest)
    located, solved = max_owned.reconcile_slot_candidates(locator, solver, manifest)
    answer = max_owned.bind_answers(located, solved)["answers"][0]
    assert answer["standard_answer"] == "" and answer["solution_steps"] == []
    assert answer["answer_slots"] == ["", "", ""]
    assert any("作图区 1[1] 生成了文字答案" in reason for reason in answer["answer_review_reasons"])


def test_v4_blank_whole_answer_on_text_question_requires_teacher():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw_solver = solver_payload(hierarchy)
    raw_solver["answers"][0]["standard_answer"] = ""
    located, solved = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(locator_payload(hierarchy, kinds=PAPER_B_KINDS), manifest),
        max_owned.receive_solver_candidates(raw_solver, manifest), manifest)
    answer = max_owned.bind_answers(located, solved)["answers"][0]
    assert answer["answer_status"] == "needs_review"
    assert "缺少整题标准答案，需教师补充" in answer["answer_review_reasons"]


def test_v4_pure_drawing_empty_answer_is_ready_and_never_needs_text_reference():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw_locator = locator_payload(hierarchy, kinds={**PAPER_B_KINDS, "Q1": ["drawing"] * 3})
    raw_solver = solver_payload(hierarchy)
    row = raw_solver["answers"][0]
    row.update(status="drawing", standard_answer="", solution_steps=[], uncertain=[])
    for item in row["answer_items"]:
        item.update(status="drawing", answer="")
    located, solved = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(raw_locator, manifest),
        max_owned.receive_solver_candidates(raw_solver, manifest), manifest)
    answer = max_owned.bind_answers(located, solved)["answers"][0]
    assert answer["standard_answer"] == "" and answer["solution_steps"] == []
    assert answer["answer_slots"] == ["", "", ""]
    assert answer["answer_status"] == "ready"


def test_v4_empty_text_answer_stays_bindable_but_requires_teacher_answer():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw = solver_payload(hierarchy)
    raw["answers"][0].update(status="answered", standard_answer="")
    raw["answers"][0]["answer_items"][0].update(status="answered", answer="")
    locator = max_owned.receive_locator_candidates(locator_payload(hierarchy, kinds=PAPER_B_KINDS), manifest)
    solver = max_owned.receive_solver_candidates(raw, manifest)
    answer = max_owned.bind_answers(locator, solver)["answers"][0]
    assert answer["answer_slots"] == ["", "8", "20"]
    assert answer["answer_status"] == "needs_review"


def test_v4_production_solver_boundary_rejects_unbindable_items():
    hierarchy = paper_b_hierarchy()
    payload = solver_payload(hierarchy)
    payload["answers"][0]["answer_items"][0] = {"answer": "丢失槽位身份"}
    with pytest.raises(ValueError):
        max_owned.receive_solver_candidates(payload, one_three.question_contract(hierarchy))


def test_v4_max_inventory_disagreement_uses_larger_proposal_and_keeps_unbound_empty():
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(CHOICE_WIRE))
    manifest = one_three.question_contract(hierarchy)
    locator_payload_raw = locator_payload(hierarchy, kinds={"Q1": ["answer"]}, question_type="fill")
    locator = max_owned.receive_locator_candidates(locator_payload_raw, manifest)

    # Solver proposes two slots while locator proposes one. The larger Max
    # proposal wins, and the solver-only slot is retained without invented
    # geometry or an inferred answer.
    solver_raw = solver_payload(hierarchy, values={"1[1]": "A"})
    solver_raw["answers"][0]["answer_items"].append({"slot_id": "1[2]", "answer": ""})
    solver = max_owned.receive_solver_candidates(solver_raw, manifest)
    locator, solver = max_owned.reconcile_slot_candidates(locator, solver, manifest)

    assert locator["questions"][0]["slot_ids"] == ["1[1]", "1[2]"]
    assert locator["questions"][0]["regions"] == [[20, 100, 50, 130], []]
    assert solver["answers"][0]["slot_ids"] == ["1[1]", "1[2]"]
    assert solver["answers"][0]["answer_items"] == [
        {"slot_id": "1[1]", "answer": "A"},
        {"slot_id": "1[2]", "answer": ""},
    ]
    assert locator["needs_review"]


def test_v4_max_inventory_same_count_disagreement_does_not_block():
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(PAPER_B_WIRE))
    manifest = one_three.question_contract(hierarchy)
    locator_raw = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    locator = max_owned.receive_locator_candidates(locator_raw, manifest)
    solver_raw = solver_payload(hierarchy)
    solver = max_owned.receive_solver_candidates(solver_raw, manifest)
    # Change only Q1's solver inventory, preserving a valid contract.
    solver["answers"][0]["slot_ids"] = ["1[1]", "1[2]", "1[4]"]
    solver["answers"][0]["answer_items"] = [
        {"slot_id": "1[1]", "answer": "12"},
        {"slot_id": "1[2]", "answer": "8"},
        {"slot_id": "1[4]", "answer": "20"},
    ]
    locator, solver = max_owned.reconcile_slot_candidates(locator, solver, manifest)
    assert locator["questions"][0]["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert solver["answers"][0]["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert any("清单不一致" in reason for reason in solver["answers"][0]["answer_review_reasons"])
    bound = max_owned.bind_answers(locator, solver)["answers"][0]
    assert bound["answer_slots"] == ["12", "8", ""]
    assert bound["slot_binding"]["unbound_items"] == [{"slot_id": "1[4]", "answer": "20"}]


@pytest.mark.parametrize("locator_indexes,solver_indexes", [
    (list(range(4, 10)), list(range(4, 10))),  # both remove the three extra Flash slots
    (list(range(1, 10)), list(range(4, 10))),  # locator larger: keep three empty answers
    (list(range(4, 10)), list(range(1, 10))),  # solver larger: keep three unlocated slots
    ([1, 3, 5], [1, 2, 3]),                 # equal counts, different identities
    ([1, 2, 3, 10], [1, 2, 3, 10]),         # both replace Flash with a new inventory
    ([1], [2, 3]),                         # no shared physical identity
])
def test_v4_inventory_resolution_survives_generation_and_draft_storage(
        tmp_path, monkeypatch, locator_indexes, solver_indexes):
    from services.grading_contract import finalize_answer_spec_for_category
    from services.template_evidence import build_template_artifact, normalize_template_handwriting_regions
    from services.template_session import TemplateSession

    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    initial = wire(slots=[{"slot_id": f"1[{i}]", "cue": f"位置{i}"} for i in range(1, 10)])
    hierarchy = one_three.receive_hierarchy(initial)
    located = locator_payload(hierarchy)
    located["questions"][0]["regions"] = [
        {"slot_id": f"1[{i}]", "kind": "answer", "bbox": [i * 40, 100, i * 40 + 30, 130]}
        for i in reversed(locator_indexes)
    ]
    solved = solver_payload(hierarchy)
    solved["answers"][0]["answer_items"] = [
        {"slot_id": f"1[{i}]", "answer": f"答案{i}"} for i in reversed(solver_indexes)
    ]
    payloads = {
        HIERARCHY_PHASE: initial, LOCATOR_PHASE: located, one_three.SOLVE_PHASE: solved,
        one_three.TRANSCRIBE_PHASE: transcription_payload(hierarchy),
        "template-coarse-locator-v1": {"coordinate_space": "normalized_1000_v1", "questions": [
            {"question_number": "1", "bbox": [0, 0, 1000, 1000]}]},
    }
    calls = []

    async def request(images, prompt, phase, validator, **kwargs):
        calls.append(phase)
        return validator(copy.deepcopy(payloads[phase]))

    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source)
    assert "error" not in result
    assert len(calls) == 5  # reconciliation adds no model round trip
    selected = sorted(locator_indexes if len(locator_indexes) >= len(solver_indexes) else solver_indexes)
    ids = [f"1[{i}]" for i in selected]
    question = result["questions"][0]
    assert question["slot_ids"] == ids
    assert question["blank_count"] == len(ids)
    expected_answers = [f"答案{i}" if i in solver_indexes else "" for i in selected]
    assert question["answer_slots"] == expected_answers
    assert result["question_directory"]["leaves"][0]["slot_ids"] == ids
    assert bool(result["requires_teacher_confirmation"]) == (set(locator_indexes) != set(solver_indexes))
    physical = result["template_answer_regions"]
    assert physical[0].get("slot_ids", []) == [f"1[{i}]" for i in selected if i in locator_indexes]
    assert normalize_template_handwriting_regions(physical) == physical

    session = TemplateSession("slot-candidates", str(tmp_path), teacher_id=1)
    session.add_page(0, result["questions"])
    session.save_meta()
    restored = TemplateSession.load_meta("slot-candidates", str(tmp_path))
    assert restored.pages[0]["questions"][0]["answer_slots"] == expected_answers
    assert restored.pages[0]["questions"][0]["slot_ids"] == ids
    # Pending candidates use the existing draft artifact path. Unlocated
    # entries never reach the confirmed geometry binder as fake rectangles.
    pending = bool(result["requires_teacher_confirmation"])
    artifact = build_template_artifact(
        source, tmp_path / "artifact", 0, result["questions"], template_mode="marked",
        question_anchors=None if pending else result["question_anchors"],
        template_answer_regions=None if pending else physical,
        template_handwriting_regions=None if pending else result["template_handwriting_regions"],
    )
    assert artifact["question_schema"][0]["slot_ids"] == ids
    if "" in expected_answers:
        with pytest.raises(ValueError, match="文字答案位不得为空"):
            finalize_answer_spec_for_category(restored.pages[0]["questions"][0], "daily")


def test_v4_malformed_solver_question_does_not_invalidate_another_questions_agreement():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw_locator = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    raw_solver = solver_payload(hierarchy)
    raw_locator["questions"][0]["regions"].pop(0)
    raw_solver["answers"][0]["answer_items"].pop(0)
    raw_solver["answers"][1]["answer_items"][1] = {"answer": "未标号的答案"}
    with pytest.raises(ValueError):
        max_owned.receive_solver_candidates(raw_solver, manifest)


@pytest.mark.parametrize("fault", ["duplicate_slot", "duplicate_question", "invalid_question_identity"])
def test_v4_candidate_solver_rejects_ambiguous_identity(fault):
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    payload = solver_payload(hierarchy)
    if fault == "duplicate_slot":
        payload["answers"][0]["answer_items"].append({"slot_id": "1[1]", "answer": "999"})
    elif fault == "duplicate_question":
        duplicate = copy.deepcopy(payload["answers"][0])
        duplicate["answer_items"][0]["answer"] = "999"
        payload["answers"].append(duplicate)
    else:
        payload["answers"][0]["q_id"] = ["Q1"]
    with pytest.raises(ValueError):
        max_owned.receive_solver_candidates(payload, manifest)


def test_v4_unselected_locator_drawing_does_not_leave_stale_graphic_metadata():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    raw_locator = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    raw_locator["questions"][1]["regions"] = raw_locator["questions"][1]["regions"][:1]
    raw_solver = solver_payload(hierarchy)
    raw_solver["answers"][1]["answer_items"].pop(0)
    locator, solver = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(raw_locator, manifest),
        max_owned.receive_solver_candidates(raw_solver, manifest), manifest)
    graphic = locator["questions"][1]
    assert graphic["slot_ids"] == ["2[2]", "2[3]", "2[4]"]
    assert graphic["regions"] == [[], [], []]
    assert graphic["drawing_regions"] == []
    assert graphic["label_anchor"] is None
    assert graphic["requires_drawing"] is False
    assert graphic["answer_kind"] == "non_graphic"


# ---------------------------------------------------------------------------
# 5. Merge: inventory order, locator owns drawing, empties kept
# ---------------------------------------------------------------------------
def test_v4_merge_binds_every_slot_by_slot_id():
    hierarchy = paper_b_hierarchy()
    located, solver, transcription = branches(hierarchy, kinds=PAPER_B_KINDS)
    merged = one_three.merge_branches(located, solver, transcription)
    first, second = merged["answers"]
    assert first["answer_slots"] == ["12", "8", "20"]
    assert first["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert first["answer_status"] == "ready"
    assert first["question_text"] == "印刷题干Q1"
    # A drawing slot keeps the empty answer and is not reported as missing.
    assert second["answer_slots"] == ["", "24", "36", "周长24厘米"]
    assert second["slot_ids"] == ["2[1]", "2[2]", "2[3]", "2[4]"]
    assert second["answer_status"] == "ready"
    assert {entry["slot_id"]: entry["method"] for entry in mapping_entries(first)} == {
        "1[1]": "slot_id", "1[2]": "slot_id", "1[3]": "slot_id"}
    second_mapping = {entry["slot_id"]: entry["method"] for entry in mapping_entries(second)}
    assert second_mapping["2[1]"] == "drawing"
    assert [second_mapping[slot_id] for slot_id in ("2[2]", "2[3]", "2[4]")] == ["slot_id"] * 3
    binding = first.get("slot_binding") or {}
    if binding:
        assert binding.get("version") == SHARED_SLOT_SOURCE
        assert not binding.get("unbound_items")


def test_v4_merge_keeps_identical_answers_without_dedup_and_preserves_middle_empty():
    hierarchy = paper_b_hierarchy()
    values = dict(PAPER_B_VALUES, **{"1[1]": "5", "1[2]": "5", "1[3]": ""})
    located, solver, transcription = branches(hierarchy, kinds=PAPER_B_KINDS, values=values)
    answer = one_three.merge_branches(located, solver, transcription)["answers"][0]
    assert answer["answer_slots"] == ["5", "5", ""]
    assert answer["answer_status"] == "needs_review"
    assert answer["answer_review_reasons"]
    assert any("1[3]" in reason for reason in answer["answer_review_reasons"])


def test_v4_merge_locator_owns_the_drawing_slot_answer():
    hierarchy = paper_b_hierarchy()
    values = dict(PAPER_B_VALUES, **{"2[1]": "学生需画图"})
    located, solver, transcription = branches(hierarchy, kinds=PAPER_B_KINDS, values=values)
    answer = one_three.merge_branches(located, solver, transcription)["answers"][1]
    # The locator declared 2[1] as a drawing slot, so no text answer is persisted.
    assert answer["answer_slots"] == ["", "24", "36", "周长24厘米"]


def test_v4_choice_question_still_skips_transcription():
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(CHOICE_WIRE))
    values = {"1[1]": "C"}
    located, solver, transcription = branches(
        hierarchy, kinds={"Q1": ["answer"]}, question_type="choice", choice_mode="single",
        values=values, texts={"Q1": ""})
    merged = one_three.merge_branches(located, solver, transcription)["answers"][0]
    assert merged["question_text"] == ""
    assert merged["answer_slots"] == ["C"]
    assert merged["answer_status"] == "ready"
    _, _, bad_text = branches(
        hierarchy, kinds={"Q1": ["answer"]}, question_type="choice", choice_mode="single",
        values=values, texts={"Q1": "伪造题干"})
    conflicted = one_three.merge_branches(located, solver, bad_text)["answers"][0]
    assert conflicted["answer_status"] == "needs_review"
    assert any("类型冲突" in reason for reason in conflicted["answer_review_reasons"])


def test_v4_pure_drawing_route_is_unchanged():
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(PURE_DRAWING_WIRE))
    values = {"1[1]": ""}
    located, solver, transcription = branches(
        hierarchy, kinds={"Q1": ["drawing"]}, question_type="drawing", values=values)
    answer = one_three.merge_branches(located, solver, transcription)["answers"][0]
    assert answer["answer_slots"] == [""]
    assert answer["answer_status"] == "ready"


def test_v4_receive_boundaries_leave_raw_payloads_immutable():
    wire_payload = paper_b_wire()
    hierarchy = one_three.receive_hierarchy(wire_payload)
    assert wire_payload == paper_b_wire()
    locator = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    solver_payload_raw = solver_payload(hierarchy)
    transcription_raw = transcription_payload(hierarchy)
    raw_snapshot = copy.deepcopy([locator, solver_payload_raw, transcription_raw])
    located = pipeline._validate_one_three_locator(locator, hierarchy)
    solver = one_three.receive_solver(solver_payload_raw, hierarchy)
    transcription = one_three.receive_transcription(transcription_raw, hierarchy)
    assert [locator, solver_payload_raw, transcription_raw] == raw_snapshot
    normalized_snapshot = copy.deepcopy([located, solver, transcription])
    one_three.merge_branches(located, solver, transcription)
    assert [located, solver, transcription] == normalized_snapshot


# ---------------------------------------------------------------------------
# 6. v3 and v4 sources never mix
# ---------------------------------------------------------------------------
def v3_locator_payload() -> dict:
    return {"coordinate_space": "normalized_1000_v1", "questions": [{
        "question_number": "1", "question_number_bbox": [5, 90, 20, 105], "question_type": "fill",
        "choice_mode": "unknown", "label_anchor": None,
        "regions": [{"kind": "answer", "bbox": [20, 100, 50, 130],
                     "answer_key": {"part": "", "anchor": "", "blank_index": 0}}]}],
        "needs_review": []}


def v3_solver_payload() -> dict:
    return {"answers": [{"q_id": "Q1", "standard_answer": "12",
                         "answer_items": [{"answer_key": {"part": "", "anchor": "", "blank_index": 0},
                                           "answer": "12"}],
                         "solution_steps": [], "visual_facts": [], "uncertain": []}]}


def v3_transcription_payload() -> dict:
    return {"questions": [{"q_id": "Q1", "question_text": "印刷题干Q1", "uncertain": []}]}


def test_v3_and_v4_sources_cannot_mix():
    hierarchy_v4 = paper_b_hierarchy()
    hierarchy_v3 = one_three.receive_hierarchy(copy.deepcopy(V3_WIRE))
    located_v4 = pipeline._validate_one_three_locator(
        locator_payload(hierarchy_v4, kinds=PAPER_B_KINDS), hierarchy_v4)
    located_v3 = pipeline._validate_one_three_locator(v3_locator_payload(), hierarchy_v3)
    assert located_v3["slot_source"] == V3_SLOT_SOURCE
    assert located_v4["slot_source"] == SHARED_SLOT_SOURCE
    solver_v4 = one_three.receive_solver(solver_payload(hierarchy_v4), hierarchy_v4)
    solver_v3 = one_three.receive_solver(v3_solver_payload(), hierarchy_v3)
    transcription_v4 = one_three.receive_transcription(transcription_payload(hierarchy_v4), hierarchy_v4)
    transcription_v3 = one_three.receive_transcription(v3_transcription_payload(), hierarchy_v3)
    with pytest.raises(ValueError):
        one_three.merge_branches(located_v4, solver_v3, transcription_v3)
    with pytest.raises(ValueError):
        one_three.merge_branches(located_v3, solver_v4, transcription_v4)


def test_wire_versions_do_not_cross_receive_boundaries():
    hierarchy_v4 = paper_b_hierarchy()
    hierarchy_v3 = one_three.receive_hierarchy(copy.deepcopy(V3_WIRE))
    with pytest.raises(ValueError):
        one_three.receive_solver(v3_solver_payload(), hierarchy_v4)
    with pytest.raises(ValueError):
        one_three.receive_solver(solver_payload(hierarchy_v4), hierarchy_v3)
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(
            {"coordinate_space": "normalized_1000_v1", "needs_review": [], "questions": [{
                "question_number": "1", "question_number_bbox": [5, 90, 20, 105], "question_type": "fill",
                "choice_mode": "unknown", "label_anchor": None,
                "regions": [{"slot_id": "1[1]", "kind": "answer", "bbox": [20, 100, 50, 130]}]}]},
            hierarchy_v3)


# ---------------------------------------------------------------------------
# 7. Synthetic real pipeline: 1 freeze -> 3 concurrent branches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("solver_shards", [1, 2])
@pytest.mark.parametrize("reverse_branches", [False, True])
def test_v4_pipeline_freezes_inventory_then_runs_one_to_three(tmp_path, monkeypatch, reverse_branches, solver_shards):
    import config
    monkeypatch.setattr(config, "OCR_TEMPLATE_SOLVER_SHARDS", solver_shards)
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    wire_payload = paper_b_wire()
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(wire_payload))
    payloads = {
        LOCATOR_PHASE: locator_payload(hierarchy, kinds=PAPER_B_KINDS, reverse=reverse_branches),
        one_three.SOLVE_PHASE: solver_payload(hierarchy, reverse=reverse_branches),
        one_three.TRANSCRIBE_PHASE: transcription_payload(hierarchy),
    }
    calls = []
    in_flight = set()
    peak = {"value": 0}
    all_three = asyncio.Event()

    async def request(images, prompt, phase, validator, **kwargs):
        calls.append((phase, kwargs.get("reasoning_effort"), kwargs.get("max_tokens"), len(images)))
        if phase == HIERARCHY_PHASE:
            assert not in_flight
            return validator(copy.deepcopy(wire_payload))
        if phase == "template-coarse-locator-v1":
            return validator({"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": str(index), "bbox": [0, (index - 1) * 500, 1000, index * 500]}
                for index, _ in enumerate(one_three.question_contract(hierarchy), 1)]})
        in_flight.add(phase)
        peak["value"] = max(peak["value"], len(in_flight))
        if len(in_flight) == 3:
            all_three.set()
        try:
            await asyncio.wait_for(all_three.wait(), 2)
        except asyncio.TimeoutError:
            pass
        in_flight.discard(phase)
        return validator(one_three.parse_json(json.dumps(payloads[phase], ensure_ascii=False)))

    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source)

    assert "error" not in result
    # The solve branch may run as one request per shard; the other four stay single.
    assert len(calls) == 4 + solver_shards
    assert [phase for phase, *_ in calls].count(one_three.SOLVE_PHASE) == solver_shards
    efforts = {phase: effort for phase, effort, _, _ in calls}
    assert efforts[HIERARCHY_PHASE] == "low"
    assert efforts["template-coarse-locator-v1"] == "low"
    assert efforts[LOCATOR_PHASE] == "high"
    assert efforts[one_three.SOLVE_PHASE] == "high"
    assert efforts[one_three.TRANSCRIBE_PHASE] == "high"
    tokens = {phase: budget for phase, _, budget, _ in calls}
    assert tokens[LOCATOR_PHASE] == one_three.MAX_TOKENS
    assert tokens[one_three.SOLVE_PHASE] == one_three.MAX_TOKENS
    assert tokens[one_three.TRANSCRIBE_PHASE] == one_three.FLASH_TOKENS
    assert peak["value"] == 3

    questions = result["questions"]
    assert [question["slot_ids"] for question in questions] == [
        ["1[1]", "1[2]", "1[3]"], ["2[1]", "2[2]", "2[3]", "2[4]"]]
    assert [question["answer_slots"] for question in questions] == [
        ["12", "8", "20"], ["", "24", "36", "周长24厘米"]]
    assert questions[1]["region_kinds"] == ["drawing", "answer", "answer", "answer"]
    assert questions[1]["answer_area_count"] == 3 and questions[1]["drawing_area_count"] == 1
    assert [question["blank_count"] for question in questions] == [3, 4]
    assert [question["answer_bboxes"] for question in questions] == [
        [[20, 100, 50, 130], [60, 100, 90, 130], [100, 100, 130, 130]],
        [[20, 100, 50, 130], [60, 100, 90, 130], [100, 100, 130, 130], [140, 100, 170, 130]]]
    assert result["template_answer_regions"][1]["slot_ids"] == ["2[1]", "2[2]", "2[3]", "2[4]"]
    assert result["question_directory"]["leaves"][0]["slot_ids"] == ["1[1]", "1[2]", "1[3]"]
    assert result["template_ocr_diagnostics"]["slot_source"] == SHARED_SLOT_SOURCE
    assert result["requires_teacher_confirmation"] == []

@pytest.mark.parametrize('disposition', ['drawing', 'extra', 'refused', 'invalid_id'])
def test_pipeline_distinguishes_legal_drawing_empty_from_max_rejection(tmp_path, monkeypatch, disposition):
    source = tmp_path / 'synthetic.png'
    Image.new('RGB', (600, 800), 'white').save(source)
    wire = paper_b_wire()
    hierarchy = one_three.receive_hierarchy(copy.deepcopy(wire))
    geometry = locator_payload(hierarchy, kinds={**PAPER_B_KINDS, 'Q1': ['drawing'] * 3})
    solved = solver_payload(hierarchy)
    row = solved['answers'][0]
    if disposition == 'drawing':
        row.update(status='drawing', standard_answer='', solution_steps=[], uncertain=[])
        for item in row['answer_items']:
            item.update(status='drawing', answer='')
    elif disposition == 'invalid_id':
        row['answer_items'][0].pop('slot_id')
    else:
        row.update(status=disposition, reason='该编号并非作答题目')
    async def request(images, prompt, phase, validator, **kwargs):
        if phase == HIERARCHY_PHASE:
            payload = wire
        elif phase == 'template-coarse-locator-v1':
            payload = {'coordinate_space': 'normalized_1000_v1', 'questions': [
                {'question_number': '1', 'bbox': [0, 0, 1000, 500]},
                {'question_number': '2', 'bbox': [0, 500, 1000, 1000]}]}
        else:
            payload = {LOCATOR_PHASE: geometry, one_three.SOLVE_PHASE: solved,
                       one_three.TRANSCRIBE_PHASE: transcription_payload(hierarchy)}[phase]
        return validator(copy.deepcopy(payload))
    monkeypatch.setattr(pipeline, '_request', request)
    result = run_pipeline(source)
    if disposition == 'drawing':
        assert 'error' not in result
        question = result['questions'][0]
        assert question['standard_answer'] == '' and question['answer_slots'] == ['', '', '']
        assert question['answer_status'] == 'ready'
        assert 'drawing_reference' not in question and 'max_flags' not in question
    elif disposition == 'invalid_id':
        assert result['error'] and result['questions'] == []
        assert result['template_ocr_diagnostics']['failed'] is True
    else:
        # An explicit report keeps the draft and blocks it for the teacher.
        assert 'error' not in result
        question = result['questions'][0]
        assert question['max_flags'] == [{'scope': 'question', 'status': disposition,
                                          'reason': '该编号并非作答题目', 'source': 'solver'}]
        assert question['answer_status'] == 'needs_review' and '1' in result['requires_teacher_confirmation']
        assert 'max_flags' not in result['questions'][1]
        from services.template_session import TemplateSession
        session = TemplateSession('max-flags', str(tmp_path), teacher_id=1)
        session.add_page(0, result['questions'])
        session.save_meta()
        restored = TemplateSession.load_meta('max-flags', str(tmp_path))
        assert restored.pages[0]['questions'][0]['max_flags'] == question['max_flags']
        assert 'max_flags' not in restored.pages[0]['questions'][1]


def test_solver_only_drawing_slot_keeps_its_type_while_geometry_awaits_teacher():
    hierarchy = paper_b_hierarchy()
    manifest = one_three.question_contract(hierarchy)
    geometry = locator_payload(hierarchy, kinds=PAPER_B_KINDS)
    solved = solver_payload(hierarchy)
    solved['answers'][0]['answer_items'].append({'slot_id': '1[4]', 'status': 'drawing', 'answer': ''})
    located, answers = max_owned.reconcile_slot_candidates(
        max_owned.receive_locator_candidates(geometry, manifest),
        max_owned.receive_solver_candidates(solved, manifest), manifest)
    question = located['questions'][0]
    assert question['regions'][-1] == [] and question['region_kinds'][-1] == 'drawing'
    assert question['answer_kind'] == 'mixed' and located['needs_review']
    answer = max_owned.bind_answers(located, answers)['answers'][0]
    assert answer['answer_slots'][-1] == ''
    assert not any('1[4]' in reason and '缺少标准答案' in reason for reason in answer['answer_review_reasons'])
