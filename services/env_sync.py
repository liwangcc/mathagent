"""模型设置的环境变量同步。

为什么需要它：设置页保存的是「进程内 os.environ + 一份 .env 文件」。
在单进程里这样够用，但生产用 gunicorn 起两个 worker，保存请求只落在其中一个进程上，
另一个进程会继续用旧值；容器部署时 .env 还写在镜像层，容器一重建就丢。

所以这里做两件事：
1. .env 的位置跟运行形态走——设置了 MATHAGENT_DATA_DIR（容器/生产）就写进持久卷，
   否则写仓库根目录的 .env；
2. 每个请求开头比对一次文件 mtime/size（一次 stat，微秒级），变了就把受管的键重新读进
   os.environ，让另一个 worker 在下一个请求就用上新值，不需要重启。
"""
from __future__ import annotations

import os
from pathlib import Path

# 设置页允许写的键；与 routers/settings.py 的白名单保持一致
MANAGED_KEYS = {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"}

_state: dict[str, int | None] = {"mtime": None, "size": None}


def env_path() -> Path:
    """返回要读写的 .env 路径：优先持久卷，其次仓库根目录。"""
    data_dir = os.environ.get("MATHAGENT_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / ".env"
    return Path(__file__).resolve().parent.parent / ".env"


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[key] = value
    return out


def sync_env(force: bool = False) -> list[str]:
    """把 .env 里受管的键同步进 os.environ；返回本次真正变化的键。"""
    path = env_path()
    try:
        stat = path.stat()
    except OSError:
        return []
    if not force and _state["mtime"] == stat.st_mtime_ns and _state["size"] == stat.st_size:
        return []

    try:
        values = _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return []

    changed: list[str] = []
    for key, value in values.items():
        if key not in MANAGED_KEYS:
            continue
        if os.environ.get(key) != value:
            os.environ[key] = value
            changed.append(key)

    _state["mtime"] = stat.st_mtime_ns
    _state["size"] = stat.st_size
    return changed


def reset_cache() -> None:
    """测试用：清掉 mtime 缓存。"""
    _state["mtime"] = None
    _state["size"] = None