"""What the Chat Completions reader does with the events that follow finish_reason.

A provider ends a streamed answer with ``finish_reason``, then sends a
usage-only event and ``[DONE]``. The reader used to close the connection at
``finish_reason``; whether the token counts of a request were ever received
then depended on how the provider's last packets happened to be split.
"""

import asyncio
import json
import logging
import time
from dataclasses import replace

import httpx
import pytest

from services import ocr_pool_scheduler_transport as transport
from services.ocr_pool_scheduler import OcrPoolError
from tests.test_ocr_pool_scheduler import (
    _AsyncSseStream,
    _DelayedAsyncSseStream,
    _install_chat_transport,
    _pool,
    _scheduler,
)

ANSWER = '{"ok":true}'
FINISH = {"choices": [{"delta": {"content": ANSWER}, "finish_reason": "stop"}]}
USAGE = {
    "prompt_tokens": 1306, "completion_tokens": 2857, "total_tokens": 4163,
    "completion_tokens_details": {"reasoning_tokens": 2429},
    "prompt_tokens_details": {"cached_tokens": 1024},
}
USAGE_LOGGED = "prompt_tokens=1306 completion_tokens=2857 total_tokens=4163 reasoning_tokens=2429 cached_tokens=1024"


class _RawStream(httpx.AsyncByteStream):
    """Serve bytes as given, events as SSE data lines, and raise exceptions in place."""

    def __init__(self, items):
        self._items = list(items)

    async def __aiter__(self):
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            yield item if isinstance(item, bytes) else f"data: {json.dumps(item)}\n\n".encode("utf-8")

    async def aclose(self):
        return None


def send(monkeypatch, events, *, stream_type=_AsyncSseStream, pool=None, request_id="tail"):
    """Send one request through the real Chat Completions reader against a scripted stream."""
    _install_chat_transport(monkeypatch, events, {}, stream_type=stream_type)
    scheduler = _scheduler(None)
    pool = pool or replace(scheduler.accelerator, model="qwen3.8-max", api_protocol="chat_completions")
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Q"}]}]}
    return asyncio.run(scheduler._send_stream(pool, payload, request_id))


def usage_lines(caplog):
    return [record.getMessage() for record in caplog.records if "OCR pool request usage" in record.getMessage()]


def test_the_usage_event_and_done_that_follow_finish_reason_are_read(monkeypatch):
    served = []

    class Recording(_AsyncSseStream):
        async def __aiter__(self):
            async for chunk in _AsyncSseStream.__aiter__(self):
                served.append(chunk)
                yield chunk

    assert send(monkeypatch, [FINISH, {"choices": [], "usage": USAGE}, "[DONE]"], stream_type=Recording) == ANSWER

    assert [chunk.decode("utf-8").strip() for chunk in served][1:] == [
        "data: " + json.dumps({"choices": [], "usage": USAGE}, separators=(",", ":")), "data: [DONE]"]


@pytest.mark.parametrize("events", [
    pytest.param([FINISH, {"choices": [], "usage": USAGE}, "[DONE]"], id="usage_only_event_after_finish"),
    pytest.param([FINISH, {"choices": [], "usage": None}, {"choices": [], "usage": USAGE}], id="no_done_after_usage"),
    pytest.param([{**FINISH, "usage": USAGE}], id="usage_on_the_finishing_event"),
    pytest.param([{"choices": [{"delta": {"content": ANSWER}}], "usage": USAGE}, "[DONE]"], id="usage_before_done_only"),
    pytest.param([{"choices": [{"delta": {"content": ANSWER}}], "usage": {"prompt_tokens": 9}},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": USAGE}], id="last_usage_wins"),
])
def test_the_token_counts_of_a_request_are_logged_with_its_request_id(monkeypatch, caplog, events):
    caplog.set_level(logging.INFO, logger="services.ocr_pool_scheduler")

    assert send(monkeypatch, events, request_id="ocr-usage-1") == ANSWER

    assert usage_lines(caplog) == [f"OCR pool request usage pool=hk request_id=ocr-usage-1 {USAGE_LOGGED}"]


def test_only_token_counts_leave_the_usage_object(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="services.ocr_pool_scheduler")
    usage = {"prompt_tokens": 12, "completion_tokens": "many", "total_tokens": True, "note": "private",
             "completion_tokens_details": {"reasoning_tokens": -1, "text": "private"},
             "prompt_tokens_details": ["private"]}

    assert send(monkeypatch, [FINISH, {"choices": [], "usage": usage}, "[DONE]"], request_id="ocr-usage-2") == ANSWER

    assert usage_lines(caplog) == ["OCR pool request usage pool=hk request_id=ocr-usage-2 prompt_tokens=12"]


def test_a_request_without_usage_logs_no_usage_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="services.ocr_pool_scheduler")

    assert send(monkeypatch, [FINISH, {"choices": [], "usage": {"note": "nothing countable"}}, "[DONE]"]) == ANSWER

    assert usage_lines(caplog) == []


def test_a_finished_answer_does_not_wait_longer_than_the_grace_for_what_follows(monkeypatch):
    monkeypatch.setattr(transport, "_CHAT_STREAM_TAIL_GRACE_S", 0.05)
    started = time.monotonic()

    assert send(monkeypatch, [(0.0, FINISH), (30.0, "[DONE]")], stream_type=_DelayedAsyncSseStream,
                pool=replace(_pool("slow-tail", ttl=60.0), api_protocol="chat_completions")) == ANSWER

    assert time.monotonic() - started < 5.0


def test_the_tail_ends_before_the_attempt_deadline_instead_of_failing_the_answer(monkeypatch):
    # The attempt may run for 0.6 s; the provider would send [DONE] only after 30 s.
    assert send(monkeypatch, [(0.0, FINISH), (30.0, "[DONE]")], stream_type=_DelayedAsyncSseStream,
                pool=replace(_pool("short-attempt", ttl=0.6), api_protocol="chat_completions")) == ANSWER


@pytest.mark.parametrize("tail", [
    pytest.param([b"data: {not json\n\n", {"choices": [], "usage": USAGE}], id="undecodable_line"),
    pytest.param([httpx.ReadError("connection reset")], id="read_error"),
    pytest.param([httpx.RemoteProtocolError("peer closed connection")], id="incomplete_body"),
    pytest.param([{"error": {"message": "late upstream error"}}, "[DONE]"], id="late_error_event"),
    pytest.param([b"data: [1,2]\n\n", b": keep-alive\n\n", b"data: [DONE]\n\n"], id="non_object_event_and_comment"),
    pytest.param([{"choices": [{"delta": {"content": "ignored"}, "finish_reason": "length"}]}, "[DONE]"],
                 id="choices_after_the_finish"),
])
def test_nothing_that_follows_finish_reason_can_fail_or_change_the_answer(monkeypatch, tail):
    events = [FINISH, *(b"data: [DONE]\n\n" if item == "[DONE]" else item for item in tail)]

    assert send(monkeypatch, events, stream_type=_RawStream) == ANSWER


def test_a_cut_off_answer_is_still_rejected_and_its_token_counts_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="services.ocr_pool_scheduler")
    cut_off = {"choices": [{"delta": {"content": '{"references":'}, "finish_reason": "length"}]}

    with pytest.raises(OcrPoolError, match="truncated_stream"):
        send(monkeypatch, [cut_off, {"choices": [], "usage": USAGE}, "[DONE]"], request_id="ocr-usage-3")

    assert usage_lines(caplog) == [f"OCR pool request usage pool=hk request_id=ocr-usage-3 {USAGE_LOGGED}"]


def test_a_stream_that_ends_without_finish_reason_or_done_is_still_incomplete(monkeypatch):
    reasoning_only = [{"choices": [{"delta": {"reasoning_content": "thinking"}}]} for _ in range(3)]

    with pytest.raises(OcrPoolError, match="incomplete_stream") as error:
        send(monkeypatch, reasoning_only)

    assert error.value.retryable is True and error.value.transport_failure is True
