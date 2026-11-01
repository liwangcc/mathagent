"""FastAPI/ASGI diagnostic boundary; streaming bodies are never buffered."""
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from services.processing_errors import (
    ProcessingError, explicit_failure_code, processing_context, report_failure,
)


def request_stage(path: str) -> str:
    if any(x in path for x in ("/import", "/upload")):
        return "import"
    if "/template" in path:
        return "template"
    if "recognize" in path:
        return "transcription"
    if "grade" in path or "answer-sheet" in path:
        return "grading"
    return "request"


class ProcessingDiagnosticsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False
        # Ignore caller-supplied X-Request-ID: it is untrusted and may be PII.
        with processing_context() as correlation_id:
            scope.setdefault("state", {})["correlation_id"] = correlation_id

            async def traced_send(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    message = {**message, "headers": [
                        (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                    ] + [(b"x-request-id", correlation_id.encode("ascii"))]}
                await send(message)

            try:
                await self.app(scope, receive, traced_send)
            except Exception as exc:
                info = report_failure(exc, stage=request_stage(scope.get("path", "")))
                if started:
                    # Never pretend a partial stream succeeded; the client sees
                    # transport failure. Normal SSE routes own their error event.
                    raise
                await JSONResponse(status_code=500, content={"detail": info["message"], "error_info": info})(scope, receive, traced_send)


def install_processing_errors(app):
    app.add_middleware(ProcessingDiagnosticsMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        stage = request_stage(request.url.path)
        # Preserve established business detail/code contracts. Underlying causes
        # (e.g. provider HTTP failures) take priority over the public wrapper.
        diagnostic_error = exc
        # A 4xx is a rejected request: never re-label it from a chained
        # cause's message text (a pydantic "validation" read as a model parse
        # failure, incidents dfe229fa / 2b5e7b50). Only a code a layer set
        # deliberately survives; 5xx keeps the provider-cause classification.
        rejected = 400 <= int(exc.status_code) < 500
        if (rejected and explicit_failure_code(exc) is None) or (not exc.__cause__ and not exc.__context__):
            code = "FORMAT_INVALID" if exc.status_code in {400, 413, 415, 422} else "REQUEST_REJECTED"
            diagnostic_error = ProcessingError(code, stage=stage)
        info = report_failure(diagnostic_error, stage=stage, status_code=exc.status_code)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "error_info": info}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        error = ProcessingError("FORMAT_INVALID", stage=request_stage(request.url.path))
        info = report_failure(error, stage=error.stage, status_code=422)
        # Pydantic input/ctx can contain submitted student data or credentials.
        detail = [{"loc": list(item["loc"]), "type": item["type"], "msg": "提交字段格式不正确"} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail, "error_info": info})
