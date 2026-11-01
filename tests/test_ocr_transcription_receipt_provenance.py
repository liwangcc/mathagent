"""Record the actual OCR transport identity, not a guessed config model."""
import asyncio
import json

import pytest
from PIL import Image

from services import ocr_service as ocr
from services.paper_cache import PaperCache
from tests.test_recognition_zero_readout import _contract, _slot_response


@pytest.mark.parametrize("streamed", [False, True])
def test_flash_receipt_survives_http_parse_and_paper_cache(monkeypatch, tmp_path, streamed):
    response = _slot_response("blank")
    response["questions"][0]["student_slots"][0].update(
        student_answer="PRIVATE_SYNTHETIC_INK", answer_state="visible",
    )
    body = json.dumps(response, ensure_ascii=False)

    class Response:
        status_code = 200
        text = (
            'data: ' + json.dumps({"model": "provider-returned-flash-version", "choices": [{
                "delta": {"content": body}, "finish_reason": "stop",
            }]}) + '\n\ndata: [DONE]\n\n'
        ) if streamed else ""

        def json(self):
            return {"model": "provider-returned-flash-version", "choices": [{
                "message": {"content": body}, "finish_reason": "stop",
            }]}

        def raise_for_status(self):
            return None

    calls = []
    async def post(url, headers, payload, api_key):
        calls.append(payload)
        return Response()

    monkeypatch.setenv("DASHSCOPE_MODEL", "requested-plus-alias")
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(ocr, "_post_vision_request", post)
    image = tmp_path / "evidence.png"
    Image.new("RGB", (40, 90), "white").save(image)
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": _contract()}])
    result = asyncio.run(cache._recognize_answers_only(
        str(image), "page_1", evidence_mode=True,
        evidence_strategy="full_column_difference", evidence_paths=(str(image),),
    ))
    assert "error" not in result, result
    assert len(calls) == 1
    receipt = json.loads(json.dumps(result["_ocr_receipt"]))
    wire = receipt["transport"]
    assert wire["requested_model"] == "qwen3.8-flash"
    assert wire["response_model"] == "provider-returned-flash-version"
    assert wire["route"] == "dashscope"
    assert wire["phase"] == "student_transcription"
    assert wire["finish_reason"] == "stop"
    assert wire["attempts"] == 1
    assert wire["enable_thinking"] is False
    assert wire["reasoning_effort"] == "none"
    assert wire["elapsed_seconds"] >= 0
    assert "PRIVATE_SYNTHETIC_INK" not in json.dumps(receipt)
    assert "Authorization" not in json.dumps(receipt)


def test_missing_provider_model_stays_unknown_not_requested_alias():
    class Response:
        text = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    parsed = ocr._parse_streamed_vision_response(Response())
    assert not parsed.get("model")
