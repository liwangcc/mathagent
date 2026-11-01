"""一次性票据存储 — 单一事实来源：SQLite。

为什么需要：验证码票据（`services/auth_service._captcha_tickets`）与微信 OAuth
state（`routers/auth._wechat_states`）原本存在进程内字典里。`deploy/gunicorn.conf.py`
允许 2 个 worker 时，票据在 A 进程签发、B 进程校验会随机失败，表现为「请先完成
图形验证」或「微信授权状态已失效」的间歇性错误，且无法通过重试自愈。

Why SQLite only (no Redis): the deployment is one school on one server. Every worker runs
on that host and opens the same file under the data directory, so the store is already
shared across workers. A Redis-first path added nothing on one host but did add failure
modes: a ticket issued to one backend and consumed from the other was rejected, and a
Redis write whose reply timed out was written to SQLite as well. Tickets live for minutes,
so the file needs no durability beyond the data volume it already sits in.

两条不变式：
1) 一次性：核销即删除。`BEGIN IMMEDIATE` 事务 + DELETE，保证跨进程只有一次成功。
2) 绑定主体：票据可绑定手机号（或 IP），核销时主体不一致 → 依然删除并拒绝，
   不给攻击者留下反复试错的机会。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time

# 票据种类：与签发/核销两端的字符串保持一致，避免拼写漂移。
KIND_CAPTCHA = "captcha"
KIND_WECHAT_STATE = "wechat_state"
KIND_CAPTCHA_CHALLENGE = "captcha_challenge"

DEFAULT_TTL = 180

_sqlite_initialized_path: str | None = None
_sqlite_lock = threading.Lock()


def _db_path() -> str:
    # 显式环境变量优先：容器里指向持久卷，测试里指向临时目录（子进程也能继承）。
    env_dir = os.environ.get("MATHAGENT_DATA_DIR", "").strip()
    if env_dir:
        return os.path.join(env_dir, "auth_tickets.db")
    try:
        from config import DATA_DIR
    except Exception:  # pragma: no cover - config 在正常启动路径上一定可用
        DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(DATA_DIR, "auth_tickets.db")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _rollback(conn: sqlite3.Connection) -> None:
    # BEGIN IMMEDIATE itself can fail (busy timeout); a bare ROLLBACK would then raise
    # "no transaction is active" and hide the real error.
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _init_sqlite() -> None:
    global _sqlite_initialized_path
    path = _db_path()
    if _sqlite_initialized_path == path:
        return
    with _sqlite_lock:
        if _sqlite_initialized_path == path:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tickets (
                    kind TEXT NOT NULL,
                    token TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (kind, token)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tickets_exp ON auth_tickets(expires_at)")
        finally:
            conn.close()
        _sqlite_initialized_path = path


def _sqlite_issue(kind: str, token: str, phone: str, ttl: int) -> None:
    _init_sqlite()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM auth_tickets WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT OR REPLACE INTO auth_tickets (kind, token, phone, expires_at) VALUES (?, ?, ?, ?)",
            (kind, token, phone, time.time() + max(1, int(ttl))),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _sqlite_consume(kind: str, token: str) -> str | None:
    _init_sqlite()
    conn = _connect()
    try:
        # IMMEDIATE 拿写锁：并发的两个请求里只有一个能读到并删除该行。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT phone, expires_at FROM auth_tickets WHERE kind = ? AND token = ?",
            (kind, token),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "DELETE FROM auth_tickets WHERE kind = ? AND token = ?", (kind, token)
        )
        conn.execute("COMMIT")
        phone, expires_at = row
        if expires_at < time.time():
            return None
        return phone or ""
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


# ---------- 对外接口 ----------

def issue(kind: str, phone: str = "", ttl: int = DEFAULT_TTL, token: str | None = None) -> str:
    """签发一次性票据。phone 为空表示不绑定主体。

    token 参数用于「自签名票据」：调用方已经生成好带签名的串，这里只负责落库与
    一次性核销（内置算术验证码就是这样把签名和一次性存储合到一起的）。
    """
    token = (token or "").strip() or secrets.token_urlsafe(24)
    _sqlite_issue(kind, token, (phone or "").strip(), ttl)
    return token


def consume(kind: str, token: str, phone: str = "") -> bool:
    """核销票据：一次性；绑定主体不一致时同样删除并拒绝。"""
    if not token:
        return False
    stored = _sqlite_consume(kind, token)
    if stored is None:
        return False
    bound = (stored or "").strip()
    if bound and (phone or "").strip() != bound:
        return False
    return True


def purge_expired() -> int:
    """清理过期票据，返回删除行数。"""
    _init_sqlite()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM auth_tickets WHERE expires_at < ?", (time.time(),))
        return cur.rowcount or 0
    finally:
        conn.close()


def reset_for_tests() -> None:
    """测试专用：清空票据，避免上一个用例的票漏进来。"""
    _init_sqlite()
    conn = _connect()
    try:
        conn.execute("DELETE FROM auth_tickets")
    finally:
        conn.close()


def expire_for_tests(kind: str, token: str) -> None:
    """测试专用：让票据立刻过期，验证 TTL 分支。"""
    _init_sqlite()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE auth_tickets SET expires_at = 0 WHERE kind = ? AND token = ?",
            (kind, token),
        )
    finally:
        conn.close()
