"""IP级别登录限流（SQLite存储，多worker共享）

规则：
- 连续10次失败 → 锁30分钟（锁定到期后计数清零）
- 连续15次失败 → 永久封禁
- 登录成功 → 计数清零
"""
import time
import logging
import sqlite3
import os
from contextlib import contextmanager

from config import DATA_DIR

logger = logging.getLogger(__name__)

IP_LOCK_THRESHOLD = 10      # 连续10次失败 → 锁30分钟
IP_LOCK_DURATION = 30 * 60  # 30分钟
IP_BAN_THRESHOLD = 15       # 连续15次失败 → 限时封禁
# 封禁必须有时限：学校普遍共用出口 IP，永久封禁会把整所学校挡在登录之外。
IP_BAN_DURATION = int(float(os.environ.get("IP_BAN_HOURS", "24")) * 3600)

DB_PATH = os.path.join(DATA_DIR, "ip_failures.db")


def _init_db():
    """初始化数据库表（仅执行一次）。"""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ip_failures (
            ip TEXT PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            locked_until REAL,
            banned INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


@contextmanager
def _get_conn():
    """SQLite连接上下文管理器，自动提交/回滚/关闭。"""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=3000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 模块加载时初始化表
_init_db()


def get_ip_lock(ip: str) -> int:
    """返回IP剩余锁定秒数，0=未锁定，-1=永久封禁。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT attempts, locked_until, banned FROM ip_failures WHERE ip=?", (ip,)
        ).fetchone()
        if not row:
            return 0
        attempts, locked_until, banned = row
        if banned:
            # 封禁到期自动解除；只有仍在封禁期内的才返回 -1。
            if not locked_until or time.time() >= locked_until:
                conn.execute("DELETE FROM ip_failures WHERE ip=?", (ip,))
                return 0
            return -1
        if locked_until:
            remaining = int(locked_until - time.time())
            if remaining <= 0:
                conn.execute("DELETE FROM ip_failures WHERE ip=?", (ip,))
                return 0
            return remaining
        return 0


def record_ip_failure(ip: str) -> None:
    """记录一次IP登录失败。"""
    with _get_conn() as conn:
        # Serialize the read-modify-write sequence across workers.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts, locked_until, banned FROM ip_failures WHERE ip=?", (ip,)
        ).fetchone()

        if not row:
            conn.execute(
                "INSERT INTO ip_failures (ip, attempts, locked_until, banned) VALUES (?, 1, NULL, 0)",
                (ip,)
            )
            return

        attempts, locked_until, banned = row
        if banned:
            return

        # 锁定已过期，清零重新计数
        if locked_until and time.time() >= locked_until:
            attempts = 0
            locked_until = None

        attempts += 1

        if attempts >= IP_BAN_THRESHOLD:
            conn.execute(
                "UPDATE ip_failures SET attempts=?, locked_until=?, banned=1 WHERE ip=?",
                (attempts, time.time() + IP_BAN_DURATION, ip)
            )
            logger.warning(
                "IP %s banned for %dh after %d consecutive failures",
                ip,
                IP_BAN_DURATION // 3600,
                attempts,
            )
        elif attempts >= IP_LOCK_THRESHOLD:
            conn.execute(
                "UPDATE ip_failures SET attempts=?, locked_until=? WHERE ip=?",
                (attempts, time.time() + IP_LOCK_DURATION, ip)
            )
            logger.warning("IP %s locked for 30min after %d consecutive failures", ip, attempts)
        else:
            conn.execute(
                "UPDATE ip_failures SET attempts=?, locked_until=NULL WHERE ip=?",
                (attempts, ip)
            )


def clear_ip_failures(ip: str) -> None:
    """登录成功后清除IP失败记录。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM ip_failures WHERE ip=?", (ip,))


def unban_ip(ip: str) -> bool:
    """管理员手动解封／清零某 IP 的失败计数。返回是否确实删除了记录。"""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM ip_failures WHERE ip=?", (ip,))
        return cur.rowcount > 0
