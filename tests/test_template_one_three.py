"""Offline 1 -> 3 production contract; transports are always intercepted."""
import asyncio
import copy
import json

import pytest
from PIL import Image

from services import template_directory_pipeline as pipeline
from services import template_one_three as contract
from services.teaching_context import make_teaching_context, use_teaching_context
from tests import legacy_template_hierarchy as legacy


def hierarchy_payload(counts=(2,)):
    return {"questions": [{"question_number": str(i), "answer_regions": ["answer"] * n}
                          for i, n in enumerate(counts, 1)]}


def branch_payloads(hierarchy, choice=False):
    located, answers, texts = [], [], []
    for expected in contract.slot_contract(hierarchy):
        qid = expected["q_id"]
        number = qid[1:]
        slots = expected["slots"]
        regions = [[30 + i * 50, 100, 60 + i * 50, 130] for i in range(len(slots))]
        drawing = [box for box, slot in zip(regions, slots) if slot["answer_type"] == "drawing"]
        located.append({"question_number": number, "question_type": "choice" if choice else "fill",
                        "choice_mode": "single" if choice else "unknown", "requires_drawing": bool(drawing),
                        "answer_kind": "drawing" if len(drawing) == len(slots) else "mixed" if drawing else "non_graphic",
                        "regions": regions, "slot_ids": [s["slot_id"] for s in slots],
                        "drawing_regions": drawing, "label_anchor": [10, 100] if drawing else None})
        answers.append({"q_id": qid, "standard_answer": "A" if choice else "答案",
                        "answer_slots": [{"slot_id": s["slot_id"], "answer": "" if s["answer_type"] == "drawing" else "A" if choice else f"答案{i}"}
                                         for i, s in enumerate(slots, 1)],
                        "solution_steps": [], "visual_facts": ["原图事实"], "uncertain": []})
        if not choice:
            texts.append({"q_id": qid, "question_text": "印刷题干" + number, "uncertain": []})
    return {"template-directory-v1": {"coordinate_space": "normalized_1000_v1", "questions": located, "needs_review": []},
            contract.SOLVE_PHASE: {"answers": answers}, contract.TRANSCRIBE_PHASE: {"questions": texts}}


def v3_payloads(counts=(2,), choice=False):
    """Physical counts are test geometry, not fields in the Flash payload."""
    raw = {"contract_version": 3, "questions": [
        {"question_number": str(i), "printed_path": [f"{i}."]}
        for i in range(1, len(counts) + 1)]}
    located, answers, texts = [], [], []
    for number, count in enumerate(counts, 1):
        keys = [{"part": "", "anchor": "印刷填写□" if count > 1 else "", "blank_index": i + 1 if count > 1 else 0} for i in range(count)]
        located.append({"question_number": str(number), "question_number_bbox": [5, 90, 20, 105],
                        "question_type": "choice" if choice else "fill", "choice_mode": "single" if choice else "unknown",
                        "regions": [{"kind": "answer", "bbox": [30 + i * 50, 100, 60 + i * 50, 130], "answer_key": key}
                                    for i, key in enumerate(keys)], "label_anchor": None})
        answers.append({"q_id": f"Q{number}", "standard_answer": "A" if choice else "答案",
                        "answer_items": [{"answer_key": key, "answer": "A" if choice else f"答案{i}"} for i, key in enumerate(keys, 1)],
                        "solution_steps": [], "visual_facts": [], "uncertain": []})
        texts.append({"q_id": f"Q{number}", "question_text": "" if choice else f"印刷题干{number}", "uncertain": []})
    return raw, {"template-directory-v1": {"coordinate_space": "normalized_1000_v1", "questions": located, "needs_review": []},
                 contract.SOLVE_PHASE: {"answers": answers}, contract.TRANSCRIBE_PHASE: {"questions": texts}}


def v4_payloads(counts=(2,), choice=False):
    """Shared IDs come from the Flash inventory, not independent text keys."""
    raw, branches = v3_payloads(counts, choice)
    raw["contract_version"] = 4
    for question, located, answer in zip(raw["questions"], branches["template-directory-v1"]["questions"], branches[contract.SOLVE_PHASE]["answers"]):
        number = question["question_number"]
        ids = [f"{number}[{i}]" for i in range(1, len(located["regions"]) + 1)]
        question["slots"] = [{"slot_id": sid, "cue": f"第{i}个填写位置"} for i, sid in enumerate(ids, 1)]
        for sid, region, item in zip(ids, located["regions"], answer["answer_items"]):
            region.pop("answer_key"); item.pop("answer_key")
            region["slot_id"] = item["slot_id"] = sid
    return raw, branches


def test_v3_choice_standard_uses_bound_letter_not_explanatory_summary():
    raw, payloads = v3_payloads(counts=(1,), choice=True)
    hierarchy = contract.receive_hierarchy(raw)
    locator = pipeline._validate_one_three_locator(payloads["template-directory-v1"], hierarchy)
    payloads[contract.SOLVE_PHASE]["answers"][0]["standard_answer"] = "C，即(5,4)，因为后方一行的行号增加1"
    payloads[contract.SOLVE_PHASE]["answers"][0]["answer_items"][0]["answer"] = "C"
    payloads[contract.SOLVE_PHASE]["answers"][0]["solution_steps"] = ["后方一行，列号不变，行号加1；对应原选项C。"]
    solver = contract.receive_solver(payloads[contract.SOLVE_PHASE], hierarchy)
    original = copy.deepcopy(solver)
    text = contract.receive_transcription(payloads[contract.TRANSCRIBE_PHASE], hierarchy)
    answer = contract.merge_branches(locator, solver, text)["answers"][0]
    assert answer["standard_answer"] == "C"
    assert answer["answer_slots"] == ["C"]
    assert answer["solution_steps"] == original["answers"][0]["solution_steps"]
    assert solver == original


def run_pipeline(source, layout=None):
    async def run():
        with use_teaching_context(make_teaching_context("primary")):
            return await pipeline.recognize_directory_template(str(source), template_mode="marked", layout_result=layout or {})
    return asyncio.run(run())


@pytest.mark.parametrize("phase", ["template-directory-v1", contract.SOLVE_PHASE, contract.TRANSCRIBE_PHASE])
@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "invalid", "truncated"])
def test_every_branch_rejects_unbindable_or_malformed_results(tmp_path, monkeypatch, phase, fault):
    source = tmp_path / "page.png"
    Image.new("RGB", (300, 400), "white").save(source)
    raw_hierarchy, payloads = v4_payloads()
    bad = payloads[phase]
    key = "answers" if phase == contract.SOLVE_PHASE else "questions"
    if fault == "missing":
        bad[key] = []
    elif fault == "duplicate":
        bad[key].append(copy.deepcopy(bad[key][0]))
    elif fault == "unknown":
        bad[key][0]["q_id" if key == "answers" or phase == contract.TRANSCRIBE_PHASE else "question_number"] = "Q999"
    elif fault == "invalid":
        bad[key][0]["unexpected"] = True

    async def request(images, prompt, requested_phase, validator, **kwargs):
        if requested_phase == "template-hierarchy-v1":
            return validator(raw_hierarchy)
        raw = json.dumps(payloads[requested_phase])
        if requested_phase == phase and fault == "truncated":
            raw = raw[:-1]
        return validator(contract.parse_json(raw))

    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source)
    assert result["questions"] == []
    assert "error" in result
    assert result["template_ocr_diagnostics"]["failed"] is True
    assert result["template_ocr_diagnostics"]["stage"] == "locate_transcribe_solve"


@pytest.mark.parametrize("raw", ['{"answers":[],}', '{"answers":[],"answers":[]}', '{"x":NaN}', 'prefix {"answers":[]}'])
def test_no_json_repair(raw):
    with pytest.raises(ValueError):
        contract.parse_json(raw)


def test_typed_drawing_reordered_answers_and_local_uncertainty():
    hierarchy = legacy._validate_flash_question_groups({"questions": [
        {"question_number": "1", "answer_regions": ["drawing", "answer", "answer"]},
        {"question_number": "2", "answer_regions": ["answer"]}]})
    payloads = branch_payloads(hierarchy)
    payloads[contract.SOLVE_PHASE]["answers"][0]["answer_slots"].reverse()
    payloads[contract.SOLVE_PHASE]["answers"][0]["uncertain"] = ["图中标记不清"]
    located = pipeline._validate_one_three_locator(payloads["template-directory-v1"], hierarchy)
    answers = contract.receive_solver(payloads[contract.SOLVE_PHASE], hierarchy)
    merged = contract.merge_branches(located, answers, contract.receive_transcription(payloads[contract.TRANSCRIBE_PHASE], hierarchy))
    assert merged["answers"][0]["answer_slots"] == ["", "答案2", "答案3"]
    assert merged["answers"][0]["answer_status"] == "needs_review"
    assert merged["answers"][1]["answer_status"] == "ready"
    assert located["questions"][0]["region_kinds"] == ["drawing", "answer", "answer"]
    payloads[contract.SOLVE_PHASE]["answers"][0]["answer_slots"][-1]["answer"] = "not a drawing blank"
    with pytest.raises(ValueError, match="drawing"):
        contract.receive_solver(payloads[contract.SOLVE_PHASE], hierarchy)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown"])
def test_solver_slot_identity_is_exact(fault):
    hierarchy = legacy._validate_flash_question_groups(hierarchy_payload((3,)))
    payload = branch_payloads(hierarchy)[contract.SOLVE_PHASE]
    slots = payload["answers"][0]["answer_slots"]
    if fault == "missing":
        slots.pop(1)
    elif fault == "duplicate":
        slots[1]["slot_id"] = slots[0]["slot_id"]
    else:
        slots[1]["slot_id"] = "2[1]"
    with pytest.raises(ValueError):
        contract.receive_solver(payload, hierarchy)


def test_blank_middle_and_multiline_are_preserved():
    hierarchy = legacy._validate_flash_question_groups(hierarchy_payload((3,)))
    payload = branch_payloads(hierarchy)[contract.SOLVE_PHASE]
    for slot, value in zip(payload["answers"][0]["answer_slots"], ["x=1\ny=2", "", "x=1\ny=2"]):
        slot["answer"] = value
    result = contract.receive_solver(payload, hierarchy)["answers"][0]
    assert result["answer_slots"] == ["x=1\ny=2", "", "x=1\ny=2"]
    assert result["answer_status"] == "needs_review"


@pytest.mark.parametrize("field,value", [("answer_region_count", 99), ("answer_region_count", True), ("answer_slot_count", True)])
def test_locator_rejects_invalid_declared_counts(field, value):
    hierarchy = legacy._validate_flash_question_groups(hierarchy_payload((1,)))
    payload = branch_payloads(hierarchy)["template-directory-v1"]
    payload["questions"][0][field] = value
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(payload, hierarchy)


def test_drawing_locator_requires_complete_label_geometry():
    hierarchy = legacy._validate_flash_question_groups({"questions": [
        {"question_number": "1", "answer_regions": ["drawing"]}]})
    payload = branch_payloads(hierarchy)["template-directory-v1"]
    payload["questions"][0]["label_anchor"] = None
    with pytest.raises(ValueError):
        pipeline._validate_one_three_locator(payload, hierarchy)


def test_choice_skips_transcription_without_fake_question_text():
    hierarchy = legacy._validate_flash_question_groups(hierarchy_payload((1,)))
    payloads = branch_payloads(hierarchy, choice=True)
    locator = pipeline._validate_one_three_locator(payloads["template-directory-v1"], hierarchy)
    solver = contract.receive_solver(payloads[contract.SOLVE_PHASE], hierarchy)
    result = contract.merge_branches(locator, solver, {"questions": []})
    assert result["answers"][0]["question_text"] == ""
    assert result["answers"][0]["answer_status"] == "ready"
    merged = contract.merge_branches(locator, solver, {"questions": [{"q_id": "Q1", "question_text": "fake", "uncertain": []}]})
    assert merged["answers"][0]["question_text"] == ""
    assert merged["answers"][0]["answer_status"] == "needs_review"
