"""Application entry-point parity, without provider calls or private fixtures."""
import asyncio
import copy
import json

import pytest

from services import flash_grading as flash


@pytest.mark.parametrize("detail_level", ["simple", "detailed"])
def test_text_entrypoint_sends_xhigh_with_the_tested_wire_contract(monkeypatch, detail_level):
    captured = []
    questions = [{"question_no": "5[3]", "question_text": "求和", "standard_answer": "2", "student_answer": "2"}]

    async def call(messages, **kwargs):
        wire = flash._payload(messages, kwargs["phase"], **{
            key: value for key, value in kwargs.items()
            if key in {"enable_thinking", "reasoning_effort", "max_tokens", "json_object", "response_schema"}
        })
        captured.append(copy.deepcopy(wire))
        item = {"result": "correct"}
        if detail_level == "detailed":
            item.update(explanation="", hint="", model_answer="", standard_answer="")
        content = json.dumps({"results": {"5[3]": item}})
        kwargs["response_validator"](content)
        return {"content": content, "model": flash.FLASH_MODEL,
                "receipt": {"upstream_attempts": 1}, "usage": {},
                "parameters": {key: value for key, value in wire.items() if key != "messages"}}

    monkeypatch.setattr(flash, "call_flash", call)
    result = asyncio.run(flash.judge_text_paper(questions, detail_level))
    assert len(captured) == 1
    wire = captured[0]
    assert {key: value for key, value in wire.items() if key not in {"messages", "response_format"}} == {
        "model": "qwen3.8-flash", "enable_thinking": True, "reasoning_effort": "xhigh",
        "temperature": 0, "stream": True, "max_tokens": 8000,
    }
    assert wire["response_format"]["type"] == "json_schema"
    assert wire["response_format"]["json_schema"]["strict"] is True
    assert wire["response_format"]["json_schema"]["schema"]["properties"]["results"]["required"] == ["5[3]"]
    assert "thinking_budget" not in wire and "max_completion_tokens" not in wire
    assert result["5[3]"]["grading_receipt"]["parameters"]["reasoning_effort"] == "xhigh"
    assert result["5[3]"]["grading_receipt"]["prompt_version"] == "flash-text-v8"
    # The shared caller also serves template, drawing and classification work.
    assert flash._payload([{"role": "user", "content": "{}"}], "text")["reasoning_effort"] == "medium"
