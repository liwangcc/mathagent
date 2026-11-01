"""Secret-free processing diagnostics. Only terminal boundaries report failures.

No exception text, prompts, response bodies, URLs, image paths or student data
are serialized. Original exceptions/causes remain in memory; logs retain typed
cause chains and source locations (without locals or source lines).
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("mathagent.processing")
_context = contextvars.ContextVar("processing_context", default=None)
_lock = threading.RLock()
STAGES = {
    "import": "文件导入", "template": "模板处理", "image": "图像处理",
    "model": "模型调用", "transcription": "答案转录", "solve": "解题",
    "grading": "判分", "storage": "结果保存", "request": "请求处理",
}
# code -> summary, action, retryable. Retryable is advice, not a new retry policy.
ERRORS = {
    "FORMAT_INVALID": ("文件或请求格式错误", "检查文件内容及提交格式后重试。", False),
    "FORMAT_UNSUPPORTED": ("不支持此文件格式", "转换为页面支持的文件格式后重新导入。", False),
    "ARCHIVE_CORRUPT": ("压缩包损坏或内容不完整", "重新下载或重新打包后导入。", False),
    "ARCHIVE_ENCRYPTED": ("压缩包已加密", "在本机解密后重新导入；不要上传密码。", False),
    "DEPENDENCY_MISSING": ("处理组件未安装或不可用", "联系管理员安装或恢复对应处理组件。", False),
    "PROVIDER_TIMEOUT": ("服务未在时限内完成", "稍后重试；持续出现时提供诊断编号。", True),
    "PROVIDER_RATE_LIMITED": ("模型服务限流", "等待当前任务结束后重试，不要连续重复提交。", True),
    "PROVIDER_AUTH_FAILED": ("模型服务认证失败", "联系管理员核对模型凭据和访问权限。", False),
    "PROVIDER_UNAVAILABLE": ("模型服务暂不可用", "稍后重试；持续出现时提供诊断编号。", True),
    "NETWORK_ERROR": ("服务连接中断", "检查网络并稍后重试。", True),
    "TRANSCRIPTION_EMPTY": ("模型未返回有效转录", "检查原图和差分证据；这不代表学生未作答。", True),
    "OUTPUT_PARSE_FAILED": ("模型返回结果不符合格式要求", "重试仅会重新生成模型结果，可能再次失败；持续出现时请联系管理员并提供诊断编号。", True),
    "TEMPLATE_SCHEMA_INVALID": ("模板题目未通过规则校验", "请教师复核题目定位后再确认；当前无法编辑时请联系管理员并提供诊断编号。重试仅会重新生成模型结果，可能再次违反同一规则，不建议连续重试。", False),
    "IMAGE_COORDINATES_INVALID": ("图像坐标或版面绑定异常", "检查当前模板的框选、页尺寸与坐标空间并重新保存。", False),
    "IMAGE_CANONICALIZATION_FAILED": ("页面矫正失败", "检查原图边界、方向与清晰度后重新导入。", False),
    "IMAGE_CANDIDATE_INVALID": ("候选图像与原图校验不一致", "重新生成客户端标准图后导入；持续出现时提供诊断编号。", False),
    "STORAGE_FAILED": ("处理结果未能保存", "不要重复提交；联系管理员检查存储。", True),
    "REQUEST_REJECTED": ("请求未通过检查", "按页面提示检查权限、输入或当前任务状态。", False),
    "PROCESSING_FAILED": ("处理发生异常", "联系管理员并提供诊断编号。", False),
}
_ALIASES = {
    "VISION_AUTH_FAILED": "PROVIDER_AUTH_FAILED", "CREDENTIAL_UNAVAILABLE": "PROVIDER_AUTH_FAILED",
    "VISION_TIMEOUT": "PROVIDER_TIMEOUT", "TEMPLATE_IMPORT_TIMEOUT": "PROVIDER_TIMEOUT",
    "SDK_UNAVAILABLE": "DEPENDENCY_MISSING", "TEMPLATE_OUTPUT_INVALID": "OUTPUT_PARSE_FAILED",
    "TEMPLATE_NO_QUESTIONS": "TRANSCRIPTION_EMPTY", "INVALID_PROVIDER_RESPONSE": "OUTPUT_PARSE_FAILED",
    "TEMPLATE_CANONICALIZATION_FAILED": "IMAGE_CANONICALIZATION_FAILED",
    "TEMPLATE_CANONICAL_CANDIDATE_INVALID": "IMAGE_CANDIDATE_INVALID",
    "TEMPLATE_CROP_FAILED": "IMAGE_COORDINATES_INVALID",
}
_OPAQUE = re.compile(r"(?:[a-fA-F0-9]{12,64}|[a-fA-F0-9-]{36})\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")

# Public details are built from rule IDs and bounded question identities only;
# arbitrary exception text, model fields and response bodies never cross here.
_SCHEMA_RULES = {
    "drawing_regions_invalid": "作图区域无效",
    "drawing_anchor_invalid": "作图题号标记坐标无效（锚点必须是整页范围内的两个有限坐标）",
    "drawing_anchor_not_left": "作图标签必须位于主作图区左侧",
    "non_graphic_geometry": "非作图题不能包含作图定位",
    "answer_kind_invalid": "作答类型必须为文字、作图或混合类型",
    "question_type_invalid": "题型必须为选择、填空或解答题",
    "question_number_duplicate": "题号重复",
    "directory_identity_invalid": "目录节点身份无效",
    "printed_path_invalid": "印刷题号层级无效",
    "answer_regions_invalid": "作答框无效",
    "slot_labels_overlap": "有两个作答框在同一行几乎重叠，题号标签无法分开（请拉开这两个框或删除重复的框）",
}
_QUESTION_ID = re.compile(r"[0-9一二三四五六七八九十百零〇①②③④⑤⑥⑦⑧⑨⑩.．()（）\-、]{1,32}\Z")


def _schema_issue(value):
    if not isinstance(value, dict) or value.get("rule") not in _SCHEMA_RULES:
        return None
    number = value.get("question_number")
    position = value.get("question_index")
    return {
        "rule": value["rule"],
        "question_number": number if isinstance(number, str) and _QUESTION_ID.fullmatch(number) else None,
        "question_index": position if type(position) is int and 1 <= position <= 10000 else None,
    }


def _schema_message(issue):
    number, index = issue["question_number"], issue["question_index"]
    label = f"第 {number} 题" if number else (f"第 {index} 个题目条目" if index else "当前题目")
    return f"{label}{_SCHEMA_RULES[issue['rule']]}，请教师复核"


class TemplateSchemaError(ValueError):
    code = "TEMPLATE_SCHEMA_INVALID"

    def __init__(self, rule, question_number, *, question_index=None):
        self.schema_issue = _schema_issue({"rule": rule, "question_number": str(question_number),
                                           "question_index": question_index})
        if self.schema_issue is None:
            raise ValueError("Unknown template schema rule")
        super().__init__(_schema_message(self.schema_issue))


def _id() -> str:
    return uuid.uuid4().hex


def _safe_metadata(values: dict) -> dict:
    result = {}
    for key in ("model", "route", "phase", "reasoning_effort", "reason_code"):
        value = values.get(key)
        if isinstance(value, str) and _TOKEN.fullmatch(value) and not value.lower().startswith(("sk-", "bearer", "key-")):
            result[key] = value
    for key in ("task_id", "operation_id"):
        value = values.get(key)
        if isinstance(value, str) and _OPAQUE.fullmatch(value):
            result[key] = value
    for key in ("duration_ms", "attempt", "retry_count", "status_code", "page_index"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            result[key] = round(value, 3)
    refs = values.get("artifact_refs")
    if isinstance(refs, (list, tuple)):
        result["artifact_refs"] = [x for x in refs[:16] if isinstance(x, str) and _OPAQUE.fullmatch(x)]
    return result


def exception_chain(error: BaseException):
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        yield error
        # Even `raise ... from None` keeps context; it must not hide diagnosis.
        error = error.__cause__ or error.__context__


def explicit_failure_code(error: BaseException) -> str | None:
    """The deepest code a layer set deliberately, never one guessed from text."""
    for exc in reversed(list(exception_chain(error))):
        code = str(getattr(exc, "code", "")).upper()
        if code in ERRORS and code != "PROCESSING_FAILED":
            return code
        if code in _ALIASES:
            return _ALIASES[code]
    return None


def classify_failure(error: BaseException, stage: str = "request") -> str:
    chain = list(exception_chain(error))
    # Explicit boundary codes precede generic nested ValueError heuristics.
    # The deepest specific code wins; PROCESSING_FAILED is only a fallback.
    explicit = explicit_failure_code(error)
    if explicit is not None:
        return explicit
    # Prefer the original typed cause over a broad outer wrapper.
    for exc in reversed(chain):
        code = str(getattr(exc, "code", "")).upper()
        if code in ERRORS:
            return code
        if code in _ALIASES:
            return _ALIASES[code]
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        text = str(exc).lower()
        if status in (401, 403) or re.search(r"\b(?:http|status)\s*[:=]?\s*(401|403)\b", text) or any(x in text for x in ("unauthorized", "invalid api key", "credentials are", "authentication")):
            return "PROVIDER_AUTH_FAILED"
        if status == 429 or any(x in text for x in ("429", "throttling", "rate limit", "rate_limited")):
            return "PROVIDER_RATE_LIMITED"
        if isinstance(exc, ImportError) or any(x in text for x in ("dependency missing", "unrar not", "cannot find working tool", "sdk unavailable")):
            return "DEPENDENCY_MISSING"
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() or status in (408, 504) or any(x in text for x in ("timeout", "timed out", "deadline", "absolute_attempt_ttl")):
            return "PROVIDER_TIMEOUT"
        if any(x in text for x in ("coordinate", "bbox", "image bounds", "坐标", "框选", "题号锚点")):
            return "IMAGE_COORDINATES_INVALID"
        if any(x in text for x in ("empty content", "empty vision", "no questions", "no sse events", "未识别到任何题目")):
            return "TRANSCRIPTION_EMPTY"
        if isinstance(exc, json.JSONDecodeError) or any(x in text for x in ("parse", "json", "contract", "validation", "question order mismatch", "visible state lacks transcription")):
            return "OUTPUT_PARSE_FAILED"
        if isinstance(status, int) and status >= 500:
            return "PROVIDER_UNAVAILABLE"
        if getattr(exc, "transport_failure", False) or "transport" in type(exc).__name__.lower() or "connect" in type(exc).__name__.lower():
            return "NETWORK_ERROR"
        if stage == "storage" and isinstance(exc, OSError):
            return "STORAGE_FAILED"
        if stage == "import" and isinstance(exc, (ValueError, TypeError)):
            return "FORMAT_INVALID"
        if stage in {"template", "transcription", "solve", "grading", "model"} and isinstance(exc, (ValueError, TypeError, KeyError, IndexError)):
            return "OUTPUT_PARSE_FAILED"
    return "PROCESSING_FAILED"


class ProcessingError(RuntimeError):
    def __init__(self, code: str, *, stage: str = "request"):
        if code not in ERRORS or stage not in STAGES:
            raise ValueError("Unknown processing error code or stage")
        self.code = code
        self.stage = stage
        super().__init__(ERRORS[code][0])


@contextmanager
def processing_context(*, correlation_id: str | None = None, **metadata):
    # Only server-generated IDs (or persisted trusted IDs) belong here.
    cid = correlation_id if isinstance(correlation_id, str) and re.fullmatch(r"[a-f0-9]{32}", correlation_id) else _id()
    token = _context.set({"correlation_id": cid, **_safe_metadata(metadata)})
    try:
        yield cid
    finally:
        _context.reset(token)


def current_correlation_id() -> str:
    context = _context.get()
    return context["correlation_id"] if context else ""


def current_processing_stage(default="model") -> str:
    stage = (_context.get() or {}).get("stage")
    return stage if stage in STAGES and stage not in {"model", "request"} else default


def _diagnostic(error, stage, metadata):
    with _lock:
        chain = list(exception_chain(error))
        existing = next((getattr(e, "_processing_diagnostic", None) for e in chain if getattr(e, "_processing_diagnostic", None)), None)
        if existing is None:
            actual_stage = getattr(error, "stage", None) or stage
            actual_stage = actual_stage if actual_stage in STAGES else "request"
            context = _context.get() or {}
            existing = {
                "error_id": _id(), "correlation_id": context.get("correlation_id") or _id(),
                "stage": actual_stage, "code": classify_failure(error, actual_stage),
                "metadata": _safe_metadata(context), "reported": False,
            }
            issue = next((e.schema_issue for e in reversed(chain) if isinstance(e, TemplateSchemaError)), None)
            if issue is not None:
                existing["schema_issue"] = _schema_issue(issue)
        # The earliest (deepest) boundary owns metadata; wrappers cannot turn a
        # transcription failure into a template failure or overwrite its route.
        for key, value in _safe_metadata(metadata).items():
            existing["metadata"].setdefault(key, value)
        for exc in reversed(chain):
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int):
                existing["metadata"].setdefault("status_code", status)
                break
        for exc in chain:
            exc._processing_diagnostic = existing
        return existing


@contextmanager
def processing_stage(stage: str, **metadata):
    started = time.monotonic()
    # A reusable provider adapter must not erase its caller's business phase.
    actual_stage = current_processing_stage(stage) if stage == "model" else stage
    token = _context.set({**(_context.get() or {"correlation_id": _id()}),
                          **_safe_metadata(metadata), "stage": actual_stage})
    try:
        yield
    except Exception as exc:
        _diagnostic(exc, actual_stage, {**(_context.get() or {}), "duration_ms": (time.monotonic() - started) * 1000})
        raise
    finally:
        _context.reset(token)


def diagnosed(stage: str):
    """Annotate async function failures without changing exceptions or retries."""
    def decorate(fn):
        signature = inspect.signature(fn)
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            metadata = _safe_metadata(bound.arguments)
            with processing_stage(stage, **metadata):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate


def _public(diag: dict) -> dict:
    summary, action, retryable = ERRORS[diag["code"]]
    issue = _schema_issue(diag.get("schema_issue")) if diag["code"] == "TEMPLATE_SCHEMA_INVALID" else None
    if issue:
        summary = _schema_message(issue)
    result = {"code": diag["code"], "stage": diag["stage"], "correlation_id": diag["correlation_id"],
            "error_id": diag["error_id"], "message": f"{STAGES[diag['stage']]}：{summary}",
            "action": action, "retryable": retryable}
    if issue:
        result["schema_issue"] = issue
    return result


def describe_failure(error: BaseException, *, stage="request", **metadata) -> dict:
    """Adapt an exception to a result without reporting it twice upstream."""
    return _public(_diagnostic(error, stage, metadata))


def failure_from_public(info: dict) -> ProcessingError:
    """Rehydrate a trusted, already-reported internal result after a result boundary."""
    error = ProcessingError(info["code"], stage=info["stage"])
    if not all(re.fullmatch(r"[a-f0-9]{32}", str(info.get(key, ""))) for key in ("error_id", "correlation_id")):
        raise ValueError("Invalid persisted diagnostic identity")
    error._processing_diagnostic = {
        key: info[key] for key in ("code", "stage", "error_id", "correlation_id")
    }
    error._processing_diagnostic.update(metadata={}, reported=True)
    issue = _schema_issue(info.get("schema_issue"))
    if issue and info["code"] == "TEMPLATE_SCHEMA_INVALID":
        error._processing_diagnostic["schema_issue"] = issue
    return error


def _chain_receipt(error):
    result = []
    for exc in exception_chain(error):
        frames = []
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            frames.append({"file": Path(code.co_filename).name, "function": code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        result.append({"type": type(exc).__name__, "code": classify_failure(exc), "frames": frames[-12:]})
    return result


def _emit(event, error, diag, metadata, level):
    record = {"schema_version": 1, "event": event, "timestamp_unix": time.time(),
              **_public(diag), **diag["metadata"], **_safe_metadata(metadata),
              "cause_chain": _chain_receipt(error)}
    logger.log(level, json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def report_failure(error: BaseException, *, stage="request", **metadata) -> dict:
    """Exactly once for this exception/causal chain; independent errors survive."""
    diag = _diagnostic(error, stage, metadata)
    with _lock:
        if not diag["reported"]:
            _emit("processing.failed", error, diag, metadata, logging.ERROR)
            diag["reported"] = True
    return _public(diag)


def record_attempt(error: BaseException, *, stage="model", **metadata) -> dict:
    """Attempt records are INFO diagnostics, not terminal failures/notifications."""
    diag = _diagnostic(error, stage, metadata)
    _emit("processing.attempt_failed", error, diag, metadata, logging.INFO)
    return _public(diag)
