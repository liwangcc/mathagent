#!/usr/bin/env python3
"""MathAgent daily watchdog run by the Hermes cron job.

Every run emits one concise report for the administrator.  The checks are
limited to live infrastructure and aggregate business state; provider keys,
URLs and legacy pool names are never read or reported.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path("/opt/mathagent")
BOSS = ROOT / ".hermes_mathagent" / "boss_sessions"
DB = ROOT / "mathagent.db"
HEALTH_URL = "http://127.0.0.1:8080/api/health"
WARNINGS: list[str] = []
DOWNS: list[str] = []
OKS: list[str] = []
USER_MESSAGE = "账号统计尚未检查"
PROCESSING_MESSAGE = "批阅统计尚未检查"


def run(cmd: list[str], timeout: int = 8) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # watchdog must not crash on one failed probe
        return 1, "", str(exc)


def add_ok(msg: str) -> None:
    OKS.append(msg)


def add_warn(msg: str) -> None:
    WARNINGS.append(msg)


def add_down(msg: str) -> None:
    DOWNS.append(msg)


def reset_report_state() -> None:
    """Reset mutable report state for one independent watchdog run."""
    global USER_MESSAGE, PROCESSING_MESSAGE
    WARNINGS.clear()
    DOWNS.clear()
    OKS.clear()
    USER_MESSAGE = "账号统计尚未检查"
    PROCESSING_MESSAGE = "批阅统计尚未检查"


def check_systemd() -> None:
    services = (
        ("mathagent", "教师 Web 服务"),
        ("hermes-gateway", "Boss Hermes 网关"),
        ("redis-server", "Redis 协调服务"),
    )
    for svc, label in services:
        code, out, err = run(["systemctl", "is-active", svc], timeout=5)
        if code == 0 and out == "active":
            add_ok(f"{label}：运行中")
        else:
            add_down(f"{label}：{out or err or '未运行'}")


def check_process_port_health() -> None:
    code, _, _ = run(["pgrep", "-f", "gunicorn.*app:app"], timeout=5)
    if code == 0:
        add_ok("教师 Web 工作进程：存在")
    else:
        add_down("教师 Web 工作进程：未找到")

    code, _, _ = run(["bash", "-lc", "ss -tlnp | grep -q ':8080'"], timeout=5)
    if code == 0:
        add_ok("教师 API 端口：监听中")
    else:
        add_down("教师 API 端口：未监听")

    code, body, _ = run(
        ["curl", "-sS", "--max-time", "6", HEALTH_URL],
        timeout=8,
    )
    try:
        health = json.loads(body) if code == 0 else {}
    except json.JSONDecodeError:
        health = {}
    if health.get("status") == "ok" and health.get("database") == "ok":
        version = str(health.get("version") or "").strip()
        suffix = f"，版本 {version}" if version else ""
        add_ok(f"教师 API 健康：数据库正常{suffix}")
    else:
        service_status = str(health.get("status") or "不可用")
        database_status = str(health.get("database") or "未知")
        add_down(
            f"教师 API 健康异常：服务 {service_status}，数据库 {database_status}"
        )


def check_database() -> None:
    if not DB.exists():
        add_down("业务数据库文件：不存在")
        return
    size_mb = DB.stat().st_size / 1024 / 1024
    if size_mb > 500:
        add_warn(f"业务数据库容量较大：{size_mb:.1f} MB")
    else:
        add_ok(f"业务数据库容量：{size_mb:.1f} MB")
    code, _, err = run(["/usr/local/bin/mathagent-db-quickcheck"], timeout=10)
    if code == 0:
        add_ok("业务数据库一致性检查：通过")
    else:
        add_down(f"业务数据库一致性检查失败：{err or '只读探针失败'}")


def check_resources() -> None:
    code, out, err = run(["df", "-P", "/"], timeout=5)
    if code == 0:
        lines = out.splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            pct = int(parts[4].rstrip("%")) if len(parts) > 4 and parts[4].rstrip("%").isdigit() else -1
            if pct >= 95:
                add_down(f"根分区使用率 {pct}%")
            elif pct >= 85:
                add_warn(f"根分区使用率 {pct}%")
            else:
                add_ok(f"根分区使用率 {pct}%")
    else:
        add_warn(f"磁盘检查失败: {err}")

    code, out, err = run(["free", "-m"], timeout=5)
    if code == 0:
        for line in out.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                total = int(parts[1])
                used = int(parts[2])
                pct = round(used * 100 / total) if total else 0
                if pct >= 95:
                    add_down(f"内存使用率 {pct}% ({used}/{total} MB)")
                elif pct >= 85:
                    add_warn(f"内存使用率 {pct}% ({used}/{total} MB)")
                else:
                    add_ok(f"内存使用率 {pct}% ({used}/{total} MB)")
                break
    else:
        add_warn(f"内存检查失败: {err}")


def check_redis() -> None:
    """Verify the shared coordination dependency without exposing its config."""
    code, out, err = run(["redis-cli", "--raw", "ping"], timeout=5)
    if code == 0 and out == "PONG":
        add_ok("Redis 协调服务连通")
    else:
        add_down(f"Redis 协调服务异常: {out or err or 'ping failed'}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MathAgent read-only daily watchdog")
    return parser.parse_args(argv)


def recent_lines(path: Path, max_lines: int = 800, hours: int = 24) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    out: list[str] = []
    ts_re = re.compile(r"(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)")
    for line in lines:
        m = ts_re.search(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= cutoff:
            out.append(line)
    return out


def check_logs() -> None:
    logs = [ROOT / "logs/error.log", ROOT / "logs/mathagent.log", BOSS / "logs/errors.log", BOSS / "logs/gateway.log"]
    patterns = {
        "模型服务认证": re.compile(
            r"Invalid API Key|invalid_key|401 Unauthorized|HTTP 401|HTTP 403|authentication failed|unauthorized",
            re.I,
        ),
        "教师小M调用": re.compile(r"Hermes agent call failed|Hermes agent returned failed", re.I),
        "应用异常堆栈": re.compile(r"Traceback", re.I),
        "Boss 通道网络": re.compile(r"poll error|Connection reset|Cannot connect", re.I),
    }
    counts: dict[str, int] = {k: 0 for k in patterns}
    for path in logs:
        for line in recent_lines(path):
            if "Worker" in line and "SIGTERM" in line:
                continue
            for label, pat in patterns.items():
                if pat.search(line):
                    counts[label] += 1
    if counts["模型服务认证"]:
        add_down(f"最近24小时模型服务认证错误：{counts['模型服务认证']} 条")
    if counts["教师小M调用"]:
        add_warn(f"最近24小时教师小M调用失败：{counts['教师小M调用']} 条")
    if counts["应用异常堆栈"]:
        add_warn(f"最近24小时应用异常堆栈：{counts['应用异常堆栈']} 条")
    if counts["Boss 通道网络"] >= 5:
        add_warn(f"最近24小时 Boss 通道网络错误：{counts['Boss 通道网络']} 条")
    elif counts["Boss 通道网络"]:
        add_ok(f"Boss 通道偶发网络错误：{counts['Boss 通道网络']} 条（低于阈值）")
    if not any(counts.values()):
        add_ok("最近24小时未发现关键错误模式")


def check_users() -> None:
    """Report aggregate account counts without exposing user identity data."""
    global USER_MESSAGE
    query = (
        "import json, sqlite3; "
        "db = sqlite3.connect('file:/opt/mathagent/mathagent.db?mode=ro', uri=True, timeout=2); "
        "q = lambda sql, params=(): db.execute(sql, params).fetchone()[0]; "
        "print(json.dumps({"
        "'total': q('select count(*) from users'), "
        "'active': q('select count(*) from users where status = ?', ('active',)), "
        "'teachers': q('select count(*) from users where role = ? and status = ?', ('teacher', 'active')), "
        "'admins': q('select count(*) from users where role = ? and status = ?', ('admin', 'active'))"
        "}, ensure_ascii=False))"
    )
    code, out, _ = run(
        ["sudo", "-u", "www-data", "/opt/mathagent/venv/bin/python", "-c", query],
        timeout=10,
    )
    if code != 0:
        USER_MESSAGE = "账号统计不可用"
        add_warn("账号统计：不可用")
        return
    try:
        report = json.loads(out)
        total = int(report["total"])
        active = int(report["active"])
        teachers = int(report["teachers"])
        admins = int(report["admins"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        USER_MESSAGE = "账号统计结果无效"
        add_warn("账号统计：返回格式无效")
        return
    USER_MESSAGE = f"注册 {total}，启用 {active}（教师 {teachers}，管理员 {admins}）"
    add_ok("账号统计：正常")


def check_processing_totals() -> None:
    """Report retained grading/image totals without reading identity data."""
    global PROCESSING_MESSAGE
    query = (
        "import json, sqlite3; "
        "db = sqlite3.connect('file:/opt/mathagent/mathagent.db?mode=ro', uri=True, timeout=2); "
        "rows = db.execute('select ocr_raw_json from grading_results where deleted_at is null').fetchall(); "
        "records = len(rows); images = 0; "
        "\nfor (raw,) in rows:\n"
        "    try: value = json.loads(raw or '{}')\n"
        "    except (TypeError, ValueError, json.JSONDecodeError): value = {}\n"
        "    pages = value.get('pages') if isinstance(value, dict) and value.get('source') == 'answer_sheet' else None\n"
        "    images += len(pages) if isinstance(pages, list) and pages else 1\n"
        "print(json.dumps({'records': records, 'images': images}, ensure_ascii=False)); "
        "db.close()"
    )
    code, out, _ = run(
        ["sudo", "-u", "www-data", "/opt/mathagent/venv/bin/python", "-c", query],
        timeout=12,
    )
    if code != 0:
        PROCESSING_MESSAGE = "批阅统计不可用"
        add_warn("批阅统计：不可用")
        return
    try:
        report = json.loads(out)
        records = int(report["records"])
        images = int(report["images"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        PROCESSING_MESSAGE = "批阅统计结果无效"
        add_warn("批阅统计：返回格式无效")
        return
    PROCESSING_MESSAGE = f"当前保留批改结果 {records} 条，对应处理图片 {images} 张"
    add_ok("批阅统计：正常")


def build_report(now: datetime | None = None) -> str:
    """Render a compact, channel-friendly Chinese status report."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    if DOWNS:
        overall = "🔴 异常"
    elif WARNINGS:
        overall = "🟡 需关注"
    else:
        overall = "🟢 正常"

    lines = [
        "🧭 MathAgent 每日巡查",
        f"时间：{current.strftime('%Y-%m-%d %H:%M %z')}",
        f"状态：{overall}",
        f"概览：✅ 正常 {len(OKS)} ｜ 🟡 提醒 {len(WARNINGS)} ｜ 🔴 异常 {len(DOWNS)}",
        "",
        "业务概览",
        f"- 账号：{USER_MESSAGE}",
        f"- 批阅：{PROCESSING_MESSAGE}",
    ]
    for title, marker, items in (
        ("异常项", "🔴", DOWNS),
        ("提醒项", "🟡", WARNINGS),
        ("正常项", "✅", OKS),
    ):
        if not items:
            continue
        lines.extend(("", title))
        lines.extend(f"{marker} {item}" for item in items)

    if DOWNS:
        conclusion = "建议：优先处理异常项，处理后重新执行巡查。"
    elif WARNINGS:
        conclusion = "建议：当前服务可用，请关注提醒项的变化。"
    else:
        conclusion = "结论：当前未发现需要处理的问题。"
    lines.extend(("", conclusion))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    reset_report_state()
    check_systemd()
    check_process_port_health()
    check_database()
    check_resources()
    check_redis()
    check_logs()
    check_users()
    check_processing_totals()

    print(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
