"""一次性票据存储（验证码票据 / 微信 OAuth state）的回归测试。

锁住三条边界：
1) 票据跨进程可校验——这正是多 worker 下「A 进程签发、B 进程核销」的最小复现；
2) 一次性——同一张票只能成功核销一次，并发下也不例外；
3) 绑定主体与 TTL——手机号/IP 不一致或过期都拒绝。

SQLite is the only ticket store, so these tests exercise exactly the production path,
whether or not REDIS_URL is set for the model limiter.
"""

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services import auth_tickets

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_tickets():
    auth_tickets.reset_for_tests()
    yield
    auth_tickets.reset_for_tests()


def test_ticket_is_single_use():
    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, phone="13900003001", ttl=60)
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003001") is True
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003001") is False


def test_ticket_is_bound_to_phone():
    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, phone="13900003002", ttl=60)
    # 换成别的手机号核销：拒绝，且票据同时被销毁（不允许反复试）
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003003") is False
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003002") is False


def test_expired_ticket_is_rejected():
    token = auth_tickets.issue(auth_tickets.KIND_WECHAT_STATE, ttl=60)
    auth_tickets.expire_for_tests(auth_tickets.KIND_WECHAT_STATE, token)
    assert auth_tickets.consume(auth_tickets.KIND_WECHAT_STATE, token) is False


def test_kinds_do_not_collide():
    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, ttl=60)
    assert auth_tickets.consume(auth_tickets.KIND_WECHAT_STATE, token) is False
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token) is True


def test_concurrent_consume_only_one_wins():
    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, ttl=60)
    results = []

    def _hit():
        results.append(auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token))

    threads = [threading.Thread(target=_hit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]


def test_ticket_issued_in_one_process_is_consumed_in_another():
    """真实跨进程：签发在 pytest 进程，核销在子进程，同一份票据库。"""
    token = auth_tickets.issue(
        auth_tickets.KIND_CAPTCHA, phone="13900003004", ttl=120
    )
    # 让子进程落在与父进程完全相同的 MATHAGENT_DATA_DIR（auth_tickets 的落库目录）
    data_dir = str(Path(auth_tickets._db_path()).parent)
    script = (
        "import sys;"
        "from services import auth_tickets;"
        "ok = auth_tickets.consume(auth_tickets.KIND_CAPTCHA, sys.argv[1], phone='13900003004');"
        "print('OK' if ok else 'MISS')"
    )
    import os

    env = dict(os.environ)
    env["MATHAGENT_DATA_DIR"] = data_dir
    env["REDIS_URL"] = ""
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    completed = subprocess.run(
        [sys.executable, "-c", script, token],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "OK", completed.stderr[-2000:]
    # 已经被另一个进程核销：本进程必须失败
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003004") is False


def test_ticket_shared_between_connections(tmp_path, monkeypatch):
    """同一 SQLite 文件被两个独立连接读写：模拟 gunicorn 两个 worker。"""
    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, phone="13900003005", ttl=120)

    import sqlite3

    db = tmp_path / "auth_tickets.db"
    assert db.is_file()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT phone FROM auth_tickets WHERE kind = ? AND token = ?",
            (auth_tickets.KIND_CAPTCHA, token),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "13900003005"

    # 另一个连接（等同另一个 worker）核销成功，原进程随即失败
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003005") is True
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003005") is False


def test_tickets_ignore_redis_url(tmp_path, monkeypatch):
    """Production sets REDIS_URL for the model limiter; tickets must still go to SQLite."""
    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    # Nothing listens on port 1: any attempt to use Redis would fail or hang here.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    token = auth_tickets.issue(auth_tickets.KIND_CAPTCHA, phone="13900003006", ttl=60)
    assert (tmp_path / "auth_tickets.db").is_file()
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003006") is True
    assert auth_tickets.consume(auth_tickets.KIND_CAPTCHA, token, phone="13900003006") is False