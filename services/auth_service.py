"""认证服务 — 短信验证码、JWT、用户注册/登录、微信OAuth。

短信发送目前在内存中存储验证码（开发阶段方案）。阿里云短信SDK调用以接口框架的
形式提供，待补全 SMS_ACCESS_KEY_ID / SECRET / SMS_TEMPLATE_CODE 后即可启用。
"""

import logging
import os
import re
import secrets
import sqlite3
import string
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt

# PyJWT 与 python-jose 的 HS256 行为一致；python-jose 已停止维护且有已知 CVE。
from sqlalchemy.orm import Session
import bcrypt

from config import (
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
    SMS_ACCESS_KEY_ID,
    SMS_ACCESS_KEY_SECRET,
    SMS_SIGN_NAME,
    SMS_TEMPLATE_CODE,
    WECHAT_APP_ID,
    WECHAT_APP_SECRET,
    WECHAT_REDIRECT_URI,
)
from models_user import User

logger = logging.getLogger(__name__)

# 验证码有效期（秒）
SMS_CODE_TTL = 5 * 60
# 同一手机号发送间隔（秒）
SMS_SEND_INTERVAL = 60
# 验证失败次数上限
SMS_MAX_ATTEMPTS = 5
# 锁定时间（秒）
SMS_LOCKOUT_DURATION = 15 * 60
# 中国手机号正则
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 仅当显式开启 DEV_MODE 时才在响应中返回验证码
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")


class _SmsCodeStore:
    """基于 SQLite 的验证码存储，多 worker 共享。

    表结构：sms_codes(phone PK, code, expires_at, sent_at, attempts, locked_until)
    使用 WAL 模式 + 短超时，适合高并发写入。
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path or self._default_db_path()
        self._local = threading.local()
        self._initialized = False
        # 惰性建表：导入期不触碰磁盘，避免路径不可用时整个模块加载失败（如测试/CI）。

    @staticmethod
    def _default_db_path() -> str:
        """默认与应用主库同一个文件，避免验证码写入与主库分叉到不同路径。

        优先级：显式 MATHAGENT_DATA_DIR/mathagent.db > 解析 config.DATABASE_URL >
        兜底 /opt/mathagent/mathagent.db。
        """
        data_dir = os.environ.get("MATHAGENT_DATA_DIR")
        if data_dir:
            return os.path.join(data_dir, "mathagent.db")
        try:
            from config import DATABASE_URL
            if DATABASE_URL.startswith("sqlite:///"):
                return DATABASE_URL[len("sqlite:///"):]
        except Exception:
            pass
        return os.path.join("/opt/mathagent", "mathagent.db")

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            self._local.conn = conn
        if not self._initialized:
            self._init_table()
            self._initialized = True
        return conn

    def _init_table(self):
        # 直接用 thread-local 连接，避免与 _get_conn 互相递归调用。
        conn = self._local.conn
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sms_codes (
                phone TEXT PRIMARY KEY,
                code TEXT,
                expires_at REAL,
                sent_at REAL,
                attempts INTEGER DEFAULT 0,
                locked_until REAL
            )
        """)
        conn.commit()

    def get(self, phone: str, default=None):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT code, expires_at, sent_at, attempts, locked_until FROM sms_codes WHERE phone=?",
            (phone,),
        ).fetchone()
        if not row:
            return default
        return {
            "code": row[0],
            "expires_at": row[1],
            "sent_at": row[2],
            "attempts": row[3],
            "locked_until": row[4],
        }

    def set(self, phone: str, record: dict):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO sms_codes (phone, code, expires_at, sent_at, attempts, locked_until) VALUES (?,?,?,?,?,?)",
            (
                phone,
                record.get("code"),
                record.get("expires_at"),
                record.get("sent_at"),
                record.get("attempts", 0),
                record.get("locked_until"),
            ),
        )
        conn.commit()

    def update(self, phone: str, **fields):
        record = self.get(phone)
        if not record:
            return
        record.update(fields)
        self.set(phone, record)

    def pop(self, phone: str, default=None) -> dict | None:
        record = self.get(phone)
        if record:
            conn = self._get_conn()
            conn.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
            conn.commit()
            return record
        return default

    def clear(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM sms_codes")
        conn.commit()

    # 字典兼容方法，让现有代码无需改动
    def __setitem__(self, phone: str, record: dict):
        self.set(phone, record)

    def __getitem__(self, phone: str) -> dict:
        record = self.get(phone)
        if record is None:
            raise KeyError(phone)
        return record

    def __contains__(self, phone: str) -> bool:
        return self.get(phone) is not None


_sms_code_store = _SmsCodeStore()


class _PasswordFailureStore:
    """基于 SQLite 的密码登录失败计数，多 worker 共享（复用 _SmsCodeStore 的连接口径）。

    表结构：password_failures(phone PK, attempts, locked_until)
    与验证码同库同文件，惰性建表。多 worker 部署下所有 worker 共享同一计数，
    否则每 worker 各数各的，锁定阈值会被稀释（5 次膨胀成约 5×worker 数）。
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path or _SmsCodeStore._default_db_path()
        self._local = threading.local()
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        # CREATE TABLE is idempotent; performing it on every new thread-local
        # connection avoids racing on the process-wide initialization flag.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS password_failures ("
            "phone TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, locked_until REAL)"
        )
        conn.commit()
        self._initialized = True
        return conn

    def get(self, phone: str):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT attempts, locked_until FROM password_failures WHERE phone=?", (phone,)
        ).fetchone()
        if not row:
            return None
        return {"attempts": row[0], "locked_until": row[1]}

    def set(self, phone: str, attempts: int, locked_until):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO password_failures (phone, attempts, locked_until) VALUES (?,?,?)",
            (phone, attempts, locked_until),
        )
        conn.commit()

    def record_failure(self, phone: str, *, max_attempts: int, lockout_duration: int) -> dict:
        """Atomically increment one account's failure count and return its state."""
        conn = self._get_conn()
        now = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempts, locked_until FROM password_failures WHERE phone=?",
                (phone,),
            ).fetchone()
            attempts = int(row[0]) if row else 0
            locked_until = row[1] if row else None
            if locked_until and now >= locked_until:
                attempts = 0
                locked_until = None
            attempts += 1
            if attempts >= max_attempts:
                locked_until = now + lockout_duration
            conn.execute(
                "INSERT INTO password_failures (phone, attempts, locked_until) VALUES (?,?,?) "
                "ON CONFLICT(phone) DO UPDATE SET attempts=excluded.attempts, "
                "locked_until=excluded.locked_until",
                (phone, attempts, locked_until),
            )
            conn.commit()
            return {"attempts": attempts, "locked_until": locked_until}
        except Exception:
            conn.rollback()
            raise

    def delete(self, phone: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM password_failures WHERE phone=?", (phone,))
        conn.commit()

    def clear(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM password_failures")
        conn.commit()


_password_failure_store = _PasswordFailureStore()


def _get_dypnsapi_client():
    """创建号码认证服务客户端。"""
    from alibabacloud_dypnsapi20170525.client import Client as Dypnsapi20170525Client
    from alibabacloud_tea_openapi import models as open_api_models

    config = open_api_models.Config(
        access_key_id=SMS_ACCESS_KEY_ID,
        access_key_secret=SMS_ACCESS_KEY_SECRET,
        endpoint="dypnsapi.aliyuncs.com",
    )
    return Dypnsapi20170525Client(config)


def send_sms_code(phone: str) -> dict:
    """调用阿里云号码认证服务发送验证码。返回 {ok, message}。"""
    if not phone or not phone.strip():
        return {"ok": False, "message": "手机号不能为空"}

    phone = phone.strip()
    now = time.time()
    prev = _sms_code_store.get(phone, {})

    # 检查是否被锁定
    locked_until = prev.get("locked_until")
    if locked_until and now < locked_until:
        wait = int(locked_until - now)
        return {"ok": False, "message": f"验证失败次数过多,请{wait // 60}分钟后再试"}

    # 检查发送间隔
    if prev.get("sent_at") and now - prev["sent_at"] < SMS_SEND_INTERVAL:
        wait = int(SMS_SEND_INTERVAL - (now - prev["sent_at"]))
        return {"ok": False, "message": f"请{wait}秒后再试"}

    # 生成6位随机验证码并存储
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    _sms_code_store[phone] = {
        "code": code,
        "expires_at": now + SMS_CODE_TTL,
        "sent_at": now,
        "attempts": 0,
        "locked_until": None,
    }

    # DEV_MODE 优先：直接回显验证码,不调用真实短信
    if DEV_MODE:
        logger.info("[DEV] SMS code for %s: %s", phone, code)
        return {"ok": True, "message": "开发模式：验证码已生成", "dev_code": code}

    # 生产模式：必须配置齐全
    if not (SMS_ACCESS_KEY_ID and SMS_ACCESS_KEY_SECRET and SMS_SIGN_NAME and SMS_TEMPLATE_CODE):
        logger.error("短信服务未配置且 DEV_MODE 未开启，无法发送验证码")
        _sms_code_store.pop(phone, None)
        return {"ok": False, "message": "短信服务未配置"}

    try:
        _send_via_aliyun(phone, code)
        return {"ok": True, "message": "验证码已发送"}
    except Exception as e:
        logger.warning("阿里云短信发送失败: %s", e)
        _sms_code_store.pop(phone, None)
        return {"ok": False, "message": f"短信发送失败: {e}"}


def _send_via_aliyun(phone: str, code: str) -> None:
    """通过阿里云号码认证服务SDK发送验证码。"""
    from alibabacloud_dypnsapi20170525 import models as dypnsapi_models
    from alibabacloud_tea_util import models as util_models
    import json

    client = _get_dypnsapi_client()
    request = dypnsapi_models.SendSmsVerifyCodeRequest(
        phone_number=phone,
        sign_name=SMS_SIGN_NAME,
        template_code=SMS_TEMPLATE_CODE,
        template_param=json.dumps({"min": "5", "code": code}),
    )
    runtime = util_models.RuntimeOptions()
    resp = client.send_sms_verify_code_with_options(request, runtime)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    if resp.body.code != "OK":
        raise RuntimeError(resp.body.message)


def verify_sms_code(phone: str, code: str) -> bool:
    """校验验证码。

    安全机制:
    - 验证码 5 分钟后过期
    - 连续 5 次失败锁定 15 分钟
    - 成功后立即清除记录
    """
    if not phone or not code:
        return False

    phone = phone.strip()
    code = code.strip()
    now = time.time()

    record = _sms_code_store.get(phone)
    if not record:
        return False

    # 检查是否被锁定
    locked_until = record.get("locked_until")
    if locked_until and now < locked_until:
        return False

    # 检查验证码是否存在
    stored_code = record.get("code")
    if not stored_code:
        return False

    # 检查过期
    if now > record.get("expires_at", 0):
        _sms_code_store.pop(phone, None)
        logger.info("SMS code expired for %s", phone)
        return False

    # 验证
    if stored_code == code:
        # 成功 — 完全清除记录(一次性消费)
        _sms_code_store.pop(phone, None)
        return True

    # 失败 — 计数并可能锁定
    attempts = record.get("attempts", 0) + 1
    if attempts >= SMS_MAX_ATTEMPTS:
        _sms_code_store.update(phone, attempts=attempts, locked_until=now + SMS_LOCKOUT_DURATION, code=None)
        logger.warning("Phone %s locked after %d failed attempts", phone, attempts)
    else:
        _sms_code_store.update(phone, attempts=attempts)
    return False


def _clear_sms_store() -> None:
    """测试辅助：清空验证码记录。"""
    _sms_code_store.clear()


# ---------- 密码 ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_password_login_lock(phone: str) -> int:
    """Return remaining lockout seconds for password login, or 0 when not locked."""
    key = (phone or "").strip()
    record = _password_failure_store.get(key)
    if not record:
        return 0
    locked_until = record.get("locked_until")
    if not locked_until:
        return 0
    remaining = int(locked_until - time.time())
    if remaining <= 0:
        _password_failure_store.delete(key)
        return 0
    return remaining


def record_password_login_failure(phone: str) -> None:
    key = (phone or "").strip()
    record = _password_failure_store.record_failure(
        key,
        max_attempts=SMS_MAX_ATTEMPTS,
        lockout_duration=SMS_LOCKOUT_DURATION,
    )
    if record["locked_until"]:
        logger.warning(
            "Phone %s password login locked after %d failed attempts",
            key,
            record["attempts"],
        )


def clear_password_login_failures(phone: str) -> None:
    _password_failure_store.delete((phone or "").strip())


def _clear_password_login_failures() -> None:
    _password_failure_store.clear()

# ---------- 用户操作 ----------

def create_user(
    db: Session,
    phone: Optional[str] = None,
    name: str = "",
    school: str = "",
    invite_code: str = "",
    wechat_openid: Optional[str] = None,
    role: str = "teacher",
) -> User:
    user = User(
        phone=phone or None,
        name=name or "未命名用户",
        school=school or "",
        invite_code=invite_code or "",
        wechat_openid=wechat_openid or None,
        role=role,
        status="active",
        last_login=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_phone(db: Session, phone: str) -> Optional[User]:
    return db.query(User).filter(User.phone == phone).first()


def get_user_by_openid(db: Session, openid: str) -> Optional[User]:
    return db.query(User).filter(User.wechat_openid == openid).first()


def get_or_create_wechat_user(db: Session, openid: str, nickname: str = "") -> User:
    user = get_user_by_openid(db, openid)
    if user:
        user.last_login = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        return user
    return create_user(
        db,
        wechat_openid=openid,
        name=nickname or "微信用户",
    )


# ---------- JWT ----------

def create_token(user_id: int, expires_hours: Optional[int] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        hours=expires_hours if expires_hours is not None else JWT_EXPIRE_HOURS
    )
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> Optional[int]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            return None
        return int(sub)
    except (jwt.PyJWTError, ValueError):
        return None


def get_current_user(db: Session, token: Optional[str]) -> Optional[User]:
    user_id = verify_token(token) if token else None
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    if user and user.status != "active":
        return None
    return user


# ---------- 微信 OAuth ----------


# ---------- 图形验证码一次性票据 ----------
#
# 为什么需要：/auth/captcha/verify 与 /auth/sms/send 原本是两个互不相干的接口，
# 攻击者可以直接跳过图形验证调用发送接口。这里让验证通过后签发一次性票据，
# 发送短信时必须消耗票据——同时把票据和手机号绑定，避免一张票复用到别的号。

_CAPTCHA_TICKET_TTL = 180  # 秒


# 票据落库（Redis 优先、SQLite 兜底），不再用进程内字典：gunicorn 起 2 个
# worker 时，A 进程签发的票在 B 进程校验必然失败。实现见 services/auth_tickets.py。
def _issue_captcha_ticket(phone: str = "") -> str:
    from services.auth_tickets import KIND_CAPTCHA, issue

    return issue(KIND_CAPTCHA, phone=phone, ttl=_CAPTCHA_TICKET_TTL)


def consume_captcha_ticket(ticket: str, phone: str = "") -> bool:
    """核销票据：一次性、限时、可绑定手机号。"""
    from services.auth_tickets import KIND_CAPTCHA, consume

    return consume(KIND_CAPTCHA, ticket, phone=phone)


# ---------- 内置图形验证（两位加减算术题）----------
# 没有阿里云验证码账号也能挡脚本：服务端出题，token 里带答案摘要、过期时间和
# 来源 IP 的 HMAC 签名；核销走 auth_tickets，保证一次性且跨 worker 可用。

_BUILTIN_CAPTCHA_TTL = _CAPTCHA_TICKET_TTL


def _captcha_signature(nonce: str, exp: str, answer_digest: str, client_ip: str) -> str:
    import hashlib
    import hmac as hmac_mod

    message = f"{nonce}.{exp}.{answer_digest}.{client_ip}".encode()
    return hmac_mod.new(JWT_SECRET.encode(), message, hashlib.sha256).hexdigest()


def issue_builtin_captcha(client_ip: str = "") -> dict:
    """出一道两位数加减题。返回题目文本与一次性签名 token。"""
    import hashlib

    a = 20 + secrets.randbelow(60)
    b = 10 + secrets.randbelow(40)
    if secrets.randbelow(2):
        text, answer = f"{a} + {b} = ?", a + b
    else:
        if a < b:
            a, b = b, a
        text, answer = f"{a} - {b} = ?", a - b
    nonce = secrets.token_urlsafe(12)
    exp = str(int(time.time()) + _BUILTIN_CAPTCHA_TTL)
    answer_digest = hashlib.sha256(str(answer).encode()).hexdigest()[:32]
    signature = _captcha_signature(nonce, exp, answer_digest, client_ip)
    token = f"{nonce}.{exp}.{answer_digest}.{signature}"
    from services.auth_tickets import KIND_CAPTCHA_CHALLENGE, issue

    issue(KIND_CAPTCHA_CHALLENGE, phone=client_ip, ttl=_BUILTIN_CAPTCHA_TTL, token=token)
    return {"ok": True, "problem": text, "token": token, "provider": "builtin"}


def _verify_builtin_captcha(captcha_verify_param: str, phone: str, client_ip: str) -> dict:
    """验签 → 一次性核销 → 比对答案。任何一步失败都必须重新取题。"""
    import hashlib
    import hmac as hmac_mod
    import json

    try:
        payload = json.loads(captcha_verify_param)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "message": "验证码参数格式错误"}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "验证码参数格式错误"}

    token = str(payload.get("token") or "")
    answer = str(payload.get("answer") or "").strip()
    parts = token.split(".")
    if len(parts) != 4 or not answer:
        return {"ok": False, "message": "请先完成算术验证"}
    nonce, exp, answer_digest, signature = parts

    expected = _captcha_signature(nonce, exp, answer_digest, client_ip)
    if not hmac_mod.compare_digest(expected, signature):
        return {"ok": False, "message": "验证信息无效，请刷新题目"}
    try:
        if int(exp) < int(time.time()):
            return {"ok": False, "message": "验证已过期，请刷新题目"}
    except ValueError:
        return {"ok": False, "message": "验证信息无效，请刷新题目"}

    from services.auth_tickets import KIND_CAPTCHA_CHALLENGE, consume

    # 一次性：先核销再比对答案，避免同一道题被反复试错。
    if not consume(KIND_CAPTCHA_CHALLENGE, token, phone=client_ip):
        return {"ok": False, "message": "验证已使用或已过期，请刷新题目"}

    import re

    if not re.fullmatch(r"-?\d{1,4}", answer):
        return {"ok": False, "message": "答案不正确"}
    if hashlib.sha256(answer.encode()).hexdigest()[:32] != answer_digest:
        return {"ok": False, "message": "答案不正确"}
    return {"ok": True, "ticket": _issue_captcha_ticket(phone)}


async def verify_captcha(
    captcha_verify_param: str, phone: str = "", client_ip: str = ""
) -> dict:
    """按 CAPTCHA_PROVIDER 校验图形验证码，成功后签发一次性短信票据。"""
    import json

    from config import CAPTCHA_APP_ID, CAPTCHA_APP_KEY, CAPTCHA_PROVIDER

    provider = (CAPTCHA_PROVIDER or "builtin").strip().lower()
    if provider == "off":
        # 关闭图形验证时直接给票，短信发送端仍受 IP/全局配额约束。
        return {"ok": True, "ticket": _issue_captcha_ticket(phone), "skipped": True}
    if provider == "builtin":
        return _verify_builtin_captcha(captcha_verify_param, phone, client_ip)

    import httpx
    import hmac as hmac_mod
    import hashlib

    if not CAPTCHA_APP_ID or not CAPTCHA_APP_KEY:
        return {"ok": False, "message": "图形验证码未配置"}

    try:
        params = json.loads(captcha_verify_param)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "message": "验证码参数格式错误"}

    lot_number = params.get("lot_number", "")
    if not lot_number:
        return {"ok": False, "message": "缺少lot_number"}

    # 用 HMAC-SHA256(appKey, lot_number) 生成签名
    sign_token = hmac_mod.new(
        CAPTCHA_APP_KEY.encode(),
        lot_number.encode(),
        hashlib.sha256,
    ).hexdigest()

    # 构建验证请求 - 阿里云验证码 Alicom4 HTTP 接口（官方 demo 方式）
    # captcha_id 放 URL 参数，其余放 form data
    url = f"https://captcha.alicaptcha.com/validate?captcha_id={CAPTCHA_APP_ID}"
    form_data = {
        "lot_number": lot_number,
        "captcha_output": params.get("captcha_output", ""),
        "pass_token": params.get("pass_token", ""),
        "gen_time": params.get("gen_time", ""),
        "sign_token": sign_token,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, data=form_data)
        data = resp.json()

    if data.get("result") == "success":
        return {"ok": True, "ticket": _issue_captcha_ticket(phone)}
    return {"ok": False, "message": data.get("reason", "图形验证失败")}

def build_wechat_authorize_url(state: str = "", scope: str = "snsapi_login") -> str:
    if not WECHAT_APP_ID or not WECHAT_REDIRECT_URI:
        return ""
    from urllib.parse import urlencode
    qs = urlencode({
        "appid": WECHAT_APP_ID,
        "redirect_uri": WECHAT_REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "state": state or "",
    })
    return f"https://open.weixin.qq.com/connect/qrconnect?{qs}#wechat_redirect"


async def exchange_wechat_code(code: str) -> dict:
    """用 code 换取 openid + access_token。返回 {openid, access_token, ...} 或抛错。"""
    if not WECHAT_APP_ID or not WECHAT_APP_SECRET:
        raise RuntimeError("微信开放平台未配置")
    url = "https://api.weixin.qq.com/sns/oauth2/access_token"
    params = {
        "appid": WECHAT_APP_ID,
        "secret": WECHAT_APP_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
        data = resp.json()
    if "errcode" in data and data["errcode"] != 0:
        raise RuntimeError(f"微信授权失败: {data.get('errmsg')}")
    return data
