import asyncio
import hashlib
import json

import pytest
from PIL import Image

from services import template_directory_pipeline as pipeline
from services import flash_grading
from services.teaching_context import make_teaching_context, use_teaching_context
from tests import legacy_template_hierarchy as legacy


def test_flash_hierarchy_prompt_requests_direct_two_level_contract():
    # V4 shares slot identity/count; geometry and kinds remain locator-owned.
    # Snapshot moved 2026-10-01: slot count is now stated as 1..20 to match the
    # frozen AnswerSpec/evidence ceiling (previously receivers capped at 20 but
    # the prompt let the model emit up to 64). Add semantic pins so a future
    # edit cannot silently drop the cap again.
    # Snapshot moved 2026-11-11: question_number is stated as the position on the
    # page (real exam pages made Flash write printed numbers), and a page may open
    # with the unlabelled rest of the previous page's question.
    assert hashlib.sha256(pipeline._HIERARCHY_PROMPT.encode()).hexdigest() == "ffcdb25d6d72a33892be576a93784a4b2daa778b43d614e11ae1e32d979ea561"
    assert "printed_path" in pipeline._HIERARCHY_PROMPT
    assert "每题1至20项" in pipeline._HIERARCHY_PROMPT
    assert "subquestions" not in pipeline._HIERARCHY_PROMPT
    assert "answer_regions" not in pipeline._HIERARCHY_PROMPT
    assert "才建立作图槽位" in pipeline._HIERARCHY_PROMPT
    assert "不明确时不要猜成作图题" in pipeline._HIERARCHY_PROMPT


def test_flash_direct_contract_preserves_order_and_drawing_kind():
    hierarchy = legacy._validate_flash_question_groups({
        "questions": [{
            "question_number": "1", "printed_label": "1", "cue": "",
            "answer_regions": ["drawing", "answer", "answer"],
        }],
        "needs_review": [],
    })
    node = hierarchy["nodes"][0]
    assert hierarchy["region_count_contract"] is True
    assert node["area_kinds"] == ["drawing", "answer", "answer"]
    assert node["answer_slot_count"] == 2
    assert node["drawing_area_count"] == 1
    assert pipeline._region_slot_count(node) == 3
    manifest = json.loads(pipeline._template_manifest(hierarchy))
    group = manifest["question_groups"][0]
    assert group["region_kinds"] == ["drawing", "answer", "answer"]
    assert group["slot_ids"] == ["1[1]", "1[2]", "1[3]"]


def test_live_typed_shape_keeps_five_groups_without_printed_labels():
    # Shape returned by the real Flash request: printed_label is absent, not
    # an assertion that all five printed questions lack labels.
    kinds = [["drawing"] + ["answer"] * 6] + [["answer"] * count for count in (8, 1, 5, 1)]
    hierarchy = legacy._validate_flash_question_groups({"questions": [
        {"question_number": str(index), "answer_regions": regions}
        for index, regions in enumerate(kinds, 1)
    ]})
    assert hierarchy["leaf_ids"] == ["1", "2", "3", "4", "5"]
    assert hierarchy["needs_review"] == []
    assert [node["area_kinds"] for node in hierarchy["nodes"]] == kinds
    manifest = json.loads(pipeline._template_manifest(hierarchy))
    assert [group["answer_region_count"] for group in manifest["question_groups"]] == [7, 8, 1, 5, 1]
    assert manifest["question_groups"][1]["slot_ids"] == [f"2[{i}]" for i in range(1, 9)]


def test_locator_count_mismatch_is_retained_for_teacher_review():
    hierarchy = legacy._validate_flash_question_groups({
        "questions": [{"question_number": "1", "printed_label": "1", "cue": "", "answer_regions": ["answer", "answer", "answer"]}],
        "needs_review": [],
    })
    result = pipeline._validate_locator({
        "coordinate_space": "normalized_1000_v1",
        "questions": [{
            "question_number": "1", "question_type": "fill", "choice_mode": "unknown",
            "requires_drawing": False, "answer_kind": "non_graphic",
            "regions": [[100, 100, 200, 120], [300, 100, 400, 120]],
            "drawing_regions": [], "label_anchor": None,
            "answer_region_count": 3, "slot_ids": [],
        }],
        "needs_review": [],
    }, hierarchy)
    assert result["needs_review"]
    assert "作答框数量" in result["needs_review"][0]


def test_active_flow_rejects_legacy_hierarchy_while_replay_receiver_remains_readable(tmp_path, monkeypatch):
    from tests.test_template_one_three import branch_payloads, run_pipeline
    source = tmp_path / "paper.png"
    Image.new("RGB", (600, 800), "white").save(source)
    nested = {"nodes": [
        {"id": "1", "parent_id": None, "raw_label": "基础", "area_kinds": []},
        {"id": "1.1", "parent_id": "1", "raw_label": "7", "area_kinds": ["answer", "answer"]},
        {"id": "2", "parent_id": None, "raw_label": "提升", "area_kinds": []},
        {"id": "2.1", "parent_id": "2", "raw_label": "12", "area_kinds": ["answer"]}], "needs_review": []}
    hierarchy = legacy._validate_flash_question_groups(nested)
    payloads = branch_payloads(hierarchy)
    phases = []
    async def request(images, prompt, phase, validator, **kwargs):
        phases.append(phase)
        return validator(nested if phase == "template-hierarchy-v1" else payloads[phase])
    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source)
    assert "题目层级合同版本无效" in result["error"]
    assert set(phases) == {"template-hierarchy-v1", "template-coarse-locator-v1"}
    assert result["questions"] == []
    assert hierarchy["leaf_ids"] == ["1", "2"]
    assert hierarchy["nodes"][0]["source_path"] == ["基础", "7"]


def test_flash_slot_count_is_shared_by_locator_and_solver_prompts():
    hierarchy = legacy._validate_hierarchy({
        "nodes": [
            {"id": "1", "parent_id": None, "raw_label": "基础练习", "cue": "栏目", "parts": []},
            {"id": "1.2", "parent_id": "1", "raw_label": "2.", "cue": "八个计算式", "parts": [], "answer_slot_count": 8},
        ],
        "needs_review": [],
    })
    locator, solver = pipeline._parallel_template_prompts(
        hierarchy, width=1000, height=2000,
        columns=[{"column_index": 1, "x1_1000": 0, "x2_1000": 1000}],
    )
    assert '"q_id":"Q1.2","answer_slot_count":8' in locator
    assert '"q_id":"Q1.2","answer_slot_count":8' in solver
    assert '1.2[8]' in locator and '1.2[8]' in solver


def test_locator_directory_keeps_one_parent_with_eight_physical_slots():
    hierarchy = legacy._validate_hierarchy({
        "nodes": [
            {"id": "1", "parent_id": None, "raw_label": "栏目", "cue": "栏目", "parts": []},
            {"id": "1.2", "parent_id": "1", "raw_label": "2.", "cue": "八空", "parts": [], "answer_slot_count": 8},
        ],
        "needs_review": [],
    })
    boxes = [[index * 10, 100, index * 10 + 5, 110] for index in range(1, 9)]
    locator = pipeline._validate_locator({
        "coordinate_space": "normalized_1000_v1",
        "questions": [{"question_number": "1.2", "question_number_bbox": [10, 10, 20, 20],
                        "question_type": "fill", "choice_mode": "unknown", "requires_drawing": False,
                        "answer_kind": "non_graphic", "answer_slot_count": 8,
                        "slot_ids": [f"1.2[{index}]" for index in range(1, 9)],
                        "regions": boxes, "drawing_regions": [], "label_anchor": None}],
        "needs_review": [],
    }, hierarchy)
    directory = pipeline._directory_from_hierarchy(hierarchy, locator)
    assert len(directory["leaves"]) == 1
    assert directory["leaves"][0]["question_number"] == "1.2"
    assert directory["leaves"][0]["answer_slot_count"] == 8
    assert directory["leaves"][0]["slot_ids"][-1] == "1.2[8]"


def test_flash_hierarchy_payload_disables_thinking_without_reasoning_level():
    payload = flash_grading._payload(
        [{"role": "user", "content": [
            {"type": "text", "text": "return JSON"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ]}],
        "template_hierarchy",
        enable_thinking=False,
        reasoning_effort=None,
        max_tokens=6500,
    )
    assert payload["enable_thinking"] is False
    assert "reasoning_effort" not in payload
    assert payload["max_tokens"] == 6500


def test_parallel_pipeline_sends_all_columns_and_two_max_calls(tmp_path, monkeypatch):
    from tests.test_template_one_three import hierarchy_payload, branch_payloads, run_pipeline
    source = tmp_path / "paper.png"
    Image.new("RGB", (1000, 500), "white").save(source)
    from tests.test_template_one_three import v4_payloads
    hierarchy_raw, payloads = v4_payloads((1,), choice=True)
    phases = []
    async def request(images, prompt, phase, validator, **kwargs):
        phases.append((phase, len(images), kwargs.get("max_tokens"), kwargs.get("reasoning_effort")))
        if phase in {"template-directory-v1", "template-coarse-locator-v1"}:
            sizes = []
            for path in images:
                with Image.open(path) as image:
                    sizes.append(image.size)
            assert sizes == [(500, 500), (500, 500)]
        elif phase != "template-hierarchy-v1":
            with Image.open(images[0]) as image:
                assert image.size == (1000, 500)
        if phase == "template-hierarchy-v1":
            return validator(hierarchy_raw)
        if phase == "template-coarse-locator-v1":
            return validator({"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": "1", "bbox": [0, 0, 1000, 1000]}]})
        return validator(payloads[phase])
    monkeypatch.setattr(pipeline, "_request", request)
    result = run_pipeline(source, {"column_layout": {"columns": [
        {"x1_1000": 0, "x2_1000": 500}, {"x1_1000": 500, "x2_1000": 1000}]}})
    assert "error" not in result
    assert {phase for phase, *_ in phases} == {
        "template-hierarchy-v1", "template-coarse-locator-v1",
        "template-directory-v1", "template-max-fullsolve-v1",
        "template-flash-transcribe-v1",
    }
    assert phases.count(("template-hierarchy-v1", 2, 8000, "low")) == 1
    assert phases.count(("template-coarse-locator-v1", 2, 4096, "low")) == 1
    assert phases.count(("template-directory-v1", 2, 32768, "high")) == 1
    assert phases.count(("template-max-fullsolve-v1", 2, 32768, "high")) == 1
    assert phases.count(("template-flash-transcribe-v1", 2, 8000, "high")) == 1
    assert result["questions"][0]["question_text"] == ""
    assert result["template_ocr_diagnostics"]["request_count"] == 5


def count_hierarchy(counts=(2,)):
    return legacy._validate_flash_question_groups({"questions": [
        {"question_number": str(index), "answer_region_count": count}
        for index, count in enumerate(counts, 1)
    ]})


@pytest.mark.parametrize("slot_ids", [None, ["1[2]", "1[1]"], ["1[1]", "1[1]"], ["1[1]"]])
def test_all_branches_reject_changed_region_identity(slot_ids):
    hierarchy = count_hierarchy()
    locator = {"coordinate_space": "normalized_1000_v1", "questions": [{
        "question_number": "1", "question_type": "fill", "choice_mode": "unknown",
        "answer_kind": "non_graphic", "regions": [[10, 10, 20, 20], [30, 10, 40, 20]],
        "slot_ids": slot_ids}], "needs_review": []}
    with pytest.raises(ValueError, match="Flash"):
        pipeline._validate_locator(locator, hierarchy)


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.parametrize("reversed_solver", [False, True])
def test_key_handoff_through_transports_starts_solver_before_transcription_and_routes_types(
        tmp_path, monkeypatch, mismatch, reversed_solver):
    from services import analyzer
    from services import template_one_three as contract
    from services.template_session import TemplateSession
    source = tmp_path / "paper.png"
    Image.new("RGB", (800, 1200), "white").save(source)
    counts = (6, 8, 1, 5, 3)
    from tests.test_template_one_three import v4_payloads
    raw_hierarchy, payloads = v4_payloads(counts)
    if reversed_solver:
        for answer in payloads[contract.SOLVE_PHASE]["answers"]:
            answer["answer_items"].reverse()
    if mismatch:
        payloads["template-directory-v1"]["questions"][0]["regions"].pop()
    captures = {}

    async def run():
        locator_started = asyncio.Event()
        locator_finished = asyncio.Event()
        solver_started = asyncio.Event()

        async def flash(messages, **kwargs):
            assert kwargs["phase"] == "template_hierarchy"
            prompt = messages[0]["content"][0]["text"]
            if prompt == pipeline._HIERARCHY_PROMPT:
                captures["hierarchy"] = kwargs
                assert kwargs["max_tokens"] == 8000
                assert kwargs["enable_thinking"] is True
                assert kwargs["reasoning_effort"] == "low"
                payload = raw_hierarchy
            elif prompt == pipeline._COARSE_LOCATOR_PROMPT:
                captures["coarse"] = kwargs
                assert kwargs["max_tokens"] == 4096
                assert kwargs["enable_thinking"] is True
                assert kwargs["reasoning_effort"] == "low"
                payload = {"coordinate_space": "normalized_1000_v1", "questions": [
                    {"question_number": str(i), "bbox": [0, (i - 1) * 200, 1000, i * 200]}
                    for i in range(1, len(counts) + 1)]}
            else:
                captures["transcription"] = kwargs
                assert kwargs["max_tokens"] == 8000
                assert kwargs["enable_thinking"] is True
                assert kwargs["reasoning_effort"] == "high"
                assert '"q_id":"Q1"' in prompt
                # One choice rule: no parallel note may contradict the base prompt.
                assert pipeline._PARALLEL_TRANSCRIPTION_NOTE in prompt
                assert "选择题的question_text为空字符串" in prompt
                assert "转录每个 q_id" not in prompt
                payload = payloads[contract.TRANSCRIBE_PHASE]
            raw = json.dumps(payload)
            kwargs["response_validator"](raw)
            return {"content": raw, "model": "qwen3.8-flash", "receipt": {}}

        async def max_call(images, prompt, **kwargs):
            assert kwargs["pool_route"] == "fallback"
            assert kwargs["max_tokens"] == 32768
            assert kwargs["reasoning_effort"] == "high"
            expected_count = 1 if "normalized_1000_v1" in prompt else 6
            assert len(images) == expected_count
            with Image.open(images[0]) as image:
                assert image.size == (800, 1200)
            if "normalized_1000_v1" in prompt:
                captures["locator"] = prompt
                locator_started.set()
                await asyncio.wait_for(solver_started.wait(), 2)
                payload = payloads["template-directory-v1"]
                raw = json.dumps(payload)
                kwargs["response_validator"](raw)
                locator_finished.set()
                return raw
            captures["solver"] = prompt
            assert "印刷题干" not in prompt and "<transcription_prompt>" not in prompt
            solver_started.set()
            await asyncio.wait_for(locator_started.wait(), 2)
            raw = json.dumps(payloads[contract.SOLVE_PHASE])
            kwargs["response_validator"](raw)
            return raw

        async def forbidden(*args, **kwargs):
            raise AssertionError("No DeepSeek or real network allowed")
        monkeypatch.setattr(flash_grading, "call_flash", flash)
        monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)
        monkeypatch.setattr(analyzer, "_call_api", forbidden)
        with use_teaching_context(make_teaching_context("primary")):
            return await pipeline.recognize_directory_template(str(source), template_mode="marked", layout_result={})

    result = asyncio.run(run())
    assert set(captures) == {"hierarchy", "coarse", "locator", "solver", "transcription"}
    assert "error" not in result
    questions = result["questions"]
    assert [q["blank_count"] for q in questions] == list(counts)
    assert questions[0]["question_text"] == "印刷题干1"
    if mismatch:
        # A shorter locator proposal no longer aborts generation. The larger
        # solver inventory survives as a draft with the last box unlocated.
        assert result["requires_teacher_confirmation"]
        assert questions[0]["answer_bboxes"][-1] == []
        assert len(result["template_answer_regions"][0]["regions"]) == counts[0] - 1
    else:
        assert result["requires_teacher_confirmation"] == []
    session = TemplateSession("handoff", str(tmp_path), teacher_id=1)
    session.add_page(0, questions)
    session.question_directories["page_1"] = result["question_directory"]
    session.save_meta()
    saved = TemplateSession.load_meta("handoff", str(tmp_path))
    for i, question in enumerate(saved.pages[0]["questions"]):
        assert question["answer_slots"] == questions[i]["answer_slots"]
        assert question["slot_ids"] == questions[i]["slot_ids"]
        assert saved.question_directories["page_1"]["leaves"][i]["slot_ids"] == question["slot_ids"]
