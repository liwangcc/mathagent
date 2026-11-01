"""模型密钥静态加密（AES-GCM）的回归测试。

三条底线：
1) 加密→存→读回一致，且库里不出现明文；
2) 密文被篡改时解密失败但不抛异常、不崩溃，按「未配置」处理；
3) 主密钥缺失时自动生成、写入持久卷，并且第二次调用复用同一把（重启不失效）。
"""

import base64
import os

import pytest

from services import secret_box


@pytest.fixture(autouse=True)
def _isolated_master(tmp_path, monkeypatch):
    import config

    monkeypatch.delenv("MODEL_KEY_MASTER", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    secret_box.reset_cache_for_tests()
    yield
    secret_box.reset_cache_for_tests()


def test_encrypt_decrypt_roundtrip():
    key = "sk-dashscope-abcdefghijklmnop"
    token = secret_box.encrypt(key)
    assert token.startswith(secret_box.PREFIX)
    assert key not in token
    assert secret_box.decrypt(token) == key


def test_empty_value_stays_empty():
    assert secret_box.encrypt("") == ""
    assert secret_box.decrypt("") == ""


def test_tampered_ciphertext_fails_closed():
    token = secret_box.encrypt("sk-original-key-123456")
    raw = bytearray(base64.b64decode(token[len(secret_box.PREFIX):]))
    raw[-1] ^= 0xFF
    tampered = secret_box.PREFIX + base64.b64encode(bytes(raw)).decode()

    # 不抛异常，按未配置处理
    assert secret_box.decrypt(tampered) == ""


def test_master_key_generated_once_and_reused(tmp_path):
    path = tmp_path / ".model_key_master"
    assert not path.exists()

    first = secret_box.master_key()
    assert len(first) == 32
    assert path.is_file()

    # 进程内缓存失效后（等价于重启）仍然读到同一把
    secret_box.reset_cache_for_tests()
    second = secret_box.master_key()
    assert second == first

    # 跨进程/重启后旧密文仍能解开
    token = secret_box.encrypt("sk-persisted-key-9999")
    secret_box.reset_cache_for_tests()
    assert secret_box.decrypt(token) == "sk-persisted-key-9999"


def test_env_master_key_takes_precedence(monkeypatch):
    env_key = b"0" * 32
    monkeypatch.setenv("MODEL_KEY_MASTER", base64.b64encode(env_key).decode())

    token = secret_box.encrypt("sk-env-master-key")
    # 换成另一把主密钥后必须解不开（隔离性）
    monkeypatch.setenv("MODEL_KEY_MASTER", base64.b64encode(b"1" * 32).decode())
    secret_box.reset_cache_for_tests()
    assert secret_box.decrypt(token) == ""


def test_invalid_env_master_key_raises(monkeypatch):
    monkeypatch.setenv("MODEL_KEY_MASTER", "not-base64!!")
    with pytest.raises(RuntimeError):
        secret_box.master_key()


def test_master_key_file_is_private(tmp_path):
    secret_box.master_key()
    path = tmp_path / ".model_key_master"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600