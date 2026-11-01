"""模型密钥的「职责组」配置与运行时解析。

为什么要按组存：一次批阅会用到三条不同线路的凭据——
模板定位/解题走 Token Plan（`TEMPLATE_CUT_*` 历史变量名），学生卷转录与数学判分
走 DashScope 按量（`DASHSCOPE_API_KEY`）。旧的设置页只写一个 `LLM_API_KEY`
（教师聊天模型），普通人根本找不到真正要填的那两个 key。

解析顺序（每个组独立生效）：**组级配置 → 全局环境变量 → config 默认值**。
组级配置存在主库 `model_profiles` 表里，因此不依赖 .env 重写、不需要重启。

One key per responsibility, by design: the deployment has a single egress IP and the
provider rate-limits per IP, so extra keys from the same vendor share one bucket and add
no capacity. More throughput means asking the provider for a higher quota, not more keys.
The legacy comma-separated `DASHSCOPE_API_KEYS` list is still parsed: student OCR and
grading use it only when neither a group key nor `DASHSCOPE_API_KEY` is set (see the
comment in config.py for the template-cut route).

三个职责组（每组只对应一个真实调用点）：
- template_solve       模板分栏（`template_column_layout`）
- student_transcribe   学生卷转录（`ocr_service`）
- student_grade        学生卷批阅（`flash_grading`；题库/题型分类也走 `call_flash`，用的就是这把密钥）
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# template_preprocess and bank_classify were removed because nothing read them: template
# preprocessing runs locally or on the desktop node, and classification already uses
# student_grade's key via call_flash. A knob that changes nothing is worse than no knob.
GROUPS: tuple[str, ...] = (
    "template_solve",
    "student_transcribe",
    "student_grade",
)

GROUP_LABELS = {
    "template_solve": "模板定位与解题",
    "student_transcribe": "学生卷转录",
    "student_grade": "学生卷批阅",
}

# 简单模式：两个槽覆盖三个组。普通人只需要填这两个。
SIMPLE_SLOTS = {
    "template": ("template_solve",),
    "student": ("student_transcribe", "student_grade"),
}
SIMPLE_SLOT_LABELS = {
    "template": "模板定位与解题",
    "student": "学生卷转录与批阅（含题库分类）",
}

# 环境变量回退：api_key / base_url / model
ENV_FALLBACK = {
    "template_solve": ("TEMPLATE_CUT_API_KEY", "TEMPLATE_CUT_BASE_URL", "TEMPLATE_CUT_MODEL"),
    "student_transcribe": ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", None),
    "student_grade": ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", None),
}

DEFAULT_MODEL = "qwen3.8-flash"
DEFAULT_CONCURRENCY = 4

_CACHE_TTL_SECONDS = 20.0
# group -> (env_signature, cached_at, payload)。缓存键包含环境变量快照，
# 否则测试或运维改了环境变量后，20 秒内仍会读到旧凭据。
_cache: dict[str, tuple[tuple[str, str, str], float, dict[str, Any]]] = {}


def _defaults(group: str) -> dict[str, Any]:
    try:
        from config import DASHSCOPE_BASE_URL
    except Exception:  # pragma: no cover
        DASHSCOPE_BASE_URL = ""
    return {
        "api_key": "",
        "base_url": DASHSCOPE_BASE_URL or "",
        "model": DEFAULT_MODEL,
        "concurrency": DEFAULT_CONCURRENCY,
    }


def _env_value(name: str | None) -> str:
    if not name:
        return ""
    return (os.environ.get(name) or "").strip()


def _read_group_row(group: str) -> dict[str, Any] | None:
    """读一行组级配置。表不存在（首次启动/迁移前）时返回 None。"""
    try:
        from database import SessionLocal
        from models_model_profile import ModelProfile
    except Exception:  # pragma: no cover
        return None
    session = SessionLocal()
    try:
        row = (
            session.query(ModelProfile)
            .filter(ModelProfile.group == group)
            .first()
        )
        if row is None:
            return None
        from services.secret_box import decrypt

        return {
            # 库里存的是 enc:v1: 密文，这里解出明文；解不开按未配置处理。
            "api_key": decrypt(row.api_key or "").strip(),
            "base_url": (row.base_url or "").strip(),
            "model": (row.model or "").strip(),
            "concurrency": row.concurrency,
        }
    except Exception:
        return None
    finally:
        session.close()


def resolve(group: str) -> dict[str, Any]:
    """按「组级 → 全局环境变量 → config 默认值」解析某一组凭据。

    返回 {"api_key", "base_url", "model", "concurrency", "source"}；
    source 取 "group" / "env" / "default"，便于设置页显示真实来源。
    """
    if group not in GROUPS:
        raise ValueError(f"未知的模型职责组: {group}")

    env_key, env_base, env_model = ENV_FALLBACK.get(group, (None, None, None))
    env_signature = (_env_value(env_key), _env_value(env_base), _env_value(env_model))

    now = time.time()
    cached = _cache.get(group)
    if (
        cached
        and cached[0] == env_signature
        and now - cached[1] < _CACHE_TTL_SECONDS
    ):
        return dict(cached[2])

    defaults = _defaults(group)
    row = _read_group_row(group) or {}
    source = "group" if (row.get("api_key") or row.get("base_url")) else "default"

    resolved = {
        "api_key": row.get("api_key") or "",
        "base_url": row.get("base_url") or "",
        "model": row.get("model") or "",
        "concurrency": row.get("concurrency"),
        "source": source,
    }

    if not resolved["api_key"]:
        env_api_key = env_signature[0]
        if env_api_key:
            resolved["api_key"] = env_api_key
            resolved["source"] = "env"
    if not resolved["base_url"]:
        resolved["base_url"] = env_signature[1]
    if not resolved["model"]:
        resolved["model"] = env_signature[2]

    resolved["base_url"] = (resolved["base_url"] or defaults["base_url"]).rstrip("/")
    resolved["model"] = resolved["model"] or defaults["model"]
    if not resolved["concurrency"]:
        resolved["concurrency"] = defaults["concurrency"]

    _cache[group] = (env_signature, now, dict(resolved))
    return dict(resolved)


def credentials(group: str) -> tuple[str, str]:
    """便捷入口：(api_key, base_url)。"""
    resolved = resolve(group)
    return resolved["api_key"], resolved["base_url"]


def invalidate_cache(group: str | None = None) -> None:
    if group is None:
        _cache.clear()
    else:
        _cache.pop(group, None)


def write_group(
    group: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    concurrency: int | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    """写入/更新一个组。api_key 传空串且 clear=False 时保持原值不变。"""
    if group not in GROUPS:
        raise ValueError(f"未知的模型职责组: {group}")
    from database import SessionLocal
    from models_model_profile import ModelProfile

    session = SessionLocal()
    try:
        row = session.query(ModelProfile).filter(ModelProfile.group == group).first()
        if row is None:
            row = ModelProfile(group=group, base_url="", api_key="", model="", concurrency=None)
            session.add(row)
        if clear:
            row.api_key = ""
            row.base_url = ""
            row.model = ""
            row.concurrency = None
        if api_key:
            from services.secret_box import encrypt

            # 只把密文写进库；明文不落盘、不写日志、不回显。
            row.api_key = encrypt(api_key.strip())
        if base_url is not None:
            row.base_url = (base_url or "").strip().rstrip("/")
        if model is not None:
            row.model = (model or "").strip()
        if concurrency is not None:
            row.concurrency = int(concurrency)
        session.commit()
    finally:
        session.close()
    invalidate_cache(group)
    return resolve(group)


def status_snapshot() -> dict[str, Any]:
    """设置页用的只读快照（密钥只给掩码，不回传明文）。"""
    groups = []
    for group in GROUPS:
        resolved = resolve(group)
        groups.append(
            {
                "group": group,
                "label": GROUP_LABELS[group],
                "configured": bool(resolved["api_key"]),
                "source": resolved["source"],
                "base_url": resolved["base_url"],
                "model": resolved["model"],
                "concurrency": resolved["concurrency"],
                "api_key_masked": mask_key(resolved["api_key"]),
            }
        )
    simple = {}
    for slot, members in SIMPLE_SLOTS.items():
        # 学生槽以 student_grade 为准（简单模式下转录与批阅写入同一把 DashScope 密钥）
        primary = members[-1] if slot == "student" else members[-1]
        resolved = resolve("student_grade" if slot == "student" else "template_solve")
        simple[slot] = {
            "label": SIMPLE_SLOT_LABELS[slot],
            "groups": list(members),
            "configured": bool(resolved["api_key"]),
            "source": resolved["source"],
            "base_url": resolved["base_url"],
            "model": resolved["model"],
            "concurrency": resolved["concurrency"],
            "api_key_masked": mask_key(resolved["api_key"]),
        }
    return {
        "groups": groups,
        "simple": simple,
        "any_configured": any(item["configured"] for item in groups),
    }


def mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]