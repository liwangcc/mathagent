import asyncio
# Historical explicit-provider pipeline contracts remain covered here.
# The default authoritative-directory entry is exercised with real generated
# pixels and mocked transport in test_template_directory_pipeline.py.
import json
from pathlib import Path

import pytest
from PIL import Image

import config
from services import template_recognition
from services.template_anchor_crops import build_anchor_crop_plan


def _choice_answer_payload_from_request(messages):
    payload = json.loads(messages[1]["content"])
    answers = []
    for question in payload["questions"]:
        text = question["question_text"]
        if "C. -3" in text or "C.-3" in text:
            result, option = "-3", "C"
        elif "A. 0" in text or "A.0" in text:
            result, option = "0", "A"
        elif "结果为" in text and ("B. 2" in text or "B.2" in text):
            result, option = "2", "B"
        else:
            result, option = "1", "A"
        answers.append({
            "question_number": question["question_number"],
            "choice_result": result,
            "standard_answer": option,
            "conclusion_answer": option,
            "model_answer": f"计算结果为{result}，故选{option}",
        })
    return json.dumps({"answers": answers}, ensure_ascii=False)


def test_template_with_answers_rejects_duplicate_question_numbers(monkeypatch):
    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        return json.dumps({"questions": [
            {
                "question_number": "1",
                "question_text": "x=____",
                "question_type": "fill",
                "standard_answer": "1",
                "answer_bbox": [100, 100, 300, 200],
            },
            {
                "question_number": "1",
                "question_text": "y=____",
                "question_type": "fill",
                "standard_answer": "2",
                "answer_bbox": [100, 300, 300, 400],
            },
        ]})

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(
        template_recognition.recognize_template_with_answers("template.png")
    )

    assert result["questions"] == []
    assert "重复" in result["error"]


def test_blank_template_discards_legacy_answer_bbox_without_discarding_ocr(monkeypatch):
    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        return json.dumps({"questions": [{
            "question_number": "1",
            "question_text": "x=____",
            "question_type": "fill",
            "standard_answer": "1",
            "answer_bbox": [100, 100, 1200, 200],
        }]})

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_blank("template.png"))

    assert "error" not in result
    assert result["questions"] == [{
        "question_number": "1",
        "question_text": "x=____",
        "question_type": "fill",
        "choice_mode": "unknown",
        "standard_answer": "",
        "solution_steps": "",
    }]


def test_blank_template_retries_once_after_semantic_contract_failure(monkeypatch):
    calls = 0

    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"questions": []})
        return json.dumps({"questions": [{
            "question_number": "1",
            "question_text": "x=____",
            "question_type": "fill",
            "standard_answer": "1",
            "answer_bbox": [100, 100, 300, 200],
        }]})

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_blank("template.png"))

    assert calls == 2
    assert [question["question_number"] for question in result["questions"]] == ["1"]


def test_blank_template_accepts_empty_answers_for_separate_reasoning(monkeypatch):
    calls = 0

    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        nonlocal calls
        calls += 1
        content = json.dumps({"questions": [
            {
                "question_number": str(number),
                "question_text": f"第{number}题（ ） A.1 B.2 C.3 D.4",
                "question_type": "choice",
                "choice_mode": "single",
                "standard_answer": "",
            }
            for number in (1, 2)
        ]})
        kwargs["response_validator"](content)
        return content

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_blank("template.png"))

    assert calls == 1
    assert [question["choice_mode"] for question in result["questions"]] == [
        "single", "single",
    ]
    assert [question["standard_answer"] for question in result["questions"]] == [
        "", "",
    ]


def test_template_ocr_maps_raw_choice_answer_to_its_option_label(monkeypatch):
    """A printed raw result like -3 must map to C when C is the matching option."""
    calls = []

    async def fake_call_vision(image_path, prompt, max_tokens=8192, **kwargs):
        calls.append({"image_path": image_path, "prompt": prompt, "max_tokens": max_tokens})
        return json.dumps({"questions": [
            {
                "question_number": "4",
                "question_text": "已知集合 M={a, 2a-1, 2a²-1}，若 1∈M，则 M 中所有元素之和为（ ）\\nA. 3  B. 1  C. -3  D. -1",
                "question_type": "choice",
                "choice_mode": "single",
                "choice_mode": "single",
                "standard_answer": "-3",
                "answer_bbox": [400, 200, 500, 250],
            },
        ]}, ensure_ascii=False)

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_with_answers("electronic-template.png"))

    assert len(calls) == 1
    question = result["questions"][0]
    assert question["question_type"] == "choice"
    assert question["standard_answer"] == "C"
    assert "answer_bbox" not in question
    assert "answer_bbox" not in calls[0]["prompt"]


def test_template_ocr_preserves_explicit_multiple_choice_contract(monkeypatch):
    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        return json.dumps({"questions": [{
            "question_number": "2",
            "question_text": "多选题（ ）\\nA. 1\\nB. 2\\nC. 3\\nD. 4",
            "question_type": "choice",
                "choice_mode": "single",
            "choice_mode": "multiple",
            "standard_answer": "C, A",
            "answer_bbox": [400, 200, 500, 250],
        }]}, ensure_ascii=False)

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_with_answers("multi.png"))

    assert result["questions"][0]["choice_mode"] == "multiple"
    assert result["questions"][0]["standard_answer"] == "AC"


def test_template_choice_mode_unknown_remains_conflict_instead_of_guessing_from_answer_count(monkeypatch):
    calls = []

    async def fake_call_vision(_image_path, _prompt, max_tokens=8192, **kwargs):
        calls.append(max_tokens)
        return json.dumps({"questions": [{
            "question_number": "2",
            "question_text": "题干（ ）\\nA. 1\\nB. 2\\nC. 3\\nD. 4",
            "question_type": "choice",
                "choice_mode": "single",
            "choice_mode": "unknown",
            "standard_answer": "AC",
            "answer_bbox": [400, 200, 500, 250],
        }]}, ensure_ascii=False)

    monkeypatch.setattr(template_recognition, "call_vision", fake_call_vision)

    result = asyncio.run(template_recognition.recognize_template_with_answers("unknown.png"))

    assert len(calls) == 2
    assert result["questions"][0]["choice_mode"] == "unknown"
    assert result["questions"][0]["standard_answer"] == "AC"



def test_blank_template_prompt_uses_chinese_secondary_math_and_transcription_only_contract():
    prompt = template_recognition.TEMPLATE_BLANK_PROMPT
    assert "中国高中数学教材、课程标准和高中考试命题语境" in prompt
    assert "确定性、互异性、无序性" not in prompt
    assert "choice_result" not in prompt
    assert "question_text 只允许忠实照抄图片" in prompt
    assert "只忠实转录题目，不要解题或生成标准答案" in prompt
    assert "choice_mode" in prompt
    assert "无法确定时返回 unknown" in prompt
    assert "中国高中数学教材、课程标准和高中考试命题语境" in template_recognition.TEMPLATE_BATCH_ANSWER_SYSTEM_PROMPT


def _frozen_transcription_layout(*, source_hash: str, width: int, height: int, identities: bool = True):
    columns = []
    for index, (left, right) in enumerate(((0, 500), (500, 1000)), 1):
        column = {
            "column_index": index,
            "x1_1000": left,
            "x2_1000": right,
            "source_width": width,
            "source_height": height,
            "image_sha256": source_hash,
        }
        if identities:
            column.update({
                "column_id": f"column-{index}",
                "column_order": index,
                "layout_revision": 1,
            })
        columns.append(column)
    return {
        "column_layout": {
            "column_count": 2,
            "columns": columns,
            "source_width": width,
            "source_height": height,
            "image_sha256": source_hash,
            "layout_revision": 1,
        }
    }


def test_multi_image_association_rejects_missing_or_conflicting_image_index():
    questions = [
        {"question_number": "1", "image_index": 1},
        {"question_number": "2", "image_index": 2},
    ]
    assert template_recognition._verify_multi_image_association(questions, 2) == [1, 2]
    assert "image_index" not in questions[0]

    with pytest.raises(ValueError, match="缺少 image_index"):
        template_recognition._verify_multi_image_association(
            [{"question_number": "1"}], 2
        )
    with pytest.raises(ValueError, match="超出 1..2"):
        template_recognition._verify_multi_image_association(
            [{"question_number": "1", "image_index": 3}], 2
        )
    with pytest.raises(ValueError, match="阅读顺序颠倒"):
        template_recognition._verify_multi_image_association(
            [
                {"question_number": "1", "image_index": 2},
                {"question_number": "2", "image_index": 1},
            ],
            2,
        )


def test_multi_image_association_requires_explicit_index_and_validates_legacy_id():
    consistent = [
        {"question_number": "1", "image_index": 1, "image_id": "1"},
        {"question_number": "2", "image_index": "2", "image_id": 2},
    ]
    assert template_recognition._verify_multi_image_association(consistent, 2) == [
        1,
        2,
    ]
    assert all("image_index" not in item and "image_id" not in item for item in consistent)

    with pytest.raises(ValueError, match="缺少 image_index") as missing:
        template_recognition._verify_multi_image_association(
            [{"question_number": "1", "image_id": 1}],
            2,
        )
    assert missing.value.image_association == []
    assert missing.value.image_association_diagnostics["conflict"] == {
        "question_number": "1",
        "status": "missing",
        "reported_value": None,
        "parsed_index": None,
        "reported_image_id": 1,
        "parsed_image_id": None,
    }

    conflicting = [
        {"question_number": "1", "image_index": 1, "image_id": 1},
        {"question_number": "2", "image_index": 2, "image_id": 1},
    ]
    with pytest.raises(ValueError, match="image_index=2 .*image_id=1 .*冲突") as conflict:
        template_recognition._verify_multi_image_association(conflicting, 2)
    assert conflict.value.image_association == [1]
    assert conflict.value.image_association_diagnostics["accepted_prefix"] == [1]
    assert conflict.value.image_association_diagnostics["conflict"] == {
        "question_number": "2",
        "status": "conflicting_fields",
        "reported_value": 2,
        "parsed_index": 2,
        "reported_image_id": 1,
        "parsed_image_id": 1,
    }


def test_multi_image_association_validator_is_one_shot_on_field_conflict(monkeypatch):
    calls = []
    content = json.dumps({"questions": [
        {
            "question_number": "1",
            "question_text": "第1题",
            "question_type": "fill",
            "image_index": 1,
            "image_id": 1,
        },
        {
            "question_number": "2",
            "question_text": "第2题",
            "question_type": "fill",
            "image_index": 2,
            "image_id": 1,
        },
    ]}, ensure_ascii=False)

    async def fake_call_vision_images(_paths, _prompt, **kwargs):
        calls.append({
            "max_attempts": kwargs.get("max_attempts"),
            "has_validator": callable(kwargs.get("response_validator")),
        })
        # Let the production validator raise the semantic review error.  This
        # mirrors the scheduler adapter boundary and proves no second paid
        # attempt is made for an association conflict.
        kwargs["response_validator"](content)
        raise AssertionError("semantic validator failure must stop this call")

    monkeypatch.setattr(
        template_recognition,
        "call_vision_images",
        fake_call_vision_images,
    )

    result = asyncio.run(template_recognition._recognize_template_blank_request(
        ["column-1.png", "column-2.png"],
        prompt="multi-image transcription",
        require_image_association=True,
    ))

    assert calls == [{"max_attempts": 1, "has_validator": True}]
    assert result["needs_review"] is True
    assert result["image_association"] == [1]
    conflict = result["image_association_diagnostics"]["conflict"]
    assert conflict["status"] == "conflicting_fields"
    assert conflict["reported_value"] == 2
    assert conflict["reported_image_id"] == 1
    assert [question["question_number"] for question in result["questions"]] == [
        "1",
        "2",
    ]
    assert all(
        "image_index" not in question and "image_id" not in question
        for question in result["questions"]
    )


@pytest.mark.parametrize(
    ("raw_indexes", "error_text", "accepted_prefix"),
    [
        ([1, None], "缺少 image_index", [1]),
        ([1, 3], "超出 1..2", [1]),
        ([2, 1], "阅读顺序颠倒", [2]),
    ],
)
def test_multi_image_association_failure_keeps_reviewable_questions_without_retry(
    monkeypatch,
    raw_indexes,
    error_text,
    accepted_prefix,
):
    calls = 0

    async def fake_call_vision_images(_paths, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        questions = []
        for number, image_index in enumerate(raw_indexes, 1):
            question = {
                "question_number": str(number),
                "question_text": f"第{number}题",
                "question_type": "fill",
            }
            if image_index is not None:
                question["image_index"] = image_index
            questions.append(question)
        return json.dumps({"questions": questions}, ensure_ascii=False)

    monkeypatch.setattr(
        template_recognition,
        "call_vision_images",
        fake_call_vision_images,
    )

    result = asyncio.run(template_recognition._recognize_template_blank_request(
        ["column-1.png", "column-2.png"],
        prompt="multi-image transcription",
        require_image_association=True,
    ))

    assert calls == 1
    assert result["needs_review"] is True
    assert error_text in result["error"]
    assert result["image_association"] == accepted_prefix
    association_diagnostics = result["image_association_diagnostics"]
    assert association_diagnostics["needs_review"] is True
    assert association_diagnostics["conflict"]["status"] in {
        "missing",
        "out_of_range",
        "reversed",
    }
    assert [question["question_number"] for question in result["questions"]] == [
        "1",
        "2",
    ]
    assert all("image_index" not in question for question in result["questions"])


def test_template_answer_batch_retries_once_after_truncated_json(monkeypatch):
    calls = []

    async def fake_max(messages, temperature=0.3, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return '{"answers":[{"question_number":"1","standard_answer":"A"}'
        return json.dumps({"answers": [{
            "question_number": "1",
            "standard_answer": "A",
            "model_answer": "",
        }]})

    monkeypatch.setattr(template_recognition, "call_template_max_text", fake_max)
    result = asyncio.run(template_recognition.generate_template_answers_batch([{
        "question_number": "1",
        "question_text": "1+1等于（ ） A.2 B.3 C.4 D.5",
        "question_type": "choice",
                "choice_mode": "single",
    }]))

    assert result["answers"]["1"]["standard_answer"] == "A"
    assert len(calls) == 2
    assert "上一次输出不是完整可解析的 JSON" in calls[1][-1]["content"]


def test_template_answer_batch_stops_after_one_bounded_retry(monkeypatch):
    calls = 0

    async def fake_max(_messages, temperature=0.3, **kwargs):
        nonlocal calls
        calls += 1
        return '{"answers":['

    monkeypatch.setattr(template_recognition, "call_template_max_text", fake_max)
    result = asyncio.run(template_recognition.generate_template_answers_batch([{
        "question_number": "1",
        "question_text": "x=1",
        "question_type": "fill",
    }]))

    assert result["answers"] == {}
    assert result["error"]
    assert calls == 2
