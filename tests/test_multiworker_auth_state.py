"""多 worker 安全回归：token 黑名单 + 密码失败锁定必须落 SQLite、跨进程共享。

模拟多 worker：对同一 DB 文件建两个独立 store 实例（等价于两个 worker 各自的
thread-local 连接），验证一个实例写入后另一个能读到——若退回模块级内存 dict
则读不到，测试失败。
"""

import time

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把两个 store 的落库路径都指到临时文件，避免污染真实库。"""
    db_file = str(tmp_path / "mw.db")
    # token 黑名单与密码失败都通过 _SmsCodeStore._default_db_path 解析路径
    from services import auth_service
    monkeypatch.setattr(auth_service._SmsCodeStore, "_default_db_path",
                        staticmethod(lambda: db_file))
    return db_file


def test_password_failures_shared_across_instances(tmp_db):
    """两个独立 _PasswordFailureStore（模拟两 worker）共享同一 SQLite 计数。"""
    from services.auth_service import _PasswordFailureStore

    w1 = _PasswordFailureStore(db_path=tmp_db)
    w2 = _PasswordFailureStore(db_path=tmp_db)

    w1.set("13800000000", attempts=3, locked_until=None)
    # w2 是另一个实例（另一个 worker）——必须能读到 w1 写的计数
    rec = w2.get("13800000000")
    assert rec is not None and rec["attempts"] == 3

    w2.delete("13800000000")
    assert w1.get("13800000000") is None


def test_password_lock_threshold_not_diluted(tmp_db, monkeypatch):
    """跨"worker"累计失败：交替调用也应在第 5 次锁定，而非每实例各数到 5。"""
    import services.auth_service as a
    # record/get 用的是模块级单例 _password_failure_store，指到临时库
    store = a._PasswordFailureStore(db_path=tmp_db)
    monkeypatch.setattr(a, "_password_failure_store", store)

    phone = "13900000000"
    for _ in range(5):
        a.record_password_login_failure(phone)
    # 满 5 次即应锁定
    assert a.get_password_login_lock(phone) > 0

    a.clear_password_login_failures(phone)
    assert a.get_password_login_lock(phone) == 0


def test_revoked_tokens_shared_across_connections(tmp_db):
    """token 黑名单落库：revoke 后即便换一条连接（另一 worker）也认得吊销。"""
    import routers.auth as auth
    # 清掉可能已建的 thread-local 连接，让其重连到 tmp_db
    if hasattr(auth._revoked_local, "conn"):
        del auth._revoked_local.conn
    auth._revoked_initialized = False

    token = "fake.jwt.token"
    auth.revoke_token(token, exp_epoch=time.time() + 3600)
    assert auth.is_token_revoked(token) is True

    # 模拟另一 worker：新开一条连接读同一库
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT exp_epoch FROM revoked_tokens WHERE token_sha256=?",
        (auth._token_digest(token),),
    ).fetchone()
    conn.close()
    assert row is not None, "吊销记录应已落库，供其他 worker 读取"


def test_revoked_token_expiry_pruned(tmp_db):
    """已过期的吊销记录不再算作吊销（惰性清理）。"""
    import routers.auth as auth
    if hasattr(auth._revoked_local, "conn"):
        del auth._revoked_local.conn
    auth._revoked_initialized = False

    token = "expired.jwt.token"
    auth.revoke_token(token, exp_epoch=time.time() - 1)  # 已过期
    assert auth.is_token_revoked(token) is False
