"""模型设置的环境变量同步。

为什么需要它：设置页保存的是「进程内 os.environ + 一份 .env 文件」。
在单进程里这样够用，但生产用 gunicorn 起两个 worker，保存请求只落在其中一个进程上，
另一个进程会继续用旧值；容器部署时 .env 还写在镜像层，容器一重建就丢。

所以这里做两件事：
1. .env 的位置跟运行形态走——设置了 MATHAGENT_DATA_DIR（容器/生产）就写进持久卷，
   否则写仓库根目录的 .env；
2. 每个请求开头比对一次文件 mtime/size（一次 stat，微秒级），变了就把受管的键重新读进
   os.environ，让另一个 worker 在下一个请求就用上新值，不需要重启。

解析规则跟 python-dotenv 对齐（启动时 python-dotenv 也读同一个文件）：只有 \n 才分行，
支持 `export ` 前缀、单/双引号、行尾注释。
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


def split_lines(text: str) -> list[str]:
    """按 python-dotenv 的规则切行：只有 \n 结束一行。

    写盘端与解析端必须共用这一个函数。str.splitlines() 还会在
    \x0b \x0c \x1c \x1d \x1e \x85 U+2028 U+2029 上分行；两边规则一旦不同，
    「写进去一行」就会「读回来两行」，等于多出一个键（第 4 轮 finding 1）。
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    # 末尾那个换行不单独算一行，行为与 splitlines() 一致
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _unquote(raw: str) -> str | None:
    """按 dotenv 的规则取一个值：单引号原样、双引号反转义、无引号去行尾注释。

    引号没有闭合时返回 None —— python-dotenv 会整行丢弃，这里也必须丢弃，
    否则同一份文件在启动时（dotenv）和同步时（这里）得到不同的值。
    """
    raw = raw.strip()
    if raw[:1] == "'":
        end = raw.find("'", 1)
        return raw[1:end] if end != -1 else None
    if raw[:1] == '"':
        out = []
        i = 1
        closed = False
        while i < len(raw):
            ch = raw[i]
            if ch == '"':
                closed = True
                break
            if ch == "\\" and i + 1 < len(raw):
                nxt = raw[i + 1]
                if nxt == "n":
                    out.append("\n")
                elif nxt == "t":
                    out.append("\t")
                elif nxt in ('"', "\\"):
                    out.append(nxt)
                else:
                    out.append(ch + nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out) if closed else None
    # 未加引号：dotenv 把空白后的 # 当注释起点
    for marker in (" #", "\t#"):
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]
    return raw.strip()


def _parse(text: str) -> dict[str, str]:
    """按 python-dotenv 的规则解析。

    只有 \\n 才分行。str.splitlines() 还会在 \\x0b \\x0c \\x85 U+2028 上分行，
    与写盘端「一行一个键」的假设不一致，等于给设置页留了注入通道。
    """
    # BOM 是刻意的例外：python-dotenv 会把 BOM 留在键名里（\ufeffLLM_API_KEY），
    # 那个键谁都读不到。这里去掉 BOM，键才能被应用；代价是带 BOM 的文件在 import 阶段
    # 由 dotenv 读出的第一行不生效，要到第一个请求的 sync_env 才补上。设置页自己写不出
    # BOM，所以只有手工编辑才会遇到。
    if text.startswith("\ufeff"):
        text = text[1:]

    out: dict[str, str] = {}
    for raw in split_lines(text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = _unquote(rest)
        if value is None:
            # 引号没闭合：dotenv 丢掉这一行，这里也丢掉，os.environ 保持启动时的值
            continue
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