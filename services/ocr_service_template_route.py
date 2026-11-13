"""Teacher-template requests on the qwen3.8-max Token Plan route, through the OCR pool scheduler.

- ``call_gpt_vision`` sends teacher-template images and ``call_template_max_text``
  one text-only JSON request (reasoning always high) on this route; both share
  the template Max in-flight gate.
- ``_call_multi_pool_vision_request`` sends one image request through the
  template scheduler (``_get_template_ocr_scheduler_for_route``), with stable,
  anonymous routing scopes derived from the batch-local image path
  (``_multi_pool_route_keys``, ``_multi_pool_route_keys_for_phase``), and records
  the route it used (``_set_template_route_diagnostics``).
- ``_multi_pool_response_validator`` accepts the answer only as valid JSON and,
  for student transcription prompts, with a consistent evidence contract;
  ``_accepts_keyword`` keeps older injected schedulers working.

Moved unchanged from services/ocr_service.py, which still exposes these names.
"""

import hashlib
import inspect
import json
import os
import re
from pathlib import Path

from config import OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS
from services.analyzer import parse_llm_json
from services.ocr_service_context import _vision_phase_key, _vision_route_ordinal
from services.ocr_service_local_image import (
    _LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS,
    _estimated_vision_images_payload_bytes,
    _get_local_ocr_payload_budget,
    _get_local_ocr_preprocess_semaphore,
    _get_local_ocr_request_semaphore,
    _run_local_ocr_image_component,
)
from services.ocr_service_transport import _encode_vision_image
from services.ocr_service_validation import validate_visual_transcriptions


def _multi_pool_route_keys_for_phase(
    image_path: str,
    phase_key: str,
) -> tuple[str, str, str]:
    """Derive stable anonymous routing scope from one batch-local image path."""
    resolved = os.path.realpath(image_path)
    parts = Path(resolved).parts
    lower_parts = [part.lower() for part in parts]
    batch_part = next(
        (part for part in parts if part.lower().startswith("quick_batch_")),
        "",
    )
    answer_sheet_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("answer_sheet_")
        ),
        -1,
    )
    student_upload_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("student_upload_")
        ),
        -1,
    )
    evidence_set_index = next(
        (
            index
            for index, part in enumerate(lower_parts)
            if part.startswith("evidence-set-")
        ),
        -1,
    )
    answer_sheet_student_batch = (
        0 <= answer_sheet_index < student_upload_index < evidence_set_index
    )
    student_paper_scope = ""
    if batch_part:
        batch_key = batch_part.lower()
        priority = "batch"
    elif answer_sheet_student_batch:
        batch_key = (
            f"{lower_parts[answer_sheet_index]}:student-recognition"
        )
        student_paper_scope = (
            f"{lower_parts[student_upload_index]}:"
            f"{lower_parts[evidence_set_index]}"
        )
        priority = "batch"
    else:
        # Template/bank/single-image operations are user-facing and use the
        # interactive scheduler priority. The raw directory is hashed by the
        # scheduler before any coordinator key is written.
        batch_key = f"interactive:{os.path.dirname(resolved)}"
        priority = "interactive"

    phase_key = str(phase_key or "").strip()
    if phase_key:
        # The scheduler hashes this value before writing coordinator keys.  A
        # phase gets its own deadline while still sharing pool, teacher, RPM,
        # circuit-breaker, and explicitly selected retry-ledger controls.
        batch_key = f"{batch_key}:phase:{phase_key}"

    filename = os.path.basename(resolved)
    filename = re.sub(r"\.identity-[0-9a-f]+\.jpg$", "", filename, flags=re.I)
    filename = re.sub(
        r"_preprocessed-[0-9a-f]{32}\.jpg$",
        "",
        filename,
        flags=re.I,
    )
    filename = re.sub(r"_preprocessed\.jpg$", "", filename, flags=re.I)
    paper_key = (
        f"{batch_key}:{student_paper_scope}:{filename}"
        if student_paper_scope
        else f"{batch_key}:{filename}"
    )
    return batch_key, paper_key, priority


def _multi_pool_route_keys(image_path: str) -> tuple[str, str, str]:
    return _multi_pool_route_keys_for_phase(image_path, _vision_phase_key.get())


def _multi_pool_response_validator(prompt: str, response_validator=None):
    """Validate JSON and, for student transcription prompts, its evidence contract."""
    strict_transcription = "answer_state" in prompt and "student_answer" in prompt
    slot_contracts = {}
    drawing_only_numbers = set()
    # Read only the frozen contract, never infer IDs from model output.
    for encoded in re.findall(r"^逐槽转录合同=(.+)$", prompt, re.MULTILINE):
        from services.template_slots import normalize_slot_ids
        mapping = json.loads(encoded)
        if not isinstance(mapping, dict):
            raise ValueError("invalid slot transcription contract")
        for number, ids in mapping.items():
            if number in slot_contracts:
                raise ValueError("duplicate slot transcription parent")
            slot_contracts[number] = normalize_slot_ids(number, ids, len(ids))
    # The page contract names pure drawing questions even though they have no
    # textual slot IDs.  Keep this narrow: only an explicit drawing type grants
    # the empty-row normalization below.
    for encoded in re.findall(r"^作答类型=(.+)$", prompt, re.MULTILINE):
        try:
            types = json.loads(encoded)
        except json.JSONDecodeError:
            # Some legacy prompt builders leave a named placeholder before
            # appending the frozen page contract; it carries no type facts.
            continue
        if isinstance(types, dict):
            drawing_only_numbers.update(
                str(number).strip() for number, kind in types.items()
                if str(kind).strip().lower() == "drawing"
            )

    def validate(content: str) -> None:
        data = parse_llm_json(content)
        if strict_transcription:
            if not isinstance(data, dict):
                raise ValueError("student transcription must be a JSON object")
            questions = validate_visual_transcriptions(
                data.get("questions", []),
                slot_contracts=slot_contracts,
                drawing_only_numbers=drawing_only_numbers,
            )
            if not questions:
                raise ValueError("student transcription returned no questions")
            actual_qns = [
                str(item.get("question_number") or "").strip()
                for item in questions
            ]
            if len(actual_qns) != len(set(actual_qns)):
                raise ValueError(
                    "student transcription returned duplicate question numbers"
                )

            # Frozen-template prompts carry an explicit machine-readable order.
            # Missing, extra, or reordered questions must retry inside the same
            # scheduler request rather than becoming a downstream batch failure.
            match = re.search(r"question_numbers=([^\n]+)", prompt)
            if match:
                raw_expected = match.group(1).strip()
                try:
                    expected_value = json.loads(raw_expected)
                except json.JSONDecodeError:
                    expected_value = [
                        item.strip().strip("'\"")
                        for item in raw_expected.strip("[]").split(",")
                        if item.strip()
                    ]
                expected_qns = [str(item).strip() for item in expected_value]
                if actual_qns != expected_qns:
                    raise ValueError("student transcription question order mismatch")

        if response_validator is not None:
            return response_validator(content)
        return None

    return validate


def _get_template_ocr_scheduler_for_route(pool_route: str):
    """Resolve the qwen3.8-max template scheduler without breaking test getters.

    A few integrations still inject a zero-argument getter; signature
    inspection keeps those callers compatible without swallowing a real
    construction error from the production getter.
    """
    from services.ocr_pool_scheduler import get_template_ocr_pool_scheduler

    route = str(pool_route or "fallback").strip().lower()
    if route in {"qwen", "qwen3.8max"}:
        route = "fallback"
    try:
        parameters = inspect.signature(get_template_ocr_pool_scheduler).parameters
    except (TypeError, ValueError):
        parameters = None
    if parameters is not None and (
        "pool_route" not in parameters
        and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    ):
        return get_template_ocr_pool_scheduler()
    return get_template_ocr_pool_scheduler(pool_route=route)


def _accepts_keyword(callable_obj, name: str) -> bool:
    """Check an optional keyword without breaking legacy injected schedulers."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _set_template_route_diagnostics(
    diagnostics: dict[str, object] | None,
    *,
    route: str,
    reasoning_effort: str,
) -> None:
    if not isinstance(diagnostics, dict):
        return
    diagnostics["actual_route"] = str(route)
    diagnostics["actual_reasoning_effort"] = str(reasoning_effort)


async def _call_multi_pool_vision_request(
    image_path: str | list[str],
    prompt: str,
    max_tokens: int = 6000,
    *,
    pool_route: str | None = None,
    response_validator=None,
    prompt_first: bool = False,
    reasoning_effort: str | None = None,
    scheduler=None,
    image_handoff_callback=None,
    image_settle_callbacks=(),
    cancel_on_waiter_cancel: bool = False,
    route_diagnostics: dict[str, object] | None = None,
    json_object: bool = False,
) -> str:
    """Send one template request through the qwen3.8-max Token Plan scheduler."""
    image_paths = [str(image_path)] if isinstance(image_path, str) else [
        str(path) for path in image_path
    ]
    try:
        output_token_limit = int(max_tokens)
        if output_token_limit < 1:
            raise ValueError("max_tokens must be positive")
    except (TypeError, ValueError) as exc:
        raise ValueError("max_tokens must be an integer") from exc
    if not isinstance(json_object, bool):
        raise ValueError("json_object must be a boolean")

    def encode_images():
        return [_encode_vision_image(path) for path in image_paths]

    encoded_images = await _run_local_ocr_image_component(
        encode_images,
        semaphore=_get_local_ocr_preprocess_semaphore(),
        on_handoff=image_handoff_callback,
        settle_callbacks=image_settle_callbacks,
    )
    image_content = [
        {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{b64}",
            "detail": "high",
        }
        for b64, mime in encoded_images
    ]
    text_content = {"type": "input_text", "text": prompt}
    content = (
        [text_content, *image_content]
        if prompt_first
        else [*image_content, text_content]
    )
    from config import OCR_TEMPLATE_GPT_REASONING_EFFORT, REASONING_EFFORTS
    selected_reasoning_effort = str(
        reasoning_effort or OCR_TEMPLATE_GPT_REASONING_EFFORT
    ).strip().lower()
    if selected_reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(
            "reasoning_effort must be none, minimal, low, medium, high, xhigh, or max"
        )

    payload = {
        "input": [{
            "role": "user",
            "content": content,
        }],
        "max_output_tokens": output_token_limit,
        "stream": True,
        "store": False,
    }
    # Internal scheduler metadata. It is consumed when constructing the
    # provider request and removed before the HTTP body is sent. Template GPT
    # calls always carry the canonical default explicitly, even when callers
    # omit ``reasoning_effort``.
    payload["model_reasoning_effort"] = selected_reasoning_effort
    if json_object:
        # Standard Responses text.format. The scheduler's Qwen chat adapter
        # converts it to the native ``response_format`` field of the provider
        # body, so the opt-in survives the internal Responses-shaped payload
        # instead of being dropped at the pool boundary.
        payload["text"] = {"format": {"type": "json_object"}}
    del encoded_images
    route_path = image_paths[0]
    _, paper_key, _ = _multi_pool_route_keys(route_path)
    ordinal = _vision_route_ordinal.get()
    if ordinal is not None:
        paper_key = f"{paper_key}-route-{ordinal}"
    # Only the qwen3.8-max Token Plan route remains; the GPT canonical and
    # YS/exemption routes are retired and fail closed in the scheduler.
    pool_route = str(pool_route if pool_route is not None else "fallback").strip().lower()
    if pool_route in {"qwen", "qwen3.8max"}:
        pool_route = "fallback"
    if pool_route != "fallback":
        raise ValueError(
            f"template pool_route {pool_route!r} is retired; only 'fallback' (qwen3.8-max) remains"
        )
    if not isinstance(route_diagnostics, dict):
        route_diagnostics = None
    if route_diagnostics is not None:
        route_diagnostics["requested_route"] = pool_route
        route_diagnostics["requested_reasoning_effort"] = selected_reasoning_effort
    scheduler = scheduler or _get_template_ocr_scheduler_for_route(pool_route)
    if route_diagnostics is not None:
        route_diagnostics["_scheduler"] = scheduler
    validator = _multi_pool_response_validator(
        prompt, response_validator=response_validator,
    )
    # The caller's effort is the one sent: the scheduler writes this argument
    # over the payload's, so a fixed value here would turn every template
    # request into that tier whatever the pipeline asked for.
    fallback_kwargs = {
        "request_key": paper_key,
        "validator": validator,
        "reasoning_effort": selected_reasoning_effort,
    }
    if route_diagnostics is not None and _accepts_keyword(
        scheduler.request_template_fallback_pool, "route_diagnostics"
    ):
        fallback_kwargs["route_diagnostics"] = route_diagnostics
    _set_template_route_diagnostics(
        route_diagnostics,
        route="qwen3.8max",
        reasoning_effort=selected_reasoning_effort,
    )
    return await scheduler.request_template_fallback_pool(
        payload,
        **fallback_kwargs,
    )


async def call_gpt_vision(
    image_path: str | list[str],
    prompt: str,
    max_tokens: int = OCR_TEMPLATE_GPT_MAX_OUTPUT_TOKENS,
    *,
    pool_route: str | None = None,
    response_validator=None,
    prompt_first: bool = False,
    reasoning_effort: str | None = None,
    cancel_on_waiter_cancel: bool = False,
    route_diagnostics: dict[str, object] | None = None,
    json_object: bool = False,
) -> str:
    """Send teacher-template images to the qwen3.8-max Token Plan route."""
    paths = [image_path] if isinstance(image_path, str) else list(image_path)
    if not 1 <= len(paths) <= 12 or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths):
        raise ValueError("Template vision requires 1-12 unique local image paths")
    payload_bytes = _estimated_vision_images_payload_bytes(paths)
    async with _get_local_ocr_request_semaphore():
        payload_budget = _get_local_ocr_payload_budget()
        reserved_bytes = await payload_budget.acquire(
            payload_bytes,
            timeout_seconds=_LOCAL_OCR_PAYLOAD_BUDGET_WAIT_SECONDS,
        )
        handed_off = False

        def mark_handed_off() -> None:
            nonlocal handed_off
            handed_off = True

        async def release_handed_off_budget() -> None:
            await payload_budget.release(reserved_bytes)

        try:
            return await _call_multi_pool_vision_request(
                image_path,
                prompt,
                max_tokens=max_tokens,
                pool_route=pool_route,
                response_validator=response_validator,
                prompt_first=prompt_first,
                reasoning_effort=reasoning_effort,
                cancel_on_waiter_cancel=cancel_on_waiter_cancel,
                route_diagnostics=route_diagnostics,
                json_object=json_object,
                image_handoff_callback=mark_handed_off,
                image_settle_callbacks=(release_handed_off_budget,),
            )
        finally:
            if not handed_off:
                await payload_budget.release(reserved_bytes)


async def call_template_max_text(
    messages: list[dict],
    *,
    max_tokens: int = 32768,
    response_validator=None,
) -> str:
    """Send one text-only JSON request on the qwen3.8-max Token Plan route.

    The request shares the template Max in-flight gate with directory/fullsolve
    and always uses reasoning=high; it carries no image payload.
    """
    payload = {
        "input": [
            {"role": str(message["role"]), "content": str(message["content"])}
            for message in messages
        ],
        "max_output_tokens": int(max_tokens),
        "stream": True,
        "store": False,
        "model_reasoning_effort": "high",
        "text": {"format": {"type": "json_object"}},
    }
    request_key = "max-text-" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    scheduler = _get_template_ocr_scheduler_for_route("fallback")

    def validate(content: str) -> None:
        parse_llm_json(content)
        if response_validator is not None:
            response_validator(content)

    return await scheduler.request_template_fallback_pool(
        payload,
        request_key=request_key,
        validator=validate,
    )
