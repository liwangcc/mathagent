"""号池管理器 — 多API Key轮询 + 并发限制 + 429自动切换。

每个 Key 有自己的在飞上限；Key 被限流（429）时冷却并换下一个 Key。
DashScope（OCR/Flash）和教师聊天的 LLM_* 线路各用一个号池。

参考 One-API（优先级+权重）和 LiteLLM（429冷却+Retry-After解析）的调度逻辑。
算法：Smooth Weighted Round Robin（Nginx风格）+ 信号量并发控制。

配置方式（config.py 或 .env）：
LLM_API_KEYS=***    # 单Key兼容
"""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field

from services.retry_backoff import jittered_backoff

logger = logging.getLogger(__name__)


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

# 429冷却时间（秒）
_COOLDOWN_BASE = 30
_COOLDOWN_MAX = 300
# 单Key并发上限（实测5是安全值，保守设4留余量）
_DEFAULT_CONCURRENCY = 4


@dataclass
class _KeySlot:
    """单个API Key的状态。"""
    key: str
    base_url: str = ""          # 该Key的API endpoint（空则用默认）
    max_concurrent: int | None = _DEFAULT_CONCURRENCY
    weight: int = 1
    _in_flight: int = 0              # 当前在飞请求数
    _cooldown_until: float = 0.0     # 冷却到期时间戳
    _consecutive_429: int = 0        # 连续429次数
    _current_weight: int = 0         # SWRR当前权重
    _total_requests: int = 0         # 总请求数（统计用）
    _total_429: int = 0              # 总429数（统计用）
    _disabled: bool = False          # 401/403 后停用，直到进程重载配置
    _invalid_status_code: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def available(self) -> bool:
        """是否可用（未冷却 + 并发未满）。"""
        if self._disabled:
            return False
        now = time.time()
        if now < self._cooldown_until:
            return False
        # 冷却刚到期：重置状态
        if self._cooldown_until > 0:
            self._cooldown_until = 0
            self._consecutive_429 = 0
        return self.max_concurrent is None or self._in_flight < self.max_concurrent

    @property
    def remaining_capacity(self) -> int | None:
        """剩余并发容量。"""
        if self.max_concurrent is None:
            return None
        return max(0, self.max_concurrent - self._in_flight)

    def acquire(self):
        """占用一个并发槽。"""
        self._in_flight += 1
        self._total_requests += 1

    def release(self):
        """释放一个并发槽。"""
        self._in_flight = max(0, self._in_flight - 1)

    def mark_success(self):
        """请求成功。"""
        self._consecutive_429 = 0

    def mark_429(self, retry_after: float | None = None) -> float:
        """收到429，进入冷却。"""
        self._consecutive_429 += 1
        self._total_429 += 1
        if retry_after and retry_after > 0:
            # Retry-After is a lower bound and must never be shortened.
            cooldown = float(retry_after)
        else:
            cooldown = min(_COOLDOWN_BASE * (2 ** (self._consecutive_429 - 1)), _COOLDOWN_MAX)
            cooldown = jittered_backoff(cooldown, ceiling_s=_COOLDOWN_MAX)
        self._cooldown_until = time.time() + cooldown
        logger.warning(
            "KeyPool: credential sha256:%s rate-limited; cooldown=%ds consecutive_429=%d in_flight=%d",
            _key_fingerprint(self.key),
            cooldown,
            self._consecutive_429,
            self._in_flight,
        )
        return cooldown

    def sync_cooldown(self, retry_after: float) -> None:
        """Import a cross-worker cooldown without inflating local 429 stats."""
        cooldown = float(retry_after)
        if not math.isfinite(cooldown) or cooldown <= 0:
            return
        self._cooldown_until = max(
            self._cooldown_until,
            time.time() + cooldown,
        )

    def mark_invalid(self, status_code: int):
        """Permanently remove an invalid credential from this process pool."""
        self._disabled = True
        self._invalid_status_code = int(status_code)
        self._current_weight = 0

    def wait_seconds(self) -> float:
        """需要等待多少秒才能用。"""
        now = time.time()
        if now < self._cooldown_until:
            return self._cooldown_until - now
        return 0.0


class KeyPool:
    """多API Key调度器。

    核心：并发控制（信号量） + Smooth Weighted Round Robin（Nginx风格）。
    每个Key有独立的并发上限（实测5），超出时自动选其他Key。
    所有Key都满时，acquire_key会等待直到有槽释放。

    线程安全：每个KeySlot有独立的_lock，KeyPool有_pool_lock。
    """

    def __init__(self, keys: list[str], max_concurrent_per_key: int | None = _DEFAULT_CONCURRENCY,
                 weights: list[int] | None = None, base_urls: list[str] | None = None):
        if not keys:
            raise ValueError("KeyPool: 至少需要一个API Key")
        normalized_keys = [str(key).strip() for key in keys]
        if any(not key for key in normalized_keys):
            raise ValueError("KeyPool: API Key 不能为空")
        if len(set(normalized_keys)) != len(normalized_keys):
            raise ValueError("KeyPool: API Key 不能重复")
        if (
            max_concurrent_per_key is not None
            and (
                isinstance(max_concurrent_per_key, bool)
                or not isinstance(max_concurrent_per_key, int)
                or max_concurrent_per_key < 1
            )
        ):
            raise ValueError("KeyPool: 每Key并发必须为正整数或None")
        if weights and len(weights) != len(keys):
            raise ValueError("KeyPool: weights长度必须与keys相同")
        if weights and any(
            isinstance(weight, bool)
            or not isinstance(weight, int)
            or weight < 1
            for weight in weights
        ):
            raise ValueError("KeyPool: 权重必须为正整数")
        if base_urls and len(base_urls) != len(keys):
            raise ValueError("KeyPool: base_urls长度必须与keys相同")

        self._slots = []
        for i, key in enumerate(normalized_keys):
            w = weights[i] if weights else 1
            u = base_urls[i] if base_urls else ""
            self._slots.append(_KeySlot(
                key=key,
                base_url=u,
                max_concurrent=max_concurrent_per_key,
                weight=w,
            ))
        self._pool_lock = threading.Lock()
        self._release_event = asyncio.Event()  # 用于通知有槽释放

    def get_key(self) -> tuple[str | None, float, str]:
        """获取一个可用的API Key。

        Returns:
            (api_key, wait_seconds, base_url) — api_key=None表示所有Key都不可用，
            base_url为空字符串表示用默认值。
        """
        with self._pool_lock:
            available = [s for s in self._slots if s.available]

            if available:
                # Smooth Weighted Round Robin
                for s in available:
                    s._current_weight += s.weight
                chosen = max(available, key=lambda s: s._current_weight)
                total = sum(s.weight for s in available)
                chosen._current_weight -= total
                chosen.acquire()
                return chosen.key, 0.0, chosen.base_url

            # 所有Key都不可用
            enabled_slots = [s for s in self._slots if not s._disabled]
            if not enabled_slots:
                return None, 0.0, ""
            cooldown_slots = [
                s for s in enabled_slots if s._cooldown_until > time.time()
            ]
            if cooldown_slots:
                earliest = min(cooldown_slots, key=lambda s: s._cooldown_until)
                return None, max(earliest._cooldown_until - time.time(), 1.0), ""

            return None, 0.1, ""

    def release(self, key: str):
        """释放一个并发槽，通知等待的acquire_key。"""
        with self._pool_lock:
            for s in self._slots:
                if s.key == key:
                    s.release()
                    break
        # set通知，不在这里clear（等待者醒来后由下一轮get_key处理）
        self._release_event.set()

    def mark_success(self, key: str):
        """标记该Key请求成功。"""
        with self._pool_lock:
            for s in self._slots:
                if s.key == key:
                    s.mark_success()
                    break

    def mark_429(self, key: str, retry_after: float | None = None) -> float:
        """标记该Key收到429限流。"""
        with self._pool_lock:
            for s in self._slots:
                if s.key == key:
                    return s.mark_429(retry_after)
        return 0.0

    def sync_cooldown(self, key: str, retry_after: float) -> None:
        """Import a shared cooldown for one credential into this process."""
        with self._pool_lock:
            for slot in self._slots:
                if slot.key == key:
                    slot.sync_cooldown(retry_after)
                    break

    def mark_invalid(self, key: str, status_code: int):
        """Disable a credential rejected with HTTP 401/403."""
        if status_code not in {401, 403}:
            raise ValueError("KeyPool: only HTTP 401/403 can disable a credential")
        with self._pool_lock:
            for s in self._slots:
                if s.key == key:
                    if not s._disabled:
                        s.mark_invalid(status_code)
                        logger.error(
                            "KeyPool: credential sha256:%s disabled after HTTP %d",
                            _key_fingerprint(s.key),
                            status_code,
                        )
        self._release_event.set()

    def has_enabled_keys(self) -> bool:
        """Return whether at least one credential has not been rejected."""
        with self._pool_lock:
            return any(not s._disabled for s in self._slots)

    def enabled_key_count(self) -> int:
        with self._pool_lock:
            return sum(not s._disabled for s in self._slots)

    def healthy_key_count(self) -> int:
        """Count credentials that are neither disabled nor cooling down."""
        now = time.time()
        with self._pool_lock:
            return sum(
                not slot._disabled and slot._cooldown_until <= now
                for slot in self._slots
            )

    def enabled_credentials(self) -> list[tuple[str, str]]:
        """Return enabled credential routes for internal shared-health checks."""
        with self._pool_lock:
            return [
                (slot.key, slot.base_url)
                for slot in self._slots
                if not slot._disabled
            ]

    def readiness(self) -> dict:
        """Return credential health counts without exposing credential values."""
        with self._pool_lock:
            enabled = [s for s in self._slots if not s._disabled]
            immediately_available = sum(s.available for s in enabled)
            return {
                "configured": bool(self._slots),
                "ready": bool(enabled),
                "configured_keys": len(self._slots),
                "enabled_keys": len(enabled),
                "immediately_available_keys": immediately_available,
            }

    def status(self) -> list[dict]:
        """返回所有Key的状态（调试/监控用）。"""
        with self._pool_lock:
            return [
                {
                    "key": "sha256:" + _key_fingerprint(s.key),
                    "max_concurrent": s.max_concurrent,
                    "in_flight": s._in_flight,
                    "remaining": s.remaining_capacity,
                    "weight": s.weight,
                    "cooldown": max(0, round(s._cooldown_until - time.time(), 1)),
                    "available": s.available,
                    "disabled": s._disabled,
                    "invalid_status_code": s._invalid_status_code,
                    "total_requests": s._total_requests,
                    "total_429": s._total_429,
                }
                for s in self._slots
            ]


# ---------- 号池内异步获取（封装等待 + 重试循环） ----------

def enabled_provider_capacity(
    pool: KeyPool | None,
    per_key_capacity: int,
) -> int:
    """Return total provider capacity without changing the per-key limit."""
    if (
        isinstance(per_key_capacity, bool)
        or not isinstance(per_key_capacity, int)
        or per_key_capacity < 1
    ):
        raise ValueError("per_key_capacity must be a positive integer")
    healthy_keys = 1 if pool is None else pool.healthy_key_count()
    if healthy_keys < 1:
        raise RuntimeError("KeyPool: no healthy credentials")
    return healthy_keys * per_key_capacity


async def shared_credential_cooldown_seconds(
    limiter,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
) -> float:
    """Read shared cooldown while remaining compatible with local test doubles."""
    reader = getattr(limiter, "credential_cooldown_seconds", None)
    if not callable(reader):
        return 0.0
    cooldown = float(await reader(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
    ))
    if not math.isfinite(cooldown) or cooldown <= 0:
        return 0.0
    return cooldown


async def mark_shared_credential_cooldown(
    limiter,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    cooldown_seconds: float,
) -> None:
    """Publish a credential cooldown when the limiter supports shared health."""
    cooldown = float(cooldown_seconds)
    if not math.isfinite(cooldown) or cooldown <= 0:
        return
    marker = getattr(limiter, "mark_credential_cooldown", None)
    if not callable(marker):
        return
    await marker(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        cooldown_seconds=cooldown,
    )


async def refresh_shared_credential_health(
    pool: KeyPool | None,
    limiter,
    *,
    provider: str,
    default_base_url: str,
    model: str,
) -> None:
    """Import permanent failures and temporary cooldowns from other workers."""
    if pool is None:
        return
    routes = pool.enabled_credentials()
    invalid, cooldowns = await asyncio.gather(
        asyncio.gather(*(
            limiter.is_credential_invalid(
                provider=provider,
                api_key=api_key,
                base_url=base_url or default_base_url,
                model=model,
            )
            for api_key, base_url in routes
        )),
        asyncio.gather(*(
            shared_credential_cooldown_seconds(
                limiter,
                provider=provider,
                api_key=api_key,
                base_url=base_url or default_base_url,
                model=model,
            )
            for api_key, base_url in routes
        )),
    )
    for (api_key, _base_url), is_invalid, cooldown in zip(
        routes, invalid, cooldowns
    ):
        if is_invalid:
            pool.mark_invalid(api_key, 401)
        elif cooldown > 0:
            pool.sync_cooldown(api_key, cooldown)


async def acquire_key(pool: KeyPool) -> tuple[str, str, str]:
    """异步获取可用Key，自动等待冷却/并发释放。

    Returns:
        (api_key, key_for_release, base_url) — 前两者相同，base_url空则用默认。

    最多重试60次，单轮最长等5s。
    """
    for _ in range(60):
        if not pool.has_enabled_keys():
            raise RuntimeError("KeyPool: 所有Key均已失效，放弃")
        key, wait, base_url = pool.get_key()
        if key is not None:
            return key, key, base_url
        # 等待：冷却到期或有槽释放
        if wait > 1.0:
            await asyncio.sleep(min(wait, 5.0))
        else:
            # 并发满，等release通知或超时
            try:
                await asyncio.wait_for(pool._release_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            pool._release_event.clear()  # 醒来后清除标记
    # 兜底
    key, _, base_url = pool.get_key()
    if key:
        return key, key, base_url
    raise RuntimeError("KeyPool: 所有Key持续不可用，放弃")


# ---------- 全局单例 ----------

def _parse_keys(env_keys: str, env_single: str) -> list[str]:
    """解析多Key配置。优先用逗号分隔的多Key，兼容单Key。"""
    keys_str = os.environ.get(env_keys, "").strip()
    if keys_str:
        return [k.strip() for k in keys_str.split(",") if k.strip()]
    single = os.environ.get(env_single, "").strip()
    if single:
        return [single]
    return []


def _parse_weights(env_weights: str, count: int) -> list[int] | None:
    """解析权重配置，如 "2,2,3"。"""
    w = os.environ.get(env_weights, "").strip()
    if w:
        weights = [int(x.strip()) for x in w.split(",") if x.strip()]
        if len(weights) == count:
            return weights
        logger.warning("KeyPool: %s 长度(%d)与keys数量(%d)不匹配，忽略权重",
                       env_weights, len(weights), count)
    return None


def _parse_urls(env_urls: str, count: int, default_url: str) -> list[str] | None:
    """解析每个Key的endpoint URL。空值或非法值用默认。

    防呆：只接受以 http:// 或 https:// 开头的值，其余（脏值、误填片段）
    一律忽略并告警，回退到 default_url，避免污染 base_url 拼出无协议的请求URL。
    """
    u = os.environ.get(env_urls, "").strip()
    if u:
        urls = [x.strip() for x in u.split(",")]
        # 补齐到count长度
        while len(urls) < count:
            urls.append("")
        result = []
        for url in urls[:count]:
            if not url:
                result.append(default_url)
            elif url.startswith(("http://", "https://")):
                result.append(url)
            else:
                logger.warning("KeyPool: %s 含非法URL %r（缺http(s)://），已忽略，回退默认 %s",
                               env_urls, url, default_url)
                result.append(default_url)
        return result
    return None


def create_llm_pool() -> KeyPool | None:
    """创建推理模型KeyPool。无配置时返回None（降级为单Key模式）。"""
    from config import LLM_API_KEY, LLM_BASE_URL  # noqa: F401
    keys = _parse_keys("LLM_API_KEYS", "LLM_API_KEY")
    if not keys:
        return None
    weights = _parse_weights("LLM_API_KEY_WEIGHTS", len(keys))
    base_urls = _parse_urls("LLM_API_KEY_URLS", len(keys), LLM_BASE_URL)
    conc = int(os.environ.get("LLM_KEY_CONCURRENCY", str(_DEFAULT_CONCURRENCY)))
    pool = KeyPool(keys, max_concurrent_per_key=conc, weights=weights, base_urls=base_urls)
    logger.info("KeyPool: LLM号池初始化 — %d个Key, 每Key %d并发, 总并发 %d",
                len(keys), conc, len(keys) * conc)
    return pool


# ── OCR 每老师并发限制 ──
_ocr_teacher_semaphores: dict[str, asyncio.Semaphore] = {}
_ocr_teacher_lock = asyncio.Lock()


async def get_ocr_teacher_semaphore(
    teacher_id: str, limit: int | None = None
) -> asyncio.Semaphore:
    """获取或创建老师的 OCR 本地请求窗口。

    默认沿用当前 OCR 请求窗口；教师间公平由共享模型准入决定。
    单 worker 下模块级 dict 即进程内共享，跨批次同一老师复用同一信号量，
    因此"每老师 N 并发"是跨该老师所有并发批次的总上限。teacher_id 为空时
    返回一个临时信号量（不参与共享，等价于本次调用无跨调用限制）。
    """
    if limit is None:
        from config import OCR_PER_TEACHER_LIMIT
        limit = OCR_PER_TEACHER_LIMIT
    if not teacher_id:
        return asyncio.Semaphore(limit)
    async with _ocr_teacher_lock:
        if teacher_id not in _ocr_teacher_semaphores:
            _ocr_teacher_semaphores[teacher_id] = asyncio.Semaphore(limit)
        return _ocr_teacher_semaphores[teacher_id]
