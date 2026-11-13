"""How the OCR pool scheduler sends one call to a pool, and how it reads a failure.

``_SchedulerTransportMixin`` holds these ``OcrPoolScheduler`` methods:

- ``_execute`` sends one call through the scheduler's transport, validates the
  answer, and records success or failure with the coordinator (which drives the
  pool's circuit); it turns timeouts, transport errors and rejected answers into
  ``OcrPoolError``;
- ``_send_stream`` is the default transport: a streamed Responses request, or
  for a ``chat_completions`` pool ``_send_chat_completion_stream``, whose
  messages come from ``_chat_messages_from_payload`` and whose answer must not
  stop mid-generation (``_TRUNCATED_FINISH_REASONS``).

``_classify_http_failure`` (``_SHARED_RETRYABLE_HTTP_STATUSES``),
``_template_failure_category_from_message`` and
``_is_student_transcription_payload`` (``_STUDENT_TRANSCRIPTION_MARKER``)
decide what a failure means and whether another pool may retry it.

Moved unchanged from services/ocr_pool_scheduler.py; OcrPoolScheduler inherits the methods.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from typing import Any

import httpx

from services.ocr_pool_scheduler_base import OcrPoolError, PoolSpec, ResponseValidator
from services.retry_backoff import parse_retry_after_seconds
from services.template_max_owned import MaxTemplateRejection

# services.ocr_pool_scheduler's logger, so these records keep their logger name.
logger = logging.getLogger("services.ocr_pool_scheduler")


# Provider finish_reason values that mean the answer stopped mid-generation.
# They must never be accepted as a complete model answer.
_TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens"}
_SHARED_RETRYABLE_HTTP_STATUSES = frozenset({401, 403, 404, 408, 429})
_STUDENT_TRANSCRIPTION_MARKER = "[task=student_transcription_only]"


def _template_failure_category_from_message(message: object) -> str:
    """Classify response validation errors without inspecting response data."""
    text = str(message or "").strip().lower()
    # These failures describe an ambiguous/low-confidence result, not a bad
    # provider lane.  Retrying on another paid model would silently turn an
    # evidentiary review condition into an attribution decision.
    semantic_markers = (
        "confidence",
        "low_confidence",
        "ambiguous",
        "needs_review",
        "semantic",
        "uncertain",
    )
    if any(marker in text for marker in semantic_markers):
        return "semantic"
    return "contract"


def _classify_http_failure(status_code: int) -> tuple[bool, bool]:
    """Return ``(retryable, shared_failure)`` for an upstream HTTP status.

    Authentication, endpoint/model, throttling, timeout, and server failures
    describe the shared pool and may be retried elsewhere. Other 4xx responses
    are request-specific and must not poison the pool-wide circuit.
    """
    shared_failure = (
        status_code in _SHARED_RETRYABLE_HTTP_STATUSES or status_code >= 500
    )
    return shared_failure, shared_failure


def _is_student_transcription_payload(payload: dict[str, Any]) -> bool:
    """Match the narrow pure-transcription contract without inspecting images."""
    for message in payload.get("input") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and _STUDENT_TRANSCRIPTION_MARKER in content:
            return True
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and _STUDENT_TRANSCRIPTION_MARKER in item["text"]
            ):
                return True
    return False


def _chat_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the internal Responses-shaped OCR payload to Chat Completions."""
    source = payload.get("input")
    if not isinstance(source, list):
        source = payload.get("messages")
    if not isinstance(source, list):
        raise OcrPoolError(
            "missing_messages",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="pre_dispatch",
        )

    messages: list[dict[str, Any]] = []
    for message in source:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"input_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    converted.append({"type": "text", "text": text})
                continue
            if item_type in {"input_image", "image_url"}:
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if isinstance(image_url, str) and image_url:
                    converted.append({
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": item.get("detail", "high"),
                        },
                    })
        if converted:
            messages.append({"role": role, "content": converted})
    if not messages:
        raise OcrPoolError(
            "missing_messages",
            retryable=False,
            transport_failure=False,
            request_sent=False,
            failure_category="pre_dispatch",
        )
    return messages


class _SchedulerTransportMixin:
    """OcrPoolScheduler's methods for sending one call to a pool."""

    async def _execute(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        validator: ResponseValidator | None,
    ) -> str:
        request_id = f"ocr-{uuid.uuid4().hex}"
        try:
            content = await self._transport(pool, payload, request_id)
        except OcrPoolError as exc:
            if exc.request_sent is None:
                # Reaching the transport callable means the permit/dispatch
                # boundary was crossed. Explicit pre-dispatch errors (such as
                # malformed local payload conversion) set ``False`` at source.
                exc.request_sent = True
            if exc.failure_category is None:
                exc.failure_category = (
                    "transport" if exc.transport_failure else "provider"
                )
            if exc.actual_model is None:
                exc.actual_model = pool.model
            if exc.transport_failure:
                fatal = exc.status_code in {401, 403, 404}
                # A 429 is an explicit capacity signal.  Release immediately and
                # cool the pool instead of occupying a slot while sleeping.
                threshold = 1 if exc.status_code == 429 else self.circuit_failure_threshold
                cooldown_s = max(
                    self.circuit_cooldown_s,
                    exc.retry_after_s or 0.0,
                )
                await self.coordinator.record_failure(
                    pool.coordination_key,
                    threshold=threshold,
                    cooldown_s=cooldown_s,
                    fatal=fatal,
                )
            raise
        except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as exc:
            await self.coordinator.record_failure(
                pool.coordination_key,
                threshold=self.circuit_failure_threshold,
                cooldown_s=self.circuit_cooldown_s,
                fatal=False,
            )
            raise OcrPoolError(
                type(exc).__name__,
                retryable=True,
                transport_failure=True,
                request_sent=True,
                failure_category="transport",
                actual_model=pool.model,
            ) from exc

        if validator is not None:
            try:
                result = validator(content)
                if inspect.isawaitable(result):
                    await result
            except MaxTemplateRejection:
                # An explicit model refusal is a semantic stop, not a pool
                # failure. Preserve the reason and do not retry another answer.
                raise
            except Exception as exc:
                await self.coordinator.record_failure(
                    pool.coordination_key,
                    threshold=self.circuit_failure_threshold,
                    cooldown_s=self.circuit_cooldown_s,
                    fatal=False,
                )
                raise OcrPoolError(
                    "strict_contract_failure",
                    retryable=True,
                    transport_failure=False,
                    request_sent=True,
                    failure_category=_template_failure_category_from_message(exc),
                    actual_model=pool.model,
                ) from exc
        await self.coordinator.record_success(pool.coordination_key)
        logger.info("OCR pool request completed pool=%s request_id=%s", pool.name, request_id)
        return content

    async def _send_chat_completion_stream(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        request_id: str,
    ) -> str:
        """Send the configured Chat Completions streaming request."""
        from config import (
            OCR_TEMPLATE_GPT_REASONING_EFFORT, OCR_TRANSCRIPTION_REASONING_EFFORT, REASONING_EFFORTS,
        )

        requested_effort = str(payload.get("model_reasoning_effort") or "").strip().lower()
        if requested_effort not in REASONING_EFFORTS:
            requested_effort = (
                OCR_TRANSCRIPTION_REASONING_EFFORT
                if _is_student_transcription_payload(payload)
                else OCR_TEMPLATE_GPT_REASONING_EFFORT
            )
        body: dict[str, Any] = {
            "model": pool.model,
            "messages": _chat_messages_from_payload(payload),
            "temperature": 0,
            "stream": True,
        }
        max_tokens = payload.get("max_output_tokens", payload.get("max_tokens"))
        if pool.model.strip().lower().startswith("qwen"):
            # Provider parameters depend on the model contract, never on words
            # in a prompt. All new template phases use this same adapter.
            body["enable_thinking"] = requested_effort != "none"
            if payload.get("text") == {"format": {"type": "json_object"}}:
                # The internal Responses text.format is consumed here, at the
                # real Qwen wire boundary, and never forwarded as-is.
                body["response_format"] = {"type": "json_object"}
            # Keep the requested effort visible at the provider boundary for
            # every Qwen model, whatever its generation: a renamed model (e.g.
            # qwen4-max-preview) must not silently fall back to the provider's
            # default tier, and ``high`` must not silently become ``xhigh``
            # (which has a materially longer latency/timeout profile).  Only
            # the provider's documented alias for minimal is normalized.
            body["reasoning_effort"] = {
                "minimal": "low",
            }.get(requested_effort, requested_effort)
        else:
            # Preserve the separately verified legacy non-Qwen Chat gateway.
            # GPT Responses calls use the distinct adapter below.
            body["model_reasoning_effort"] = requested_effort
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        url = pool.base_url.rstrip("/")
        if url.endswith("/responses"):
            url = url[: -len("/responses")]
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        headers = {
            "Authorization": f"Bearer {pool.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=max(pool.first_event_timeout_s, self.sse_idle_timeout_s),
            write=self.write_timeout_s,
            pool=self.connect_timeout_s,
        )
        response: httpx.Response | None = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(pool.attempt_ttl_s):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    http2=False,
                ) as client:
                    request = client.build_request("POST", url, headers=headers, json=body)
                    try:
                        response = await asyncio.wait_for(
                            client.send(request, stream=True),
                            timeout=pool.first_event_timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        raise OcrPoolError(
                            "response_header_timeout",
                            retryable=True,
                            transport_failure=True,
                        ) from exc
                    if response.status_code != 200:
                        status = response.status_code
                        retryable, shared_failure = _classify_http_failure(status)
                        raise OcrPoolError(
                            f"http_{status}",
                            retryable=retryable,
                            transport_failure=shared_failure,
                            status_code=status,
                            retry_after_s=parse_retry_after_seconds(
                                response.headers.get("retry-after")
                            ),
                        )

                    iterator = response.aiter_lines()
                    chunks: list[str] = []
                    data_seen = False
                    ended: set[str] = set()
                    done_seen = False
                    while True:
                        timeout_s = (
                            max(
                                0.01,
                                started
                                + pool.first_event_timeout_s
                                - time.monotonic(),
                            )
                            if not data_seen
                            else self.sse_idle_timeout_s
                        )
                        try:
                            line = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise OcrPoolError(
                                "sse_idle_timeout" if data_seen else "first_sse_timeout",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        data_seen = True
                        if data == "[DONE]":
                            done_seen = True
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OcrPoolError(
                                "invalid_sse_json",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not isinstance(event, dict):
                            continue
                        if event.get("error"):
                            raise OcrPoolError(
                                "stream_error",
                                retryable=True,
                                transport_failure=True,
                            )
                        for choice in event.get("choices") or []:
                            if not isinstance(choice, dict):
                                continue
                            delta = choice.get("delta") or {}
                            if isinstance(delta, dict) and delta.get("refusal"):
                                raise OcrPoolError("model_refused", retryable=False)
                            if isinstance(delta, dict) and isinstance(
                                delta.get("content"), str
                            ):
                                chunks.append(delta["content"])
                            finish_reason = choice.get("finish_reason")
                            if isinstance(finish_reason, str) and finish_reason.strip():
                                ended.add(finish_reason.strip())

                        if ended:
                            # finish_reason is the terminator: never keep the
                            # connection open until idle timeout or [DONE], and
                            # never accept a stream the provider cut short.
                            break

                    if ended & _TRUNCATED_FINISH_REASONS:
                        raise OcrPoolError(
                            "truncated_stream", retryable=True
                        )
                    if "content_filter" in ended:
                        raise OcrPoolError("content_filtered", retryable=False)
                    if ended - {"stop"}:
                        raise OcrPoolError("unsupported_finish_reason", retryable=True)
                    if not data_seen:
                        raise OcrPoolError(
                            "missing_sse", retryable=True, transport_failure=True
                        )
                    if not ended and not done_seen:
                        raise OcrPoolError(
                            "incomplete_stream", retryable=True, transport_failure=True
                        )
                    content = "".join(chunks).strip()
                    if not content:
                        raise OcrPoolError(
                            "empty_content", retryable=True, transport_failure=True
                        )
                    return content
        except TimeoutError as exc:
            raise OcrPoolError(
                "absolute_attempt_ttl",
                retryable=True,
                transport_failure=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()

    async def _send_stream(
        self,
        pool: PoolSpec,
        payload: dict[str, Any],
        request_id: str,
    ) -> str:
        if pool.api_protocol == "chat_completions":
            return await self._send_chat_completion_stream(pool, payload, request_id)

        body = dict(payload)
        body["model"] = pool.model
        from config import REASONING_EFFORTS

        requested_effort = str(payload.get("model_reasoning_effort") or "").strip().lower()
        if requested_effort in REASONING_EFFORTS:
            body["reasoning"] = {"effort": requested_effort}
        elif _is_student_transcription_payload(payload):
            from config import OCR_TRANSCRIPTION_REASONING_EFFORT

            body["reasoning"] = {
                "effort": OCR_TRANSCRIPTION_REASONING_EFFORT,
            }
        else:
            # Legacy Responses callers without internal route metadata retain
            # the historical high-effort default. Active template calls set
            # ``model_reasoning_effort`` before reaching this adapter.
            body["reasoning"] = {"effort": "high"}
        body["stream"] = True
        body.pop("reasoning_effort", None)
        body.pop("model_reasoning_effort", None)
        body.pop("temperature", None)
        body.pop("max_completion_tokens", None)
        body.pop("messages", None)
        body.pop("stream_options", None)
        body.pop("enable_thinking", None)
        # The internal Responses json_object marker is forwarded only to Qwen
        # pools, where text.format is already the native field of this protocol.
        # Every non-Qwen Responses gateway keeps its byte-identical legacy body.
        if body.get("text") == {"format": {"type": "json_object"}} and not pool.model.strip().lower().startswith("qwen"):
            body.pop("text")

        url = pool.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        if not url.endswith("/responses"):
            url += "/responses"
        headers = {
            "Authorization": f"Bearer {pool.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            # A provider can remain silent while reasoning before its first SSE
            # event. The stricter idle timeout is enforced below after data
            # starts flowing.
            read=max(pool.first_event_timeout_s, self.sse_idle_timeout_s),
            write=self.write_timeout_s,
            pool=self.connect_timeout_s,
        )
        response: httpx.Response | None = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(pool.attempt_ttl_s):
                async with httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    http2=False,
                ) as client:
                    request = client.build_request("POST", url, headers=headers, json=body)
                    try:
                        response = await asyncio.wait_for(
                            client.send(request, stream=True),
                            timeout=pool.first_event_timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        raise OcrPoolError(
                            "response_header_timeout",
                            retryable=True,
                            transport_failure=True,
                        ) from exc
                    if response.status_code != 200:
                        status = response.status_code
                        retryable, shared_failure = _classify_http_failure(status)
                        raise OcrPoolError(
                            f"http_{status}",
                            retryable=retryable,
                            transport_failure=shared_failure,
                            status_code=status,
                            retry_after_s=parse_retry_after_seconds(
                                response.headers.get("retry-after")
                            ),
                        )

                    iterator = response.aiter_lines()
                    chunks: list[str] = []
                    completed_seen = False
                    data_seen = False
                    while True:
                        if not data_seen:
                            remaining_first = (
                                started + pool.first_event_timeout_s - time.monotonic()
                            )
                            if remaining_first <= 0:
                                raise OcrPoolError(
                                    "first_sse_timeout",
                                    retryable=True,
                                    transport_failure=True,
                                )
                            timeout_s = remaining_first
                        else:
                            timeout_s = self.sse_idle_timeout_s
                        try:
                            line = await asyncio.wait_for(anext(iterator), timeout=timeout_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise OcrPoolError(
                                "sse_idle_timeout" if data_seen else "first_sse_timeout",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        data_seen = True
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OcrPoolError(
                                "invalid_sse_json",
                                retryable=True,
                                transport_failure=True,
                            ) from exc
                        event_type = str(event.get("type") or "")
                        if event_type == "error" or event.get("error"):
                            raise OcrPoolError(
                                "stream_error",
                                retryable=True,
                                transport_failure=True,
                            )
                        if event_type in {"response.failed", "response.incomplete"}:
                            raise OcrPoolError(
                                event_type.replace(".", "_"),
                                retryable=True,
                                transport_failure=False,
                            )
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if isinstance(delta, str):
                                chunks.append(delta)
                            continue
                        if event_type != "response.completed":
                            continue

                        completed_response = event.get("response")
                        if not isinstance(completed_response, dict):
                            completed_response = {}
                        terminal_status = str(
                            completed_response.get("status") or ""
                        ).strip()
                        if terminal_status and terminal_status != "completed":
                            raise OcrPoolError(
                                f"response_terminal_{terminal_status}",
                                retryable=True,
                                transport_failure=False,
                            )
                        completed_seen = True
                        if not chunks:
                            for output_item in completed_response.get("output") or []:
                                if not isinstance(output_item, dict):
                                    continue
                                for part in output_item.get("content") or []:
                                    if (
                                        isinstance(part, dict)
                                        and part.get("type") == "output_text"
                                        and isinstance(part.get("text"), str)
                                    ):
                                        chunks.append(part["text"])
                        break
                    if not data_seen:
                        raise OcrPoolError(
                            "missing_sse", retryable=True, transport_failure=True
                        )
                    if not completed_seen:
                        raise OcrPoolError(
                            "missing_completed", retryable=True, transport_failure=True
                        )
                    content = "".join(chunks).strip()
                    if not content:
                        raise OcrPoolError(
                            "empty_content", retryable=True, transport_failure=True
                        )
                    return content
        except TimeoutError as exc:
            raise OcrPoolError(
                "absolute_attempt_ttl",
                retryable=True,
                transport_failure=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()
