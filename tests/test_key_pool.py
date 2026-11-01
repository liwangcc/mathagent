"""Tests for 号池管理器 — KeyPool（并发控制模型）。"""

import asyncio
import time
import pytest
from services.key_pool import (
    KeyPool,
    _KeySlot,
    acquire_key,
    enabled_provider_capacity,
    refresh_shared_credential_health,
)


class TestKeySlot:
    def test_acquire_and_release(self):
        s = _KeySlot(key="sk-test", max_concurrent=5, weight=1)
        assert s.remaining_capacity == 5
        s.acquire()
        assert s.remaining_capacity == 4
        assert s._in_flight == 1
        s.release()
        assert s.remaining_capacity == 5

    def test_available_respects_concurrency_limit(self):
        s = _KeySlot(key="sk-test", max_concurrent=2, weight=1)
        s.acquire()
        s.acquire()
        assert s.available is False
        assert s.remaining_capacity == 0

    def test_cooldown_blocks_availability(self):
        s = _KeySlot(key="sk-test", max_concurrent=10, weight=1)
        s.mark_429()
        assert s.available is False
        assert s._cooldown_until > time.time()

    def test_success_resets_429_counter(self):
        s = _KeySlot(key="sk-test", max_concurrent=10, weight=1)
        s.mark_429()
        assert s._consecutive_429 == 1
        s.mark_success()
        assert s._consecutive_429 == 0

    def test_cooldown_expiry_resets_state(self):
        """冷却到期后应重置状态。"""
        s = _KeySlot(key="sk-test", max_concurrent=2, weight=1)
        s.mark_429()
        s._cooldown_until = time.time() - 1
        assert s.available is True
        assert s._consecutive_429 == 0  # 被重置了

    def test_wait_seconds_cooldown(self):
        """冷却中应返回冷却剩余时间。"""
        s = _KeySlot(key="sk-test", max_concurrent=10, weight=1)
        s.mark_429()
        wait = s.wait_seconds()
        assert wait >= 30
        assert wait <= 31

    def test_retry_after_overrides_default(self):
        s = _KeySlot(key="sk-test", max_concurrent=10, weight=1)
        cooldown = s.mark_429(retry_after=5.0)
        wait = s.wait_seconds()
        assert cooldown == 5.0
        assert 4.9 <= wait <= 5.0

    def test_429_cooldown_uses_positive_jitter(self, monkeypatch):
        import services.key_pool as key_pool

        observed = {}

        def fixed_jitter(delay, *, ceiling_s):
            observed.update(delay=delay, ceiling_s=ceiling_s)
            return delay + 0.75

        monkeypatch.setattr(key_pool, "jittered_backoff", fixed_jitter)
        monkeypatch.setattr(key_pool.time, "time", lambda: 100.0)
        slot = _KeySlot(key="sk-test", max_concurrent=10, weight=1)

        slot.mark_429()

        assert observed == {
            "delay": key_pool._COOLDOWN_BASE,
            "ceiling_s": key_pool._COOLDOWN_MAX,
        }
        assert slot._cooldown_until == 130.75

    def test_consecutive_429_escalation(self):
        s = _KeySlot(key="sk-test", max_concurrent=10, weight=1)
        s.mark_429()
        w1 = s.wait_seconds()
        s._cooldown_until = time.time() - 1
        s.mark_429()
        w2 = s.wait_seconds()
        assert w2 > w1


class TestKeyPool:
    def test_shared_cooldown_is_imported_and_removed_from_healthy_capacity(self):
        pool = KeyPool(
            ["cooling-key", "healthy-key"],
            max_concurrent_per_key=4,
            base_urls=["https://one.test/v1", "https://two.test/v1"],
        )

        class SharedHealth:
            async def is_credential_invalid(self, **_credential):
                return False

            async def credential_cooldown_seconds(self, **credential):
                return 20.0 if credential["api_key"] == "cooling-key" else 0.0

        asyncio.run(refresh_shared_credential_health(
            pool,
            SharedHealth(),
            provider="mimo",
            default_base_url="https://default.test/v1",
            model="reasoning-model",
        ))

        assert pool.enabled_key_count() == 2
        assert pool.healthy_key_count() == 1
        assert enabled_provider_capacity(pool, 4) == 4
        key, _, _ = pool.get_key()
        assert key == "healthy-key"

    def test_enabled_provider_capacity_scales_without_changing_key_limits(self):
        pool = KeyPool(
            ["sk-a", "sk-b", "sk-c"],
            max_concurrent_per_key=4,
        )

        assert enabled_provider_capacity(pool, 4) == 12
        assert {item["max_concurrent"] for item in pool.status()} == {4}

        pool.mark_invalid("sk-a", 401)

        assert enabled_provider_capacity(pool, 4) == 8
        assert {item["max_concurrent"] for item in pool.status()} == {4}

        pool.mark_429("sk-b", retry_after=10)

        assert pool.healthy_key_count() == 1
        assert enabled_provider_capacity(pool, 4) == 4

    def test_single_key(self):
        pool = KeyPool(["sk-only"], max_concurrent_per_key=10)
        key, wait, _ = pool.get_key()
        assert key == "sk-only"
        assert wait == 0.0

    def test_round_robin_rotation(self):
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], max_concurrent_per_key=100)
        seen = set()
        for _ in range(6):
            key, _, _ = pool.get_key()
            assert key is not None
            pool.release(key)
            seen.add(key)
        assert seen == {"sk-a", "sk-b", "sk-c"}

    def test_weighted_selection_distribution(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=1000, weights=[3, 1])
        counts = {"sk-a": 0, "sk-b": 0}
        for _ in range(1000):
            key, _, _ = pool.get_key()
            assert key is not None
            pool.release(key)
            counts[key] += 1
        ratio = counts["sk-a"] / 1000
        assert 0.65 < ratio < 0.85

    def test_429_cooldown_skips_key(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=100)
        pool.mark_429("sk-a")
        keys = set()
        for _ in range(5):
            key, _, _ = pool.get_key()
            assert key is not None
            pool.release(key)
            keys.add(key)
        assert keys == {"sk-b"}

    def test_invalid_key_is_disabled_and_skipped(self):
        pool = KeyPool(
            ["sk-bad", "sk-good"],
            max_concurrent_per_key=100,
            weights=[100, 1],
        )
        key, _, _ = pool.get_key()
        assert key == "sk-bad"
        pool.release(key)

        pool.mark_invalid("sk-bad", 401)

        for _ in range(3):
            key, _, _ = pool.get_key()
            assert key == "sk-good"
            pool.release(key)
        status = pool.status()
        assert status[0]["disabled"] is True
        assert status[0]["invalid_status_code"] == 401

    def test_readiness_reports_counts_without_credentials(self):
        pool = KeyPool(["sk-secret-a", "sk-secret-b"])
        pool.mark_invalid("sk-secret-a", 403)

        state = pool.readiness()

        assert state == {
            "configured": True,
            "ready": True,
            "configured_keys": 2,
            "enabled_keys": 1,
            "immediately_available_keys": 1,
        }
        assert "sk-secret" not in str(state)

    def test_all_concurrent_full_returns_none(self):
        """所有Key并发都满时应返回None。"""
        pool = KeyPool(["sk-a"], max_concurrent_per_key=1)
        key, _, _ = pool.get_key()
        assert key == "sk-a"
        # 并发满了
        key2, wait, _ = pool.get_key()
        assert key2 is None
        assert wait > 0  # 等待冷却或release

    def test_all_429_returns_none(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=10)
        pool.mark_429("sk-a")
        pool.mark_429("sk-b")
        key, wait, _ = pool.get_key()
        assert key is None
        assert wait > 0

    def test_release_makes_slot_available(self):
        """release后应能再获取。"""
        pool = KeyPool(["sk-a"], max_concurrent_per_key=1)
        key1, _, _ = pool.get_key()
        assert key1 == "sk-a"
        key2, _, _ = pool.get_key()
        assert key2 is None  # 满了
        pool.release("sk-a")
        key3, _, _ = pool.get_key()
        assert key3 == "sk-a"

    def test_status_returns_all_keys(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=10)
        status = pool.status()
        assert len(status) == 2
        assert all(s["available"] for s in status)

    def test_empty_keys_raises(self):
        with pytest.raises(ValueError, match="至少需要一个"):
            KeyPool([])

    def test_weights_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="weights长度"):
            KeyPool(["sk-a", "sk-b"], weights=[1])

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"keys": ["sk-a", " sk-a "]}, "不能重复"),
            ({"keys": ["sk-a", " "]}, "不能为空"),
            ({"keys": ["sk-a"], "weights": [0]}, "权重"),
            ({"keys": ["sk-a"], "weights": [-1]}, "权重"),
            ({"keys": ["sk-a"], "max_concurrent_per_key": 0}, "并发"),
            ({"keys": ["sk-a"], "max_concurrent_per_key": -1}, "并发"),
        ],
    )
    def test_invalid_pool_configuration_fails_fast(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            KeyPool(**kwargs)


class TestAcquireKey:
    def test_acquire_returns_immediately_when_available(self):
        pool = KeyPool(["sk-a"], max_concurrent_per_key=10)
        key, release_key, _ = asyncio.run(acquire_key(pool))
        assert key == "sk-a"
        assert release_key == "sk-a"

    def test_acquire_waits_for_release(self):
        """并发满时acquire_key应等待release后返回。"""
        pool = KeyPool(["sk-a"], max_concurrent_per_key=1)
        key1, _, _ = pool.get_key()
        assert key1 == "sk-a"

        async def _test():
            async def _release_later():
                await asyncio.sleep(0.5)
                pool.release("sk-a")

            asyncio.create_task(_release_later())
            start = time.time()
            key, _, _ = await acquire_key(pool)
            elapsed = time.time() - start
            return key, elapsed

        key, elapsed = asyncio.run(_test())
        assert key == "sk-a"
        assert elapsed >= 0.3  # 等了release

    def test_all_invalid_keys_fail_immediately(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=1)
        pool.mark_invalid("sk-a", 401)
        pool.mark_invalid("sk-b", 403)

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="均已失效"):
            asyncio.run(acquire_key(pool))
        assert time.monotonic() - start < 0.2


class TestConcurrency:
    def test_concurrent_get_key_no_crash(self):
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], max_concurrent_per_key=100)

        async def _grab():
            key, _, _ = pool.get_key()
            if key:
                pool.release(key)
            return key

        async def _test():
            return await asyncio.gather(*[_grab() for _ in range(50)])

        results = asyncio.run(_test())
        assert len(results) == 50
        keys = {r for r in results if r}
        assert len(keys) >= 2

    def test_concurrent_acquire_key(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=100)

        async def _test():
            pairs = await asyncio.gather(*[acquire_key(pool) for _ in range(20)])
            for _, rk, _ in pairs:
                pool.release(rk)
            return pairs

        pairs = asyncio.run(_test())
        assert len(pairs) == 20

    def test_concurrency_limit_enforced(self):
        """并发限制应被严格遵守。"""
        pool = KeyPool(["sk-a"], max_concurrent_per_key=3)
        keys = []
        for _ in range(3):
            k, _, _ = pool.get_key()
            keys.append(k)
        assert all(k == "sk-a" for k in keys)
        # 第4个应该拿不到
        k4, _, _ = pool.get_key()
        assert k4 is None

    def test_multi_key_concurrency(self):
        """3个Key×4并发=12同时在飞。"""
        pool = KeyPool(["sk-a", "sk-b", "sk-c"], max_concurrent_per_key=4)
        acquired = []
        for _ in range(12):
            k, _, _ = pool.get_key()
            assert k is not None
            acquired.append(k)
        # 第13个拿不到
        k13, _, _ = pool.get_key()
        assert k13 is None
        # 释放一个就能拿到
        pool.release(acquired[0])
        k14, _, _ = pool.get_key()
        assert k14 is not None


class TestKeyPoolEdgeCases:
    def test_many_keys_performance(self):
        keys = [f"sk-{i}" for i in range(100)]
        pool = KeyPool(keys, max_concurrent_per_key=100)
        for _ in range(200):
            k, _, _ = pool.get_key()
            if k:
                pool.release(k)

    def test_concurrent_429_recovery(self):
        pool = KeyPool(["sk-a", "sk-b"], max_concurrent_per_key=10)
        pool.mark_429("sk-a")
        pool.mark_429("sk-b")
        for s in pool._slots:
            s._cooldown_until = time.time() - 1
        key, wait, _ = pool.get_key()
        assert key in ("sk-a", "sk-b")
        assert wait == 0.0
