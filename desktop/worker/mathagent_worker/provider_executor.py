from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from queue import Empty, Full, Queue
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


PROVIDER_RESPONSE_CONTRACT_VERSION = "openai-responses-sse-v1"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PROMPT_BYTES = 128 * 1024
DEFAULT_MEMORY_BUDGET_BYTES = 512 * 1024 * 1024
MIN_OUTPUT_TOKENS = 25_000
MAX_OUTPUT_TOKENS = 32_768
MIN_FIRST_EVENT_TIMEOUT_SECONDS = 5.0
MAX_FIRST_EVENT_TIMEOUT_SECONDS = 180.0
MIN_ATTEMPT_TTL_SECONDS = 30.0
MAX_ATTEMPT_TTL_SECONDS = 600.0
MIN_SSE_IDLE_TIMEOUT_SECONDS = 10.0
MAX_SSE_IDLE_TIMEOUT_SECONDS = 300.0

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_ASSET_KEYS = frozenset({"kind", "sha256", "byte_size", "media_type"})
_SUPPORTED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class ProviderExecutorError(RuntimeError):
    """A bounded, non-sensitive provider failure suitable for job status."""

    def __init__(self, code: str):
        normalized = str(code or "provider_execution_failed").strip().lower()
        safe = "".join(
            character
            for character in normalized
            if character.isascii()
            and (character.isalnum() or character == "_")
        )
        self.code = (safe or "provider_execution_failed")[:64]
        super().__init__(self.code)


@dataclass(frozen=True)
class ProviderProfile:
    base_url: str = field(repr=False)
    model: str
    api_key: str = field(repr=False)
    request_timeout_s: float = 240.0

    def __post_init__(self) -> None:
        base_url = _validated_base_url(self.base_url)
        model = str(self.model or "").strip()
        api_key = str(self.api_key or "")
        if not _MODEL.fullmatch(model):
            raise ProviderExecutorError("provider_profile_invalid")
        if (
            not 8 <= len(api_key) <= 512
            or api_key != api_key.strip()
            or not api_key.isascii()
            or any(not 33 <= ord(character) <= 126 for character in api_key)
        ):
            raise ProviderExecutorError("provider_profile_invalid")
        timeout = self.request_timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 30.0 <= float(timeout) <= 600.0
        ):
            raise ProviderExecutorError("provider_profile_invalid")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "request_timeout_s", float(timeout))

    @property
    def responses_url(self) -> str:
        parsed = urlparse(self.base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/chat/completions"):
            path = path[: -len("/chat/completions")]
        if not path.endswith("/responses"):
            path += "/responses"
        return urlunparse(parsed._replace(path=path or "/responses"))


def _validated_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderExecutorError("provider_profile_invalid")
    raw = value.strip()
    if not raw or len(raw) > 512 or raw != value:
        raise ProviderExecutorError("provider_profile_invalid")
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        raise ProviderExecutorError("provider_profile_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.params
        or (port is not None and not 1 <= port <= 65_535)
        or "\\" in parsed.path
        or any(segment == ".." for segment in parsed.path.split("/"))
        or any(ord(character) < 33 or ord(character) > 126 for character in raw)
    ):
        raise ProviderExecutorError("provider_profile_invalid")
    return raw.rstrip("/")


class ThreadSafeByteBudget:
    """Bound resident request bodies across provider threads."""

    def __init__(self, capacity: int = DEFAULT_MEMORY_BUDGET_BYTES):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self._used = 0
        self._condition = threading.Condition()

    @property
    def used(self) -> int:
        with self._condition:
            return self._used

    def acquire(self, amount: int, *, timeout_s: float | None = None) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        if amount > self.capacity:
            raise ProviderExecutorError("provider_payload_memory_limit")
        if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise ValueError("timeout_s must be finite and positive")
        deadline = (
            None if timeout_s is None else time.monotonic() + float(timeout_s)
        )
        with self._condition:
            while self._used + amount > self.capacity:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderExecutorError("provider_payload_memory_busy")
                self._condition.wait(remaining)
            self._used += amount
        return amount

    def release(self, amount: int) -> None:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("amount must be a positive integer")
        with self._condition:
            if amount > self._used:
                raise RuntimeError("provider byte budget released too many bytes")
            self._used -= amount
            self._condition.notify_all()

    @contextmanager
    def reserve(
        self,
        amount: int,
        *,
        timeout_s: float | None = None,
    ) -> Iterator[int]:
        reserved = self.acquire(amount, timeout_s=timeout_s)
        try:
            yield reserved
        finally:
            self.release(reserved)


def estimated_payload_bytes(raw_bytes: int) -> int:
    if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int) or raw_bytes <= 0:
        raise ValueError("raw_bytes must be a positive integer")
    encoded_bytes = 4 * ((raw_bytes + 2) // 3)
    return raw_bytes + 3 * encoded_bytes


@dataclass(frozen=True)
class _Asset:
    path: Path
    kind: str
    sha256: str
    byte_size: int
    media_type: str


def _validated_stream_timeout(
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise ProviderExecutorError("provider_job_invalid")
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ProviderExecutorError("provider_job_invalid") from None
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ProviderExecutorError("provider_job_invalid")
    return normalized


def _validated_job(job: object, paths: Iterable[str | Path]) -> tuple[dict[str, Any], list[_Asset]]:
    if not isinstance(job, dict):
        raise ProviderExecutorError("provider_job_invalid")
    request_digest = str(job.get("request_digest") or "").strip().lower()
    requested_model = str(job.get("model") or "").strip()
    prompt = job.get("prompt")
    prompt_first = job.get("prompt_first", False)
    raw_assets = job.get("assets")
    if not _HEX_64.fullmatch(request_digest) or not _MODEL.fullmatch(requested_model):
        raise ProviderExecutorError("provider_job_invalid")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProviderExecutorError("provider_job_invalid")
    try:
        prompt_size = len(prompt.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProviderExecutorError("provider_job_invalid") from None
    if prompt_size > MAX_PROMPT_BYTES:
        raise ProviderExecutorError("provider_prompt_too_large")
    if not isinstance(prompt_first, bool):
        raise ProviderExecutorError("provider_job_invalid")
    if job.get("reasoning_effort", "high") != "high" or job.get("stream", True) is not True:
        raise ProviderExecutorError("provider_job_invalid")
    first_event_timeout = _validated_stream_timeout(
        job.get("first_event_timeout_s"),
        minimum=MIN_FIRST_EVENT_TIMEOUT_SECONDS,
        maximum=MAX_FIRST_EVENT_TIMEOUT_SECONDS,
    )
    attempt_ttl = _validated_stream_timeout(
        job.get("attempt_ttl_s"),
        minimum=MIN_ATTEMPT_TTL_SECONDS,
        maximum=MAX_ATTEMPT_TTL_SECONDS,
    )
    sse_idle_timeout = _validated_stream_timeout(
        job.get("sse_idle_timeout_s"),
        minimum=MIN_SSE_IDLE_TIMEOUT_SECONDS,
        maximum=MAX_SSE_IDLE_TIMEOUT_SECONDS,
    )
    if first_event_timeout > attempt_ttl or sse_idle_timeout > attempt_ttl:
        raise ProviderExecutorError("provider_job_invalid")
    output_tokens = job.get("max_output_tokens", MAX_OUTPUT_TOKENS)
    if (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or not MIN_OUTPUT_TOKENS <= output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ProviderExecutorError("provider_job_invalid")
    if not isinstance(raw_assets, list) or not 1 <= len(raw_assets) <= 3:
        raise ProviderExecutorError("provider_assets_invalid")
    local_paths = [Path(path) for path in paths]
    if len(local_paths) != len(raw_assets):
        raise ProviderExecutorError("provider_assets_invalid")
    try:
        resolved_paths = [path.resolve(strict=True) for path in local_paths]
    except (OSError, RuntimeError):
        raise ProviderExecutorError("provider_asset_unavailable") from None
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ProviderExecutorError("provider_assets_invalid")

    assets: list[_Asset] = []
    for position, (raw, path) in enumerate(zip(raw_assets, resolved_paths), 1):
        if not isinstance(raw, dict) or set(raw) != _ASSET_KEYS:
            raise ProviderExecutorError("provider_assets_invalid")
        kind = raw.get("kind")
        sha256 = str(raw.get("sha256") or "").strip().lower()
        byte_size = raw.get("byte_size")
        media_type = raw.get("media_type")
        if (
            kind != f"input_{position:02d}"
            or not _HEX_64.fullmatch(sha256)
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or not 1 <= byte_size <= MAX_IMAGE_BYTES
            or media_type not in _SUPPORTED_MIME_TYPES
        ):
            raise ProviderExecutorError("provider_assets_invalid")
        try:
            stat = path.stat()
        except OSError:
            raise ProviderExecutorError("provider_asset_unavailable") from None
        if not path.is_file() or stat.st_size != byte_size:
            raise ProviderExecutorError("provider_asset_identity_mismatch")
        assets.append(
            _Asset(
                path=path,
                kind=kind,
                sha256=sha256,
                byte_size=byte_size,
                media_type=media_type,
            )
        )
    normalized = {
        "request_digest": request_digest,
        "model": requested_model,
        "prompt": prompt,
        "prompt_first": prompt_first,
        "max_output_tokens": output_tokens,
        "first_event_timeout_s": first_event_timeout,
        "attempt_ttl_s": attempt_ttl,
        "sse_idle_timeout_s": sse_idle_timeout,
    }
    return normalized, assets


def _detected_mime_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _encode_assets(assets: list[_Asset]) -> list[dict[str, str]]:
    encoded: list[dict[str, str]] = []
    for asset in assets:
        try:
            with asset.path.open("rb") as stream:
                raw = stream.read(asset.byte_size + 1)
        except OSError:
            raise ProviderExecutorError("provider_asset_unavailable") from None
        if (
            len(raw) != asset.byte_size
            or hashlib.sha256(raw).hexdigest() != asset.sha256
            or _detected_mime_type(raw) != asset.media_type
        ):
            raise ProviderExecutorError("provider_asset_identity_mismatch")
        image_url = (
            f"data:{asset.media_type};base64,"
            + base64.b64encode(raw).decode("ascii")
        )
        del raw
        encoded.append({"type": "input_image", "image_url": image_url})
    return encoded


def _http_status_code(response: object) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getter = getattr(response, "getcode", None)
        status = getter() if callable(getter) else 200
    try:
        return int(status)
    except (TypeError, ValueError):
        raise ProviderExecutorError("provider_response_invalid") from None


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    value = getter("Content-Type", "") if callable(getter) else ""
    return str(value or "").split(";", 1)[0].strip().lower()


def _status_error(status: int) -> ProviderExecutorError:
    if status in {401, 403}:
        return ProviderExecutorError("provider_auth_failed")
    if status == 429:
        return ProviderExecutorError("provider_rate_limited")
    if status in {408, 504}:
        return ProviderExecutorError("provider_timeout")
    if 500 <= status <= 599:
        return ProviderExecutorError("provider_unavailable")
    return ProviderExecutorError("provider_request_rejected")


def _completed_output_text(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(part["text"])
    return "".join(chunks)


def _dispatch_sse_event(data_lines: list[str], state: dict[str, Any]) -> None:
    if not data_lines:
        return
    data = "\n".join(data_lines).strip()
    data_lines.clear()
    if not data:
        return
    if data == "[DONE]":
        state["done_seen"] = True
        return
    try:
        event = json.loads(data)
    except (TypeError, json.JSONDecodeError):
        raise ProviderExecutorError("provider_sse_invalid") from None
    if not isinstance(event, dict):
        raise ProviderExecutorError("provider_sse_invalid")
    event_type = str(event.get("type") or "")
    if event_type == "error" or event.get("error"):
        raise ProviderExecutorError("provider_stream_error")
    if event_type == "response.failed":
        raise ProviderExecutorError("provider_response_failed")
    if event_type == "response.incomplete":
        raise ProviderExecutorError("provider_response_incomplete")
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if not isinstance(delta, str):
            raise ProviderExecutorError("provider_sse_invalid")
        state["content_bytes"] += len(delta.encode("utf-8"))
        if state["content_bytes"] > MAX_RESPONSE_BYTES:
            raise ProviderExecutorError("provider_response_too_large")
        state["chunks"].append(delta)
        return
    if event_type != "response.completed":
        return
    response = event.get("response")
    if not isinstance(response, dict):
        raise ProviderExecutorError("provider_sse_invalid")
    terminal_status = str(response.get("status") or "completed").strip()
    if terminal_status != "completed":
        raise ProviderExecutorError("provider_response_incomplete")
    if not state["chunks"]:
        fallback = _completed_output_text(response)
        state["content_bytes"] = len(fallback.encode("utf-8"))
        if state["content_bytes"] > MAX_RESPONSE_BYTES:
            raise ProviderExecutorError("provider_response_too_large")
        if fallback:
            state["chunks"].append(fallback)
    state["completed_seen"] = True


def _set_response_socket_timeout(response: object, timeout_s: float) -> bool:
    """Set the underlying urllib socket timeout when one is available."""

    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    connection = getattr(response, "_connection", None)
    candidates = (
        response,
        fp,
        raw,
        getattr(raw, "_sock", None),
        connection,
        getattr(connection, "sock", None),
    )
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if not callable(setter):
            continue
        try:
            setter(max(0.001, float(timeout_s)))
        except (OSError, ValueError, TypeError):
            continue
        return True
    return False


class _TimedLineReader:
    """Read one SSE line with a deadline, including test/custom openers.

    urllib responses expose a socket whose timeout can be adjusted directly. A
    custom opener may return only an iterator, so use one daemon reader thread
    as a bounded fallback and close the response on timeout.
    """

    def __init__(self, response: object):
        self._response = response
        self._readline = getattr(response, "readline", None)
        try:
            self._iterator = iter(response)
        except TypeError:
            self._iterator = None
        self._socket_mode = _set_response_socket_timeout(response, 1.0)
        self._stop = threading.Event()
        self._queue: Queue[tuple[str, object]] | None = None
        self._thread: threading.Thread | None = None

    def _read_one(self) -> bytes:
        if callable(self._readline):
            try:
                return self._readline(MAX_RESPONSE_BYTES + 1)
            except TypeError:
                return self._readline()
        if self._iterator is None:
            raise ProviderExecutorError("provider_sse_invalid")
        return next(self._iterator)

    def _put(self, item: tuple[str, object]) -> None:
        queue = self._queue
        if queue is None:
            return
        while not self._stop.is_set():
            try:
                queue.put(item, timeout=0.1)
                return
            except Full:
                continue

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                line = self._read_one()
            except StopIteration:
                self._put(("eof", None))
                return
            except BaseException as exc:
                self._put(("error", exc))
                return
            self._put(("line", line))

    def read(self, timeout_s: float) -> bytes:
        if self._socket_mode:
            _set_response_socket_timeout(self._response, timeout_s)
            try:
                return self._read_one()
            except StopIteration:
                raise
            except (TimeoutError, socket.timeout):
                raise TimeoutError from None

        if self._thread is None:
            self._queue = Queue(maxsize=1)
            self._thread = threading.Thread(
                target=self._run,
                name="mathagent-provider-sse-reader",
                daemon=True,
            )
            self._thread.start()
        queue = self._queue
        if queue is None:
            raise RuntimeError("provider SSE reader is not initialized")
        try:
            kind, value = queue.get(timeout=max(0.001, float(timeout_s)))
        except Empty:
            raise TimeoutError from None
        if kind == "eof":
            raise StopIteration
        if kind == "error":
            if isinstance(value, BaseException):
                raise value
            raise RuntimeError("provider SSE reader failed")
        if not isinstance(value, bytes):
            raise ProviderExecutorError("provider_sse_invalid")
        return value

    def close(self) -> None:
        self._stop.set()
        closer = getattr(self._response, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)


def _parse_sse(
    response: object,
    *,
    first_event_timeout_s: float,
    attempt_ttl_s: float,
    sse_idle_timeout_s: float,
    started_at: float | None = None,
) -> str:
    state: dict[str, Any] = {
        "chunks": [],
        "content_bytes": 0,
        "completed_seen": False,
        "done_seen": False,
        "data_seen": False,
    }
    data_lines: list[str] = []
    body_bytes = 0
    started = time.monotonic() if started_at is None else float(started_at)
    first_deadline = started + first_event_timeout_s
    attempt_deadline = started + attempt_ttl_s
    reader = _TimedLineReader(response)
    try:
        while True:
            now = time.monotonic()
            remaining_attempt = attempt_deadline - now
            if remaining_attempt <= 0:
                raise ProviderExecutorError("provider_timeout")
            remaining_first = first_deadline - now
            if not state["data_seen"] and remaining_first <= 0:
                raise ProviderExecutorError("provider_timeout")
            timeout_s = min(
                remaining_attempt,
                sse_idle_timeout_s if state["data_seen"] else remaining_first,
            )
            try:
                raw_line = reader.read(timeout_s)
            except StopIteration:
                break
            except (TimeoutError, socket.timeout):
                raise ProviderExecutorError("provider_timeout") from None
            if not isinstance(raw_line, bytes):
                raise ProviderExecutorError("provider_sse_invalid")
            body_bytes += len(raw_line)
            if body_bytes > MAX_RESPONSE_BYTES:
                raise ProviderExecutorError("provider_response_too_large")
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                raise ProviderExecutorError("provider_sse_invalid") from None
            if not line:
                _dispatch_sse_event(data_lines, state)
                if state["completed_seen"]:
                    break
                if state["done_seen"]:
                    break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data = line[5:].lstrip(" ")
                if data:
                    state["data_seen"] = True
                data_lines.append(data)
        if data_lines and not state["completed_seen"]:
            _dispatch_sse_event(data_lines, state)
        if not state["completed_seen"]:
            raise ProviderExecutorError("provider_completed_missing")
        content = "".join(state["chunks"]).strip()
        if not content:
            raise ProviderExecutorError("provider_content_empty")
        return content
    finally:
        reader.close()


class ProviderExecutor:
    def __init__(
        self,
        *,
        byte_budget: ThreadSafeByteBudget | None = None,
        opener: Callable[..., Any] = urlopen,
        budget_wait_timeout_s: float | None = None,
    ) -> None:
        self.byte_budget = byte_budget or ThreadSafeByteBudget()
        self._opener = opener
        self._budget_wait_timeout_s = budget_wait_timeout_s

    def execute(
        self,
        job: dict[str, Any],
        paths: Iterable[str | Path],
        profile: ProviderProfile,
    ) -> dict[str, Any]:
        if not isinstance(profile, ProviderProfile):
            raise ProviderExecutorError("provider_profile_invalid")
        normalized, assets = _validated_job(job, paths)
        if normalized["model"] != profile.model:
            raise ProviderExecutorError("provider_model_mismatch")
        estimate = sum(estimated_payload_bytes(asset.byte_size) for asset in assets)
        started = time.monotonic()
        with self.byte_budget.reserve(
            estimate,
            timeout_s=self._budget_wait_timeout_s,
        ):
            content: list[dict[str, str]] | None = None
            payload: dict[str, Any] | None = None
            encoded_body: bytes | None = None
            request: Request | None = None
            try:
                content = _encode_assets(assets)
                text_content = {"type": "input_text", "text": normalized["prompt"]}
                if normalized["prompt_first"]:
                    content.insert(0, text_content)
                else:
                    content.append(text_content)
                payload = {
                    "model": profile.model,
                    "input": [{"role": "user", "content": content}],
                    "max_output_tokens": normalized["max_output_tokens"],
                    "reasoning": {"effort": "high"},
                    "stream": True,
                }
                encoded_body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                request = Request(
                    profile.responses_url,
                    data=encoded_body,
                    headers={
                        "Authorization": f"Bearer {profile.api_key}",
                        "Accept": "text/event-stream",
                        "Content-Type": "application/json",
                        "User-Agent": "MathAgent-Windows-Provider/1",
                        "X-Request-ID": normalized["request_digest"],
                    },
                    method="POST",
                )
                try:
                    # The absolute attempt TTL covers connection setup and
                    # response-header time as well as SSE parsing.  Starting
                    # the clock only after ``urlopen`` returns would allow a
                    # slow connect to outlive the server lease and create a
                    # duplicate paid retry.
                    attempt_started = time.monotonic()
                    with self._opener(
                        request,
                        timeout=min(
                            profile.request_timeout_s,
                            normalized["first_event_timeout_s"],
                            normalized["attempt_ttl_s"],
                        ),
                    ) as response:
                        status = _http_status_code(response)
                        if status != 200:
                            raise _status_error(status)
                        if _content_type(response) != "text/event-stream":
                            raise ProviderExecutorError("provider_content_type_invalid")
                        output = _parse_sse(
                            response,
                            first_event_timeout_s=normalized["first_event_timeout_s"],
                            attempt_ttl_s=normalized["attempt_ttl_s"],
                            sse_idle_timeout_s=normalized["sse_idle_timeout_s"],
                            started_at=attempt_started,
                        )
                except ProviderExecutorError:
                    raise
                except HTTPError as exc:
                    raise _status_error(int(exc.code)) from None
                except (TimeoutError, socket.timeout):
                    raise ProviderExecutorError("provider_timeout") from None
                except (URLError, OSError) as exc:
                    reason = getattr(exc, "reason", None)
                    if isinstance(reason, (TimeoutError, socket.timeout)):
                        raise ProviderExecutorError("provider_timeout") from None
                    raise ProviderExecutorError("provider_transport_failed") from None
                except Exception:
                    raise ProviderExecutorError("provider_transport_failed") from None
            finally:
                if content is not None:
                    content.clear()
                if payload is not None:
                    payload.clear()
                encoded_body = None
                request = None
        return {
            "status": "succeeded",
            "contract_version": PROVIDER_RESPONSE_CONTRACT_VERSION,
            "request_digest": normalized["request_digest"],
            "model": profile.model,
            "content": output,
            "metrics": {
                "image_count": len(assets),
                "payload_budget_bytes": estimate,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            },
        }


_DEFAULT_EXECUTOR = ProviderExecutor()


def execute(
    job: dict[str, Any],
    paths: Iterable[str | Path],
    profile: ProviderProfile,
) -> dict[str, Any]:
    """Execute one hash-bound Responses SSE job using only a local profile."""

    return _DEFAULT_EXECUTOR.execute(job, paths, profile)
