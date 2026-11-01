"""设置持久化与跨 worker 同步（services/env_sync.py）的回归测试。

背景：设置页保存只改「当前进程的 os.environ + 一份 .env 文件」。
容器里 .env 原本落在镜像层，容器一重建就丢；gunicorn 起两个 worker 时，
保存请求只落到其中一个进程，另一个继续用旧值。
"""
import os

import pytest

from services import env_sync


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    env_sync.reset_cache()
    yield
    env_sync.reset_cache()


def test_env_path_follows_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    assert env_sync.env_path() == tmp_path / ".env"

    monkeypatch.delenv("MATHAGENT_DATA_DIR", raising=False)
    assert env_sync.env_path().name == ".env"
    assert env_sync.env_path().parent.name != tmp_path.name


def test_sync_picks_up_changes_from_another_process(tmp_path, monkeypatch):
    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text('LLM_MODEL="first"\n', encoding="utf-8")

    # 首次读入
    assert env_sync.sync_env(force=True) == ["LLM_MODEL"]
    assert os.environ["LLM_MODEL"] == "first"

    # 另一个 worker 写了新值：本进程下一次 stat 就能看到
    env_file.write_text('LLM_MODEL="second"\nLLM_BASE_URL="https://api.example.com/v1"\n', encoding="utf-8")
    changed = env_sync.sync_env()
    assert set(changed) == {"LLM_MODEL", "LLM_BASE_URL"}
    assert os.environ["LLM_MODEL"] == "second"

    # 文件没变时不做任何事（每个请求只有一次 stat）
    assert env_sync.sync_env() == []


def test_settings_write_goes_to_data_dir(tmp_path, monkeypatch):
    """设置页写 .env 时，容器形态必须落到持久卷。"""
    from routers import settings as settings_mod

    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_mod, "ENV_PATH", tmp_path / ".env")

    settings_mod._update_config_file("LLM_MODEL", "qwen3.8-flash")

    written = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'LLM_MODEL="qwen3.8-flash"' in written
    # 写完立刻同步进本进程
    assert os.environ.get("LLM_MODEL") == "qwen3.8-flash"


def test_unknown_key_is_refused(tmp_path, monkeypatch):
    from routers import settings as settings_mod

    monkeypatch.setattr(settings_mod, "ENV_PATH", tmp_path / ".env")
    with pytest.raises(ValueError):
        settings_mod._update_config_file("PATH", "/tmp/evil")
    assert not (tmp_path / ".env").exists() or "PATH=" not in (tmp_path / ".env").read_text(encoding="utf-8")