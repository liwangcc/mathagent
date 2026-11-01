"""短信发送限流（SQLite 存储，多 worker 共享）。

为什么需要：/auth/sms/send 历史上只对「同一手机号」做冷却，攻击者换号即可无限发送，
账单直接计入阿里云账号。本模块在发号前按「来源 IP」和「全局日总量」封顶。

规则（均可用环境变量覆盖）：
- 单 IP：每小时 SMS_IP_HOURLY_LIMIT 条（默认 10），每天 SMS_IP_DAILY_LIMIT 条（默认 40）
- 全局：每天 SMS_DAILY_LIMIT 条（默认 300）
- 手机号维度的冷却仍由 auth_service.SMS_SEND_INTERVAL 负责，这里不重复实现

计数在真正调用运营商接口**之前**写入：宁可少发一条，也不允许超发。
"""

import logging
import os
import sqlite3
import threading
import time

from config import DATA_DIR

logger = logging.getLogger(__name__)

IP_HOURLY_LIMIT = int(os.environ.get("SMS_IP_HOURLY_LIMIT", "10"))
IP_DAILY_LIMIT = int(os.environ.get("SMS_IP_DAILY_LIMIT", "40"))
GLOBAL_DAILY_LIMIT = int(os.environ.get("SMS_DAILY_LIMIT", "300"))

DB_PATH = os.path.join(DATA_DIR, "sms_throttle.db")

_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    global _initialized
    if _initialized:
        return
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sms_sends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_ip_ts ON sms_sends(ip, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_ts ON sms_sends(ts)")
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def check_and_record(ip: str) -> dict:
    """检查配额并记账。返回 {"ok": bool, "message": str}。

    ip 为空时按 "unknown" 计数（不会因为拿不到 IP 而放行）。
    """
    _init_db()
    ip = (ip or "unknown").strip() or "unknown"
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400

    with _lock:
        conn = _connect()
        try:
            # 顺手清理 2 天前的记录，表始终很小
            conn.execute("DELETE FROM sms_sends WHERE ts < ?", (now - 2 * 86400,))

            ip_hour = conn.execute(
                "SELECT COUNT(*) FROM sms_sends WHERE ip = ? AND ts >= ?", (ip, hour_ago)
            ).fetchone()[0]
            if ip_hour >= IP_HOURLY_LIMIT:
                return {"ok": False, "message": "验证码发送过于频繁，请稍后再试"}

            ip_day = conn.execute(
                "SELECT COUNT(*) FROM sms_sends WHERE ip = ? AND ts >= ?", (ip, day_ago)
            ).fetchone()[0]
            if ip_day >= IP_DAILY_LIMIT:
                return {"ok": False, "message": "今日验证码发送次数已达上限"}

            total_day = conn.execute(
                "SELECT COUNT(*) FROM sms_sends WHERE ts >= ?", (day_ago,)
            ).fetchone()[0]
            if total_day >= GLOBAL_DAILY_LIMIT:
                logger.error(
                    "短信全局日配额已用尽：%s/%s，疑似被刷", total_day, GLOBAL_DAILY_LIMIT
                )
                return {"ok": False, "message": "系统今日短信额度已用尽，请联系管理员"}

            conn.execute("INSERT INTO sms_sends (ip, ts) VALUES (?, ?)", (ip, now))
            conn.commit()
            return {"ok": True, "message": ""}
        finally:
            conn.close()


def reset_for_tests() -> None:
    """测试专用：清空发送记录。生产代码不要调用。"""
    _init_db()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sms_sends")
            conn.commit()
        finally:
            conn.close()


def usage_summary() -> dict:
    """仅供诊断：返回今日各 IP 与总量。"""
    _init_db()
    day_ago = time.time() - 86400
    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM sms_sends WHERE ts >= ?", (day_ago,)
            ).fetchone()[0]
            top = conn.execute(
                "SELECT ip, COUNT(*) c FROM sms_sends WHERE ts >= ? GROUP BY ip ORDER BY c DESC LIMIT 5",
                (day_ago,),
            ).fetchall()
            return {"total_24h": total, "top_ips": [{"ip": r[0], "count": r[1]} for r in top]}
        finally:
            conn.close()