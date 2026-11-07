"""Whether the answer-sheet workflow can take work: GET /answer-sheet/availability and its gate.

- ``answer_sheet_availability`` combines ``_answer_sheet_service_availability``
  (feature switch, storage, vision and grading readiness, teacher capacity)
  with the anonymous hardware readiness (``_with_answer_sheet_hardware``),
  without calling a billable model API.
- ``_require_answer_sheet_chain`` is the gate before new model or image work:
  503 unless the chain is available or queued.

Moved unchanged from routers/answer_sheet.py, which still exposes these names
and includes this module's router before its own routes.
Annotations are evaluated when this module is imported (no ``from __future__
import annotations``): FastAPI resolves a decorated route's string annotations
in the decorator's module, so routes here carry real types.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from config import MAX_UPLOAD_TOTAL_SIZE, UPLOAD_DIR
from models_user import User
from routers.auth import _require_user
from services.system_readiness import (
    READINESS_IO_TIMEOUT_SECONDS,
    get_public_hardware_readiness,
    get_upload_storage_readiness_async,
)

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")
router = APIRouter()


def _with_answer_sheet_hardware(availability: dict, hardware: dict) -> dict:
    """Attach anonymous hardware readiness and apply its public severity."""
    fallback_messages = {
        "ready": "教师客户端或机会式加速节点已就绪。",
        "waiting_accelerator": "等待教师客户端加速节点，已保存进度不会丢失。",
        "unavailable": "本地加速节点不可用，已停止新的图像处理任务；已保存进度仍可查看。",
        "fault": "本地加速节点故障，已停止新的图像处理任务；已保存进度仍可查看。",
    }
    hardware_status = str(hardware.get("status") or "fault")
    if hardware_status not in fallback_messages:
        hardware_status = "fault"
    public_hardware = {
        "status": hardware_status,
        "message": str(hardware.get("message") or fallback_messages[hardware_status]),
    }
    result = dict(availability)
    result["hardware"] = public_hardware

    mapped_status = {
        "waiting_accelerator": "queued",
        "unavailable": "unavailable",
        "fault": "fault",
    }.get(hardware_status)
    ranks = {"available": 0, "queued": 1, "unavailable": 2, "fault": 3}
    current_status = str(result.get("status") or "fault")
    if mapped_status and ranks[mapped_status] > ranks.get(current_status, 3):
        result.update(
            status=mapped_status,
            label={
                "queued": "需排队",
                "unavailable": "暂不可用",
                "fault": "链路故障",
            }[mapped_status],
            message=public_hardware["message"],
        )
    return result


async def _answer_sheet_service_availability(user: User) -> dict:
    """Return feature, storage, model and coordinator readiness."""
    import config

    if not config.ANSWER_SHEET_GRADING_ENABLED:
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "答题卡批阅暂时不可用：真实学生答题卡裁剪验收尚未通过。",
        }

    storage = await get_upload_storage_readiness_async(
        UPLOAD_DIR,
        MAX_UPLOAD_TOTAL_SIZE,
        required_copies=2,
    )
    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "暂不可用",
                "message": "服务器存储空间不足，已暂停接收新答题卡批阅任务，请联系管理员清理空间。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "服务器存储状态异常，已暂停接收新答题卡批阅任务，请联系管理员。",
        }

    from services.analyzer import get_grading_readiness, get_grading_readiness_async
    from services.model_concurrency import RedisModelLimiter, get_model_limiter
    from services.ocr_service import get_ocr_readiness
    from services.ocr_teacher_fairness import public_teacher_availability

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            limiter = get_model_limiter()
            if isinstance(limiter, RedisModelLimiter):
                await limiter.ping()
            ocr = await get_ocr_readiness()
            grading = get_grading_readiness()
            if isinstance(limiter, RedisModelLimiter):
                grading = await get_grading_readiness_async(limiter)
    except Exception:
        logger.exception("answer-sheet grading readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not ocr.get("configured") or not ocr.get("ready"):
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "视觉识别服务尚未就绪，请稍后重试或联系管理员。",
        }
    if not grading.get("configured") or not grading.get("ready"):
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "数学批改服务尚未就绪，请稍后重试或联系管理员。",
        }

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            capacity = await public_teacher_availability(str(user.id))
    except Exception:
        logger.exception("answer-sheet teacher availability failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }
    if capacity.get("status") == "queued":
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前使用人数较多，提交后会按顺序处理。",
        }
    if capacity.get("status") == "fault":
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "答题卡批阅任务协调服务发生故障，请稍后重试或联系管理员。",
        }
    if capacity.get("status") != "available":
        return {
            "status": "unavailable",
            "label": "暂不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
    }


@router.get("/availability")
async def answer_sheet_availability(user: User = Depends(_require_user)):
    """Expose whole-chain readiness without probing a billable model API."""
    hardware, availability = await asyncio.gather(
        get_public_hardware_readiness(user.id),
        _answer_sheet_service_availability(user),
    )
    return _with_answer_sheet_hardware(availability, hardware)


async def _require_answer_sheet_chain(user: User) -> None:
    """Reject new model/image work while preserving read-only session access."""
    availability = await answer_sheet_availability(user)
    if availability.get("status") in {"available", "queued"}:
        return
    detail = "答题卡批阅服务当前暂不可用，请稍后重试。"
    if availability.get("status") == "fault":
        detail = str(
            availability.get("message")
            or "答题卡批阅链路发生故障，请稍后重试或联系管理员。"
        )
    raise HTTPException(503, detail=detail)
