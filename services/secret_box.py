"""模型密钥的静态加密（AES-256-GCM）。

为什么要加密：`model_profiles.api_key` 存的是学校的真实账单凭据。SQLite 文件一旦
被拷走（备份、误上传、运维顺手 `scp`），明文 Key 就等于泄露。

主密钥来源（按顺序）：
1. 环境变量 `MODEL_KEY_MASTER`：base64 编码的 32 字节；
2. 持久卷里的 `data/.model_key_master`：首次使用时生成一次（0600），之后固定复用，
   不随重启变化。

存储格式：`enc:v1:<base64(nonce(12B) || ciphertext+tag)>`。
解密失败（密文被改、主密钥换了）时返回空串并按「未配置」处理，绝不把密文或异常
内容写进日志——日志里出现 Key 和没加密一样糟。
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import threading

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
NONCE_BYTES = 12
AAD = b"mathagent:model_profiles:v1"
KEY_BYTES = 32

_master_lock = threading.Lock()
_master_cache: bytes | None = None


def _master_file_path() -> str:
    try:
        from config import DATA_DIR
    except Exception:  # pragma: no cover - config 在正常启动路径上一定可用
        DATA_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
    return os.path.join(DATA_DIR, ".model_key_master")


def _decode_master(raw: bytes) -> bytes:
    if len(raw) != KEY_BYTES:
        raise RuntimeError("模型密钥主密钥必须是 32 字节（base64 解码后）")
    return raw


def master_key() -> bytes:
    """返回 32 字节主密钥；环境变量优先，其次持久卷文件，最后生成并落盘。"""
    global _master_cache

    env_value = (os.environ.get("MODEL_KEY_MASTER") or "").strip()
    if env_value:
        try:
            return _decode_master(base64.b64decode(env_value, validate=True))
        except Exception as exc:
            raise RuntimeError(
                "MODEL_KEY_MASTER 无效：需要 base64 编码的 32 字节随机值"
            ) from exc

    if _master_cache is not None:
        return _master_cache

    with _master_lock:
        if _master_cache is not None:
            return _master_cache

        path = _master_file_path()
        if os.path.exists(path):
            with open(path, "rb") as fh:
                stored = fh.read().strip()
            _master_cache = _decode_master(base64.b64decode(stored))
            return _master_cache

        generated = secrets.token_bytes(KEY_BYTES)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # 先以 0600 创建再写入，避免中间态被其他用户读到
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, base64.b64encode(generated))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows 上 chmod 语义有限，失败不阻断启动
            pass
        logger.warning(
            "已生成模型密钥主密钥并写入 %s（0600）；备份 data/ 时必须一并备份",
            path,
        )
        _master_cache = generated
        return _master_cache


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(plaintext: str) -> str:
    """把明文 Key 变成 `enc:v1:...`。空值直接返回空串。"""
    text = (plaintext or "").strip()
    if not text:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(master_key()).encrypt(nonce, text.encode("utf-8"), AAD)
    return PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(value: str) -> str:
    """解密。历史明文（没有前缀）原样返回；任何失败都返回空串。"""
    token = (value or "").strip()
    if not token:
        return ""
    if not token.startswith(PREFIX):
        # 兼容本次改造之前写入的明文行；下次保存会自动加密。
        return token
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.b64decode(token[len(PREFIX):], validate=True)
        if len(raw) <= NONCE_BYTES:
            raise ValueError("ciphertext too short")
        nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        return AESGCM(master_key()).decrypt(nonce, ciphertext, AAD).decode("utf-8")
    except Exception:
        # 不打印任何密文/明文片段，只说「这一行失效了」。
        logger.warning("模型密钥解密失败（证书损坏或主密钥已更换），按未配置处理")
        return ""


def reset_cache_for_tests() -> None:
    global _master_cache
    with _master_lock:
        _master_cache = None