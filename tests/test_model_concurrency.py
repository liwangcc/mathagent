import asyncio

from services.bounded_dispatch import bounded_completion_map
from services.model_concurrency import (
    MIMO_SUBSCRIPTION_PROVIDER,
    configured_fair_share_policy,
    teacher_fingerprint,
)


def test_route_policy_can_use_full_capacity_when_reserve_is_explicitly_zero():
    policy = configured_fair_share_policy(500, global_reserve_capacity=0)
    assert policy.global_reserve_capacity == 0
    assert policy.teacher_burst_capacity == 500


def test_route_policy_defaults_mimo_reserve_to_zero_without_changing_legacy_defaults():
    subscription = configured_fair_share_policy(8, provider=MIMO_SUBSCRIPTION_PROVIDER)

    assert subscription.global_reserve_capacity == 0
    assert subscription.teacher_burst_capacity == 8


def test_route_policy_clamps_explicit_reserve_after_provider_capacity_cap():
    policy = configured_fair_share_policy(
        8,
        provider=MIMO_SUBSCRIPTION_PROVIDER,
        teacher_base_capacity=30,
        teacher_burst_capacity=500,
        global_reserve_capacity=30,
    )

    assert policy.global_capacity == 8
    assert policy.teacher_base_capacity == 4
    assert policy.teacher_burst_capacity == 8
    assert policy.global_reserve_capacity == 4


def test_teacher_fingerprint_tolerates_minimal_config_without_jwt_secret(monkeypatch):
    import config

    monkeypatch.delattr(config, "JWT_SECRET", raising=False)

    fingerprint = teacher_fingerprint("xiaomi_mimo_payg", "teacher-1")

    assert len(fingerprint) == 24
    assert "teacher-1" not in fingerprint


def test_configured_policy_clamps_large_teacher_limits_for_small_mimo_account(
    monkeypatch,
):
    import config

    monkeypatch.setattr(config, "MODEL_TEACHER_BASE_CONCURRENCY", 30)
    monkeypatch.setattr(config, "MODEL_TEACHER_BURST_CONCURRENCY", 60)
    monkeypatch.setattr(config, "MODEL_GLOBAL_RESERVE_CONCURRENCY", 30)

    mimo = configured_fair_share_policy(4)
    large = configured_fair_share_policy(500)

    assert (
        mimo.teacher_base_capacity,
        mimo.teacher_burst_capacity,
        mimo.global_reserve_capacity,
    ) == (2, 4, 0)
    assert (
        large.teacher_base_capacity,
        large.teacher_burst_capacity,
        large.global_reserve_capacity,
    ) == (30, 60, 30)


def test_bounded_completion_map_yields_success_before_concurrent_failure():
    events = []

    async def worker(item: int) -> int:
        if item == 0:
            await asyncio.sleep(0.04)
            raise RuntimeError("provider unavailable")
        await asyncio.sleep(0.005)
        return item * 10

    async def run():
        with_error = None
        try:
            async for index, result in bounded_completion_map(
                range(2), worker, max_concurrent=2,
            ):
                events.append((index, result))
        except RuntimeError as exc:
            with_error = str(exc)
        return with_error

    assert asyncio.run(run()) == "provider unavailable"
    assert events == [(1, 10)]
