"""大模型分析 — 薄弱点分析、题目解析、错误分类

模型架构：
    - OCR层：Qwen 视觉模型 — 学生答案转录
    - 学生批阅推理层：qwen3.8-flash — 数学批改（judge_answer, judge_paper 委托 flash_grading）
    - 报告文字：qwen3.8-flash（generate_report_text，走 call_flash 的 report 阶段）
    - 教师聊天：Hermes 按 TEACHER_LLM_* 配置；关闭 Hermes 时的旧意图模式走 LLM_*
    - 识图：qwen3.8-flash，见 services.ocr_service.read_image
"""

import asyncio
import json
import logging
import os
import re
from contextvars import ContextVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from config import LLM_BASE_URL as _DEFAULT_LLM_BASE_URL
from config import LLM_API_KEY as _DEFAULT_LLM_API_KEY
from config import LLM_MODEL as _DEFAULT_LLM_MODEL
from config import (
    LLM_KEY_CONCURRENCY,
    MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS,
    KNOWLEDGE_CLASSIFIER_MAX_TOKENS,
    KNOWLEDGE_CLASSIFIER_REASONING_EFFORT,
)
from services.key_pool import (
    acquire_key,
    create_llm_pool,
    enabled_provider_capacity,
    mark_shared_credential_cooldown,
    refresh_shared_credential_health,
    shared_credential_cooldown_seconds,
)
from services.model_concurrency import (
    MIMO_SUBSCRIPTION_PROVIDER,
    configured_fair_share_policy,
    get_model_limiter,
)
from services.retry_backoff import (
    exponential_retry_delay,
    parse_retry_after_seconds,
)
from services.text_grading_rules import (
    TEXT_GRADING_RESULTS,
    TextGradingResult,
)

logger = logging.getLogger(__name__)

# 号池（模块加载时初始化，无多Key配置时为None走单Key模式）
# LLM_* configures teacher chat: Hermes' fallback route and the legacy intent mode.
_llm_pool = create_llm_pool()
# Request-local teacher id for the Flash grading limiter (flash_grading reads it).
_grading_teacher_id: ContextVar[str] = ContextVar("grading_teacher_id", default="system")
_knowledge_classifier_lock: asyncio.Lock | None = None
_knowledge_classifier_lock_loop: asyncio.AbstractEventLoop | None = None


class KnowledgeClassifierConfigurationError(RuntimeError):
    """A local classifier configuration error that must not be retried."""


def set_grading_teacher_id(teacher_id: str):
    """Set request-local teacher context for the shared grading limiter."""
    return _grading_teacher_id.set(str(teacher_id or "system"))


def reset_grading_teacher_id(token) -> None:
    _grading_teacher_id.reset(token)


def get_grading_readiness() -> dict:
    """Readiness for the qwen3.8-flash mathematical grading route."""
    from services.flash_grading import get_flash_readiness
    return get_flash_readiness()


async def get_grading_readiness_async(limiter=None) -> dict:
    from services.flash_grading import get_flash_readiness_async
    return await get_flash_readiness_async(limiter)


_CTRL_RE = re.compile(r'[\x00-\x1f]')
# Legacy chat calls (_call_llm) get one retry.
_MAX_LLM_RETRIES = 2
_LLM_RETRY_DELAY = 2.0
_LLM_RETRY_MAX_DELAY = 5.0
_LLM_POOL_FAILOVER_DELAY = 0.05
_VALID_JUDGMENT_RESULTS = frozenset(TEXT_GRADING_RESULTS)


class StructuredOutputError(ValueError):
    """Model response was received but does not satisfy the grading contract."""


class _ModelCapacityTimeout(TimeoutError):
    """A model admission slot was unavailable before its bounded wait expired."""


class _JudgedQuestionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    question_no: str
    result: TextGradingResult
    explanation: str = ""
    hint: str = ""
    model_answer: str = ""
    standard_answer: str = ""


class _PaperJudgmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_JudgedQuestionOutput]


class _UncacheableJudgmentResult(dict):
    """A fail-closed grading result that must be recomputed on resubmission."""

    __task_queue_cacheable__ = False


# LaTeX command names that begin with a JSON control escape letter (b f n r t),
# taken from the KaTeX 0.17 build the frontend renders with. ``\ni`` (∋) is
# deliberately absent: a new line starting with ``i`` (``i^2=-1``) is far more
# common in school maths than the symbol.
_LATEX_CONTROL_ESCAPE_COMMANDS = frozenset((
    "backepsilon backprime backsim backsimeq backslash bar barwedge bcancel because begin "
    "begingroup beta beth between bf bgroup big bigcap bigcirc bigcup bigg biggl biggm biggr "
    "bigl bigm bigodot bigoplus bigotimes bigr bigsqcup bigstar bigtriangledown bigtriangleup "
    "biguplus bigvee bigwedge binom blacklozenge blacksquare blacktriangle blacktriangledown "
    "blacktriangleleft blacktriangleright blue blueA blueB blueC blueD blueE bm bmod bold "
    "boldsymbol bot bowtie boxdot boxed boxminus boxplus boxtimes bra brace brack braket breve "
    "bull bullet bumpeq "
    "fallingdotseq fbox fcolorbox flat footnotesize forall frac frak frown futurelet "
    "nLeftarrow nLeftrightarrow nRightarrow nVDash nVdash nabla natnums natural ncong ne "
    "nearrow neg negmedspace negthickspace negthinspace neq newcommand newline nexists ngeq "
    "ngeqq ngeqslant ngtr nleftarrow nleftrightarrow nleq nleqq nleqslant nless nmid nobreak "
    "nobreakspace noexpand nolimits nonumber normalsize not notag notin notni nparallel nprec "
    "npreceq nrightarrow nshortmid nshortparallel nsim nsubseteq nsubseteqq nsucc nsucceq "
    "nsupseteq nsupseteqq ntriangleleft ntrianglelefteq ntriangleright ntrianglerighteq nu "
    "nvDash nvdash nwarrow "
    "rArr rBrace rVert raisebox rang rangle rarr ratio rbrace rbrack rceil real reals red redA "
    "redB redC redD redE relax renewcommand restriction rfloor rgroup rhd rho right rightarrow "
    "rightarrowtail rightharpoondown rightharpoonup rightleftarrows rightleftharpoons "
    "rightrightarrows rightsquigarrow rightthreetimes risingdotseq rlap rm rmoustache rparen "
    "rq rrbracket rtimes rule rvert "
    "tag tan tanh tau tbinom tealA tealB tealC tealD tealE text textasciicircum "
    "textasciitilde textbackslash textbar textbardbl textbf textbraceleft textbraceright "
    "textcircled textcolor textcopyright textdagger textdaggerdbl textdegree textdollar "
    "textellipsis textemdash textendash textgreater textit textless textmd textnormal "
    "textquotedblleft textquotedblright textquoteleft textquoteright textregistered textrm "
    "textsf textsterling textstyle texttt textunderscore textup tfrac tg th therefore theta "
    "thetasym thickapprox thicksim thickspace thinspace tilde times tiny tmspace to top "
    "triangle triangledown triangleleft trianglelefteq triangleq triangleright "
    "trianglerighteq tt twoheadleftarrow twoheadrightarrow"
).split())

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_ascii_letter(char: str) -> bool:
    return ("a" <= char <= "z") or ("A" <= char <= "Z")


def _ascii_letter_run(text: str, start: int) -> str:
    end = start
    while end < len(text) and _is_ascii_letter(text[end]):
        end += 1
    return text[start:end]


def _json_escapes_latex_itself(text: str) -> bool:
    """Return whether the model wrote its LaTeX as valid JSON (``\\\\frac``).

    True only when some string holds an escaped backslash before a letter and
    no string holds an invalid single-backslash escape. In such a response a
    ``\\n`` is always a real line break, even before ``u=`` or ``e^x`` (which
    would otherwise read as the commands ``\\nu`` / ``\\ne``).
    """
    escaped_command = False
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if not in_string:
            in_string = c == '"'
            i += 1
            continue
        if c == '"':
            in_string = False
            i += 1
            continue
        if c != '\\':
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ''
        if nxt == '\\':
            escaped_command = escaped_command or (i + 2 < n and _is_ascii_letter(text[i + 2]))
            i += 2
        elif nxt and nxt in '"/bfnrt':
            i += 2
        elif nxt == 'u' and len(text[i + 2:i + 6]) == 4 and set(text[i + 2:i + 6]) <= _HEX_DIGITS:
            i += 6
        else:
            return False
    return escaped_command


def _sanitize_json_backslashes(text: str) -> str:
    """规范化 JSON 字符串值中的反斜杠，修复 LLM 输出的单反斜杠 LaTeX。

    LLM 常吐出单反斜杠的 LaTeX（如 \\frac \\begin \\neq），直接 json.loads 时：
      - \\b \\f \\n \\r \\t 开头的命令会被静默解码成控制字符 + 残体
        （\\frac→<\\x0c>rac, \\begin→<\\x08>egin, \\right→<\\x0d>ight）
      - 其他字母会触发非法转义报错

    \\n 等后跟字母时，只有整段字母恰为已知 LaTeX 命令才视为命令；
    \\nB、\\ny=2 等仍是真换行。模型已自行写出 \\\\frac 的合法 JSON 中，
    合法转义一律原样保留。

    本函数只处理字符串内部的反斜杠，对已规范的 JSON 幂等（\\\\ \\" \\uXXXX 原样保留）。
    """
    trust_escapes = _json_escapes_latex_itself(text)
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if not in_string:
            if c == '"':
                in_string = True
            out.append(c)
            i += 1
            continue
        # 字符串内部
        if c == '"':
            in_string = False
            out.append(c)
            i += 1
            continue
        if c != '\\':
            out.append(c)
            i += 1
            continue
        # 遇到反斜杠
        nxt = text[i + 1] if i + 1 < n else ''
        if nxt == '\\':
            # 已是转义反斜杠，原样保留两个字符
            out.append('\\\\')
            i += 2
            continue
        if nxt in '"/':
            # 合法转义 \" \/  保留
            out.append(c)
            out.append(nxt)
            i += 2
            continue
        if nxt == 'u' and len(text[i + 2:i + 6]) == 4 and set(text[i + 2:i + 6]) <= _HEX_DIGITS:
            # \uXXXX  原样保留
            out.append(text[i:i + 6])
            i += 6
            continue
        if nxt in 'bfnrt':
            # 整段字母恰为已知 LaTeX 命令（\begin \frac \neq \times \tan）→ 加倍
            if not trust_escapes and _ascii_letter_run(text, i + 1) in _LATEX_CONTROL_ESCAPE_COMMANDS:
                out.append('\\\\')
                out.append(nxt)
                i += 2
                continue
            # 否则视为真正的控制字符转义（\n \t 等；\nB \ny 不是命令）→ 保留
            out.append(c)
            out.append(nxt)
            i += 2
            continue
        # 其他非法转义（\l \s \c \d 等）或末尾孤立反斜杠 → 反斜杠加倍
        out.append('\\\\')
        if nxt:
            out.append(nxt)
        i += 2 if nxt else 1
    return ''.join(out)

_chapters_cache = None


def _load_chapters():
    from services.teaching_context import current_teaching_context
    context = current_teaching_context()
    if context:
        from services.math_curriculum import get_math_catalog
        return get_math_catalog(context["education_stage"])
    global _chapters_cache
    if _chapters_cache is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'references', 'chapters_topics.json'), encoding='utf-8') as f:
            _chapters_cache = json.load(f)
    return _chapters_cache


def parse_llm_json(text: str) -> dict | list:
    """Parse LLM response as JSON, handling markdown fences, control characters,
    and thinking-text-before-JSON patterns."""
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.lstrip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3].rstrip("`")
    text = _CTRL_RE.sub(" ", text).strip()
    # 主路径：先规范化字符串内的单反斜杠 LaTeX，再解析。
    # 避免 \begin \frac 等被 json.loads 静默解码成控制字符（题库 LaTeX 损坏根因）。
    try:
        return json.loads(_sanitize_json_backslashes(text))
    except (json.JSONDecodeError, ValueError):
        pass
    # 退路：裸解析（极少数已规范且 sanitize 误判的情况）
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try fixing LaTeX backslashes in JSON
    try:
        # Replace LaTeX backslashes with safe placeholder, parse, then restore
        # Find all string values and escape backslashes within them
        def _fix_json_string(m):
            s = m.group(0)
            # Escape unescaped backslashes
            s = s.replace(chr(92), chr(92)+chr(92))
            return s
        # Match JSON string values (between quotes)
        fixed = re.sub(r'"(?:[^"\\]|\\.)*"', _fix_json_string, text)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass

    # Last resort: extract result field from malformed JSON
    try:
        m = re.search(r'"result"\s*:\s*"(correct|partial|wrong|needs_review)"', text)
        if m:
            result = m.group(1)
            exp_m = re.search(r'"explanation"\s*:\s*"([^"]*)"', text)
            hint_m = re.search(r'"hint"\s*:\s*"([^"]*)"', text)
            return {
                "result": result,
                "explanation": exp_m.group(1) if exp_m else "",
                "hint": hint_m.group(1) if hint_m else "",
                "model_answer": ""
            }
    except Exception:
        pass
    
    # Find outermost { } or [ ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    raise ValueError(f"Failed to parse JSON from: {text[:200]}")


async def _post_model_request(
    *,
    url: str,
    headers: dict,
    payload: dict,
    timeout,
    api_key: str,
    shared_provider: str = "",
    teacher_id: str = "",
    global_capacity: int = 0,
    credential_capacity: int = 0,
    global_reserve_capacity: int | None = None,
    trust_env: bool = True,
    stream: bool = False,
):
    """Post one model request, optionally under a cross-worker Redis lease."""
    if shared_provider:
        policy = configured_fair_share_policy(
            global_capacity,
            provider=shared_provider,
            global_reserve_capacity=global_reserve_capacity,
        )
        try:
            lease = await get_model_limiter().acquire(
                provider=shared_provider,
                api_key=api_key,
                teacher_id=teacher_id,
                global_capacity=policy.global_capacity,
                credential_capacity=credential_capacity or policy.global_capacity,
                teacher_base_capacity=policy.teacher_base_capacity,
                teacher_burst_capacity=policy.teacher_burst_capacity,
                global_reserve_capacity=policy.global_reserve_capacity,
            )
        except TimeoutError as exc:
            if "waiting for shared model capacity" in str(exc).lower():
                raise _ModelCapacityTimeout(
                    f"{shared_provider} shared capacity wait timed out"
                ) from exc
            raise
        async with lease:
            async with httpx.AsyncClient(timeout=timeout, trust_env=trust_env) as client:
                if not stream:
                    return await client.post(url, headers=headers, json=payload)
                request = client.build_request("POST", url, headers=headers, json=payload)
                response = await client.send(request, stream=True)
                try:
                    await response.aread()
                finally:
                    await response.aclose()
                return response
    async with httpx.AsyncClient(timeout=timeout, trust_env=trust_env) as client:
        if not stream:
            return await client.post(url, headers=headers, json=payload)
        request = client.build_request("POST", url, headers=headers, json=payload)
        response = await client.send(request, stream=True)
        try:
            await response.aread()
        finally:
            await response.aclose()
        return response


def _parse_streamed_chat_content(body: str) -> str:
    """Extract final assistant content from an OpenAI-compatible SSE response."""
    content_parts: list[str] = []
    seen_event = False
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            break
        try:
            event = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid streaming model event") from exc
        seen_event = True
        choices = event.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        value = delta.get("content")
        if isinstance(value, str):
            content_parts.append(value)
    content = "".join(content_parts).strip()
    if not seen_event or not content:
        raise ValueError("empty streaming model content")
    return content


async def _call_api(
    *, pool, messages, model,
    single_key_env, single_base_env, single_key_default, single_base_default,
    max_tokens=8192, temperature: float | None = 0.3,
    call_type="chat", billable=False, log_tag="LLM",
    token_field="max_tokens",
    timeout=120.0,
    extra_body=None,
    shared_provider="",
    teacher_id="",
    global_capacity=0,
    per_key_capacity=0,
    global_reserve_capacity: int | None = None,
    trust_env=True,
    stream: bool = False,
    max_attempts: int = _MAX_LLM_RETRIES,
) -> str:
    """Call an OpenAI-compatible chat API with key-pool retry."""
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts must be a positive integer")
    payload = {
        "model": model,
        "messages": messages,
        token_field: max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if stream:
        payload["stream"] = True
    if extra_body:
        payload.update(extra_body)

    last_error: Exception | None = None
    transient_attempt = 0
    shared_limiter = get_model_limiter() if shared_provider else None
    default_base_url = os.environ.get(single_base_env, single_base_default)
    if pool and shared_limiter:
        await refresh_shared_credential_health(
            pool,
            shared_limiter,
            provider=shared_provider,
            default_base_url=default_base_url,
            model=model,
        )
        if pool.healthy_key_count() < 1:
            raise RuntimeError(f"{log_tag} credentials are temporarily rate-limited")
    while transient_attempt < max_attempts:
        _release_key = None
        _released = False
        if pool:
            try:
                api_key, _release_key, pool_base_url = await asyncio.wait_for(
                    acquire_key(pool),
                    timeout=float(MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS),
                )
            except RuntimeError:
                if last_error is not None:
                    raise last_error
                raise
            except TimeoutError as exc:
                raise _ModelCapacityTimeout(
                    f"{log_tag} key pool capacity wait timed out"
                ) from exc
        else:
            api_key = os.environ.get(single_key_env, single_key_default)
            pool_base_url = ""

        def _release_once() -> None:
            nonlocal _released
            if pool and _release_key and not _released:
                pool.release(_release_key)
                _released = True

        base_url = pool_base_url or default_base_url
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        try:
            if shared_limiter and await shared_limiter.is_credential_invalid(
                provider=shared_provider,
                api_key=api_key,
                base_url=base_url,
                model=model,
            ):
                _release_once()
                last_error = RuntimeError(f"{log_tag} credential is disabled")
                if pool:
                    pool.mark_invalid(api_key, 401)
                    if pool.has_enabled_keys():
                        continue
                raise last_error

            if shared_limiter:
                shared_cooldown = await shared_credential_cooldown_seconds(
                    shared_limiter,
                    provider=shared_provider,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                )
                if shared_cooldown > 0:
                    _release_once()
                    if pool:
                        pool.sync_cooldown(api_key, shared_cooldown)
                        if pool.healthy_key_count() > 0:
                            continue
                    raise last_error or RuntimeError(
                        f"{log_tag} credential is temporarily rate-limited"
                    )

            provider_capacity = (
                enabled_provider_capacity(pool, per_key_capacity)
                if per_key_capacity
                else 0
            )
            request_global_capacity = (
                min(global_capacity, provider_capacity)
                if global_capacity and provider_capacity
                else (provider_capacity or global_capacity)
            )
            request_credential_capacity = (
                per_key_capacity if per_key_capacity else request_global_capacity
            )
            resp = await _post_model_request(
                url=url,
                headers=headers,
                payload=payload,
                timeout=timeout,
                api_key=api_key,
                shared_provider=shared_provider,
                teacher_id=teacher_id,
                global_capacity=request_global_capacity,
                credential_capacity=request_credential_capacity,
                global_reserve_capacity=global_reserve_capacity,
                trust_env=trust_env,
                stream=stream,
            )
            status = resp.status_code
            if status in {401, 403}:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    auth_error: Exception = exc
                else:
                    auth_error = RuntimeError(f"{log_tag} returned HTTP {status}")
                _release_once()
                last_error = auth_error
                logger.error(
                    "%s credential rejected with HTTP %d from %s",
                    log_tag,
                    status,
                    url,
                )
                if pool:
                    pool.mark_invalid(api_key, status)
                if shared_limiter:
                    await shared_limiter.mark_credential_invalid(
                        provider=shared_provider,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        status_code=status,
                    )
                if pool:
                    if pool.has_enabled_keys():
                        continue
                raise auth_error
            if status == 429:
                retry_after = parse_retry_after_seconds(
                    resp.headers.get("retry-after")
                    or resp.headers.get("x-ratelimit-reset-requests")
                )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                else:
                    last_error = RuntimeError(f"{log_tag} returned HTTP 429")
                _release_once()
                transient_attempt += 1
                cooldown = (
                    pool.mark_429(api_key, retry_after)
                    if pool
                    else (
                        retry_after
                        or exponential_retry_delay(
                            transient_attempt,
                            base_delay_s=_LLM_RETRY_DELAY,
                            ceiling_s=_LLM_RETRY_MAX_DELAY,
                        )
                    )
                )
                if shared_limiter:
                    await mark_shared_credential_cooldown(
                        shared_limiter,
                        provider=shared_provider,
                        api_key=api_key,
                        base_url=base_url,
                        model=model,
                        cooldown_seconds=cooldown,
                    )
                has_alternate_key = bool(
                    pool and pool.healthy_key_count() > 0
                )
                logger.warning(
                    "%s 429, 换Key重试 (attempt %d/%d)",
                    log_tag,
                    transient_attempt,
                    max_attempts,
                )
                if transient_attempt < max_attempts:
                    retry_ceiling = (
                        0.5 if has_alternate_key else _LLM_RETRY_MAX_DELAY
                    )
                    retry_delay = exponential_retry_delay(
                        transient_attempt,
                        base_delay_s=(
                            _LLM_POOL_FAILOVER_DELAY
                            if has_alternate_key
                            else _LLM_RETRY_DELAY
                        ),
                        retry_after_s=(None if has_alternate_key else cooldown),
                        ceiling_s=retry_ceiling,
                    )
                    if not has_alternate_key and retry_delay > retry_ceiling:
                        raise last_error
                    await asyncio.sleep(retry_delay)
                continue
            if status == 408 or status >= 500:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                else:
                    last_error = RuntimeError(f"{log_tag} returned HTTP {status}")
                _release_once()
                transient_attempt += 1
                retry_after = parse_retry_after_seconds(
                    resp.headers.get("retry-after")
                )
                logger.warning(
                    "%s %d, retrying (attempt %d/%d)",
                    log_tag,
                    status,
                    transient_attempt,
                    max_attempts,
                )
                if transient_attempt < max_attempts:
                    retry_delay = exponential_retry_delay(
                        transient_attempt,
                        base_delay_s=_LLM_RETRY_DELAY,
                        retry_after_s=retry_after,
                        ceiling_s=_LLM_RETRY_MAX_DELAY,
                    )
                    if retry_delay > _LLM_RETRY_MAX_DELAY:
                        raise last_error
                    await asyncio.sleep(retry_delay)
                continue
            if status != 200:
                _release_once()
                logger.error("%s API error %d from %s", log_tag, status, url)
                resp.raise_for_status()
            try:
                if stream:
                    content = _parse_streamed_chat_content(resp.text)
                else:
                    data = resp.json()
                    msg = data["choices"][0]["message"]
                    content = msg.get("content", "")
                    if not content and msg.get("reasoning_content"):
                        content = msg["reasoning_content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("empty model content")
            except (KeyError, IndexError, TypeError, ValueError):
                _release_once()
                transient_attempt += 1
                last_error = RuntimeError(
                    f"{log_tag} returned an invalid response envelope"
                )
                logger.warning(
                    "%s returned an invalid response envelope "
                    "(attempt %d/%d)",
                    log_tag,
                    transient_attempt,
                    max_attempts,
                )
                if transient_attempt < max_attempts:
                    await asyncio.sleep(
                        exponential_retry_delay(
                            transient_attempt,
                            base_delay_s=_LLM_RETRY_DELAY,
                            ceiling_s=_LLM_RETRY_MAX_DELAY,
                        )
                    )
                continue
            if pool:
                pool.mark_success(api_key)
            _release_once()
            return content
        except asyncio.CancelledError:
            _release_once()
            raise
        except httpx.TransportError as e:
            _release_once()
            last_error = e
            transient_attempt += 1
            logger.warning(
                "%s request failed (attempt %d/%d): %s",
                log_tag,
                transient_attempt,
                max_attempts,
                e,
            )
            if transient_attempt < max_attempts:
                await asyncio.sleep(
                    exponential_retry_delay(
                        transient_attempt,
                        base_delay_s=_LLM_RETRY_DELAY,
                        ceiling_s=_LLM_RETRY_MAX_DELAY,
                    )
                )
        except Exception:
            _release_once()
            raise

    raise last_error or RuntimeError(f"{log_tag} call failed after retries")


async def _call_llm(
    messages: list[dict],
    temperature: float = 0.3,
    *,
    teacher_id: str = "system",
) -> str:
    """Call LLM API (OpenAI-compatible format) with retry on transient errors."""
    return await _call_api(
        pool=_llm_pool,
        messages=messages,
        model=os.environ.get("LLM_MODEL", _DEFAULT_LLM_MODEL),
        single_key_env="LLM_API_KEY",
        single_base_env="LLM_BASE_URL",
        single_key_default=_DEFAULT_LLM_API_KEY,
        single_base_default=_DEFAULT_LLM_BASE_URL,
        max_tokens=8192,
        temperature=temperature,
        call_type="chat",
        billable=False,
        log_tag="LLM API",
        # Legacy chat leases in the subscription namespace whatever LLM_*
        # points at, which keeps it out of the student-grading namespace.
        shared_provider=MIMO_SUBSCRIPTION_PROVIDER,
        teacher_id=str(teacher_id or "system"),
        per_key_capacity=LLM_KEY_CONCURRENCY,
        global_reserve_capacity=0,
        trust_env=False,
    )


async def generate_report_text(messages: list[dict], *, teacher_id: str = "system") -> str:
    """Write teacher-facing report prose with qwen3.8-flash.

    One request in call_flash's plain-text ``report`` phase, sharing the
    DashScope budget and Flash lane with grading. Callers keep their own
    fallback text when it fails.
    """
    from services.flash_grading import call_flash

    result = await call_flash(
        messages, teacher_id=str(teacher_id or "system"), phase="report",
    )
    return result["content"]


async def judge_answer(
    question: str,
    student_answer: str,
    standard_answer: str | None = None,
    knowledge_points: list | None = None,
    detail_level: str = "detailed",
) -> dict:
    """Flash judges one student answer. Returns {result, explanation, hint, model_answer}.

    - standard_answer provided → the model uses it as reference
    - standard_answer is None → the model judges purely from knowledge
    - detail_level: "detailed"（解析+标准答案+建议）或 "simple"（只判对错，省 token）
    """
    from services.flash_grading import judge_text_paper
    judged = await judge_text_paper([{
        "question_no": "1", "question_text": question,
        "student_answer": student_answer, "standard_answer": standard_answer,
    }], detail_level=detail_level, teacher_id=_grading_teacher_id.get())
    return judged.get("1", _fallback_judged("Flash数学批改未返回本题，请老师复核"))


def _normalize_judged(raw_item: dict) -> dict:
    """归一化输出字段；数学判断由模型负责，不按解析关键词改判。"""
    raw = raw_item.get("result")
    if not isinstance(raw, str) or raw not in _VALID_JUDGMENT_RESULTS:
        raise StructuredOutputError("invalid text grading result")
    explanation = raw_item.get("explanation", "") or ""
    hint = raw_item.get("hint", "") or ""
    model_answer = raw_item.get("model_answer", "") or ""
    standard_answer = raw_item.get("standard_answer", "") or ""
    if raw == "correct":
        explanation = ""
        hint = ""
        model_answer = ""
        standard_answer = ""

    return {
        "result": raw,
        "explanation": explanation,
        "hint": hint,
        "model_answer": model_answer,
        "standard_answer": standard_answer,
    }


def _validate_paper_judgment(content: str | None, expected_question_numbers: list[str]) -> dict[str, dict]:
    """Parse and validate a whole-paper response before accepting any result."""
    if not isinstance(content, str) or not content.strip():
        raise StructuredOutputError("empty or non-text content")
    try:
        parsed = parse_llm_json(content)
        if not isinstance(parsed, dict):
            raise StructuredOutputError("top-level response must be a JSON object")
        output = _PaperJudgmentOutput.model_validate(parsed)
    except (ValueError, ValidationError) as exc:
        raise StructuredOutputError(str(exc)) from exc

    expected = set(expected_question_numbers)
    by_question_no: dict[str, dict] = {}
    for item in output.results:
        if item.question_no not in expected:
            raise StructuredOutputError(f"unexpected question_no={item.question_no}")
        if item.question_no in by_question_no:
            raise StructuredOutputError(f"duplicate question_no={item.question_no}")
        if item.result not in _VALID_JUDGMENT_RESULTS:
            raise StructuredOutputError(f"invalid result={item.result}")
        by_question_no[item.question_no] = _normalize_judged(item.model_dump())

    missing = expected - set(by_question_no)
    if missing:
        raise StructuredOutputError(f"missing question_no={','.join(sorted(missing))}")
    return by_question_no


def _fallback_judged(reason: str) -> dict:
    return {
        "result": "needs_review", "explanation": reason, "hint": "",
        "model_answer": "", "standard_answer": "",
    }


def _fallback_paper_result(
    question_numbers: list[str],
    reason: str,
) -> _UncacheableJudgmentResult:
    return _UncacheableJudgmentResult(
        {question_no: _fallback_judged(reason) for question_no in question_numbers}
    )


async def judge_paper(
    questions: list[dict],
    detail_level: str = "detailed",
    *, teacher_material: list[dict] | None = None,
) -> dict[str, dict]:
    """整卷批改：一次 qwen3.8-flash 调用判完一张试卷的所有 fill/solve 题。

    questions: [{question_no, question_text, student_answer, standard_answer}]
    返回 {question_no: 判分dict}（与 judge_answer 同形）。

    teacher_material 为独立的完整教师资料。仅校验并返回 questions 中的待判 ID。
    失败结果不可缓存。
    """
    if not questions:
        return {}
    from services.flash_grading import judge_text_paper
    return await judge_text_paper(
        questions, detail_level=detail_level, teacher_id=_grading_teacher_id.get(),
        teacher_material=teacher_material,
    )


def _knowledge_classifier_runtime_config() -> dict[str, object]:
    """Return classifier-only parameters; Flash owns transport and quotas."""
    effort = str(KNOWLEDGE_CLASSIFIER_REASONING_EFFORT).strip().lower()
    if effort == "high":
        effort = "xhigh"
    if effort not in {"none", "low", "medium", "xhigh"}:
        raise KnowledgeClassifierConfigurationError("知识点分类思考档位无效")
    try:
        max_tokens = int(
            os.environ.get(
                "KNOWLEDGE_CLASSIFIER_MAX_TOKENS",
                str(KNOWLEDGE_CLASSIFIER_MAX_TOKENS),
            )
        )
    except (TypeError, ValueError) as exc:
        raise KnowledgeClassifierConfigurationError(
            "知识点分类参数不是有效数字"
        ) from exc
    if max_tokens < 1:
        raise KnowledgeClassifierConfigurationError(
            "知识点分类 max_tokens 必须为正数"
        )
    return {
        "enable_thinking": effort != "none",
        "reasoning_effort": effort,
        "max_tokens": max_tokens,
    }


def _get_knowledge_classifier_lock() -> asyncio.Lock:
    """Keep direct test/integration calls single-line even outside the worker."""
    global _knowledge_classifier_lock, _knowledge_classifier_lock_loop
    loop = asyncio.get_running_loop()
    if (
        _knowledge_classifier_lock is None
        or _knowledge_classifier_lock_loop is not loop
    ):
        _knowledge_classifier_lock = asyncio.Lock()
        _knowledge_classifier_lock_loop = loop
    return _knowledge_classifier_lock


async def _call_knowledge_classifier_text(
    messages: list[dict], *, question_count: int = 1,
) -> str:
    """Make one Qwen Flash text request; the durable worker owns retries."""
    from services.flash_grading import call_flash, get_flash_readiness

    parameters = _knowledge_classifier_runtime_config()
    readiness = get_flash_readiness()
    if not readiness.get("configured") or readiness.get("reason") == "endpoint_not_configured":
        raise KnowledgeClassifierConfigurationError("Qwen Flash 知识点分类服务未配置")
    max_tokens = int(parameters["max_tokens"])
    if question_count > 1:
        # A whole text-ready template is one request. Retain the existing
        # per-item allowance and any larger explicitly configured budget.
        max_tokens = max(max_tokens, min(32768, 2048 + 384 * question_count))
    lock = _get_knowledge_classifier_lock()
    async with lock:
        receipt = await call_flash(
            messages,
            teacher_id="question-classifier",
            phase="text",
            json_object=True,
            enable_thinking=bool(parameters["enable_thinking"]),
            reasoning_effort=str(parameters["reasoning_effort"]),
            max_tokens=max_tokens,
            diagnostic_phase="knowledge-classification",
        )
    return receipt["content"]


def _knowledge_catalog() -> tuple[list[dict], str]:
    """Return the canonical catalog and a short stable version identifier."""
    from services.teaching_context import current_teaching_context
    context = current_teaching_context()
    if context:
        from services.math_curriculum import get_math_catalog, catalog_version
        stage = context["education_stage"]
        return get_math_catalog(stage), catalog_version(stage)
    catalog = _load_chapters() or []
    compact = [
        {
            "chapter": str(item.get("chapter") or ""),
            "topics": [str(topic) for topic in (item.get("topics") or [])],
        }
        for item in catalog
        if isinstance(item, dict) and str(item.get("chapter") or "").strip()
    ]
    digest = __import__("hashlib").sha256(
        json.dumps(compact, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return compact, f"chapters_topics:{digest}"


def _validate_knowledge_result(
    raw: object,
    expected_ids: list[str],
    catalog: list[dict],
    catalog_version: str,
    *,
    per_question: bool = False,
) -> list[dict]:
    """Validate one catalog-approved knowledge point for every question.

    By default any off-catalog answer rejects the whole response.  With
    ``per_question`` such a question instead gets a ``status="rejected"`` row
    and the valid rows are kept; malformed ids or shapes still raise.
    """
    from services.teaching_context import current_teaching_context
    from services.math_curriculum import get_knowledge_point, resolve_classifier_label
    context = current_teaching_context()
    if context and catalog_version != context["curriculum_version"]:
        raise ValueError("knowledge classifier catalog differs from frozen task context")
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("items")
    if not isinstance(raw, list):
        raise ValueError("knowledge classifier output must be a list")
    expected = set(expected_ids)
    by_id: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("knowledge classifier item must be an object")
        item_id = str(item.get("id") or item.get("question_id") or "").strip()
        if not item_id or item_id not in expected or item_id in by_id:
            raise ValueError("knowledge classifier returned duplicate or unknown id")
        by_id[item_id] = item
    if set(by_id) != expected:
        raise ValueError("knowledge classifier omitted a question")

    normalized: list[dict] = []
    chapter_map = {
        str(item.get("chapter") or "").strip(): item for item in catalog
    }
    for item_id in expected_ids:
        item = by_id[item_id]
        raw_points = item.get("knowledge_points")
        if raw_points is None:
            raw_points = [{
                "chapter": item.get("chapter"),
                "topic": item.get("topic"),
            }]
        elif isinstance(raw_points, dict):
            raw_points = [raw_points]
        elif not isinstance(raw_points, list):
            raw_points = []

        if len(raw_points) != 1:
            if per_question:
                normalized.append({"id": item_id, "status": "rejected",
                                   "error": "knowledge classifier must return exactly one point per question"})
                continue
            raise ValueError("knowledge classifier must return exactly one point per question")

        points: list[dict] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, dict):
                continue
            chapter = str(raw_point.get("chapter") or "").strip()
            topic = str(raw_point.get("topic") or "").strip()
            matched = chapter_map.get(chapter)
            topics = [str(value) for value in (matched or {}).get("topics", [])]
            if (matched is None or topic not in topics) and context:
                # A declared alias, or an exact topic under the wrong chapter,
                # names one catalog point; ambiguous answers stay rejected.
                resolved = resolve_classifier_label(context["education_stage"], chapter, topic)
                if resolved is not None:
                    chapter, topic = resolved["chapter"], resolved["topic"]
                    matched = chapter_map.get(chapter)
                    topics = [str(value) for value in (matched or {}).get("topics", [])]
            if matched is None or topic not in topics:
                continue
            canonical = {
                "chapter": str(matched.get("chapter") or ""),
                "topic": topic,
            }
            if context:
                point = get_knowledge_point(context["education_stage"], canonical["chapter"], topic)
                if point is None:
                    continue
                canonical.update({
                    "knowledge_point_id": point["id"],
                    "education_stage": context["education_stage"],
                    "catalog_version": context["curriculum_version"],
                })
            if canonical not in points:
                points.append(canonical)

        if len(points) != 1:
            if per_question:
                normalized.append({"id": item_id, "status": "rejected",
                                   "error": "knowledge classifier returned a point outside the catalog"})
                continue
            raise ValueError("knowledge classifier returned a point outside the catalog")
        confidence = str(item.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        normalized.append({
            "id": item_id,
            "status": "suggested",
            "chapter": points[0]["chapter"],
            "topic": points[0]["topic"],
            "knowledge_points": points,
            "confidence": confidence,
            "error": "",
            "classification_version": catalog_version,
        })
        if context:
            normalized[-1].update({key: points[0][key] for key in (
                "knowledge_point_id", "education_stage", "catalog_version",
            )})
    return normalized


def build_knowledge_classification_messages(
    questions: list[dict],
) -> tuple[list[dict], list[str], list[dict], str]:
    """Build the production text-only prompt without mutation or model calls."""
    catalog, catalog_version = _knowledge_catalog()
    if not catalog:
        raise RuntimeError("知识点目录为空")
    normalized_questions = []
    expected_ids = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            raise ValueError("待分类题目必须是对象")
        item_id = str(item.get("id") or item.get("question_id") or index).strip()
        if not item_id or item_id in expected_ids:
            raise ValueError("待分类题目 ID 为空或重复")
        text = item.get("question") or item.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("待分类题目必须包含非空文字题干")
        normalized_questions.append({"id": item_id, "text": text.strip()})
        expected_ids.append(item_id)
    catalog_text = json.dumps(
        catalog, ensure_ascii=False, separators=(",", ":")
    )
    prompt = (
        "只接收并分析文字题干，不接收图片、不做图像识别；不解题，不修改题干。"
        "每道题必须且只能选择一个最相关的知识点；"
        "chapter 和 topic 必须逐字来自下方完整目录，不能自创、改写、留空或返回多个。"
        "即使把握较低，也必须从目录中选出最相关的一项，并将 confidence 设为 low。"
        "只返回 JSON，不要解释。\n"
        f"目录版本：{catalog_version}\n允许目录：{catalog_text}\n"
        f"题目：{json.dumps(normalized_questions, ensure_ascii=False, separators=(',', ':'))}\n"
        "返回：{\"results\":[{\"id\":\"...\",\"knowledge_points\":["
        "{\"chapter\":\"完整章节名\",\"topic\":\"目录中的知识点\"}],"
        "\"confidence\":\"high|medium|low\"}]}"
    )
    from services.teaching_context import stage_prompt
    messages = [
        {
            "role": "system",
            "content": stage_prompt(
                "你是高中数学题库知识点分类器。使用 Qwen Flash 文本路由，"
                "只输出严格 JSON；知识点目录是唯一允许的分类集合。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    return messages, expected_ids, catalog, catalog_version


async def classify_questions_knowledge(
    questions: list[dict], *, per_question: bool = False,
) -> list[dict]:
    """Classify through Qwen Flash with whole-catalog and exact-ID validation.

    ``per_question`` keeps valid rows when some answers fall outside the
    catalog (see ``_validate_knowledge_result``).
    """
    if not questions:
        return []
    messages, expected_ids, catalog, catalog_version = build_knowledge_classification_messages(questions)
    if len(expected_ids) > 1:
        content = await _call_knowledge_classifier_text(
            messages, question_count=len(expected_ids),
        )
    else:
        # Preserve the existing single-question compatibility entry point.
        content = await _call_knowledge_classifier_text(messages)
    return _validate_knowledge_result(
        parse_llm_json(content),
        expected_ids,
        catalog,
        catalog_version,
        per_question=per_question,
    )
