"""自然语言聊天栏接口 — 系统秘书

定位：MathAgent 的活体说明书 + 老师的聊天助手
能力：当前教师范围内的只读数据查询、导航指引、系统帮助、日常聊天
红线：不提供源码/服务器能力，不代替老师生成、导出或修改业务数据

重构说明（2026-06-29）：
- 业务逻辑拆分到 services/chat_intent.py、chat_handlers.py、chat_utils.py
- 本文件只做 HTTP 收发（请求解析、路由分发、响应格式化）
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from services.hermes_config import (
    HERMES_ENABLED,
    compress_history,
    create_agent,
    run_hermes_with_model_lease,
)
from services.hermes_tools import set_current_user_id
from services.llm_rate_limiter import check_llm_rate
from services.teaching_context import make_teaching_context, require_teacher_stage, use_teaching_context
from services.chat_intent import recognize_intent
from services.chat_handlers import (
    handle_navigate,
    handle_help,
    handle_chat,
    handle_query_student,
    handle_summary,
    handle_pending,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatHistoryItem(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system|tool|function)$")
    content: str = Field(..., max_length=4000)

    @field_validator("content", mode="before")
    @classmethod
    def clip_long_history(cls, value):
        # Older clients persisted long assistant replies verbatim; clip them
        # instead of rejecting every later message with a 422.
        if isinstance(value, str) and len(value) > 4000:
            return value[:3999] + "…"
        return value


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=30)


@router.post("/chat")
async def chat(
    data: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    context = make_teaching_context(require_teacher_stage(user))
    with use_teaching_context(context):
        return await _chat_with_context(data, db, user)


async def _chat_with_context(data: ChatRequest, db: Session, user: User):
    """自然语言聊天栏接口 — Hermes Agent 驱动（小M 大脑）

    支持对话上下文：传入 history 数组 [{"role": "user/assistant", "content": "..."}]
    HERMES_ENABLED=false 时仅保留旧链路中的只读查询与帮助能力。
    """
    message = data.message.strip()
    history = [item.model_dump() for item in data.history]

    if not message:
        return {"intent": "chat", "response": "老师您好，有什么可以帮您的？", "data": {}}

    logger.info("chat request received user_id=%s message=%s", user.id, message[:80])
    check_llm_rate(user.id)

    # ═══════════════════════════════════════════════════════════════
    # Hermes Agent 驱动模式（默认）
    # ═══════════════════════════════════════════════════════════════
    if HERMES_ENABLED:
        try:
            conv_history = None
            if history:
                conv_history = [
                    {"role": h["role"], "content": h["content"]}
                    for h in history
                    if h.get("role") in ("user", "assistant") and h.get("content")
                ] or None

            def _run_hermes_conversation():
                set_current_user_id(user.id)
                agent = create_agent(user)
                return agent.run_conversation(
                    user_message=message,
                    conversation_history=conv_history,
                )

            result = await run_hermes_with_model_lease(
                user,
                _run_hermes_conversation,
            )
            response = (result.get("final_response") or "").strip()

            # Hermes returns failed=True for provider/auth/network failures.  Do not
            # fall through to the legacy intent path here: that path may call another
            # app LLM chain and turn one teacher-Hermes outage into extra latency.
            if result.get("failed"):
                err = str(result.get("error") or "")
                logger.warning(
                    "Hermes agent returned failed result; using stable teacher fallback: %s",
                    err[:200],
                )
                response = response or "小M现在连接模型服务不稳定，请稍后再试。你也可以先按页面提示操作；如果一直不可用，请联系管理员。"
                full_history = (conv_history or []) + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": response},
                ]
                compressed = await compress_history(full_history)
                return {
                    "intent": "chat",
                    "response": response,
                    "session_id": result.get("session_id", ""),
                    "history": compressed,
                    "data": {},
                }

            if not response:
                response = "嗯，我来想想... 能再说清楚一点吗？"

            full_history = result.get("messages", []) if result.get("completed") else (
                (conv_history or []) + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": response},
                ]
            )
            full_history = [m for m in full_history if m.get("role") in ("user", "assistant")]
            compressed = await compress_history(full_history)

            return {
                "intent": "chat",
                "response": response,
                "session_id": result.get("session_id", ""),
                "history": compressed,
                "data": {},
            }
        except Exception as e:
            logger.exception("Hermes agent call failed; using stable teacher fallback: %s", e)
            response = "小M现在连接模型服务不稳定，请稍后再试。你也可以先按页面提示操作；如果一直不可用，请联系管理员。"
            full_history = (conv_history or []) + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": response},
            ]
            compressed = await compress_history(full_history)
            return {
                "intent": "chat",
                "response": response,
                "history": compressed,
                "data": {},
            }

    # ═══════════════════════════════════════════════════════════════
    # 旧意图识别模式（仅在明确关闭 Hermes 时使用，只保留只读能力）
    # ═══════════════════════════════════════════════════════════════
    try:
        intent_data = await recognize_intent(message, teacher_id=str(user.id))
        intent = intent_data.get("intent", "chat")
        params = intent_data.get("params", {})
        clarify = intent_data.get("clarify", "")

        if intent == "error":
            return {"intent": "error", "response": intent_data.get("error", "服务暂时不可用"), "data": {}}

        if clarify:
            return {"intent": intent, "response": clarify, "clarify": clarify, "data": {}}

        # 按意图分发到 handler
        if intent == "query_student":
            return await handle_query_student(params, db, user)
        if intent == "summary":
            return await handle_summary(params, db, user)
        if intent == "pending":
            return handle_pending(db, user)
        if intent == "navigate":
            return handle_navigate(params)
        if intent == "help":
            return await handle_help(message, history, teacher_id=str(user.id))
        if intent in {"exercise", "report", "export_error_sheet"}:
            return {
                "intent": "help",
                "response": "小M可以说明操作方法，但不会代替您生成或导出内容。请前往「出题练习」「成绩报告」或批改结果复核页，由您确认后手动操作。",
                "data": {},
            }

        # chat / fallback
        return await handle_chat(message, history, db, user)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Chat fallback failed: %s", e)
        return {"intent": "chat", "response": "小M刚才处理请求时出错了，请稍后再试。", "data": {}}


# ═══════════════════════════════════════════════════════════════════
# Hermes 工具生成文件的下载端点
# ═══════════════════════════════════════════════════════════════════

@router.get("/download/{file_id}")
async def download_file(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """下载 Hermes 工具生成的 Word/Excel/PPT 文件（P0-1/P0-2）。"""
    from services.generated_files import owned_file_response
    return owned_file_response(db, file_id, user.id)


@router.get("/recent-files")
async def recent_files(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """返回当前用户最近生成的 10 个文件。"""
    from models import GeneratedFile
    files = (
        db.query(GeneratedFile)
        .filter(GeneratedFile.owner_id == user.id)
        .order_by(GeneratedFile.created_at.desc())
        .limit(5)
        .all()
    )
    return {
        "items": [
            {
                "id": f.id,
                "name": f.display_name or f.stored_name,
                "download_url": f"/api/download/{f.id}",
                "created_at": f.created_at.strftime("%m-%d %H:%M") if f.created_at else "",
            }
            for f in files
        ]
    }
