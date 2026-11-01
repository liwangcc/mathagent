"""设置写入的注入面：分隔符、重复键、原子替换、与 python-dotenv 的解析一致性。

第 4 轮复审 finding 1/2/3/4 的回归测试。
"""
import os
import threading

import pytest
from dotenv import dotenv_values
from fastapi import HTTPException

from routers import settings as S
from services import env_sync

SEPARATORS = ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    # 让 settings 与 env_sync 指向同一个文件
    monkeypatch.setenv("MATHAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(S, "ENV_PATH", tmp_path / ".env")
    env_sync.reset_cache()
    yield tmp_path / ".env"
    env_sync.reset_cache()


@pytest.mark.parametrize("sep", SEPARATORS)
def test_separator_in_model_is_rejected_before_any_write(env_file, sep, monkeypatch):
    """写进去一行、读回来两行 = 能插任意键（JWT_SECRET / LLM_BASE_URL）。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://good.example/v1")
    payload = f"qwen3.8-flash{sep}LLM_BASE_URL=https://evil.example/v1{sep}#"

    with pytest.raises(HTTPException) as exc:
        S.save_settings(S.SettingsSaveRequest(model=payload), user=None)

    assert exc.value.status_code == 400
    assert not env_file.exists()
    assert os.environ["LLM_BASE_URL"] == "https://good.example/v1"


@pytest.mark.parametrize("sep", SEPARATORS)
def test_writer_rejects_separators_too(env_file, sep):
    with pytest.raises(ValueError):
        S._update_config_file("LLM_MODEL", f"qwen3.8-flash{sep}JWT_SECRET=x")
    assert not env_file.exists()


def test_duplicate_key_is_collapsed_to_one_line(env_file):
    """只改第一行会让「读最后一行」的解析器继续用旧值。"""
    env_file.write_text(
        'LLM_MODEL="old"\n# comment\nLLM_MODEL="poisoned"\nLLM_API_KEY="k"\n',
        encoding="utf-8",
    )

    S._update_config_file("LLM_MODEL", "qwen3.8-flash")

    text = env_file.read_text(encoding="utf-8")
    assert text.count("LLM_MODEL=") == 1
    assert env_sync._parse(text)["LLM_MODEL"] == "qwen3.8-flash"
    assert 'LLM_API_KEY="k"' in text


def test_write_is_atomic_and_leaves_no_lock_or_tmp(env_file):
    S._update_config_file("LLM_MODEL", "qwen3.8-flash")
    assert env_file.exists()
    siblings = {p.name for p in env_file.parent.iterdir()}
    assert siblings == {".env"}, siblings


def test_concurrent_saves_keep_both_keys(env_file):
    errors = []

    def save(key, value):
        try:
            S._update_config_file(key, value)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=save, args=("LLM_MODEL", "qwen3.8-flash")),
        threading.Thread(target=save, args=("LLM_API_KEY", "sk-test")),
        threading.Thread(target=save, args=("LLM_BASE_URL", "https://api.example/v1")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    parsed = env_sync._parse(env_file.read_text(encoding="utf-8"))
    assert parsed["LLM_MODEL"] == "qwen3.8-flash"
    assert parsed["LLM_API_KEY"] == "sk-test"
    assert parsed["LLM_BASE_URL"] == "https://api.example/v1"


def test_parser_matches_python_dotenv(tmp_path):
    """管理员手改 data/.env 时，两边不能给出不同的值。"""
    text = (
        'export LLM_MODEL="qwen3.8-flash"\n'
        "LLM_API_KEY='sk-abc123'\n"
        "LLM_BASE_URL=https://api.example/v1  # 主线路\n"
        'X="a\\"b"\n'
    )
    path = tmp_path / "t.env"
    path.write_text(text, encoding="utf-8")

    ours = env_sync._parse(text)
    theirs = dotenv_values(path)
    for key in ("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "X"):
        assert ours[key] == theirs[key], (key, ours[key], theirs[key])


def test_bom_is_stripped():
    assert env_sync._parse('\ufeffLLM_MODEL="m"\n')["LLM_MODEL"] == "m"


def test_exotic_separators_do_not_split_lines():
    """\x0b 等字符留在值里，而不是变成新的键。"""
    parsed = env_sync._parse('LLM_MODEL="qwen\x0bLLM_BASE_URL=evil"\n')
    assert "LLM_BASE_URL" not in parsed
    assert parsed["LLM_MODEL"] == "qwen\x0bLLM_BASE_URL=evil"