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


@pytest.mark.parametrize("sep", SEPARATORS[2:])
def test_legacy_poisoned_file_is_not_materialised_on_next_save(env_file, sep, monkeypatch):
    """旧版写进去的单行注入值，不能在下一次保存时被拆成真正的多行。

    写盘端用 str.splitlines()、解析端只按 \n 切行时，第一次正常保存就会把注入的那段
    「扶正」成独立的一行，于是注入重新生效（第 4 轮 finding 1 的升级迁移路径）。
    """
    monkeypatch.setenv("LLM_BASE_URL", "https://good.example/v1")
    poisoned = 'LLM_MODEL="qwen3.8-flash' + sep + "LLM_BASE_URL=https://evil.example/v1" + sep + '#"\n'
    env_file.write_text(poisoned, encoding="utf-8")
    env_sync.reset_cache()
    # 注入的键在读取阶段就不该出现
    assert "LLM_BASE_URL" not in env_sync._parse(env_file.read_text(encoding="utf-8"))

    S._update_config_file("LLM_MODEL", "qwen3.8-max")

    text = env_file.read_text(encoding="utf-8")
    parsed = env_sync._parse(text)
    assert "LLM_BASE_URL" not in parsed, text
    assert parsed["LLM_MODEL"] == "qwen3.8-max"
    assert "evil.example" not in text
    assert os.environ["LLM_BASE_URL"] == "https://good.example/v1"


def test_unterminated_quote_is_dropped_like_dotenv(tmp_path):
    """引号没闭合时 python-dotenv 整行丢弃；截断成半个值就会与启动态不一致。"""
    text = 'LLM_API_KEY=k\nLLM_MODEL="abc\n'
    path = tmp_path / "t.env"
    path.write_text(text, encoding="utf-8")

    ours = env_sync._parse(text)
    theirs = dotenv_values(path)
    assert "LLM_MODEL" not in ours
    assert ours == {k: v for k, v in theirs.items()}


def test_multiline_quoted_value_is_not_truncated(tmp_path):
    """dotenv 支持跨行引号值；这里不支持，但必须「不设置」而不是「设置半个」。"""
    text = 'LLM_MODEL="line1\nline2"\nLLM_API_KEY=k\n'
    path = tmp_path / "t.env"
    path.write_text(text, encoding="utf-8")

    ours = env_sync._parse(text)
    theirs = dict(dotenv_values(path))
    assert theirs["LLM_MODEL"] == "line1\nline2"
    assert "LLM_MODEL" not in ours, "半个值会覆盖启动时由 dotenv 读到的正确值"
    assert ours["LLM_API_KEY"] == "k"


def test_write_failure_is_not_reported_as_success(env_file, monkeypatch):
    """写盘失败必须报错并回滚内存值。

    以前这里是 `except Exception: logger.warning(...)`，接口照样返回 ok：
    管理员以为存上了，重启后又变回旧配置。Windows 上另一个 worker 正打开
    .env 时最容易撞上（os.replace 抛 PermissionError）。
    """
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-flash")

    def boom(key, value):
        raise PermissionError("simulated: file is open by another worker")

    monkeypatch.setattr(S, "_update_config_file", boom)

    with pytest.raises(HTTPException) as exc:
        S.save_settings(S.SettingsSaveRequest(model="qwen3.8-max"), user=None)

    assert exc.value.status_code == 500
    assert os.environ["LLM_MODEL"] == "qwen3.8-flash"   # 回滚
    assert not env_file.exists()


def test_partial_failure_rolls_back_only_the_failed_key(env_file, monkeypatch):
    """前一个键写成功、后一个失败时，只回滚失败的那个。"""
    monkeypatch.setenv("LLM_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("LLM_API_KEY", "sk-old")

    real = S._update_config_file
    calls = {"n": 0}

    def flaky(key, value):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(key, value)

    monkeypatch.setattr(S, "_update_config_file", flaky)

    with pytest.raises(HTTPException) as exc:
        S.save_settings(S.SettingsSaveRequest(api_key="sk-new", model="qwen3.8-max"), user=None)

    assert exc.value.status_code == 500
    assert os.environ["LLM_API_KEY"] == "sk-new"        # 第一个键已落盘，保留
    assert os.environ["LLM_MODEL"] == "qwen3.8-flash"   # 第二个键回滚


def test_bom_is_stripped_whatever_dotenv_version_does(tmp_path):
    """BOM 一律去掉，但不断言 python-dotenv 某一版的具体行为。

    旧版 dotenv（1.0.x）把 BOM 留在键名里，那个键等于废键；新版自己会去掉。
    requirements 里是 python-dotenv>=1.0.1，没有上界，所以这里只断言
    「我们一定拿到可用的键」，dotenv 给出哪一种形态都算它自己那版的事。
    """
    text = '\ufeffLLM_API_KEY="sk-bom"\nLLM_MODEL="qwen3.8-flash"\n'
    path = tmp_path / "t.env"
    path.write_text(text, encoding="utf-8")

    ours = env_sync._parse(text)
    theirs = dotenv_values(path)
    assert ours["LLM_API_KEY"] == "sk-bom"
    assert ours["LLM_MODEL"] == "qwen3.8-flash"
    # 两种版本行为都允许，但不能出现第三种（比如把 BOM 留在值里）
    assert set(theirs) in (
        {"LLM_API_KEY", "LLM_MODEL"},
        {"\ufeffLLM_API_KEY", "LLM_MODEL"},
    ), dict(theirs)
    if "LLM_API_KEY" in theirs:
        assert theirs["LLM_API_KEY"] == "sk-bom"