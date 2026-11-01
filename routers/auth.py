import logging
import os
import re
import secrets
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from services.ip_rate_limit import get_ip_lock, record_ip_failure, clear_ip_failures
from services.auth_service import (
    send_sms_code,
    verify_sms_code,
    create_token,
    get_current_user,
    build_wechat_authorize_url,
    hash_password,
    verify_password,
    clear_password_login_failures,
    get_password_login_lock,
    record_password_login_failure,
)

router = APIRouter()
security = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


# ---------- Token 吊销（登出）----------
# JWT 是无状态的，签发后在 exp 前一直有效。这里维护一个黑名单，
# 让「登出」能立即失效 token（缓解 token 被盗/换设备无法踢下线的问题）。
# 用 SQLite 落库（非进程内存），多 worker 部署下所有 worker 共享同一黑名单——
# 否则登出只在处理该请求的 worker 生效，其余 worker 仍认旧 token（gunicorn 多 worker）。
# 存 token 的 sha256 而非明文；按 exp 惰性清理，避免无限增长。
import hashlib
import sqlite3
import threading
import time as _time

_revoked_local = threading.local()
_revoked_initialized = False


def _revoked_db_path() -> str:
    """与应用主库同一 SQLite 文件（复用 auth_service 的解析口径）。"""
    from services.auth_service import _SmsCodeStore
    return _SmsCodeStore._default_db_path()


def _revoked_conn() -> sqlite3.Connection:
    global _revoked_initialized
    conn = getattr(_revoked_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_revoked_db_path(), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        _revoked_local.conn = conn
    if not _revoked_initialized:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS revoked_tokens ("
            "token_sha256 TEXT PRIMARY KEY, exp_epoch REAL)"
        )
        conn.commit()
        _revoked_initialized = True
    return conn


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _prune_revoked(now: float | None = None) -> None:
    now = now or _time.time()
    conn = _revoked_conn()
    conn.execute("DELETE FROM revoked_tokens WHERE exp_epoch <= ?", (now,))
    conn.commit()


def revoke_token(token: str, exp_epoch: float) -> None:
    _prune_revoked()
    conn = _revoked_conn()
    conn.execute(
        "INSERT OR REPLACE INTO revoked_tokens (token_sha256, exp_epoch) VALUES (?, ?)",
        (_token_digest(token), exp_epoch),
    )
    conn.commit()


def is_token_revoked(token: str) -> bool:
    conn = _revoked_conn()
    row = conn.execute(
        "SELECT exp_epoch FROM revoked_tokens WHERE token_sha256 = ?",
        (_token_digest(token),),
    ).fetchone()
    if row is None:
        return False
    if row[0] <= _time.time():
        conn.execute("DELETE FROM revoked_tokens WHERE token_sha256 = ?", (_token_digest(token),))
        conn.commit()
        return False
    return True


def _clear_revoked_tokens() -> None:
    """测试辅助：清空黑名单。"""
    conn = _revoked_conn()
    conn.execute("DELETE FROM revoked_tokens")
    conn.commit()


class SmsSendRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    # 图形验证通过后拿到的票据；图形验证码已配置时必须提供
    captcha_ticket: str | None = Field(default=None, max_length=128)


class CaptchaVerifyRequest(BaseModel):
    captcha_verify_param: str = Field(..., min_length=1, max_length=2000)
    # 可选：把票据绑定到手机号，避免一张票被拿去给任意号码发短信
    phone: str | None = Field(default=None, pattern=r"^1[3-9]\d{9}$")


class SmsLoginRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    code: str = Field(..., pattern=r"^\d{6}$")
    name: str | None = Field(None, min_length=1, max_length=50)
    school: str | None = Field(None, max_length=100)
    invite_code: str | None = Field(None, max_length=32)


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=50)
    school: str | None = Field(None, max_length=100)
    education_stage: Literal["primary", "junior", "senior"] | None = None

    @field_validator("education_stage", mode="before")
    @classmethod
    def require_valid_stage(cls, value):
        if value not in ("primary", "junior", "senior"):
            raise ValueError("请选择小学、初中或高中学段")
        return value


def _user_profile_payload(user: User) -> dict:
    payload = user.to_dict()
    stage = getattr(user, "education_stage", None)
    payload["education_stage"] = stage if stage in ("primary", "junior", "senior") else None
    payload["profile_complete"] = payload["education_stage"] is not None
    return payload

class SetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=6, max_length=20)
    old_password: str | None = Field(None, max_length=20)


class PasswordLoginRequest(BaseModel):
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    password: str = Field(..., min_length=1, max_length=20)


def _validate_password_complexity(password: str) -> bool:
    return bool(
        password
        and 6 <= len(password) <= 20
        and re.search(r"[A-Za-z]", password)
        and re.search(r"\d", password)
    )


# ---------- 依赖注入 ----------

def _get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User | None:
    """从 Authorization: Bearer *** 中提取用户，无token返回None。"""
    token = credentials.credentials if credentials else None
    if token and is_token_revoked(token):
        return None  # 已登出的 token 立即失效
    return get_current_user(db, token)


def _require_user(
    user: User | None = Depends(_get_current_user),
) -> User:
    if user is None:
        raise HTTPException(401, "未登录或token已过期")
    return user


def _require_admin(user: User = Depends(_require_user)) -> User:
    """要求当前用户为管理员（role == 'admin'）。"""
    if (user.role or "").lower() != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def ensure_current_class(db, user: User) -> None:
    """保证"当前选中班级"不变空——产品约束：一切操作都在某个已选班级下进行。

    若 current_class_id 为空但老师有活跃班级，则默认选中第一个（按 id）。
    调用方：登录、/auth/me、/auth/classes 等入口，让不变式在服务端稳定成立，
    不依赖前端。若老师尚无任何班级，保持空（引导去建班），批改接口会拦截。
    """
    if user.current_class_id is not None:
        return
    from models_rate_limit import TeacherClass
    first = (
        db.query(TeacherClass)
        .filter(TeacherClass.teacher_id == user.id, TeacherClass.deleted_at.is_(None))
        .order_by(TeacherClass.id)
        .first()
    )
    if first is not None:
        user.current_class_id = first.id
        db.commit()
        db.refresh(user)


# ---------- 图形验证码 ----------

def _captcha_provider() -> str:
    from config import CAPTCHA_PROVIDER

    return (CAPTCHA_PROVIDER or "builtin").strip().lower()


@router.get("/auth/features")
def api_auth_features():
    """公开能力探测：登录页据此隐藏短信区块、选择图形验证方式。

    不含任何密钥或内部地址，只是布尔能力位。
    """
    from config import SMS_ENABLED, WECHAT_APP_ID

    provider = _captcha_provider()
    require = os.environ.get("SMS_REQUIRE_CAPTCHA", "1") != "0"
    if provider == "aliyun":
        from config import CAPTCHA_APP_ID, CAPTCHA_APP_KEY

        require = require and bool(CAPTCHA_APP_ID and CAPTCHA_APP_KEY)
    elif provider == "off":
        require = False
    return {
        "sms_enabled": bool(SMS_ENABLED),
        "wechat_enabled": bool(WECHAT_APP_ID),
        "captcha_provider": provider,
        "captcha_required": bool(require),
        "sms_hint": (
            ""
            if SMS_ENABLED
            else "本实例未开启短信登录，请联系管理员创建账号"
        ),
    }


@router.get("/auth/captcha/challenge")
def api_captcha_challenge(request: Request):
    """内置算术验证码取题。aliyun/off 模式返回空题面，前端走各自分支。"""
    provider = _captcha_provider()
    if provider != "builtin":
        return {"ok": True, "provider": provider, "problem": "", "token": ""}
    from services.auth_service import issue_builtin_captcha

    return issue_builtin_captcha(_client_ip(request))


@router.post("/auth/captcha/verify")
async def api_verify_captcha(req: CaptchaVerifyRequest, request: Request):
    """验证图形验证码。前端完成图形验证码后调用，成功后返回一次性票据。"""
    from services.auth_service import verify_captcha

    result = await verify_captcha(
        req.captcha_verify_param, phone=req.phone or "", client_ip=_client_ip(request)
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "图形验证失败"))
    return {"ok": True, "ticket": result.get("ticket", "")}


# ---------- 短信验证码 ----------

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 应用只监听 127.0.0.1，生产流量全部经 nginx 反代；此时 request.client.host 恒为回环地址。
# nginx 会用 $remote_addr 覆盖 X-Real-IP，因此只有「来源是回环」时才信任该头，
# 避免直连部署时被伪造头绕过按 IP 限流。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip and peer in _LOOPBACK_HOSTS:
        return real_ip
    return peer or "unknown"


def _require_sms_enabled() -> None:
    """SMS_ENABLED=0 的实例（Docker 默认）不提供短信登录。"""
    from config import SMS_ENABLED

    if not SMS_ENABLED:
        raise HTTPException(400, "本实例未开启短信登录，请联系管理员创建账号")


@router.post("/auth/sms/send")
def api_send_sms(req: SmsSendRequest, request: Request):
    _require_sms_enabled()
    phone = req.phone.strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "手机号格式错误")

    # 1) 图形验证票据：**必须排在配额之前**。票据是一次性的、且与手机号绑定，
    #    放在前面才能挡住「不带票据的请求把全局日额度刷光、让所有人发不出短信」。
    from services.auth_service import consume_captcha_ticket
    provider = _captcha_provider()
    require_env = os.environ.get("SMS_REQUIRE_CAPTCHA", "1") != "0"
    if provider == "aliyun":
        from config import CAPTCHA_APP_ID, CAPTCHA_APP_KEY

        captcha_required = bool(CAPTCHA_APP_ID and CAPTCHA_APP_KEY) and require_env
    else:
        # builtin 永远可用（不依赖外部账号）；off 明确关闭。
        captcha_required = provider == "builtin" and require_env
    if captcha_required and not consume_captcha_ticket(req.captcha_ticket or "", phone):
        raise HTTPException(400, "请先完成图形验证")

    # 2) 按来源 IP / 全局日总量封顶：过了票据才记账，配额只统计真实要发出去的短信。
    from services.sms_throttle import check_and_record
    client_ip = _client_ip(request)
    guard = check_and_record(client_ip)
    if not guard.get("ok"):
        raise HTTPException(429, guard.get("message", "发送过于频繁，请稍后再试"))

    result = send_sms_code(phone)
    if not result.get("ok"):
        raise HTTPException(400, result.get("message", "发送失败"))
    return {"ok": True, "msg": result.get("message", "验证码已发送"), "dev_code": result.get("dev_code")}


@router.post("/auth/sms/login")
def api_sms_login(req: SmsLoginRequest, db: Session = Depends(get_db)):
    _require_sms_enabled()
    phone = req.phone.strip()

    from services.auth_service import get_user_by_phone, create_user
    user = get_user_by_phone(db, phone)

    # 关键：先确定本次能否完成，再校验/消费验证码。
    # verify_sms_code 成功即一次性核销验证码。若先核销、后因「缺姓名」报错，
    # 用户补填姓名再次提交时验证码已失效，必然得到「验证码错误或已过期」——
    # 这正是新用户无法登录的根因。故把注册场景的姓名校验提到消费之前。
    if not user and (not req.name or not req.name.strip()):
        raise HTTPException(400, "首次登录需要填写姓名")

    if not verify_sms_code(phone, req.code):
        raise HTTPException(400, "验证码错误或已过期")

    if user:
        if user.status == "disabled":
            raise HTTPException(403, "账号已被禁用")
        from datetime import datetime, timezone
        user.last_login = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
    else:
        user = create_user(
            db,
            phone=phone,
            name=req.name.strip(),
            school=(req.school or "").strip(),
            invite_code=(req.invite_code or "").strip(),
        )

    token = create_token(user.id)
    ensure_current_class(db, user)
    return {"ok": True, "token": token, "user": _user_profile_payload(user)}


# ---------- 管理员建号 ----------

class AdminCreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=50)
    # 手机号可空：没有短信账号的部署里，老师不需要手机号也能拿密码登录。
    phone: str | None = Field(None, max_length=20)
    school: str | None = Field(None, max_length=100)
    password: str = Field(..., min_length=6, max_length=20)


def _generate_login_account(db: Session) -> str:
    """手机号留空时生成一个 199 开头的唯一登录账号（可当作工号使用）。"""
    for _ in range(20):
        candidate = "199" + "".join(secrets.choice("0123456789") for _ in range(8))
        if not db.query(User).filter(User.phone == candidate).first():
            return candidate
    raise HTTPException(500, "生成登录账号失败，请填写手机号后重试")


@router.post("/auth/admin/user")
def api_admin_create_user(
    req: AdminCreateUserRequest,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    """管理员直接创建教师账号。

    适用场景：实例没有开通阿里云短信/微信登录，老师无法自助注册。管理员建号后
    把「登录账号 + 初始密码」交给老师，老师用密码登录并可自行改密。
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "姓名不能为空")
    if not _validate_password_complexity(req.password):
        raise HTTPException(400, "密码需6-20位，且至少包含字母和数字")

    phone = (req.phone or "").strip()
    generated = False
    if phone:
        if not _PHONE_RE.match(phone):
            raise HTTPException(400, "手机号格式错误")
        if db.query(User).filter(User.phone == phone).first():
            raise HTTPException(400, "该手机号已存在")
    else:
        phone = _generate_login_account(db)
        generated = True

    from services.auth_service import create_user, hash_password

    user = create_user(
        db,
        phone=phone,
        name=name,
        school=(req.school or "").strip(),
        role="teacher",
    )
    user.password_hash = hash_password(req.password)
    # 管理员设的是初始密码，老师首次登录后应当自己改一次。
    user.must_change_password = True
    db.commit()
    db.refresh(user)
    logger.info("admin=%s created teacher=%s", admin.id, user.id)
    return {
        "ok": True,
        "user": _user_profile_payload(user),
        "login_account": phone,
        "generated_account": generated,
    }


@router.get("/auth/admin/users")
def api_admin_list_users(
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    """管理员查看账号列表（仅用于建号后的交接与核对）。"""
    users = db.query(User).order_by(User.id).limit(500).all()
    return {"users": [_user_profile_payload(u) for u in users]}


# ---------- 微信登录 ----------

# 微信登录 CSRF 防护：发起时生成一次性 state，回调必须带回并核销。
# 没有 state 时，攻击者可以把自己的授权 code 诱使受害者访问回调地址，
# 从而把受害者登录进攻击者的账号。
_WECHAT_STATE_TTL = 600


def _issue_wechat_state() -> str:
    # 落库（Redis 优先、SQLite 兜底）：多 worker 下必须能被任意进程核销，
    # 进程内字典会让「发起登录的 worker ≠ 处理回调的 worker」随机失败。
    from services.auth_tickets import KIND_WECHAT_STATE, issue

    return issue(KIND_WECHAT_STATE, ttl=_WECHAT_STATE_TTL)


def _consume_wechat_state(state: str) -> bool:
    if not state:
        return False
    from services.auth_tickets import KIND_WECHAT_STATE, consume

    return consume(KIND_WECHAT_STATE, state)


@router.get("/auth/wechat/url")
def api_wechat_url(response: Response):
    state = _issue_wechat_state()
    url = build_wechat_authorize_url(state=state)
    if not url:
        raise HTTPException(501, "微信登录尚未配置")
    # state 必须同时落到浏览器 Cookie：只校验「库里存在这个 state」挡不住登录 CSRF
    # ——攻击者可以先拿一个合法 state，再把自己的授权 code 诱使受害者访问回调，
    # 受害者就被登进了攻击者的账号。Cookie 由发起端写入，攻击者跨站带不过来。
    from config import FORCE_HTTPS

    response.set_cookie(
        "wechat_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=bool(FORCE_HTTPS),
        path="/api/auth/wechat",
    )
    return {"url": url, "state": state}


@router.get("/auth/wechat/callback")
async def api_wechat_callback(
    code: str = "",
    state: str = "",
    request: Request = None,
    db: Session = Depends(get_db),
):
    from services.auth_service import exchange_wechat_code, get_or_create_wechat_user

    # 回调必须带回发起时写入浏览器的 Cookie，二者一致才继续；否则只带 state 参数
    # 也能通过（攻击者拿自己的 state 就能把受害者登进攻击者账号）。
    cookie_state = (request.cookies.get("wechat_oauth_state", "") if request else "").strip()
    if not cookie_state or not state or cookie_state != state:
        raise HTTPException(400, detail="微信授权状态已失效，请重新发起登录")
    if not _consume_wechat_state(state):
        raise HTTPException(400, detail="微信授权状态已失效，请重新发起登录")
    try:
        wx_data = await exchange_wechat_code(code)
    except Exception as e:
        logger.warning("Wechat OAuth callback failed: %s", e)
        raise HTTPException(400, "微信授权失败，请稍后重试")
    openid = wx_data.get("openid", "")
    nickname = wx_data.get("nickname", "")
    user = get_or_create_wechat_user(db, openid, nickname)
    token = create_token(user.id)
    ensure_current_class(db, user)
    return {"ok": True, "token": token, "user": _user_profile_payload(user)}


# ---------- 用户信息 ----------

@router.get("/auth/me")
def api_get_me(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    ensure_current_class(db, user)
    return {"user": _user_profile_payload(user)}


@router.put("/auth/me")
def api_update_me(
    req: UserUpdateRequest,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    if req.education_stage is not None and not hasattr(User, "education_stage"):
        # Never report success for a transient Python attribute before the additive migration lands.
        raise HTTPException(503, "教学资料功能尚未完成更新，请稍后再试")
    if req.name is not None:
        name = req.name.strip()
        if not name or len(name) > 50:
            raise HTTPException(400, "姓名长度需1-50字")
        user.name = name
    if req.school is not None:
        school = req.school.strip()
        if len(school) > 100:
            raise HTTPException(400, "学校名称不超过100字")
        user.school = school
    if req.education_stage is not None:
        user.education_stage = req.education_stage
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_profile_payload(user)}


# ---------- 密码登录 ----------

@router.post("/auth/password/set")
def api_set_password(
    req: SetPasswordRequest,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    password = req.password
    if not _validate_password_complexity(password):
        raise HTTPException(400, "密码需6-20位，且至少包含字母和数字")
    # 已设过密码 → 修改密码必须校验旧密码（FE-C1：防被盗 token 直接改密）
    if user.password_hash:
        if not req.old_password or not verify_password(req.old_password, user.password_hash):
            raise HTTPException(400, "当前密码错误")
    user.password_hash = hash_password(password)
    # 首启/管理员建号的初始密码标记：改密成功后清零。
    user.must_change_password = False
    db.commit()
    return {"ok": True, "msg": "密码设置成功"}


@router.post("/auth/password/login")
def api_password_login(req: PasswordLoginRequest, request: Request, db: Session = Depends(get_db)):
    phone = req.phone.strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "手机号格式错误")
    if not req.password:
        raise HTTPException(400, "请输入密码")

    # IP级别限流
    client_ip = request.client.host if request.client else "unknown"
    ip_lock = get_ip_lock(client_ip)
    if ip_lock == -1:
        raise HTTPException(403, "该IP已被封禁")
    if ip_lock > 0:
        raise HTTPException(429, f"登录失败次数过多，请{ip_lock // 60 + 1}分钟后再试")

    locked_seconds = get_password_login_lock(phone)
    if locked_seconds:
        raise HTTPException(429, f"密码错误次数过多，请{locked_seconds // 60 + 1}分钟后再试")

    from services.auth_service import get_user_by_phone
    user = get_user_by_phone(db, phone)
    if not user:
        record_password_login_failure(phone)
        record_ip_failure(client_ip)
        raise HTTPException(400, "手机号或密码错误")
    if not user.password_hash:
        raise HTTPException(400, "该账号未设置密码，请使用短信验证码登录")
    if not verify_password(req.password, user.password_hash):
        record_password_login_failure(phone)
        record_ip_failure(client_ip)
        raise HTTPException(400, "手机号或密码错误")
    if user.status == "disabled":
        raise HTTPException(403, "账号已被禁用")
    clear_password_login_failures(phone)
    clear_ip_failures(client_ip)

    from datetime import datetime, timezone
    user.last_login = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    token = create_token(user.id)
    ensure_current_class(db, user)
    return {"ok": True, "token": token, "user": _user_profile_payload(user)}


@router.post("/auth/logout")
def api_logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    user: User = Depends(_require_user),
):
    """登出 — 将当前 token 加入黑名单，立即失效。

    依赖 _require_user 确保只能登出有效 token；幂等（重复登出无副作用）。
    """
    token = credentials.credentials if credentials else None
    if not token:
        return {"ok": True}
    # 取 token 的 exp 作为黑名单条目过期时间，到期自动清理
    from config import JWT_SECRET, JWT_ALGORITHM
    import jwt as _jwt
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = float(payload.get("exp", 0))
    except Exception:
        exp = _time.time() + 60  # 解码失败也兜底拉黑一分钟
    revoke_token(token, exp)
    logger.info("User %s logged out (token revoked)", user.id)
    return {"ok": True}


# ---------- 班级管理 ----------

class SwitchClassRequest(BaseModel):
    class_id: int | None = Field(None, ge=1)  # None表示切换到"全部班级"


class CreateClassRequest(BaseModel):
    class_name: str = Field(..., min_length=1, max_length=50)


class UpdateClassRequest(BaseModel):
    class_name: str = Field(..., min_length=1, max_length=50)


@router.get("/auth/classes")
def api_get_classes(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    """获取当前老师的班级列表（不含已删除）"""
    from models_rate_limit import TeacherClass
    classes = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).order_by(TeacherClass.id).all()
    # 有班级但未选中时默认选第一个，保证"总有选中班级"的不变式在服务端成立
    ensure_current_class(db, user)
    return {
        "classes": [c.to_dict(db) for c in classes],
        "current_class_id": user.current_class_id,
        "max_classes": TeacherClass.MAX_CLASSES_PER_TEACHER,
    }


@router.post("/auth/class/create")
def api_create_class(
    req: CreateClassRequest,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """创建班级（每教师最多4个）"""
    from datetime import datetime, timedelta, timezone
    from models_rate_limit import TeacherClass

    class_name = req.class_name.strip()
    if not class_name or len(class_name) > 50:
        raise HTTPException(400, "班级名称需1-50字")

    # 检查活跃班级数量
    active_count = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).count()
    if active_count >= TeacherClass.MAX_CLASSES_PER_TEACHER:
        raise HTTPException(400, f"最多创建{TeacherClass.MAX_CLASSES_PER_TEACHER}个班级")

    # 检查是否有同名的已删除班级在冷却期内
    now = datetime.now(timezone.utc)
    cooldown_date = now - timedelta(days=TeacherClass.DELETE_COOLDOWN_DAYS)
    deleted_class = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.class_name == class_name,
        TeacherClass.deleted_at.isnot(None),
        TeacherClass.deleted_at > cooldown_date,
    ).first()
    if deleted_class:
        deleted_at = deleted_class.deleted_at
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        days_left = TeacherClass.DELETE_COOLDOWN_DAYS - (now - deleted_at).days
        raise HTTPException(400, f"同名班级在冷却期内，还需等待{days_left}天")

    # 检查同名活跃班级
    existing = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.class_name == class_name,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if existing:
        raise HTTPException(400, "班级名称已存在")

    # 恢复已过冷却期的同名班级，或创建新班级
    old_class = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.class_name == class_name,
        TeacherClass.deleted_at.isnot(None),
        TeacherClass.deleted_at <= cooldown_date,
    ).first()
    if old_class:
        old_class.deleted_at = None
        old_class.student_count = 0
        db.commit()
        db.refresh(old_class)
        return {"ok": True, "class": old_class.to_dict()}

    tc = TeacherClass(teacher_id=user.id, class_name=class_name)
    db.add(tc)
    db.commit()
    db.refresh(tc)
    return {"ok": True, "class": tc.to_dict()}


@router.put("/auth/class/{class_id}")
def api_update_class(
    class_id: int,
    req: UpdateClassRequest,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """更新班级名称"""
    from models_rate_limit import TeacherClass
    from models import Student

    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not tc:
        raise HTTPException(404, "班级不存在")

    new_name = req.class_name.strip()
    if not new_name or len(new_name) > 50:
        raise HTTPException(400, "班级名称需1-50字")

    # 检查同名
    existing = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.class_name == new_name,
        TeacherClass.deleted_at.is_(None),
        TeacherClass.id != class_id,
    ).first()
    if existing:
        raise HTTPException(400, "班级名称已存在")

    old_name = tc.class_name
    tc.class_name = new_name
    # 同步更新学生的class_name字段（兼容旧数据）
    db.query(Student).filter(
        Student.class_id == class_id,
    ).update({"class_name": new_name})
    db.commit()
    db.refresh(tc)
    return {"ok": True, "class": tc.to_dict()}


@router.delete("/auth/class/{class_id}")
def api_delete_class(
    class_id: int,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """删除班级（软删除，30天冷却期）"""
    from datetime import datetime, timedelta, timezone
    from models import Student
    from models_rate_limit import TeacherClass

    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not tc:
        raise HTTPException(404, "班级不存在")

    # 检查最近是否删除过班级（冷却期限制）
    now = datetime.now(timezone.utc)
    cooldown_date = now - timedelta(days=TeacherClass.DELETE_COOLDOWN_DAYS)
    recent_delete = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.isnot(None),
        TeacherClass.deleted_at > cooldown_date,
    ).first()
    if recent_delete:
        deleted_at = recent_delete.deleted_at
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        days_left = TeacherClass.DELETE_COOLDOWN_DAYS - (now - deleted_at).days
        raise HTTPException(400, f"删除班级功能冷却中，还需等待{days_left}天")

    tc.deleted_at = now
    db.query(Student).filter(
        Student.teacher_id == user.id,
        Student.class_id == class_id,
    ).update({"class_id": None, "class_name": ""})
    # 删的是当前选中班级：切到另一个剩余班级（保持"总有选中班级"不变式）；
    # 若已无其他班级，才置空（无班可选，前端会引导建班，批改接口也会拦截）。
    if user.current_class_id == class_id:
        next_class = (
            db.query(TeacherClass)
            .filter(
                TeacherClass.teacher_id == user.id,
                TeacherClass.deleted_at.is_(None),
                TeacherClass.id != class_id,
            )
            .order_by(TeacherClass.id)
            .first()
        )
        user.current_class_id = next_class.id if next_class else None
    db.commit()
    if TeacherClass.DELETE_COOLDOWN_DAYS > 0:
        return {"ok": True, "message": f"班级已删除，{TeacherClass.DELETE_COOLDOWN_DAYS}天内无法再次删除班级"}
    return {"ok": True, "message": "班级已删除"}


@router.post("/auth/class/switch")
def api_switch_class(
    req: SwitchClassRequest,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """切换当前班级"""
    from models_rate_limit import TeacherClass
    if req.class_id is not None:
        # 验证班级属于当前老师且未删除
        tc = db.query(TeacherClass).filter(
            TeacherClass.id == req.class_id,
            TeacherClass.teacher_id == user.id,
            TeacherClass.deleted_at.is_(None),
        ).first()
        if not tc:
            raise HTTPException(404, "班级不存在")
        user.current_class_id = tc.id
    else:
        # 切换到全部班级
        user.current_class_id = None
    db.commit()
    db.refresh(user)
    return {"ok": True, "current_class_id": user.current_class_id}
