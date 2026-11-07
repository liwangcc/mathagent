"""Whether a template import can run: GET /batch-grade/template/availability and its gate.

- ``batch_grade_template_availability`` reports the template chain to the
  upload page; ``_batch_grade_template_service_availability`` reads the layout
  service, where the local two-column split keeps the page usable while the
  remote probe is down.
- ``_require_template_recognition_chain`` is the gate before an import starts:
  the core grading chain must be up (503 otherwise), the remote layout service
  is advisory.

Moved unchanged from routers/grade_template.py, which still exposes these names
and includes this module's router before its own routes.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from models_user import User
from routers.auth import _require_user
from routers.grade_availability import _batch_grade_service_availability
from services.accelerator_artifacts import CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION
from services.accelerator_jobs import STUDENT_MULTI_EVIDENCE_STAGE
from services.system_readiness import get_public_hardware_readiness
from services.template_column_layout import get_column_layout_readiness

# routers.grade_template's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade_template")
router = APIRouter()


async def _batch_grade_template_service_availability() -> dict:
    """Return template readiness, including the local two-column fallback."""
    try:
        state = await get_column_layout_readiness()
    except Exception:
        logger.exception("template layout readiness probe failed")
        state = {"status": "fault"}
    status = str(state.get("status") or "fault")
    if status == "available":
        return {
            "status": "available",
            "label": "可用",
            "message": "模板版面识别链路已就绪。",
        }
    if status == "queued":
        return {
            "status": "queued",
            "label": "需等待",
            "message": "模板识别容量繁忙，提交后可能需要等待。",
        }
    # A common landscape two-column template is split locally in a bounded CPU
    # pass, so an unavailable remote layout probe must not disable the upload UI.
    # Ambiguous pages still fail closed when the actual remote layout call is
    # reached; expose the remote status for diagnostics without blocking work.
    return {
        "status": "available",
        "label": "可用",
        "message": (
            "本地双栏版式可直接处理；复杂版式将在识别时检查远程分栏服务。"
        ),
        "remote_layout_status": status,
        "local_layout_fallback": "local_gutter_v1",
    }


@router.get("/batch-grade/template/availability")
async def batch_grade_template_availability(user: User = Depends(_require_user)):
    # Template registration performs page correction and frozen-column montage
    # generation on the server CPU.  The retired canonicalization/OCR evidence
    # accelerator stages must therefore remain informational and must not turn a
    # healthy template route into ``waiting_accelerator``.
    registration_hardware, availability = await asyncio.gather(
        get_public_hardware_readiness(
            user.id,
            stage=STUDENT_MULTI_EVIDENCE_STAGE,
            artifact_contract=CLIENT_EVIDENCE_FULL_COLUMN_CONTRACT_VERSION,
        ),
        _batch_grade_template_service_availability(),
    )
    result = dict(availability)
    result["service_status"] = str(result.get("status") or "fault")
    result["hardware"] = {
        "status": "ready",
        "message": "模板页面矫正与分栏识别图由服务器本地处理，无需等待加速节点。",
    }
    result["registration_hardware"] = {
        "status": str(registration_hardware.get("status") or "fault"),
        # Student recognition has the verified server CPU matcher fallback.
        # Keep node health visible without imposing a second upload gate.
        "advisory": True,
        "message": str(
            registration_hardware.get("message")
            or "加速节点不可用，请使用客户端"
        ),
    }
    return result


async def _require_template_recognition_chain(user: User) -> dict:
    """Require core grading services while keeping local layout fallback usable.

    The remote layout model is still used for ambiguous page layouts, but the
    common two-column layout is detected locally before that call. Its readiness
    probe is therefore advisory here; the actual remote path remains fail-closed
    if it is needed.
    """
    try:
        availability = await _batch_grade_service_availability(user)
    except Exception:
        logger.exception("template core readiness check failed")
        availability = {
            "status": "fault",
            "message": "批改链路状态检查失败，请稍后重试或联系管理员。",
        }
    status = str(availability.get("status") or "fault")
    if status not in {"available", "queued"}:
        raise HTTPException(
            503,
            detail={
                "error": "GRADING_CHAIN_UNAVAILABLE",
                "status": status if status in {"unavailable", "fault"} else "fault",
                "message": str(
                    availability.get("message")
                    or "批改链路当前不可用，请等待恢复后再重试。"
                ),
            },
        )
    try:
        state = await get_column_layout_readiness()
    except Exception:
        logger.exception("template recognition readiness check failed")
        state = {"status": "fault"}
    status = str(state.get("status") or "fault")
    if status not in {"available", "queued"}:
        logger.warning(
            "template layout readiness is %s; local gutter layout will be tried "
            "before any remote fallback",
            status,
        )
        return {
            **availability,
            "template_layout_status": status,
            "template_layout_fallback": "local_gutter_v1",
            "template_layout_message": (
                "本地双栏版式可直接处理；其他版式将在实际识别时检查远程服务。"
            ),
        }
    return state
