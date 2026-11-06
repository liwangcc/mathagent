"""Whether the batch-grading chain can take work now, asked before work is created.

- ``batch_grade_availability`` (GET /batch-grade/availability) and
  ``_batch_grade_service_availability``: storage, OCR and grading models, and
  the teacher's OCR capacity, as one status (available / queued / unavailable
  / fault) with a label and message for the teacher.
- ``_require_batch_grade_chain``: the same check, failing closed with 503
  before a request creates or changes grading work; ``_with_grading_hardware``
  adds the accelerator when a stage needs one.
- ``_batch_grade_scoring_availability`` / ``_require_batch_scoring_chain``:
  only what grading needs once OCR is checkpointed.

Moved unchanged from routers/grade.py, which still exposes these names and
includes this module's router first.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from config import MAX_UPLOAD_TOTAL_SIZE, UPLOAD_DIR
from models_user import User
from routers.auth import _require_user
from services.quick_grader import QuickBatch
from services.system_readiness import (
    READINESS_IO_TIMEOUT_SECONDS,
    get_public_hardware_readiness,
    get_upload_storage_readiness_async,
)

# routers.grade's logger, so these records keep their logger name.
logger = logging.getLogger("routers.grade")
router = APIRouter()


def _with_grading_hardware(
    availability: dict,
    hardware: dict,
    *,
    queued_label: str = "需排队",
    unavailable_label: str = "批改不可用",
    fault_label: str = "链路故障",
) -> dict:
    """Attach the anonymous hardware state and fail closed on bad contracts."""
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
    result["service_status"] = str(result.get("status") or "fault")

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
                "queued": queued_label,
                "unavailable": unavailable_label,
                "fault": fault_label,
            }[mapped_status],
            message=public_hardware["message"],
        )
    return result


async def _batch_grade_service_availability(user: User) -> dict:
    """Return model, storage and coordinator readiness."""
    from services.model_concurrency import RedisModelLimiter, get_model_limiter
    from services.ocr_service import get_ocr_readiness
    from services.ocr_teacher_fairness import public_teacher_availability

    async def model_readiness() -> tuple[dict, dict | None]:
        from services.analyzer import (
            get_grading_readiness,
            get_grading_readiness_async,
        )

        limiter = get_model_limiter()
        if isinstance(limiter, RedisModelLimiter):
            await limiter.ping()
        ocr = await get_ocr_readiness()
        grading = get_grading_readiness()
        if isinstance(limiter, RedisModelLimiter):
            grading = await get_grading_readiness_async(limiter)
        return ocr, grading

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            storage, models, ocr_capacity = await asyncio.gather(
                get_upload_storage_readiness_async(
                    UPLOAD_DIR,
                    MAX_UPLOAD_TOTAL_SIZE,
                    required_copies=2,
                ),
                model_readiness(),
                public_teacher_availability(str(user.id)),
            )
    except TimeoutError as exc:
        # asyncio.timeout's budget expiry raises TimeoutError FROM the
        # CancelledError it injected (see timeouts.__aexit__).  An inner
        # leg raising its own TimeoutError (e.g. a failed Redis ping) is a
        # real coordination fault and must stay fail-closed.
        if not isinstance(exc.__cause__, asyncio.CancelledError):
            logger.exception("batch grading readiness coordination failed")
            return {
                "status": "fault",
                "label": "链路故障",
                "message": "批改任务协调服务发生故障，请稍后重试或联系管理员。",
            }
        # Slow under load means the chain is busy, not broken.  "queued"
        # keeps the submit gate open (work queues behind in-flight
        # batches) instead of mislabeling OCR saturation as a fault.
        logger.warning("batch grading readiness coordination busy (under load)")
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前批改任务处理中，提交后会按顺序处理。",
        }
    except Exception:
        logger.exception("batch grading readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "批改任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "批改不可用",
                "message": "服务器存储空间不足，已暂停接收新批改任务，请联系管理员清理空间。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "服务器存储状态异常，已暂停接收新批改任务，请联系管理员。",
        }

    ocr, grading = models

    if not ocr.get("configured"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务尚未就绪，请联系管理员。",
        }
    if not ocr.get("ready"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    if (
        not grading
        or not grading.get("configured")
        or not grading.get("ready")
    ):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "数学批改服务尚未就绪，请联系管理员。",
        }

    if ocr_capacity.get("status") == "queued":
        return {
            "status": "queued",
            "label": "需排队",
            "message": "当前使用人数较多，提交后会按顺序处理。",
        }
    if ocr_capacity.get("status") != "available":
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "视觉识别服务暂不可用，请稍后重试或联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "视觉识别、数学批改和任务协调链路已就绪。",
    }


@router.get("/batch-grade/availability")
async def batch_grade_availability(user: User = Depends(_require_user)):
    """Return readiness for ordinary grading without requiring an accelerator."""
    return await _batch_grade_service_availability(user)


async def _require_batch_grade_chain(
    user: User,
    *,
    requires_accelerator: bool = False,
    accelerator_stage: str | None = None,
    accelerator_artifact_contract: str | None = None,
) -> dict:
    """Fail closed before a request creates or mutates grading work."""
    if requires_accelerator and (
        accelerator_stage is None or accelerator_artifact_contract is None
    ):
        raise ValueError("accelerator stage and artifact contract are required together")
    if not requires_accelerator and (
        accelerator_stage is not None or accelerator_artifact_contract is not None
    ):
        raise ValueError("accelerator contract requires requires_accelerator=True")
    try:
        availability = await batch_grade_availability(user)
        if requires_accelerator:
            hardware_kwargs = {}
            if accelerator_stage is not None:
                hardware_kwargs["stage"] = accelerator_stage
            if accelerator_artifact_contract is not None:
                hardware_kwargs["artifact_contract"] = accelerator_artifact_contract
            hardware = await get_public_hardware_readiness(user.id, **hardware_kwargs)
            availability = _with_grading_hardware(availability, hardware)
    except Exception:
        logger.exception("batch grading readiness check failed")
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
    return availability


async def _batch_grade_scoring_availability(batch: QuickBatch) -> dict:
    """Return only the dependencies needed after OCR has been checkpointed."""
    # A completed OCR batch does not need capacity for another multi-GB upload.
    # The readiness service adds its global disk reserve to this one-byte write,
    # which is sufficient to gate the remaining atomic metadata checkpoints.
    storage = await get_upload_storage_readiness_async(
        batch.batch_dir,
        1,
        required_copies=1,
    )
    if not storage.get("ready"):
        if storage.get("status") == "unavailable":
            return {
                "status": "unavailable",
                "label": "批改不可用",
                "message": "服务器空间不足，当前批次无法保存新的判分进度，请联系管理员。",
            }
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "当前批次的进度存储异常，请稍后重试或联系管理员。",
        }

    from services.analyzer import get_grading_readiness, get_grading_readiness_async
    from services.model_concurrency import RedisModelLimiter, get_model_limiter

    try:
        async with asyncio.timeout(READINESS_IO_TIMEOUT_SECONDS):
            limiter = get_model_limiter()
            if isinstance(limiter, RedisModelLimiter):
                await limiter.ping()
            grading = get_grading_readiness()
            if isinstance(limiter, RedisModelLimiter):
                grading = await get_grading_readiness_async(limiter)
    except Exception:
        logger.exception("batch scoring readiness coordination failed")
        return {
            "status": "fault",
            "label": "链路故障",
            "message": "数学批改任务协调服务发生故障，请稍后重试或联系管理员。",
        }

    if not grading.get("configured") or not grading.get("ready"):
        return {
            "status": "unavailable",
            "label": "批改不可用",
            "message": "数学批改服务尚未就绪，请联系管理员。",
        }
    return {
        "status": "available",
        "label": "可用",
        "message": "数学批改和进度保存链路已就绪。",
    }


async def _require_batch_scoring_chain(batch: QuickBatch) -> dict:
    """Fail closed on scoring dependencies without re-gating completed OCR."""
    try:
        availability = await _batch_grade_scoring_availability(batch)
    except Exception:
        logger.exception("batch scoring readiness check failed")
        availability = {
            "status": "fault",
            "message": "数学批改链路状态检查失败，请稍后重试或联系管理员。",
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
                    or "数学批改链路当前不可用，请等待恢复后再重试。"
                ),
            },
        )
    return availability
