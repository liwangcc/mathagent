"""No-network output envelope, prompt/receiver and private evidence regressions."""
import asyncio
import copy
import hashlib
import json
import os
import time

import httpx
import pytest
from PIL import Image

from services import flash_grading as flash, ocr_service as ocr
from services import template_directory_pipeline as pipeline, template_one_three as contract
from services import template_max_owned as max_owned, template_output_evidence as evidence
from services.key_pool import KeyPool
from services.processing_errors import processing_context, report_failure
from tests.test_flash_grading import Limiter, _event, _response
from tests.test_template_one_three import v4_payloads, run_pipeline
from services import ocr_service_template_route


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "EVIDENCE_DIR", tmp_path / "private")
    monkeypatch.setattr(ocr, "_vision_pool", KeyPool(["test-only-output-key"], max_concurrent_per_key=None))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-output-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://unit.invalid/v1")
    monkeypatch.setattr(ocr, "get_model_limiter", lambda: Limiter())
    monkeypatch.setattr(flash, "_credential_failures", {})
    monkeypatch.setattr(flash, "_semaphore", None)
    monkeypatch.setattr(flash, "_semaphore_loop", None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("unmocked paid transport")

    monkeypatch.setattr(ocr, "_post_vision_request", forbidden)
    monkeypatch.setattr(pipeline, "call_gpt_vision", forbidden)


def hierarchy():
    raw, _ = v4_payloads(counts=(1, 1))
    return contract.receive_hierarchy(raw, version=4)


def text_payload():
    return {"questions": [{"q_id": "Q2", "question_text": "保留原式2×3=（）", "uncertain": []},
                          {"q_id": "Q1", "question_text": "1+1=（）", "uncertain": []}]}


@pytest.mark.parametrize("shape", ["object", "rows", "singleton"])
@pytest.mark.parametrize("wire", ["sse", "json"])
def test_transcription_envelope_before_flash_object_gate(shape, wire):
    h, payload = hierarchy(), text_payload()
    raw = payload if shape == "object" else payload["questions"] if shape == "rows" else [payload]
    content = json.dumps(raw, ensure_ascii=False)
    response = (_response([_event(content, finish="stop", reasoning="PRIVATE_REASONING")]) if wire == "sse"
                else httpx.Response(200, json={"model": flash.FLASH_MODEL, "choices": [{"index": 0,
                    "message": {"content": content}, "finish_reason": "stop"}]}))
    result = flash.parse_flash_response(response, output_adapter=lambda value: contract.adapt_transcription_payload(value, h))
    assert contract.receive_transcription(contract.parse_json(result["content"]), h) == payload
    assert "PRIVATE_REASONING" not in str(result)
    if shape != "object":
        with pytest.raises(flash.FlashGradingError):
            flash.parse_flash_response(response)  # default grading contracts are unchanged


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "extra_field", "missing_field", "wrong_type", "numeric_id", "scalar", "truncated", "duplicate_key", "nonfinite", "prose"])
def test_adapter_does_not_repair_or_guess(fault):
    h, payload = hierarchy(), text_payload()["questions"]
    if fault == "missing":
        payload.pop()
    elif fault == "duplicate":
        payload.append(copy.deepcopy(payload[0]))
    elif fault == "unknown":
        payload[0]["q_id"] = "Q999"
    elif fault == "extra_field":
        payload[0]["answer"] = "cannot import answers as transcription"
    elif fault == "missing_field":
        payload[0].pop("uncertain")
    elif fault == "wrong_type":
        payload[0]["uncertain"] = "not an array"
    elif fault == "numeric_id":
        payload[0]["q_id"] = 2
    elif fault == "scalar":
        payload = "not a list"
    text = json.dumps(payload, ensure_ascii=False)
    if fault == "truncated":
        text = text[:-1]
    elif fault == "duplicate_key":
        text = '[{"q_id":"Q1","q_id":"Q2","question_text":"x","uncertain":[]}]'
    elif fault == "nonfinite":
        text = '[{"q_id":"Q1","question_text":NaN,"uncertain":[]}]'
    elif fault == "prose":
        text = "说明：" + text
    with pytest.raises(flash.FlashGradingError):
        flash.parse_flash_response(_response([_event(text, finish="stop")]),
                                  output_adapter=lambda value: contract.adapt_transcription_payload(value, h))


def output_example(prompt):
    # The declared JSON example is the first object after the last output instruction.
    suffix = prompt.split("\n")[-2:] if "\n作图项" in prompt else [prompt.split("\n")[-1]]
    for line in suffix:
        if "返回" in line or "完整响应" in line:
            return json.JSONDecoder().raw_decode(line[line.index("{"):])[0]
    raise AssertionError("output instruction missing")


def test_every_active_prompt_example_passes_its_real_receiver():
    raw = output_example(pipeline._HIERARCHY_PROMPT)
    h = contract.receive_hierarchy(raw, version=4)
    assert len(h["nodes"][0]["slots"]) == 2
    coarse = output_example(pipeline._COARSE_LOCATOR_PROMPT)
    pipeline._receive_coarse_locator(coarse, width=1000, height=1000)
    one_raw, _ = v4_payloads(counts=(1,))
    h = contract.receive_hierarchy(one_raw, version=4)
    manifest = contract.question_contract(h)
    loc = max_owned.locator_prompt(manifest, width=1000, height=1000, columns=[])
    solve, text = contract.build_prompts(h, education_stage="primary")
    received_loc = max_owned.receive_locator(output_example(loc), manifest)
    received_solve = contract.receive_solver(output_example(solve), h)
    received_text = contract.receive_transcription(output_example(text), h)
    assert contract.merge_branches(received_loc, received_solve, received_text)["answers"][0]["answer_slots"] == ["答案"]


def test_slot_capacity_is_aligned_without_truncation():
    raw, branches = v4_payloads(counts=(20,))
    # Use valid in-page geometry at the capacity boundary.
    for i, region in enumerate(branches["template-directory-v1"]["questions"][0]["regions"]):
        region["bbox"] = [20 + i * 40, 100, 40 + i * 40, 130]
    h = contract.receive_hierarchy(raw, version=4)
    manifest = contract.question_contract(h)
    loc = max_owned.receive_locator(branches["template-directory-v1"], manifest)
    solved = contract.receive_solver(branches[contract.SOLVE_PHASE], h)
    merged = contract.merge_branches(loc, solved, contract.receive_transcription(branches[contract.TRANSCRIBE_PHASE], h))
    assert len(merged["answers"][0]["answer_slots"]) == 20
    from services.grading_contract import AnswerSpec
    from pydantic import ValidationError
    assert AnswerSpec(question_no="1", blank_count=20).blank_count == 20
    with pytest.raises(ValidationError):
        AnswerSpec(question_no="1", blank_count=21)
    assert contract.MAX_SLOTS_PER_QUESTION == max_owned.MAX_SLOTS_PER_QUESTION == 20
    raw, branches = v4_payloads(counts=(21,))
    with pytest.raises(ValueError):
        contract.receive_hierarchy(raw, version=4)
    too_large_manifest = [{"q_id": "Q1", "printed_path": ["1."], "slots": raw["questions"][0]["slots"]}]
    with pytest.raises(ValueError):
        max_owned.receive_locator(branches["template-directory-v1"], too_large_manifest)
    with pytest.raises(ValueError):
        max_owned.receive_solver(branches[contract.SOLVE_PHASE], too_large_manifest)


@pytest.mark.parametrize("shape", ["rows", "singleton"])
def test_real_pipeline_to_transport_to_merge_has_five_calls_no_retry(shape, monkeypatch, tmp_path):
    source = tmp_path / "page.png"
    Image.new("RGB", (300, 400), "white").save(source)
    raw, branches = v4_payloads(counts=(1, 1))
    calls = []

    async def post(url, headers, payload, api_key):
        calls.append(("flash", copy.deepcopy(payload)))
        prompt = payload["messages"][0]["content"][0]["text"]
        if "建立题目与作答位清单" in prompt:
            result = raw
        elif "粗定位每道独立题" in prompt:
            result = {"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": str(i), "bbox": [10, 10, 900, 900]} for i in (1, 2)]}
        else:
            result = branches[contract.TRANSCRIBE_PHASE]
            result = result["questions"] if shape == "rows" else [result]
        return _response([_event(json.dumps(result, ensure_ascii=False), finish="stop")])

    async def vision(images, prompt, **kwargs):
        phase = ocr._vision_phase_key.get()
        calls.append((phase, copy.deepcopy(kwargs)))
        text = json.dumps(branches[phase], ensure_ascii=False)
        kwargs["response_validator"](text)
        return text

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    monkeypatch.setattr(pipeline, "call_gpt_vision", vision)
    result = run_pipeline(source)
    assert "error" not in result and len(result["questions"]) == 2
    assert [q["question_text"] for q in result["questions"]] == ["印刷题干1", "印刷题干2"]
    assert [q["slot_ids"] for q in result["questions"]] == [["1[1]"], ["2[1]"]]
    assert len(calls) == 5 and sum(name == "flash" for name, _ in calls) == 3
    for name, payload in calls:
        if name == "flash":
            assert payload["response_format"] == {"type": "json_object"}
            assert payload["enable_thinking"] is True
        else:
            assert payload["json_object"] is True and payload["reasoning_effort"] == "high"
    # Serialization does not strip adapted text or identity before persistence.
    saved = tmp_path / "roundtrip.json"
    saved.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    restored = json.loads(saved.read_text(encoding="utf-8"))
    assert restored["questions"] == result["questions"]
    assert not evidence.EVIDENCE_DIR.exists()  # successes never retain raw model output


def test_failed_raw_output_private_receipt_and_public_log_are_connected(monkeypatch, tmp_path, caplog):
    h = hierarchy()
    raw = '[{"q_id":"Q1","question_text":"PRIVATE_PRINTED_TEXT","uncertain":[]}]'
    calls = []

    async def post(*args, **kwargs):
        calls.append(1)
        return _response([_event(raw, finish="stop", reasoning="PRIVATE_REASONING")])

    monkeypatch.setattr(ocr, "_post_vision_request", post)
    monkeypatch.setattr(
        ocr_service_template_route,
        "_encode_vision_image",
        lambda _: ("iVBORw0KGgo=", "image/png"),
    )
    monkeypatch.setattr(ocr, "_encode_vision_image", lambda _: ("iVBORw0KGgo=", "image/png"))
    caplog.set_level("INFO", logger="mathagent.processing")
    with processing_context() as cid:
        with pytest.raises(flash.FlashGradingError) as caught:
            asyncio.run(pipeline._request(
                ["not-read.png"], "PRIVATE_PROMPT", contract.TRANSCRIBE_PHASE,
                lambda value: contract.receive_transcription(value, h),
                output_adapter=lambda value: contract.adapt_transcription_payload(value, h),
                expected_ids=("Q1", "Q2"),
            ))
        info = report_failure(caught.value, stage="template")
    packet = json.loads((evidence.EVIDENCE_DIR / (info["error_id"] + ".json")).read_text(encoding="utf-8"))
    assert packet["content"] == raw and packet["correlation_id"] == cid
    assert packet["phase"] == contract.TRANSCRIBE_PHASE
    assert packet["analysis"]["missing_ids"] == ["Q2"]
    assert packet["content_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert packet["prompt_sha256"] == hashlib.sha256(b"PRIVATE_PROMPT").hexdigest()
    assert "PRIVATE_PROMPT" not in json.dumps(packet) and "PRIVATE_REASONING" not in json.dumps(packet)
    events = [json.loads(r.message) for r in caplog.records if r.name == "mathagent.processing"]
    event = next(e for e in events if e["event"] == "processing.failed")
    assert event["artifact_refs"] == [info["error_id"]] and event["phase"] == contract.TRANSCRIBE_PHASE
    assert "PRIVATE_PRINTED_TEXT" not in json.dumps(events) and "PRIVATE_PRINTED_TEXT" not in json.dumps(info)
    assert calls == [1]
    if os.name != "nt":
        assert evidence.EVIDENCE_DIR.stat().st_mode & 0o777 == 0o700
        assert (evidence.EVIDENCE_DIR / (info["error_id"] + ".json")).stat().st_mode & 0o777 == 0o600


def test_private_evidence_bound_redaction_and_io_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "MAX_RECEIPTS", 2)
    monkeypatch.setattr(evidence, "MAX_CONTENT_BYTES", 200)
    for index in (1, 2, 3):
        info = {"error_id": f"{index:032x}", "correlation_id": "a" * 32}
        raw = "sk-shouldneverbesaved " + "x" * 300
        ref = evidence.save_output_failure(ValueError("contract"), error_info=info, phase=contract.TRANSCRIBE_PHASE,
            model=flash.FLASH_MODEL, route="payg", prompt="p", content=raw)
        assert ref == info["error_id"]
    paths = list(evidence.EVIDENCE_DIR.glob("*.json"))
    assert len(paths) == 2
    packet = json.loads(paths[-1].read_text(encoding="utf-8"))
    assert packet["content_truncated"] is True and "shouldneverbesaved" not in packet["content"]
    for path in paths:
        os.utime(path, (time.time() - evidence.RETENTION_SECONDS - 1,) * 2)
    info = {"error_id": "b" * 32, "correlation_id": "a" * 32}
    evidence.save_output_failure(ValueError("contract"), error_info=info, phase=contract.TRANSCRIBE_PHASE,
        model=flash.FLASH_MODEL, route="payg", prompt="p", content="{}")
    assert len(list(evidence.EVIDENCE_DIR.glob("*.json"))) == 1
    blocked = tmp_path / "file-not-directory"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setattr(evidence, "EVIDENCE_DIR", blocked)
    assert evidence.save_output_failure(ValueError("original"), error_info=info, phase=contract.TRANSCRIBE_PHASE,
        model=flash.FLASH_MODEL, route="payg", prompt="p", content="{}") is None
    assert evidence.save_output_failure(ValueError("original"), error_info=info, phase=contract.TRANSCRIBE_PHASE,
        model=flash.FLASH_MODEL, route="payg", prompt="p", content="\ud800") is None
