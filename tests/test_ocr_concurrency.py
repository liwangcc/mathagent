"""OCR 每老师并发限制 + grade.py 基准建立后并发调度的回归测试。

锁定两件事：
  1. get_ocr_teacher_semaphore：同一老师跨调用共享同一信号量（进程内每老师上限），
     不同老师互不影响，空 teacher_id 不共享。
  2. recognize_paper 的 Vision 调用受每老师上限约束：同一老师并发发起 N(>limit) 个，
     同时在途数不超过 limit；不同老师各自独立。
"""

import asyncio
import os
import threading
import time

import httpx
import pytest
from PIL import Image
from services import ocr_service_local_image
from services import ocr_service_template_route
from services import ocr_service_papers


async def _qwen_post_ok(url, _headers, _payload, _api_key):
    request = httpx.Request("POST", url)
    return httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
    )


@pytest.fixture(autouse=True)
def _reset_ocr_semaphores():
    """每个用例前清空模块级信号量字典，避免跨用例串扰。"""
    import services.key_pool as kp
    kp._ocr_teacher_semaphores.clear()
    yield
    kp._ocr_teacher_semaphores.clear()


def test_same_teacher_shares_semaphore():
    from services.key_pool import get_ocr_teacher_semaphore

    async def _run():
        a = await get_ocr_teacher_semaphore("t1", 30)
        b = await get_ocr_teacher_semaphore("t1", 30)
        c = await get_ocr_teacher_semaphore("t2", 30)
        return a, b, c

    a, b, c = asyncio.run(_run())
    assert a is b          # 同老师复用同一信号量对象
    assert a is not c      # 不同老师不同信号量


def test_empty_teacher_returns_unshared():
    from services.key_pool import get_ocr_teacher_semaphore

    async def _run():
        return await get_ocr_teacher_semaphore("", 30), await get_ocr_teacher_semaphore("", 30)

    a, b = asyncio.run(_run())
    assert a is not b      # 空 id 每次新建，不参与共享


def test_full_ocr_prompt_locks_model_to_visual_transcription(monkeypatch):
    import services.ocr_service as ocr

    captured = {}

    async def _fake_call_vision(_image_path, prompt, max_tokens=8192):
        captured["prompt"] = prompt
        return '{"questions": [{"question_number": "1", "question_text": "x", "student_answer": "", "answer_state": "blank", "question_type": "fill"}], "student_name": "", "student_no": ""}'

    monkeypatch.setattr(ocr, "call_vision", _fake_call_vision)
    result = asyncio.run(ocr._recognize_paper_preprocessed("paper.jpg"))

    assert result["questions"]
    assert "中国高中数学试卷 OCR" in captured["prompt"]
    assert "不解题、不判分" in captured["prompt"]
    assert "空白返回 blank" in captured["prompt"]
    assert "student_answer 只抄学生实际写下的内容" in captured["prompt"]
    assert "不要根据题干猜答案" in captured["prompt"]
    assert "差分后的学生试卷" in captured["prompt"]
    assert "question_text 只能抄图片中仍然可见的题干文字" in captured["prompt"]
    assert "绝不重建、补全、猜测或改写被差分去除的题干" in captured["prompt"]
    assert "不纠正、不补全、不改写、不合并、不移动" in captured["prompt"]
    assert "answer_state=unreadable" in captured["prompt"]
    assert "answer_state" in captured["prompt"]


def test_visual_transcription_contract_rejects_unknown_states():
    import services.ocr_service as ocr

    with pytest.raises(ValueError):
        ocr.validate_visual_transcriptions([{
            "question_number": "1",
            "student_answer": "A",
            "answer_state": "unknown",
        }])


@pytest.mark.parametrize("state,answer", [
    ("blank", "A"),
    ("visible", ""),
    ("visible", "[无法识别]"),
])
def test_visual_transcription_contradiction_fails_closed_for_that_question(state, answer):
    import services.ocr_service as ocr

    questions = ocr.validate_visual_transcriptions([
        {"question_number": "1", "student_answer": answer, "answer_state": state},
        {"question_number": "2", "student_answer": " B ", "answer_state": "visible"},
    ])

    assert questions[0]["answer_state"] == "unreadable"
    assert questions[0]["student_answer"] == "[无法识别]"
    assert questions[1]["answer_state"] == "visible"
    assert questions[1]["student_answer"] == "B"


def test_visual_transcription_contract_accepts_conservative_states():
    import services.ocr_service as ocr

    questions = ocr.validate_visual_transcriptions([
        {"question_number": "1", "student_answer": "", "answer_state": "blank"},
        {"question_number": "2", "student_answer": " A ", "answer_state": "visible"},
        {"question_number": "3", "student_answer": "[无法识别]", "answer_state": "unreadable"},
    ])

    assert [item["student_answer"] for item in questions] == ["", "A", "[无法识别]"]


def test_visual_transcription_contract_canonicalizes_qualified_unreadable_marker():
    import services.ocr_service as ocr

    questions = ocr.validate_visual_transcriptions([{
        "question_number": "1",
        "student_answer": "[手写汉字，无法识别]",
        "answer_state": "unreadable",
    }])

    assert questions[0]["student_answer"] == "[无法识别]"


@pytest.mark.parametrize("answer", ["", "A"])
def test_visual_transcription_contract_discards_text_on_unreadable_state(answer):
    import services.ocr_service as ocr

    questions = ocr.validate_visual_transcriptions([{
        "question_number": "1",
        "student_answer": answer,
        "answer_state": "unreadable",
    }])

    assert questions[0]["student_answer"] == "[无法识别]"


def test_answer_sheet_transcription_prompt_uses_validator_unreadable_marker(monkeypatch):
    import services.answer_sheet_acceleration as acceleration
    import services.ocr_service as ocr

    captured = {}

    async def fake_call_vision(_image_path, prompt, max_tokens=8192):
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return (
            '{"questions":[{"question_number":"I001",'
            '"student_answer":"[无法识别]",'
            '"answer_state":"unreadable","confidence":0.2}]}'
        )

    monkeypatch.setattr(ocr, "call_vision", fake_call_vision)
    result = asyncio.run(acceleration.transcribe_answer_sheet_evidence(
        "evidence.png",
        [{
            "question_number": "I001",
            "kind": "identity",
            "identity_type": "name",
            "image_index": 1,
            "answer_bbox_1000": [0, 0, 1000, 1000],
        }],
        teacher_id=1,
    ))

    assert "student_answer" in captured["prompt"]
    assert "[unable to recognize]" not in captured["prompt"]
    assert "中国高中数学答题区域 OCR" in captured["prompt"]
    assert "空白返回 blank" in captured["prompt"]
    assert "看不清返回 unreadable" in captured["prompt"]
    normalized_prompt = " ".join(captured["prompt"].lower().split())
    assert "student_transcription_only" not in normalized_prompt
    assert '"image_index":1' in captured["prompt"]
    assert '"answer_bbox_1000":[0,0,1000,1000]' in captured["prompt"]
    assert result[0]["state"] == "unreadable"


def test_answer_sheet_transcription_sends_all_column_images_once(monkeypatch):
    import services.answer_sheet_acceleration as acceleration
    import services.ocr_service as ocr

    captured = {}

    async def must_not_call_single_image(*_args, **_kwargs):
        raise AssertionError("split evidence must use one multi-image request")

    async def fake_call_vision_images(image_paths, prompt, max_tokens=8192):
        captured["image_paths"] = image_paths
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return (
            '{"questions":['
            '{"question_number":"A001","student_answer":"A",'
            '"answer_state":"visible","confidence":0.99},'
            '{"question_number":"A002","student_answer":"4\\\\sqrt{5}",'
            '"answer_state":"visible","confidence":0.95}'
            ']}'
        )

    monkeypatch.setattr(ocr, "call_vision", must_not_call_single_image)
    monkeypatch.setattr(ocr, "call_vision_images", fake_call_vision_images)
    result = asyncio.run(acceleration.transcribe_answer_sheet_evidence(
        ("left.png", "right.png"),
        [
            {
                "question_number": "A001",
                "kind": "answer",
                "question_no": "1",
                "question_type": "choice",
                "choice_mode": "single",
                "choice_options": ["A", "B", "C", "D"],
                "image_index": 1,
                "answer_bbox_1000": [100, 100, 900, 200],
            },
            {
                "question_number": "A002",
                "kind": "answer",
                "question_no": "21",
                "question_type": "fill",
                "choice_mode": "single",
                "choice_options": [],
                "image_index": 2,
                "answer_bbox_1000": [100, 200, 900, 300],
            },
        ],
        teacher_id=1,
    ))

    assert captured["image_paths"] == ["left.png", "right.png"]
    assert "region_schema" in captured["prompt"]
    assert captured["max_tokens"] == 512
    assert [item["text"] for item in result] == ["A", r"4\sqrt{5}"]


def test_student_image_preparation_enables_review_ink_suppression(monkeypatch):
    import services.ocr_service as ocr

    captured = {}

    def fake_preprocess(image_path, mode, max_edge, suppress_review_ink=False):
        captured.update({
            "image_path": image_path,
            "mode": mode,
            "max_edge": max_edge,
            "suppress_review_ink": suppress_review_ink,
        })
        return image_path

    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", fake_preprocess)

    result = asyncio.run(ocr._prepare_vision_image("paper.jpg"))

    assert result == "paper.jpg"
    assert captured["suppress_review_ink"] is True

    asyncio.run(ocr._prepare_vision_image("paper.jpg", max_edge=3072))
    assert captured["max_edge"] == 3072


def test_browser_ocr_ready_jpeg_skips_server_pixel_preprocessing(
    tmp_path,
    monkeypatch,
):
    import services.ocr_service as ocr

    source = tmp_path / "paper.ocr-ready-v1.jpg"
    Image.new("RGB", (1200, 800), "white").save(source, "JPEG")

    def must_not_preprocess(*_args, **_kwargs):
        raise AssertionError("browser-normalized JPEG must not be decoded again")

    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", must_not_preprocess)

    result = asyncio.run(ocr._prepare_vision_image(str(source)))

    assert result == str(source)


def test_ocr_ready_filename_does_not_bypass_contract_checks(tmp_path, monkeypatch):
    import services.ocr_service as ocr

    source = tmp_path / "paper.ocr-ready-v1.jpg"
    Image.new("RGB", (2100, 1400), "white").save(source, "JPEG")
    calls = []

    def preprocess(image_path, **_kwargs):
        calls.append(image_path)
        return image_path

    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", preprocess)

    assert asyncio.run(ocr._prepare_vision_image(str(source))) == str(source)
    assert calls == [str(source)]


def test_enhanced_mode_still_trusts_verified_browser_contract(tmp_path, monkeypatch):
    import services.ocr_service as ocr

    source = tmp_path / "paper.ocr-ready-v1.jpg"
    Image.new("RGB", (1200, 800), "white").save(source, "JPEG")
    calls = []

    def preprocess(image_path, **_kwargs):
        calls.append((image_path, _kwargs.get("mode")))
        return image_path

    monkeypatch.setattr(ocr_service_papers, "OCR_PREPROCESS_MODE", "enhanced")
    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", preprocess)

    assert asyncio.run(ocr._prepare_vision_image(str(source))) == str(source)
    assert calls == []


def test_cache_recognition_forwards_teacher_context():
    import services.ocr_service as ocr

    captured = {}

    class _Cache:
        async def recognize_page(
            self, image_path, *, teacher_id="", batch_id="", paper_id="",
        ):
            captured.update(
                image_path=image_path,
                teacher_id=teacher_id,
                batch_id=batch_id,
                paper_id=paper_id,
                vision_teacher_id=ocr._vision_teacher_id.get(),
            )
            return {"questions": [{"question_number": "1"}]}

    result = asyncio.run(
        ocr.recognize_paper_with_cache(
            "paper.jpg",
            "batch",
            _Cache(),
            teacher_id="teacher-42",
            paper_id="paper-7",
        )
    )

    assert result["questions"]
    assert captured == {
        "image_path": "paper.jpg",
        "teacher_id": "teacher-42",
        "batch_id": "batch",
        "paper_id": "paper-7",
        "vision_teacher_id": "teacher-42",
    }
    assert ocr._vision_teacher_id.get() == "system"


def test_recognize_paper_caps_concurrency_per_teacher(monkeypatch):
    """同一老师并发发起 6 个 recognize_paper，上限=2 → 同时在途不超过 2。"""
    import services.ocr_service as ocr
    import services.key_pool as kp

    # 每老师上限设 2，覆盖 recognize_paper 内部读取的 config 值
    monkeypatch.setattr(ocr, "OCR_PER_TEACHER_LIMIT", 2, raising=False)
    import config
    monkeypatch.setattr(config, "OCR_PER_TEACHER_LIMIT", 2, raising=False)

    state = {"cur": 0, "peak": 0}

    async def _fake_call_vision(image_path, prompt, max_tokens=8192):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.05)          # 模拟在途
        state["cur"] -= 1
        return '{"questions": [{"question_number": "1", "question_text": "x", "student_answer": "1", "answer_state": "visible", "question_type": "fill"}], "student_name": "张三", "student_no": "01"}'

    monkeypatch.setattr(ocr, "call_vision", _fake_call_vision)
    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", lambda p: p)

    async def _run():
        tasks = [
            asyncio.create_task(ocr.recognize_paper(f"img{i}.png", teacher_id="teacherA"))
            for i in range(6)
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run())
    assert len(results) == 6
    assert all(r.get("questions") for r in results)
    assert state["peak"] <= 2, f"峰值并发 {state['peak']} 超过上限 2"
    assert state["peak"] >= 2, "应真正并发（峰值应达到上限 2），否则退化为串行"


def test_different_teachers_not_capped_together(monkeypatch):
    """不同老师各自独立：两个老师各发起 2 个、每老师上限 2 → 4 个可同时在途。"""
    import services.ocr_service as ocr
    import config

    monkeypatch.setattr(ocr, "OCR_PER_TEACHER_LIMIT", 2, raising=False)
    monkeypatch.setattr(config, "OCR_PER_TEACHER_LIMIT", 2, raising=False)

    state = {"cur": 0, "peak": 0}

    async def _fake_call_vision(image_path, prompt, max_tokens=8192):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.05)
        state["cur"] -= 1
        return '{"questions": [{"question_number": "1", "question_text": "x", "student_answer": "1", "answer_state": "visible", "question_type": "fill"}], "student_name": "李四", "student_no": "02"}'

    monkeypatch.setattr(ocr, "call_vision", _fake_call_vision)
    monkeypatch.setattr(ocr_service_papers, "preprocess_paper_image", lambda p: p)

    async def _run():
        tasks = []
        for t in ("teacherA", "teacherB"):
            for i in range(2):
                tasks.append(asyncio.create_task(
                    ocr.recognize_paper(f"{t}-{i}.png", teacher_id=t)))
        return await asyncio.gather(*tasks)

    asyncio.run(_run())
    # 两老师各上限2，互不相干 → 4 个可同时在途
    assert state["peak"] == 4, f"跨老师应互相独立，峰值应达 4，实际 {state['peak']}"


def test_multi_pool_failure_is_not_wrapped_in_legacy_same_pool_retries(monkeypatch):
    import services.ocr_service as ocr
    from services.ocr_pool_scheduler import OcrPoolError

    attempts = 0

    async def failed_once(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OcrPoolError(
            "vip_retry_failed",
            retryable=False,
            transport_failure=True,
        )

    monkeypatch.setattr(ocr, "call_vision", failed_once)

    result = asyncio.run(ocr._recognize_paper_preprocessed("paper.jpg"))

    assert attempts == 1
    assert result["questions"] == []
    assert result["error_info"]["code"] == "NETWORK_ERROR"
    assert result["error_info"]["stage"] == "transcription"
    assert len(result["error_info"]["error_id"]) == 32
    assert result["_ocr_error_code"] == "provider_transient_failure"
    assert "vip_retry_failed" not in result["error"]


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (408, "provider_timeout"),
        (429, "provider_rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_http_failure_is_one_request_with_a_retryable_code(
    monkeypatch,
    status_code,
    error_code,
):
    import services.ocr_service as ocr

    attempts = 0
    sleeps = []

    async def failed_once(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        request = httpx.Request("POST", "https://vision.invalid")
        response = httpx.Response(
            status_code,
            request=request,
            headers={"retry-after": "120"},
        )
        raise httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=request,
            response=response,
        )

    async def record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ocr, "call_vision", failed_once)
    monkeypatch.setattr(ocr.asyncio, "sleep", record_sleep)

    result = asyncio.run(ocr._recognize_paper_preprocessed("paper.jpg"))

    assert attempts == 1
    assert sleeps == []
    assert result["_ocr_retryable"] is True
    assert result["_ocr_error_code"] == error_code


def test_vision_payload_memory_is_bounded_by_bytes_not_only_request_count(
    monkeypatch, tmp_path
):
    import services.ocr_service as ocr

    images = []
    for index in range(3):
        path = tmp_path / f"payload-{index}.jpg"
        path.write_bytes(b"x" * 30)
        images.append(str(path))

    active = 0
    peak = 0
    two_entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_request(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            two_entered.set()
        try:
            await release.wait()
            return "ok"
        finally:
            active -= 1

    async def run():
        tasks = [
            asyncio.create_task(ocr.call_vision(path, "prompt"))
            for path in images
        ]
        await asyncio.wait_for(two_entered.wait(), timeout=1)
        await asyncio.sleep(0.02)
        assert peak == 2
        release.set()
        assert await asyncio.gather(*tasks) == ["ok", "ok", "ok"]

    # A 30-byte file is charged as 30 raw + three 40-byte Base64/JSON copies.
    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 320)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(ocr, "_call_vision_request", blocked_request)
    asyncio.run(run())


def test_single_vision_payload_larger_than_memory_budget_fails_before_encoding(
    monkeypatch, tmp_path
):
    import services.ocr_service as ocr

    image = tmp_path / "oversized-payload.jpg"
    image.write_bytes(b"x" * 30)
    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 100)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(
        ocr,
        "_call_vision_request",
        lambda *_args, **_kwargs: pytest.fail("oversized payload must not be encoded"),
    )

    with pytest.raises(ValueError, match="memory budget"):
        asyncio.run(ocr.call_vision(str(image), "prompt"))


def test_multi_image_vision_uses_one_scheduler_request_in_manifest_order(
    monkeypatch, tmp_path
):
    import services.ocr_service as ocr

    paths = []
    for index, content in enumerate((b"first", b"second", b"third"), 1):
        path = tmp_path / f"evidence_{index:02d}.png"
        path.write_bytes(content)
        paths.append(str(path))
    requests = []

    async def capture_post(url, headers, payload, api_key):
        requests.append(payload)
        return await _qwen_post_ok(url, headers, payload, api_key)

    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(ocr, "_post_vision_request", capture_post)

    result = asyncio.run(ocr.call_vision_images(paths, "transcribe"))

    assert result == "ok"
    assert len(requests) == 1
    content = requests[0]["messages"][0]["content"]
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "image_url",
        "image_url",
    ]
    assert content[0] == {"type": "text", "text": "transcribe"}
    assert [
        item["image_url"]["url"].split(",", 1)[1]
        for item in content[1:]
    ] == ["Zmlyc3Q=", "c2Vjb25k", "dGhpcmQ="]


def test_multi_image_memory_budget_uses_total_payload(monkeypatch, tmp_path):
    import services.ocr_service as ocr

    paths = []
    for index in range(2):
        path = tmp_path / f"multi-{index}.png"
        path.write_bytes(b"x" * 30)
        paths.append(str(path))
    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 250)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(
        ocr,
        "_call_vision_request",
        lambda *_args, **_kwargs: pytest.fail("over-budget images must not be encoded"),
    )

    with pytest.raises(ValueError, match="memory budget"):
        asyncio.run(ocr.call_vision_images(paths, "prompt"))


def test_payload_budget_wait_times_out_without_releasing_active_reservation(
    monkeypatch,
    tmp_path,
):
    import services.ocr_service as ocr

    images = []
    for index in range(3):
        path = tmp_path / f"budget-timeout-{index}.jpg"
        path.write_bytes(b"x" * 30)
        images.append(str(path))

    first_started = threading.Event()
    release_first = threading.Event()
    encode_calls = 0

    def encode(_image_path):
        nonlocal encode_calls
        encode_calls += 1
        if encode_calls == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        return "encoded", "image/jpeg"

    async def wait_until_started():
        async with asyncio.timeout(1):
            while not first_started.is_set():
                await asyncio.sleep(0.005)

    async def run():
        first = asyncio.create_task(ocr.call_vision(images[0], "prompt"))
        await wait_until_started()

        with pytest.raises(ocr.LocalImagePipelineStalled, match="memory budget"):
            await ocr.call_vision(images[1], "prompt")
        assert encode_calls == 1

        release_first.set()
        assert await first == "ok"
        assert await ocr.call_vision(images[2], "prompt") == "ok"

    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 150)
    monkeypatch.setattr(ocr_service_template_route, "_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(ocr, "_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(ocr_service_template_route, "_encode_vision_image", encode)
    monkeypatch.setattr(ocr, "_encode_vision_image", encode)
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(ocr, "_post_vision_request", _qwen_post_ok)

    asyncio.run(run())


def test_payload_budget_default_wait_defers_to_the_owning_route_deadline():
    import services.ocr_service as ocr

    assert ocr._LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS is None


def test_payload_budget_wait_allows_a_slow_active_request_to_release_memory(
    monkeypatch,
    tmp_path,
):
    import services.ocr_service as ocr

    images = []
    for index in range(2):
        path = tmp_path / f"slow-budget-{index}.jpg"
        path.write_bytes(b"x" * 30)
        images.append(str(path))

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def slow_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        return "ok"

    async def run():
        first = asyncio.create_task(ocr.call_vision(images[0], "prompt"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = asyncio.create_task(ocr.call_vision(images[1], "prompt"))

        # Queue pressure alone must not invent a local-image fault; the owning
        # route or batch deadline is responsible for cancellation.
        await asyncio.sleep(0.05)
        assert second.done() is False
        release_first.set()

        assert await asyncio.gather(first, second) == ["ok", "ok"]

    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 150)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(ocr, "_call_vision_request", slow_request)

    asyncio.run(run())


def test_cancelled_vision_encoding_holds_memory_budget_until_thread_settles(
    monkeypatch,
    tmp_path,
):
    import services.ocr_service as ocr

    images = []
    for index in range(2):
        path = tmp_path / f"cancelled-encoding-{index}.jpg"
        path.write_bytes(b"x" * 30)
        images.append(str(path))

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_encode(_image_path):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        return "encoded", "image/jpeg"

    async def wait_for_thread_event(event):
        async with asyncio.timeout(1):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run():
        first = asyncio.create_task(ocr.call_vision(images[0], "prompt"))
        await wait_for_thread_event(first_started)
        first.cancel()
        second = asyncio.create_task(ocr.call_vision(images[1], "prompt"))
        for _ in range(100):
            if first.done():
                break
            await asyncio.sleep(0.01)

        assert first.done() is True
        assert second_started.is_set() is False

        with pytest.raises(asyncio.CancelledError):
            await first

        release_first.set()
        assert await second == "ok"

    # Each 30-byte file is charged 150 bytes, allowing exactly one encoder.
    monkeypatch.setattr(ocr_service_local_image, "OCR_IMAGE_PAYLOAD_MEMORY_BUDGET_BYTES", 150)
    monkeypatch.setattr(ocr_service_local_image, "_local_ocr_payload_budget", None)
    monkeypatch.setattr(ocr_service_template_route, "_encode_vision_image", blocked_encode)
    monkeypatch.setattr(ocr, "_encode_vision_image", blocked_encode)
    monkeypatch.setattr(ocr, "_vision_pool", None)
    monkeypatch.setattr(ocr, "_post_vision_request", _qwen_post_ok)

    asyncio.run(run())


def test_cancelled_local_image_component_holds_gate_until_thread_settles():
    import services.ocr_service as ocr

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def blocked_component(call_number):
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        return call_number

    async def wait_for_thread_event(event):
        async with asyncio.timeout(1):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run():
        gate = asyncio.Semaphore(1)
        first = asyncio.create_task(
            ocr._run_local_ocr_image_component(
                blocked_component,
                1,
                semaphore=gate,
            )
        )
        await wait_for_thread_event(first_started)
        first.cancel()
        second = asyncio.create_task(
            ocr._run_local_ocr_image_component(
                blocked_component,
                2,
                semaphore=gate,
            )
        )
        try:
            # Windows can take longer than one scheduler tick to deliver task
            # cancellation under the full suite's thread load. The contract is
            # bounded handoff plus gate retention, not a fixed 30 ms latency.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, timeout=1)
            assert second_started.is_set() is False
        finally:
            release_first.set()
        assert await second == 2

    asyncio.run(run())


def test_timed_out_local_image_component_returns_but_keeps_gate_and_readiness_fault(
    monkeypatch,
    tmp_path,
):
    import services.ocr_service as ocr

    started = threading.Event()
    release = threading.Event()

    def blocked_component():
        started.set()
        assert release.wait(timeout=2)

    async def wait_until(predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.005)

    async def run():
        gate = asyncio.Semaphore(1)
        task = asyncio.create_task(
            ocr._run_local_ocr_image_component(
                blocked_component,
                semaphore=gate,
            )
        )
        await wait_until(started.is_set)
        with pytest.raises(ocr.LocalImagePipelineStalled) as stalled_error:
            await task
        assert ocr.classify_ocr_exception(stalled_error.value) == (
            False,
            "local_image_pipeline_stalled",
        )
        await wait_until(lambda: bool(tuple(tmp_path.glob("ocr-image-*.stall"))))
        stalled = ocr.get_local_image_readiness()
        assert gate.locked() is True
        release.set()
        await wait_until(lambda: not ocr._stalled_image_settlers)
        recovered = ocr.get_local_image_readiness()
        return stalled, recovered, gate.locked()

    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_COMPONENT_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_STALL_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_STALL_MARKER_DIR", tmp_path)
    stalled, recovered, gate_locked = asyncio.run(run())

    assert stalled["ready"] is False
    assert stalled["reason"] == "local_image_pipeline_stalled"
    assert stalled["shared_stall_components"] == 1
    assert recovered["ready"] is True
    assert recovered["shared_stall_components"] == 0
    assert gate_locked is False


def test_shared_image_stall_marker_blocks_other_worker_and_stale_marker_is_pruned(
    monkeypatch,
    tmp_path,
):
    import services.ocr_service as ocr

    marker = tmp_path / "ocr-image-other-worker.stall"
    marker.write_text("", encoding="ascii")
    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_STALL_MARKER_DIR", tmp_path)
    # Windows stamps file times from a coarse clock (~15.6 ms ticks), so a
    # fresh marker can already look older than a millisecond-scale window.
    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_STALL_MARKER_STALE_SECONDS", 5.0)

    assert ocr.get_local_image_readiness()["ready"] is False
    old = time.time() - 10
    os.utime(marker, (old, old))
    recovered = ocr.get_local_image_readiness()

    assert recovered["ready"] is True
    assert recovered["shared_stall_components"] == 0
    assert marker.exists() is False


def test_stalled_local_image_component_fails_readiness_until_it_settles(
    monkeypatch,
):
    import services.ocr_service as ocr

    started = threading.Event()
    release = threading.Event()

    def blocked_component():
        started.set()
        assert release.wait(timeout=2)

    async def wait_for_thread_event(event):
        async with asyncio.timeout(1):
            while not event.is_set():
                await asyncio.sleep(0.005)

    async def run():
        task = asyncio.create_task(ocr._run_local_image_thread(blocked_component))
        await wait_for_thread_event(started)
        await asyncio.sleep(0.02)
        stalled = ocr.get_local_image_readiness()
        release.set()
        await task
        recovered = ocr.get_local_image_readiness()
        return stalled, recovered

    monkeypatch.setattr(ocr_service_local_image, "_LOCAL_IMAGE_WORK_STALL_SECONDS", 0.01)
    stalled, recovered = asyncio.run(run())

    assert stalled["ready"] is False
    assert stalled["reason"] == "local_image_pipeline_stalled"
    assert stalled["active_components"] == 1
    assert recovered["ready"] is True
    assert recovered["active_components"] == 0


def test_ocr_readiness_fails_closed_before_scheduler_when_image_work_stalls(
    monkeypatch,
):
    import services.ocr_service as ocr

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-qwen-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://qwen.example.invalid/v1")
    monkeypatch.setattr(
        ocr,
        "get_local_image_readiness",
        lambda: {
            "ready": False,
            "reason": "local_image_pipeline_stalled",
        },
    )

    state = asyncio.run(ocr.get_ocr_readiness())

    assert state == {
        "provider": "ocr",
        "configured": True,
        "ready": False,
        "reason": "local_image_pipeline_stalled",
    }
