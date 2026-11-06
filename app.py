import os
import asyncio
import inspect
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse

from config import (
    HOST,
    PORT,
    CORS_ORIGINS,
    CORS_ALLOW_ALL,
    ALLOWED_HOSTS,
    CONTENT_SECURITY_POLICY,
    FORCE_HTTPS,
    HSTS_MAX_AGE,
    MAX_IMPORT_FILE_SIZE,
    MAX_UPLOAD_TOTAL_SIZE,
    UPLOAD_DIR,
)
from database import init_db, get_db
from services.system_readiness import (
    READINESS_IO_TIMEOUT_SECONDS,
    configure_multipart_temp_dir,
)


_MULTIPART_ENVELOPE_ALLOWANCE_BYTES = 32 * 1024 * 1024
_MAX_GRADING_MULTIPART_BYTES = (
    MAX_UPLOAD_TOTAL_SIZE + _MULTIPART_ENVELOPE_ALLOWANCE_BYTES
)
_ANSWER_SHEET_MULTIPART_PATH = re.compile(
    r"/api/answer-sheet/sessions/[^/]+/"
    r"(?:source/upload|layout/upload|students/upload)\Z"
)
_GRADING_MULTIPART_PATHS = {
    "/api/batch-grade/recognize",
    "/api/batch-grade/template/recognize",
}
_SMALL_MULTIPART_ENVELOPE_ALLOWANCE_BYTES = 2 * 1024 * 1024
_CLASS_IMPORT_MULTIPART_PATH = re.compile(
    r"/api/class/[^/]+/"
    r"(?:students/import-(?:excel|image)|exam-scores/import)\Z"
)
_ANSWER_IMPORT_MULTIPART_PATH = re.compile(
    r"/api/answer/import/(?:excel|json)\Z"
)
_LOCAL_MULTIPART_ADMISSION_TIMEOUT_SECONDS = 1.0
_LOCAL_MULTIPART_ADMISSION_CAPACITY = 2
_MULTIPART_MEMORY_SPOOL_BYTES = 64 * 1024
_GRADING_MULTIPART_ADMISSION_CAPACITY = 1

# Starlette otherwise retains the first 1 MiB of every multipart file in RAM.
# A large class upload can contain hundreds of files, so roll each file to the
# monitored upload volume after a small bounded prefix instead.
MultiPartParser.max_file_size = _MULTIPART_MEMORY_SPOOL_BYTES

# Spooled UploadFile data is sensitive and can be as large as the whole batch.
# Keep it on the same volume checked by grading readiness instead of an
# unrelated systemd PrivateTmp filesystem.
MULTIPART_TEMP_DIR = configure_multipart_temp_dir(UPLOAD_DIR)


def _is_grading_multipart_path(path: str) -> bool:
    return path in _GRADING_MULTIPART_PATHS or bool(
        _ANSWER_SHEET_MULTIPART_PATH.fullmatch(path)
    )


def _local_multipart_policy(path: str) -> tuple[int, str] | None:
    """Return a pre-parser body limit and local resource class when needed."""
    if path == "/api/vision-office":
        return (
            20 * 1024 * 1024 + _SMALL_MULTIPART_ENVELOPE_ALLOWANCE_BYTES,
            "office",
        )
    if (
        _CLASS_IMPORT_MULTIPART_PATH.fullmatch(path)
        or _ANSWER_IMPORT_MULTIPART_PATH.fullmatch(path)
    ):
        return (
            MAX_IMPORT_FILE_SIZE + _SMALL_MULTIPART_ENVELOPE_ALLOWANCE_BYTES,
            "structured_import",
        )
    return None


class GradingMultipartGuardMiddleware:
    """Prepare multipart storage and gate grading before body parsing."""

    def __init__(
        self,
        app,
        *,
        admission_timeout_seconds: float = (
            _LOCAL_MULTIPART_ADMISSION_TIMEOUT_SECONDS
        ),
    ):
        self.app = app
        self.admission_timeout_seconds = float(admission_timeout_seconds)
        self._grading_admission = asyncio.Semaphore(
            _GRADING_MULTIPART_ADMISSION_CAPACITY
        )
        self._local_admission = {
            name: asyncio.Semaphore(_LOCAL_MULTIPART_ADMISSION_CAPACITY)
            for name in ("office", "structured_import")
        }

    @staticmethod
    async def _respond(scope, send, status_code: int, detail: object) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, None, send)

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or str(scope.get("method") or "").upper() != "POST"
        ):
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        grading_path = _is_grading_multipart_path(path)
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        content_type = headers.get(b"content-type", b"").lower()
        if not content_type.startswith(b"multipart/form-data"):
            await self.app(scope, receive, send)
            return

        raw_length = headers.get(b"content-length")
        try:
            content_length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            content_length = 0
        local_policy = _local_multipart_policy(path)
        if content_length <= 0:
            await self._respond(
                scope,
                send,
                411,
                "文件上传必须提供有效的 Content-Length。",
            )
            return
        max_multipart_bytes = (
            _MAX_GRADING_MULTIPART_BYTES
            if local_policy is None
            else int(local_policy[0])
        )
        if content_length > max_multipart_bytes:
            await self._respond(scope, send, 413, "上传文件总大小超过限制")
            return

        if not grading_path:
            from services.system_readiness import get_upload_storage_readiness_async

            gate = (
                self._local_admission[local_policy[1]]
                if local_policy is not None
                else None
            )
            gate_acquired = False

            def release_gate() -> None:
                nonlocal gate_acquired
                if gate is not None and gate_acquired:
                    gate.release()
                    gate_acquired = False

            async def guarded_send(message) -> None:
                if message.get("type") == "http.response.start":
                    release_gate()
                await send(message)

            try:
                if gate is not None:
                    try:
                        await asyncio.wait_for(
                            gate.acquire(),
                            timeout=self.admission_timeout_seconds,
                        )
                        gate_acquired = True
                    except TimeoutError:
                        await self._respond(
                            scope,
                            send,
                            503,
                            {
                                "error": "LOCAL_RESOURCE_BUSY",
                                "status": "queued",
                                "message": "本机正在处理其他图片或文档，请稍后重试。",
                            },
                        )
                        return

                storage = await get_upload_storage_readiness_async(
                    UPLOAD_DIR,
                    min(content_length, MAX_UPLOAD_TOTAL_SIZE),
                    required_copies=2,
                )
                if not storage.get("ready"):
                    status = str(storage.get("status") or "fault")
                    await self._respond(
                        scope,
                        send,
                        503,
                        {
                            "error": "UPLOAD_STORAGE_UNAVAILABLE",
                            "status": (
                                status
                                if status in {"fault", "unavailable"}
                                else "fault"
                            ),
                            "message": "服务器上传存储暂不可用，请稍后重试或联系管理员。",
                        },
                    )
                    return
                await self.app(scope, receive, guarded_send)
            finally:
                # Cancellation or disconnect may happen before response.start.
                release_gate()
            return

        import config
        from services.system_readiness import get_grading_infrastructure_readiness

        # The two ordinary grading upload endpoints share one parser/storage
        # slot.  Reject immediately while occupied: waiting clients may already
        # be streaming large request bodies into the ASGI server or proxy.
        grading_gate = (
            self._grading_admission
            if path in _GRADING_MULTIPART_PATHS
            else None
        )
        grading_gate_acquired = False

        def release_grading_gate() -> None:
            nonlocal grading_gate_acquired
            if grading_gate is not None and grading_gate_acquired:
                grading_gate.release()
                grading_gate_acquired = False

        async def guarded_grading_send(message) -> None:
            if message.get("type") == "http.response.start":
                release_grading_gate()
            await send(message)

        if grading_gate is not None:
            if grading_gate.locked():
                await self._respond(
                    scope,
                    send,
                    503,
                    {
                        "error": "GRADING_UPLOAD_BUSY",
                        "status": "queued",
                        "message": "服务器正在接收另一批试卷，请稍后重试。",
                    },
                )
                return
            # Semaphore.acquire() does not suspend while capacity is available,
            # making the locked-check/acquire pair atomic within this event loop.
            await grading_gate.acquire()
            grading_gate_acquired = True

        try:
            if (
                path.startswith("/api/answer-sheet/")
                and not config.ANSWER_SHEET_GRADING_ENABLED
            ):
                state = {
                    "ready": False,
                    "status": "unavailable",
                    "code": "answer_sheet_disabled",
                }
            else:
                state = await get_grading_infrastructure_readiness(
                    UPLOAD_DIR,
                    min(content_length, MAX_UPLOAD_TOTAL_SIZE),
                )
            if not state.get("ready"):
                status = str(state.get("status") or "fault")
                await self._respond(
                    scope,
                    send,
                    503,
                    {
                        "error": "GRADING_CHAIN_UNAVAILABLE",
                        "status": (
                            status
                            if status in {"fault", "unavailable"}
                            else "fault"
                        ),
                        "message": (
                            "批改链路发生故障，请稍后重试或联系管理员。"
                            if status == "fault"
                            else "批改服务当前不可用，请等待恢复后再上传。"
                        ),
                    },
                )
                return
            await self.app(scope, receive, guarded_grading_send)
        finally:
            # Exceptions, cancellation, and disconnects before response.start
            # must not permanently block future grading uploads.
            release_grading_gate()


def _validate_cors_config() -> None:
    """跨域白名单校验。

    同源部署（前端 dist 由本进程托管，Docker 默认就是这样）不需要任何跨域配置，
    此时不开放跨域是最严格的状态，因此空列表合法。真问题是「通配 + 携带凭据」——
    那样任何站点都能带 Cookie 调用接口。
    """
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    if CORS_ALLOW_ALL and "*" in origins:
        raise RuntimeError(
            "CORS_ALLOW_ALL 与显式 '*' 同时开启且携带凭据，拒绝启动：\n"
            "请改为列出具体来源，例如 CORS_ORIGINS=https://your-domain.com"
        )


def _guard_dev_mode() -> None:
    """DEV_MODE 会把短信验证码直接回显在响应里，等于任意手机号都能登录。

    生产部署（CORS 指向公网域名）下必须在启动阶段硬失败，而不是依赖运维记得关。
    本地开发仍可用；确有需要可显式设置 ALLOW_DEV_MODE_IN_PRODUCTION=1 绕过。
    """
    if os.environ.get("DEV_MODE", "").lower() not in ("1", "true", "yes"):
        return
    if os.environ.get("ALLOW_DEV_MODE_IN_PRODUCTION", "").lower() in ("1", "true", "yes"):
        return
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    public = [o for o in origins if "localhost" not in o and "127.0.0.1" not in o]
    if public:
        raise RuntimeError(
            "DEV_MODE 已开启但 CORS_ORIGINS 指向公网地址，拒绝启动：\n"
            "DEV_MODE 会把短信验证码直接返回给调用方，等于任何手机号都能登录。\n"
            "请在 .env 中关闭 DEV_MODE，或显式设置 ALLOW_DEV_MODE_IN_PRODUCTION=1。"
        )


async def _check_grading_dependency(
    name: str,
    check,
    *,
    timeout_seconds: float = READINESS_IO_TIMEOUT_SECONDS,
) -> bool:
    """Keep the UI online while model coordination remains fail-closed."""

    async def invoke() -> None:
        if inspect.iscoroutinefunction(check):
            await check()
            return
        result = await asyncio.to_thread(check)
        if inspect.isawaitable(result):
            await result

    try:
        async with asyncio.timeout(float(timeout_seconds)):
            await invoke()
        return True
    except Exception as exc:
        logging.getLogger(__name__).error(
            "grading dependency unavailable at startup component=%s error=%s",
            name,
            type(exc).__name__,
        )
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.logging_config import setup_logging
    setup_logging()
    _validate_cors_config()
    _guard_dev_mode()
    # Keep the teacher-facing UI online during a coordinator outage so grading
    # pages can report "链路故障". Runtime limiters remain Redis-backed and fail
    # closed; these checks never switch production traffic to local limits.
    from services.model_concurrency import ensure_redis_model_limiter_ready
    from services.llm_rate_limiter import ensure_redis_llm_rate_limiter_ready
    from services.ocr_pool_scheduler import (
        ensure_ocr_pool_scheduler_ready,
        reset_ocr_pool_scheduler_for_tests,
    )
    await asyncio.gather(
        _check_grading_dependency(
            "model_limiter",
            ensure_redis_model_limiter_ready,
        ),
        _check_grading_dependency(
            "ocr_pool_scheduler",
            ensure_ocr_pool_scheduler_ready,
        ),
        _check_grading_dependency(
            "llm_rate_limiter",
            ensure_redis_llm_rate_limiter_ready,
        ),
    )
    init_db()

    # 设置页可能已经配置了「学生卷转录」职责组：启动后立刻让号池用上它，
    # 不必等第一个批改请求才切换（解析顺序：组级 → 环境变量 → 默认值）。
    try:
        from services.ocr_service import _sync_pool_from_profiles

        _sync_pool_from_profiles()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "model profile credential sync skipped", exc_info=True
        )

    # Phone capture photos are sensitive; drop sessions abandoned before a
    # restart instead of waiting for the next teacher to open one.
    try:
        from services.capture_session import sweep_expired_sessions
        sweep_expired_sessions()
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning("capture session sweep failed", exc_info=True)

    # Question-bank knowledge classification is intentionally a separate,
    # low-priority lane.  It stores imports first and classifies them one
    # batch at a time, so a slow YS response never blocks grading requests.
    from services.question_classification import (
        classifier_worker_configured,
        question_classification_worker_loop,
    )
    classification_stop = asyncio.Event()
    classification_task = None
    if classifier_worker_configured():
        classification_task = asyncio.create_task(
            question_classification_worker_loop(classification_stop),
            name="question-classification-worker",
        )

    # Lease expiry is server-authoritative and cannot depend on another node
    # polling the queue.  Start a bounded application-lifecycle reaper so a
    # disconnected last node is recovered even when no further lease request
    # arrives.  ``expire_leases`` uses atomic conditional updates, so running
    # one loop in each Uvicorn worker is safe.
    from services.accelerator_reaper import accelerator_reaper_loop
    accelerator_reaper_stop = asyncio.Event()
    accelerator_reaper_task = asyncio.create_task(
        accelerator_reaper_loop(accelerator_reaper_stop),
        name="accelerator-lease-reaper",
    )

    # Hermes Agent 工具注册（提前加载，export_error_sheet 等工具依赖此步骤）
    try:
        import sys as _sys
        _hermes_src = os.environ.get("HERMES_AGENT_PATH", "").strip()
        if not _hermes_src:
            _hermes_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes", "hermes-agent"))
        else:
            _hermes_src = os.path.abspath(_hermes_src)
        if os.path.isdir(_hermes_src) and _hermes_src not in _sys.path:
            _sys.path.insert(0, _hermes_src)
        from services.hermes_tools import register_mathagent_tools
        register_mathagent_tools()
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Hermes tool registration failed — set HERMES_AGENT_PATH or pip install hermes-agent"
        )

    try:
        yield
    finally:
        if classification_task is not None:
            classification_stop.set()
            await classification_task
        accelerator_reaper_stop.set()
        await accelerator_reaper_task
        await reset_ocr_pool_scheduler_for_tests()


app = FastAPI(
    title="MathAgent",
    description="AI数学个性化教学系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(GradingMultipartGuardMiddleware)


def _allowed_hosts() -> list[str]:
    hosts = [h.strip() for h in ALLOWED_HOSTS.split(",") if h.strip()]
    if "*" in hosts:
        logging.getLogger(__name__).warning(
            "ALLOWED_HOSTS='*'：按 IP 直访可用，但不再校验 Host 头；生产建议改成实际域名"
        )
    if not hosts:
        # 未配置时默认允许本机 + 常见反代透传 host
        hosts = ["127.0.0.1", "localhost", "0.0.0.0", "[::1]"]
    # testserver 是 TestClient 的默认 host。只在测试进程里放行：
    # 生产部署就算正确配了域名，也不该接受这个测试专用的 Host。
    if os.environ.get("MATHAGENT_ALLOW_TESTSERVER") == "1" and "testserver" not in hosts:
        hosts.append("testserver")
    return hosts


def _is_safe_static_path(full_path: str) -> bool:
    """限制 SPA fallback 只直出明确静态资源，避免源码/映射/隐藏文件被探测。"""
    from pathlib import Path

    if not full_path or "\x00" in full_path:
        return False
    parts = Path(full_path).parts
    if any(part in ("..", "") or part.startswith(".") for part in parts):
        return False
    suffix = Path(full_path).suffix.lower()
    allowed = {
        ".css", ".js", ".mjs", ".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp",
        ".svg", ".mp4", ".woff", ".woff2", ".ttf", ".otf", ".json", ".txt", ".html", ".webmanifest",
    }
    return suffix in allowed and suffix != ".map"


app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())

@app.middleware("http")
async def _sync_model_env(request, call_next):
    """设置页保存后，另一个 worker 靠 .env 的 mtime 变化在下一个请求生效。"""
    try:
        from services.env_sync import sync_env as _sync

        _sync()
    except Exception:
        logging.getLogger(__name__).exception("env sync failed")
    return await call_next(request)

if FORCE_HTTPS:
    app.add_middleware(HTTPSRedirectMiddleware)


def _permissions_policy(path: str) -> str:
    """Only the phone capture page may open the camera, and only same-origin."""
    if path == "/capture" or path.startswith("/capture/"):
        return "camera=(self), microphone=(), geolocation=()"
    return "camera=(), microphone=(), geolocation=()"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", _permissions_policy(request.url.path))
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={HSTS_MAX_AGE}; includeSubDomains",
        )
    return response


# CORS 配置
if CORS_ALLOW_ALL:
    # 开发模式：允许所有来源。
    # 注意：即使放开 origin，也不要放开方法/请求头到通配，缩小误配后的攻击面。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Template-ID", "X-Request-ID"],
    )
else:
    # 生产模式：仅允许白名单域名，且收紧方法/头到实际所需，缩小攻击面。
    origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    # Tauri 2 on Windows serves the bundled frontend from this fixed local
    # origin.  It must reach the canonical production API without widening
    # CORS to arbitrary desktop or web origins.
    if "http://tauri.localhost" not in origins:
        origins.append("http://tauri.localhost")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Template-ID", "X-Request-ID"],
    )

from services.processing_http import install_processing_errors
install_processing_errors(app)

from routers import upload, grade, grade_template, grade_review, grading_records, answer, bank, student, report, stats, teacher, exercise, chat, settings, auth, bank_import, pending_answers, usage, student_confirm, exam_export, vision_office, class_mgmt, parent_letter, answer_sheet, accelerator, courseware, past_papers, capture, student_submission

# ── 健康检查接口（监控探针用） ──
@app.get("/health", tags=["系统"])
def health_check():
    """健康检查 — 用于负载均衡器/监控探测。"""
    return {"status": "ok"}


@app.get("/api/health", tags=["系统"])
def api_health_check():
    """API 健康检查 — 包含数据库连接状态。"""
    from sqlalchemy import text
    from database import SessionLocal
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_status = "ok"
    except Exception as e:
        # 健康检查接口无需登录，不能把原始异常（含连接串/路径等内部细节）外泄（M5）。
        import logging as _logging
        _logging.getLogger(__name__).error("DB health check failed: %s", e)
        db_status = "error"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "database": db_status,
        "version": app.version,
    }

app.include_router(upload.router, prefix="/api", tags=["上传"])
app.include_router(grade.router, prefix="/api", tags=["批改"])
app.include_router(grade_template.router, prefix="/api", tags=["批改"])
app.include_router(grade_review.router, prefix="/api", tags=["批改"])
app.include_router(grading_records.router, prefix="/api", tags=["批改"])
app.include_router(answer.router, prefix="/api", tags=["答案管理"])
app.include_router(bank.router, prefix="/api", tags=["题库与出题"])
app.include_router(student.router, prefix="/api", tags=["学生管理"])
app.include_router(report.router, prefix="/api", tags=["报告生成"])
app.include_router(stats.router, prefix="/api", tags=["统计查询"])
app.include_router(teacher.router, prefix="/api", tags=["待处理面板"])
app.include_router(exercise.router, prefix="/api", tags=["练习题"])
app.include_router(chat.router, prefix="/api", tags=["聊天栏"])
app.include_router(settings.router, prefix="/api", tags=["设置"])
app.include_router(auth.router, prefix="/api", tags=["用户认证"])
app.include_router(bank_import.router, prefix="/api", tags=["拍照导入题库"])
app.include_router(pending_answers.router, prefix="/api", tags=["待入库答案"])
app.include_router(usage.router, prefix="/api", tags=["用量统计"])
app.include_router(student_confirm.router, prefix="/api", tags=["学生确认"])
app.include_router(exam_export.router, prefix="/api", tags=["成绩导出与错题条"])
app.include_router(vision_office.router, prefix="/api", tags=["识图办公"])
app.include_router(class_mgmt.router, prefix="/api", tags=["班级管理"])
app.include_router(parent_letter.router, prefix="/api", tags=["家长信分享"])
app.include_router(answer_sheet.router, prefix="/api", tags=["答题卡批阅"])
app.include_router(accelerator.router, prefix="/api", tags=["机会式预处理节点"])
app.include_router(courseware.router, prefix="/api", tags=["课件库"])
app.include_router(past_papers.router, prefix="/api", tags=["整卷真题"])
app.include_router(capture.router, prefix="/api", tags=["手机拍摄"])
app.include_router(student_submission.router, prefix="/api", tags=["学生上传"])


# 家长信群发链接落地页（公开，无需登录）。
# 必须在下方 SPA catch-all 之前注册，否则 /letter/{token} 会被前端 fallback 吞掉。
# 有手机号时页面只含校验框，正文经 /api/letter/{token}/verify 校验后才返回；
# 无手机号时 token 本身就是唯一访问凭证，直接返回已生成的正文。
@app.get("/letter/{token}", tags=["家长信分享"])
def parent_letter_landing(token: str, db=Depends(get_db)):
    from fastapi.responses import HTMLResponse
    from routers.parent_letter import _live_share, build_gate_page
    rec = _live_share(db, token)  # 无效/过期/撤销/锁死 → 统一 404
    if not rec.phone_last4:
        rec.view_count = (rec.view_count or 0) + 1
        db.commit()
        return HTMLResponse(content=rec.html)
    return HTMLResponse(content=build_gate_page(token))


# uploads/ 下是学生试卷照片（含未成年人面部+姓名+成绩，敏感 PII）。
# 原先用 StaticFiles 公开 mount，任何人知道/猜到文件名即可匿名下载（M2）。
# 改为需登录的鉴权端点：仅认证用户可访问，且复用 H6 路径防护防遍历。
# P0-1：再加按老师归属校验——仅当该文件被当前老师名下的批改记录/档案引用时才放行，
# 杜绝登录老师凭路径越权读取他人班级的试卷照片。
from routers.auth import _require_user


def _teacher_owns_upload(db, teacher_id: int, full_path: str) -> bool:
    """校验 full_path 指向的上传文件是否归当前老师所有（P0-1）。

    依据：文件名为全局唯一 uuid。只要当前老师名下任意 StudentProfile.image_path
    或 GradingResult.grading_details 引用了同名文件，即视为其所有。
    用 basename 做 LIKE 匹配并转义通配符，避免 _/% 误命中。
    """
    import os as _os
    from models import GradingResult, StudentProfile, Student

    basename = _os.path.basename(full_path)
    if not basename:
        return False
    like = basename.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = f"%{like}%"

    # SQL LIKE 只是粗筛（同名前缀会命中），真正判定按完整文件名比较，
    # 避免 "abc.jpg" 命中 "xabc.jpg" 这类子串越权。
    prof_paths = (
        db.query(StudentProfile.image_path)
        .join(Student, StudentProfile.student_id == Student.id)
        .filter(Student.teacher_id == teacher_id,
                StudentProfile.image_path.like(pat, escape="\\"))
        .limit(500)
        .all()
    )
    for (stored,) in prof_paths:
        if _os.path.basename((stored or "").replace("\\", "/")) == basename:
            return True

    # grading_details 是 JSON 文本列，image_path 嵌在其中：
    # 要求文件名以完整 token 出现（前后不能紧邻文件名字符），而不是任意子串。
    token_re = re.compile(r"(?<![0-9A-Za-z_-])" + re.escape(basename) + r"(?![0-9A-Za-z_-])")
    details_rows = (
        db.query(GradingResult.grading_details)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(Student.teacher_id == teacher_id,
                GradingResult.grading_details.like(pat, escape="\\"))
        .limit(500)
        .all()
    )
    return any(token_re.search(text or "") for (text,) in details_rows)


@app.get("/uploads/{file_path:path}", tags=["上传文件"])
def serve_upload(file_path: str, user=Depends(_require_user), db=Depends(get_db)):
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    from routers.grade_uploads import _resolve_safe_image_path  # 复用 H6 路径校验（限定 UPLOAD_DIR 内）
    full = _resolve_safe_image_path(file_path)
    # 归属校验：非本老师引用的文件统一 404（不泄露文件是否存在的差异）
    if not _teacher_owns_upload(db, user.id, full):
        raise HTTPException(404, detail="文件不存在或已被清理")
    return FileResponse(full)


# Vue 前端（frontend/dist/）
_frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_frontend_dist):
    app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="vue-assets")

    @app.get("/")
    def root():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(_frontend_dist, "index.html"))

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """SPA fallback — 所有非API路由返回 index.html，交给 Vue Router 处理。"""
        # 检查是否是真实文件（如 favicon.ico）
        file_path = os.path.join(_frontend_dist, full_path)
        full = os.path.abspath(file_path)
        dist = os.path.abspath(_frontend_dist)
        if (
            os.path.isfile(full)
            and os.path.commonpath([dist, full]) == dist
            and _is_safe_static_path(full_path)
        ):
            from fastapi.responses import FileResponse
            return FileResponse(full)
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(_frontend_dist, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)
