"""A Max template request whose stream dies before an answer is sent once more.

Measured on the real provider: HTTP 200, 150 s of reasoning chunks, then the
stream ended with no content, no ``finish_reason`` and no ``[DONE]``. The
template route sent each request exactly once, so that one lost stream failed
the whole page.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from services.ocr_pool_scheduler import OcrPoolError
from tests.test_ocr_pool_scheduler import _AsyncSseStream, _DelayedAsyncSseStream, _pool, _scheduler

ANSWER = '{"ok":true}'
REASONING_ONLY = [{"choices": [{"delta": {"reasoning_content": "thinking"}}]} for _ in range(3)]
ANSWERED = [{"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"content": ANSWER}, "finish_reason": "stop"}]}]


def dead(code, **extra):
    return OcrPoolError(code, retryable=True, transport_failure=True, **extra)


def scripted_scheduler(outcomes, sent, *, hard=2):
    """A scheduler whose Max pool meets each send with the next scripted outcome."""
    pending = iter(outcomes)

    async def transport(_pool_spec, payload, _request_id):
        sent.append(payload)
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    scheduler = _scheduler(transport)
    scheduler.template_fallback_pool = _pool("max", hard=hard)
    return scheduler


def request(scheduler, **kwargs):
    return asyncio.run(scheduler.request_template_fallback_pool({"task": "locate"}, request_key="page-1", **kwargs))


class _CutStream(httpx.AsyncByteStream):
    """Serve the events, then break the connection the way a dropped chunked body does."""

    def __init__(self, events):
        self._events = list(events)

    async def __aiter__(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    async def aclose(self):
        return None


def serve_streams(monkeypatch, streams):
    """Answer each HTTP request with the next scripted SSE stream; return the request bodies."""
    import services.ocr_pool_scheduler as scheduler_module

    real_client, pending, bodies = httpx.AsyncClient, iter(streams), []

    async def handler(http_request):
        bodies.append(json.loads(http_request.content.decode("utf-8")))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=next(pending))

    monkeypatch.setattr(scheduler_module.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return bodies


@pytest.mark.parametrize("first_stream", [
    pytest.param(_AsyncSseStream(REASONING_ONLY), id="ended_without_finish_reason_or_done"),
    pytest.param(_CutStream(REASONING_ONLY), id="connection_dropped_mid_stream"),
    pytest.param(_AsyncSseStream([{"choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": "stop"}]}]),
                 id="finished_without_any_content"),
    pytest.param(_AsyncSseStream([]), id="no_events_at_all"),
])
def test_a_stream_that_gives_no_answer_is_followed_by_one_resend_of_the_same_request(monkeypatch, first_stream):
    bodies = serve_streams(monkeypatch, [first_stream, _AsyncSseStream(ANSWERED)])
    scheduler = _scheduler(None)
    scheduler.template_fallback_pool = replace(_pool("max"), model="qwen3.8-max", api_protocol="chat_completions")
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "locate"}]}]}

    answer = asyncio.run(scheduler.request_template_fallback_pool(payload, request_key="page-1"))

    assert answer == ANSWER
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def max_scheduler():
    """A real scheduler whose Max pool speaks the provider's chat protocol over the default transport."""
    scheduler = _scheduler(None)
    scheduler.template_fallback_pool = replace(_pool("max", ttl=30.0), model="qwen3.8-max", api_protocol="chat_completions")
    return scheduler


def test_the_call_every_template_max_request_makes_resends_a_dead_stream(tmp_path, monkeypatch, caplog):
    """Nothing between the pipeline's Max call and the wire drops the resend.

    Measured stream: HTTP 200, reasoning chunks only, then a clean end with no
    ``finish_reason`` and no ``[DONE]``.
    """
    import services.ocr_pool_scheduler as scheduler_module
    from PIL import Image
    from services import ocr_service

    image = tmp_path / "tile.png"
    Image.new("RGB", (40, 40), "white").save(image)
    bodies = serve_streams(monkeypatch, [_AsyncSseStream(REASONING_ONLY * 500), _AsyncSseStream(ANSWERED)])
    scheduler = max_scheduler()
    monkeypatch.setattr(scheduler_module, "get_template_ocr_pool_scheduler", lambda *, pool_route=None: scheduler)
    validated, diagnostics = [], {}

    with caplog.at_level("WARNING", logger="services.ocr_pool_scheduler"):
        answer = asyncio.run(ocr_service.call_gpt_vision(
            str(image), "定位", response_validator=validated.append, json_object=True, route_diagnostics=diagnostics))

    assert answer == ANSWER and validated == [ANSWER]
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert diagnostics["resent_after"] == "incomplete_stream"
    assert "Template request is sent once more after incomplete_stream" in caplog.text


def test_a_request_cancelled_while_still_reasoning_is_closed_and_not_resent(monkeypatch):
    """A sibling's failure cancels the requests beside it; on the wire that is also a 200 with reasoning only."""
    slow = _DelayedAsyncSseStream([(0.0, REASONING_ONLY[0])] + [(0.2, event) for event in REASONING_ONLY * 100])
    bodies = serve_streams(monkeypatch, [slow, _AsyncSseStream(ANSWERED)])
    scheduler = max_scheduler()
    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "locate"}]}]}

    async def run():
        request_task = asyncio.create_task(scheduler.request_template_fallback_pool(payload, request_key="page-1"))
        for _ in range(100):
            if bodies:
                break
            await asyncio.sleep(0.02)
        in_flight = len(bodies)
        await asyncio.sleep(0.3)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        # A resend would start here if a cancelled request counted as a dead stream.
        await asyncio.sleep(0.3)
        return in_flight

    assert asyncio.run(run()) == 1
    assert len(bodies) == 1


@pytest.mark.parametrize("failure,recorded", [
    (dead("incomplete_stream"), "incomplete_stream"),
    (dead("empty_content"), "empty_content"),
    (dead("sse_idle_timeout"), "sse_idle_timeout"),
    (dead("response_header_timeout"), "response_header_timeout"),
    (dead("stream_error"), "stream_error"),
    (httpx.RemoteProtocolError("peer closed connection"), "RemoteProtocolError"),
    (httpx.ReadTimeout("read timed out"), "ReadTimeout"),
])
def test_the_resend_is_recorded_in_the_callers_diagnostics(failure, recorded):
    sent, diagnostics = [], {}

    assert request(scripted_scheduler([failure, ANSWER], sent), route_diagnostics=diagnostics) == ANSWER

    assert len(sent) == 2 and sent[0] == sent[1]
    assert diagnostics["resent_after"] == recorded


def test_a_request_is_never_sent_a_third_time():
    sent = []
    scheduler = scripted_scheduler([dead("incomplete_stream"), dead("empty_content"), ANSWER], sent)

    with pytest.raises(OcrPoolError, match="empty_content"):
        request(scheduler)

    assert len(sent) == 2


@pytest.mark.parametrize("failure", [
    pytest.param(dead("http_429", status_code=429), id="throttled"),
    pytest.param(dead("http_503", status_code=503), id="server_error"),
    pytest.param(dead("http_401", status_code=401), id="credential_rejected"),
    pytest.param(dead("absolute_attempt_ttl"), id="attempt_used_its_whole_budget"),
    pytest.param(OcrPoolError("truncated_stream", retryable=True), id="cut_at_the_token_limit"),
    pytest.param(OcrPoolError("unsupported_finish_reason", retryable=True), id="unsupported_finish_reason"),
    pytest.param(OcrPoolError("model_refused", retryable=False), id="model_refused"),
    pytest.param(OcrPoolError("content_filtered", retryable=False), id="content_filtered"),
])
def test_a_failure_that_is_not_a_dead_stream_is_not_resent(failure):
    sent, diagnostics = [], {}

    with pytest.raises(OcrPoolError, match=failure.code):
        request(scripted_scheduler([failure, ANSWER], sent), route_diagnostics=diagnostics)

    assert len(sent) == 1 and "resent_after" not in diagnostics


def test_an_answer_the_validator_rejects_is_not_resent():
    sent = []

    def reject(_content):
        raise ValueError("locator answer misses question 7")

    with pytest.raises(OcrPoolError, match="strict_contract_failure"):
        request(scripted_scheduler(['{"questions":[]}', ANSWER], sent), validator=reject)

    assert len(sent) == 1


def test_the_resend_stays_inside_the_requests_own_in_flight_slot():
    sent = []

    async def run():
        resend_started, release = asyncio.Event(), asyncio.Event()

        async def transport(_pool_spec, payload, _request_id):
            sent.append(payload["task"])
            if sent == ["a"]:
                raise dead("incomplete_stream")
            if sent == ["a", "a"]:
                resend_started.set()
                await release.wait()
            return ANSWER

        scheduler = _scheduler(transport)
        scheduler.template_fallback_pool = _pool("max", hard=1, ttl=30.0)
        first = asyncio.create_task(scheduler.request_template_fallback_pool({"task": "a"}, request_key="a"))
        # Bounded: without a resend this must fail, not wait forever.
        await asyncio.wait_for(resend_started.wait(), timeout=10.0)
        second = asyncio.create_task(scheduler.request_template_fallback_pool({"task": "b"}, request_key="b"))
        await asyncio.sleep(0.3)
        waiting_for_the_slot = list(sent)
        release.set()
        return waiting_for_the_slot, await first, await second

    waiting_for_the_slot, first, second = asyncio.run(run())

    assert waiting_for_the_slot == ["a", "a"]
    assert (first, second) == (ANSWER, ANSWER) and sent == ["a", "a", "b"]
