"""设置页面接口 — API Key 配置 / 模型选择 / 连接测试"""

import ipaddress
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from services.env_sync import env_path as _env_path, sync_env as _sync_env

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from config import LLM_BASE_URL, LLM_API_KEY as _CFG_LLM_KEY, LLM_MODEL
from models_user import User
from routers.auth import _require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# .env 位置跟运行形态走：容器/生产在 data 卷里，本地在仓库根目录
ENV_PATH = _env_path()

# 真实线路（与 config.py / README 的模型职责表一致）。
# MiMo v2.5 系列已退役，这里不能再把它列成可选项，否则老师会以为那是当前路线。
AVAILABLE_MODELS = [
    {"id": "qwen3.8-flash", "name": "Qwen3.8 Flash（DashScope 按量：转录/判分/分类）", "env": "prod"},
    {"id": "qwen3.8-max", "name": "Qwen3.8 Max（Token Plan：模板定位与解题）", "env": "prod"},
]


class ConnectionTestRequest(BaseModel):
    api_key: str | None = Field(None, max_length=300)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=100)


class SettingsSaveRequest(BaseModel):
    api_key: str | None = Field(None, max_length=300)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=100)


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


@router.get("/settings")
def get_settings(user: User = Depends(_require_admin)):
    """Return current settings with masked API keys."""
    return {
        "llm_base_url": os.environ.get("LLM_BASE_URL", LLM_BASE_URL),
        "llm_api_key": _mask_key(os.environ.get("LLM_API_KEY", _CFG_LLM_KEY)),
        "llm_model": os.environ.get("LLM_MODEL", LLM_MODEL),
        "is_configured": _is_configured(),
        "available_models": AVAILABLE_MODELS,
        "model_profiles": _profile_summary(),
    }


@router.post("/settings/test")
async def test_connection(data: ConnectionTestRequest, user: User = Depends(_require_admin)):
    """Test LLM API connection. Uses GET /models to verify key and fetch available models.

    两条硬约束（历史上这里可以把服务器的模型 Key 骗走）：
    1) 使用服务器保存的 Key 时，目标地址只能是配置的 LLM_BASE_URL，忽略调用方传入的 base_url；
       否则任何持有管理员 token 的人都能把 Key 发到自己的服务器上。
    2) 不回显上游响应正文，避免异常信息里夹带凭据或内网探测结果。
    """
    supplied_key = (data.api_key or "").strip()
    uses_stored_key = not supplied_key
    api_key = supplied_key or os.environ.get("LLM_API_KEY", _CFG_LLM_KEY)

    if not api_key:
        raise HTTPException(400, detail="API Key 不能为空")

    if uses_stored_key:
        configured_url = (os.environ.get("LLM_BASE_URL", LLM_BASE_URL) or "").strip()
        requested_url = (data.base_url or "").strip()
        if requested_url and requested_url.rstrip("/") != configured_url.rstrip("/"):
            # 静默改用配置地址会让管理员误以为新地址已通过测试，这里明确拒绝。
            raise HTTPException(
                400,
                detail="使用服务器已保存的密钥时不能切换到其它地址，请先填入该地址对应的 API Key",
            )
        url = configured_url
    else:
        url = ((data.base_url or "").strip() or os.environ.get("LLM_BASE_URL", LLM_BASE_URL)).strip()

    url = (url or LLM_BASE_URL).rstrip("/")
    _ensure_public_url(url)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Step 1: GET /models to verify connectivity and key validity
            models_url = url + "/models"
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                try:
                    models_data = resp.json()
                except ValueError:
                    return {"success": False, "error": "上游返回的不是合法 JSON"}
                model_ids = _extract_model_ids(models_data)
                return {
                    "success": True,
                    "response": f"连接成功，可用模型: {len(model_ids)}个",
                    "available_remote_models": model_ids[:20],
                }
            # Step 2: if /models failed, try a minimal chat completion
            chat_url = url + "/chat/completions"
            resp2 = await client.post(
                chat_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": data.model or LLM_MODEL,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                },
            )
            if resp2.status_code == 200:
                return {"success": True, "response": "API连接正常"}
            logger.warning(
                "LLM connection test failed status=%s host=%s",
                resp2.status_code,
                _safe_host(url),
            )
            return {"success": False, "error": f"HTTP {resp2.status_code}：连接失败，请检查地址与密钥"}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        return {"success": False, "error": "连接超时，请检查网络或API地址"}
    except Exception:
        logger.exception("LLM connection test failed")
        return {"success": False, "error": "连接测试失败，请检查配置后重试"}


def _safe_host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _ensure_public_url(url: str) -> None:
    """SSRF 防护：只允许 http/https，且目标不能是回环/内网/链路本地地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, detail="API 地址必须是 http 或 https")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(400, detail="API 地址缺少主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(400, detail="API 地址无法解析")
    for info in infos:
        raw_ip = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(400, detail="API 地址不允许指向内网或本机地址")


class UnbanIpRequest(BaseModel):
    ip: str = Field(..., min_length=3, max_length=64)


@router.post("/settings/unban-ip")
def api_unban_ip(data: UnbanIpRequest, user: User = Depends(_require_admin)):
    """清除某来源 IP 的登录失败计数与封禁。

    学校普遍共用出口 IP，误封会把整所学校挡在门外；封禁本身已改为限时，
    这里是管理员提前解封的入口。
    """
    from services.ip_rate_limit import unban_ip

    return {"ok": True, "removed": unban_ip(data.ip.strip())}


def _extract_model_ids(data: dict | list) -> list[str]:
    """Extract model IDs from a /models response."""
    if isinstance(data, list):
        return [m.get("id", "") for m in data if m.get("id")]
    if isinstance(data, dict):
        items = data.get("data", data.get("models", []))
        if isinstance(items, list):
            return [m.get("id", "") for m in items if isinstance(m, dict) and m.get("id")]
    return []


@router.post("/settings/save")
def save_settings(data: SettingsSaveRequest, user: User = Depends(_require_admin)):
    """Save settings to .env file and environment variables."""
    api_key = (data.api_key or "").strip()
    base_url = (data.base_url or "").strip()
    model = (data.model or "").strip()

    changed = []

    if api_key and api_key != "***":
        os.environ["LLM_API_KEY"] = api_key
        try:
            _update_config_file("LLM_API_KEY", api_key)
            changed.append("LLM_API_KEY")
        except Exception as e:
            logger.warning("Failed to write config file: %s", e)

    if base_url:
        # 关键：只改地址、不带新密钥，等于把服务器上已存的密钥发往未经确认的主机，
        # 先改地址再点「测试连接」就能把密钥送出去（/settings/test 用的是已存密钥）。
        # 因此换地址必须同时提交该地址对应的密钥，让「地址 + 密钥」成为一次显式配对。
        if not (api_key and api_key != "***"):
            stored_key = os.environ.get("LLM_API_KEY", _CFG_LLM_KEY)
            if stored_key:
                raise HTTPException(
                    400,
                    "更换模型地址时必须同时填写该地址对应的密钥："
                    "只改地址会把服务器已保存的密钥发送到新地址。",
                )
        _guard_base_url(base_url)
        os.environ["LLM_BASE_URL"] = base_url
        try:
            _update_config_file("LLM_BASE_URL", base_url)
            changed.append("LLM_BASE_URL")
        except Exception as e:
            logger.warning("Failed to write config file: %s", e)

    if model:
        os.environ["LLM_MODEL"] = model
        try:
            _update_config_file("LLM_MODEL", model)
            changed.append("LLM_MODEL")
        except Exception as e:
            logger.warning("Failed to write config file: %s", e)

    return {"ok": True, "changed": changed}


def _update_config_file(key: str, value: str):
    """更新 .env 中的一个键值对（替换已有行或追加），不再写可执行的 config.py。

    值按 .env 字面量处理：含特殊字符时用双引号包裹并转义，杜绝注入。
    仅允许预期的配置键，防止任意键写入。
    """
    allowed = {
        "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    }
    if key not in allowed:
        raise ValueError(f"不允许写入的配置键: {key}")

    # .env 安全转义：包裹双引号并转义反斜杠/双引号/换行
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")
    line = f'{key}="{safe}"'

    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    replaced = False
    for i, ln in enumerate(lines):
        # 匹配 KEY=... 或 KEY = ...（忽略前导空白与注释行）
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _sync_env(force=True)


def _is_configured() -> bool:
    """Check if at least one API key is set (not placeholder)."""
    llm_key = os.environ.get("LLM_API_KEY", _CFG_LLM_KEY)
    if llm_key and llm_key not in ("sk-xxxxxxxx", "nvapi-xxx", "your_api_key_here", ""):
        return True
    return False


def _profile_summary() -> dict:
    """首次启动引导用：没有任何批阅模型 key 时前端要把人引到设置页。"""
    from services.model_profiles import status_snapshot

    try:
        snapshot = status_snapshot()
    except Exception:
        logger.exception("model profile snapshot failed")
        return {"any_configured": False, "needs_setup": True, "groups": []}
    return {
        "any_configured": snapshot["any_configured"],
        "needs_setup": not snapshot["any_configured"],
        "groups": snapshot["groups"],
        "simple": snapshot["simple"],
    }


# ---------- 模型职责组：简单模式两槽 / 专家模式三组 ----------
# 解析顺序在 services/model_profiles.py：组级 → 全局环境变量 → config 默认值。
# 这里只负责校验、落库与连接测试，不碰任何模型路由参数。


class ModelProfileSlot(BaseModel):
    api_key: str | None = Field(None, max_length=300)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=100)
    concurrency: int | None = Field(None, ge=1, le=64)
    clear: bool = False


class SimpleProfilesRequest(BaseModel):
    template: ModelProfileSlot | None = None
    student: ModelProfileSlot | None = None


class ExpertProfileRequest(ModelProfileSlot):
    group: str = Field(..., max_length=40)


class ProfileTestRequest(BaseModel):
    group: str | None = Field(None, max_length=40)
    api_key: str | None = Field(None, max_length=300)
    base_url: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=100)


def _validate_group(group: str):
    from services.model_profiles import GROUP_LABELS, GROUPS

    if group not in GROUPS:
        raise HTTPException(400, detail="未知的模型职责组")
    return GROUP_LABELS[group]


def _guard_base_url(base_url: str | None) -> None:
    """填了地址就先做 SSRF 校验（与 /settings/test 同一套规则）。"""
    candidate = (base_url or "").strip()
    if candidate:
        _ensure_public_url(candidate)


@router.get("/settings/model-profiles")
def get_model_profiles(user: User = Depends(_require_admin)):
    """三个职责组的当前解析结果（密钥只回掩码）。"""
    from services.model_profiles import SIMPLE_SLOTS, status_snapshot

    snapshot = status_snapshot()
    return {
        "simple": snapshot["simple"],
        "simple_slots": {slot: list(members) for slot, members in SIMPLE_SLOTS.items()},
        "groups": snapshot["groups"],
        "any_configured": snapshot["any_configured"],
        "needs_setup": not snapshot["any_configured"],
        "expert_mode": True,
    }


@router.post("/settings/model-profiles/simple")
def save_simple_model_profiles(
    data: SimpleProfilesRequest, user: User = Depends(_require_admin)
):
    """简单模式：两个槽写满三个组。同一个 key 会自动覆盖组内全部成员。"""
    from services.model_profiles import SIMPLE_SLOTS, status_snapshot, write_group

    changed: list[str] = []
    for slot, members in SIMPLE_SLOTS.items():
        payload = getattr(data, slot)
        if payload is None:
            continue
        _guard_base_url(payload.base_url)
        api_key = (payload.api_key or "").strip()
        for group in members:
            write_group(
                group,
                api_key=api_key or None,
                base_url=payload.base_url,
                model=payload.model,
                concurrency=payload.concurrency,
                clear=payload.clear,
            )
            changed.append(group)

    snapshot = status_snapshot()
    return {
        "ok": True,
        "changed": changed,
        "needs_setup": not snapshot["any_configured"],
        "profiles": snapshot,
    }


@router.post("/settings/model-profiles")
def save_expert_model_profile(
    data: ExpertProfileRequest, user: User = Depends(_require_admin)
):
    """专家模式：单独写某一个职责组。留空的字段表示继续继承下一层。"""
    from services.model_profiles import status_snapshot, write_group

    _validate_group(data.group)
    _guard_base_url(data.base_url)
    write_group(
        data.group,
        api_key=(data.api_key or "").strip() or None,
        base_url=data.base_url,
        model=data.model,
        concurrency=data.concurrency,
        clear=data.clear,
    )
    snapshot = status_snapshot()
    return {
        "ok": True,
        "changed": [data.group],
        "needs_setup": not snapshot["any_configured"],
        "profiles": snapshot,
    }


@router.post("/settings/model-profiles/test")
async def test_model_profile(
    data: ProfileTestRequest, user: User = Depends(_require_admin)
):
    """按职责组测试连接。

    保留上一轮加固的两条硬约束：
    1) 使用「已保存的密钥」时不允许改 base_url——否则管理员 token 就能把服务器
       上的密钥发到任意地址；
    2) 不回显上游响应正文，只回状态与可用模型数量。
    """
    from services.model_profiles import resolve

    group = (data.group or "student_grade").strip()
    _validate_group(group)
    resolved = resolve(group)

    supplied_key = (data.api_key or "").strip()
    uses_stored_key = not supplied_key
    api_key = supplied_key or resolved["api_key"]
    if not api_key:
        raise HTTPException(400, detail="这一组还没有可用的 API Key，请先填写")

    requested_url = (data.base_url or "").strip()
    if uses_stored_key:
        configured_url = (resolved["base_url"] or "").rstrip("/")
        if requested_url and requested_url.rstrip("/") != configured_url:
            raise HTTPException(
                400,
                detail="使用服务器已保存的密钥时不能切换到其它地址，请先填入该地址对应的 API Key",
            )
        url = configured_url
    else:
        url = (requested_url or resolved["base_url"]).rstrip("/")

    if not url:
        raise HTTPException(400, detail="API 地址不能为空")
    _ensure_public_url(url)

    model = (data.model or "").strip() or resolved["model"]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url + "/models", headers={"Authorization": f"Bearer {api_key}"}
            )
            if resp.status_code == 200:
                try:
                    model_ids = _extract_model_ids(resp.json())
                except ValueError:
                    return {"success": False, "error": "上游返回的不是合法 JSON"}
                return {
                    "success": True,
                    "group": group,
                    "response": f"连接成功，可用模型: {len(model_ids)}个",
                    "available_remote_models": model_ids[:20],
                }
            chat = await client.post(
                url + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                },
            )
            if chat.status_code == 200:
                return {"success": True, "group": group, "response": "API连接正常"}
            logger.warning(
                "model profile test failed group=%s status=%s host=%s",
                group,
                chat.status_code,
                _safe_host(url),
            )
            return {
                "success": False,
                "error": f"HTTP {chat.status_code}：连接失败，请检查地址与密钥",
            }
    except HTTPException:
        raise
    except httpx.TimeoutException:
        return {"success": False, "error": "连接超时，请检查网络或API地址"}
    except Exception:
        logger.exception("model profile connection test failed")
        return {"success": False, "error": "连接测试失败，请检查配置后重试"}
