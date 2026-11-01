"""Flash grading local concurrency window: environment-driven, safe default.

Production selects the per-worker window through ``FLASH_LOCAL_MAX_CONCURRENT``;
the default matches the shared Flash window. An explicit smaller local value
still applies. Redis, not worker count or credential count, enforces the total.
"""
import asyncio
import importlib

import pytest

import config
from services import flash_grading as flash


@pytest.fixture(autouse=True)
def _restore_default_window(monkeypatch):
    yield
    monkeypatch.delenv("FLASH_LOCAL_MAX_CONCURRENT", raising=False)
    importlib.reload(flash)


def test_default_window_matches_shared_flash(monkeypatch):
    monkeypatch.delenv("FLASH_LOCAL_MAX_CONCURRENT", raising=False)
    assert importlib.reload(flash).FLASH_LOCAL_MAX_CONCURRENT == config.DASHSCOPE_GLOBAL_CONCURRENCY


def test_env_selects_window(monkeypatch):
    monkeypatch.setenv("FLASH_LOCAL_MAX_CONCURRENT", "256")
    assert importlib.reload(flash).FLASH_LOCAL_MAX_CONCURRENT == config.DASHSCOPE_GLOBAL_CONCURRENCY


@pytest.mark.parametrize("raw", ["0", "-3", "many", ""])
def test_invalid_values_fall_back_to_shared_window(monkeypatch, raw):
    monkeypatch.setenv("FLASH_LOCAL_MAX_CONCURRENT", raw)
    assert importlib.reload(flash).FLASH_LOCAL_MAX_CONCURRENT == config.DASHSCOPE_GLOBAL_CONCURRENCY


def test_semaphore_uses_configured_window(monkeypatch):
    monkeypatch.setenv("FLASH_LOCAL_MAX_CONCURRENT", "7")
    reloaded = importlib.reload(flash)

    async def _window() -> int:
        return reloaded._local_semaphore()._value

    assert asyncio.run(_window()) == 7
