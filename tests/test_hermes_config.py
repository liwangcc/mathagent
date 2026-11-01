import asyncio
import os
import subprocess
import sys
import threading
import time
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import services.hermes_config as hermes_config
import services.hermes_tools as hermes_tools
from services.key_pool import KeyPool


def test_teacher_chat_on_mimo_uses_one_lane_of_eight():
    from services import mimo_lane

    async def lane():
        return hermes_config._get_hermes_local_semaphore()

    chat = asyncio.run(lane())
    assert chat is mimo_lane._lane
    assert chat._value == 8


class _FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def fake_agent_runtime(monkeypatch):
    # Factory-contract tests need no real Hermes installation or its imports.
    module = ModuleType("run_agent")
    module.__spec__ = ModuleSpec("run_agent", loader=None)
    module.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", module)


class _Lease:
    def __init__(self, limiter):
        self.limiter = limiter

    async def __aenter__(self):
        self.limiter.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.limiter.released += 1


class _Limiter:
    def __init__(self, invalid=None):
        self.acquisitions = []
        self.entered = 0
        self.released = 0
        self.invalid = set(invalid or ())

    async def is_credential_invalid(self, **credential):
        return credential["api_key"] in self.invalid

    async def mark_credential_invalid(self, **credential):
        self.invalid.add(credential["api_key"])

    async def acquire(self, **kwargs):
        self.acquisitions.append(kwargs)
        return _Lease(self)


def test_create_agent_defaults_to_pro_main_model(monkeypatch, tmp_path, fake_agent_runtime):
    monkeypatch.setattr(hermes_config, "_TEACHER_DIR", tmp_path / "teacher")
    monkeypatch.setattr(hermes_config, "_BOSS_DIR", tmp_path / "boss")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:18765/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.delenv("TEACHER_LLM_MODEL", raising=False)
    monkeypatch.delenv("TEACHER_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("TEACHER_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TEACHER_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_TEACHER_WEB_SEARCH_ENABLED", raising=False)
    monkeypatch.setattr(
        "services.hermes_tools.register_mathagent_tools",
        lambda: None,
    )

    agent = hermes_config.create_agent(SimpleNamespace(id=7, education_stage="senior"))

    assert agent.kwargs["model"] == "mimo-v2.6-pro"
    assert agent.kwargs["provider"] == "xiaomi-token-plan"
    assert agent.kwargs["base_url"] == "http://127.0.0.1:18765/v1"
    assert agent.kwargs["enabled_toolsets"] == ["mathagent_teacher_read"]


def test_create_agent_adds_only_search_when_teacher_search_is_enabled(
    monkeypatch,
    tmp_path,
    fake_agent_runtime,
):
    monkeypatch.setattr(hermes_config, "_TEACHER_DIR", tmp_path / "teacher")
    monkeypatch.setattr(hermes_config, "_BOSS_DIR", tmp_path / "boss")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:18765/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-only-key")
    monkeypatch.setenv("HERMES_TEACHER_WEB_SEARCH_ENABLED", "true")
    monkeypatch.setattr(
        "services.hermes_tools.register_mathagent_tools",
        lambda: None,
    )

    agent = hermes_config.create_agent(SimpleNamespace(id=8, education_stage="junior"))

    assert agent.kwargs["enabled_toolsets"] == [
        "mathagent_teacher_read",
        "mathagent_teacher_search",
    ]
    assert "web" not in agent.kwargs["enabled_toolsets"]
    assert "browser" not in agent.kwargs["enabled_toolsets"]
    assert "file" not in agent.kwargs["enabled_toolsets"]
    assert "terminal" not in agent.kwargs["enabled_toolsets"]


def test_teacher_prompt_contains_current_workflows_and_infrastructure_boundary():
    prompt = hermes_config._make_system_prompt()

    for page in (
        "「详细批改」",
        "「无身份批阅」",
        "「答题卡批阅」",
        "「班级管理」",
        "「成绩报告」",
        "「批改记录」",
        "「统计分析」",
    ):
        assert page in prompt
    assert "当前登录老师本来就有权限看到" in prompt
    assert "不得读取、搜索、总结或透露源码" in prompt
    assert "绝不能把学生姓名" in prompt
    assert "不写入学生档案" in prompt


def test_create_agent_ys_uses_responses_and_high(monkeypatch, tmp_path, fake_agent_runtime):
    monkeypatch.setattr(hermes_config, "_TEACHER_DIR", tmp_path / "teacher")
    monkeypatch.setattr(hermes_config, "_BOSS_DIR", tmp_path / "boss")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("TEACHER_LLM_PROVIDER", "ys-gpt")
    monkeypatch.setenv("TEACHER_LLM_BASE_URL", "https://ys.test/v1")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "ys-test-key")
    monkeypatch.setenv("TEACHER_LLM_API_MODE", "codex_responses")
    monkeypatch.setenv("TEACHER_LLM_REASONING_EFFORT", "high")
    monkeypatch.delenv("XIAOMI_API_KEY", raising=False)
    constants = ModuleType("hermes_constants")
    constants.parse_reasoning_effort = lambda effort: {"enabled": True, "effort": effort}
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr(hermes_tools, "register_mathagent_tools", lambda: None)

    agent = hermes_config.create_agent(SimpleNamespace(id=7, education_stage="senior"))

    assert agent.kwargs["model"] == "gpt-6.1-sol"
    assert agent.kwargs["provider"] == "ys-gpt"
    assert agent.kwargs["base_url"] == "https://ys.test/v1"
    assert agent.kwargs["api_mode"] == "codex_responses"
    assert agent.kwargs["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert "XIAOMI_API_KEY" not in os.environ
    assert agent.kwargs["enabled_toolsets"] == ["mathagent_teacher_read"]


def test_ys_lease_does_not_consume_mimo_subscription_lane(monkeypatch):
    limiter = _Limiter()
    monkeypatch.setenv("TEACHER_LLM_PROVIDER", "ys-gpt")
    monkeypatch.setenv("TEACHER_LLM_BASE_URL", "https://ys.test/v1")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "ys-test-key")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "gpt-6.1-sol")
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)
    def reject_mimo_lane():
        raise AssertionError("YS must not acquire the MiMo subscription lane")
    monkeypatch.setattr(hermes_config, "_get_hermes_local_semaphore", reject_mimo_lane)

    result = asyncio.run(hermes_config.run_hermes_with_model_lease(
        SimpleNamespace(id=17), lambda: "done",
    ))

    assert result == "done"
    assert limiter.entered == limiter.released == 1
    assert limiter.acquisitions[0]["provider"] == "ys-gpt"


def test_mathagent_registry_separates_teacher_reads_from_action_tools(monkeypatch):
    registered = {}
    fake_registry = SimpleNamespace(
        register=lambda **kwargs: registered.__setitem__(
            kwargs["name"],
            kwargs["toolset"],
        )
    )
    registry_module = ModuleType("tools.registry")
    registry_module.registry = fake_registry
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)

    hermes_tools.register_mathagent_tools()

    teacher_read_names = {
        name
        for name, toolset in registered.items()
        if toolset == "mathagent_teacher_read"
    }
    assert teacher_read_names == {
        "mathagent_query_student",
        "mathagent_query_summary",
        "mathagent_query_pending",
        "mathagent_list_classes",
        "mathagent_list_students",
        "mathagent_query_question_bank",
    }
    assert {
        "mathagent_generate_report",
        "mathagent_export_error_sheet",
        "mathagent_office_generate",
        "mathagent_export_scores_excel",
        "mathagent_parse_document",
    }.isdisjoint(teacher_read_names)
    assert registered["mathagent_public_web_search"] == "mathagent_teacher_search"


def test_setup_environment_prefers_teacher_shared_key(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_config, "_TEACHER_DIR", tmp_path / "teacher")
    monkeypatch.setattr(hermes_config, "_BOSS_DIR", tmp_path / "boss")
    monkeypatch.setenv("XIAOMI_API_KEY", "stale-host-key")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("MIMO_TEMPLATE_API_KEY", "shared-key")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "teacher-key")

    hermes_config._setup_environment()

    assert hermes_config.os.environ["XIAOMI_API_KEY"] == "teacher-key"


def test_hermes_conversation_uses_shared_mimo_fair_lease(monkeypatch):
    limiter = _Limiter()
    monkeypatch.setenv("TEACHER_LLM_BASE_URL", "https://mimo.test/v1")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "hermes-test-key")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "mimo-v2.5-pro")
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)

    result = asyncio.run(hermes_config.run_hermes_with_model_lease(
        SimpleNamespace(id=17),
        lambda: "done",
    ))

    assert result == "done"
    assert limiter.entered == limiter.released == 1
    acquisition = limiter.acquisitions[0]
    assert acquisition["provider"] == "xiaomi_mimo_subscription"
    assert acquisition["teacher_id"] == "17"
    assert acquisition["global_capacity"] == hermes_config.LLM_KEY_CONCURRENCY
    assert acquisition["credential_capacity"] == hermes_config.LLM_KEY_CONCURRENCY
    assert acquisition["teacher_base_capacity"] <= acquisition["global_capacity"]
    assert acquisition["teacher_burst_capacity"] <= acquisition["global_capacity"]
    assert acquisition["global_reserve_capacity"] < acquisition["global_capacity"]


def test_hermes_shared_pool_capacity_excludes_invalid_key(monkeypatch):
    pool = KeyPool(
        ["disabled-key", "healthy-a", "healthy-b"],
        max_concurrent_per_key=4,
        base_urls=["https://mimo.test/v1"] * 3,
    )
    limiter = _Limiter(invalid={"disabled-key"})
    observed = {}
    monkeypatch.setenv("LLM_BASE_URL", "https://mimo.test/v1")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "mimo-v2.5-pro")
    monkeypatch.delenv("TEACHER_LLM_API_KEY", raising=False)
    monkeypatch.setattr(hermes_config, "_resolve_hermes_key_pool", lambda: pool)
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)

    def call():
        observed["route"] = hermes_config._teacher_model_route()
        return "done"

    result = asyncio.run(hermes_config.run_hermes_with_model_lease(
        SimpleNamespace(id=29),
        call,
    ))

    assert result == "done"
    assert pool.enabled_key_count() == 2
    assert observed["route"][1] in {"healthy-a", "healthy-b"}
    assert limiter.acquisitions[0]["global_capacity"] == 2 * hermes_config.LLM_KEY_CONCURRENCY
    assert limiter.acquisitions[0]["credential_capacity"] == hermes_config.LLM_KEY_CONCURRENCY
    assert limiter.acquisitions[0]["teacher_id"] == "29"
    assert {item["max_concurrent"] for item in pool.status()} == {4}


def test_hermes_auth_failure_switches_key_and_recomputes_capacity(monkeypatch):
    pool = KeyPool(
        ["bad-key", "good-key"],
        max_concurrent_per_key=4,
        weights=[100, 1],
        base_urls=["https://mimo.test/v1"] * 2,
    )
    limiter = _Limiter()
    seen = []
    monkeypatch.setenv("LLM_BASE_URL", "https://mimo.test/v1")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "mimo-v2.5-pro")
    monkeypatch.delenv("TEACHER_LLM_API_KEY", raising=False)
    monkeypatch.setattr(hermes_config, "_resolve_hermes_key_pool", lambda: pool)
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)

    def call():
        key = hermes_config._teacher_model_route()[1]
        seen.append(key)
        if key == "bad-key":
            return {"failed": True, "error": "HTTP 401 unauthorized"}
        return "done"

    result = asyncio.run(hermes_config.run_hermes_with_model_lease(
        SimpleNamespace(id=31),
        call,
    ))

    assert result == "done"
    assert seen == ["bad-key", "good-key"]
    assert limiter.invalid == {"bad-key"}
    assert [
        item["global_capacity"] for item in limiter.acquisitions
    ] == [2 * hermes_config.LLM_KEY_CONCURRENCY, hermes_config.LLM_KEY_CONCURRENCY]
    assert all(item["in_flight"] == 0 for item in pool.status())


def test_cancelled_hermes_waits_for_worker_before_releasing_lease(monkeypatch):
    limiter = _Limiter()
    started = threading.Event()
    finish = threading.Event()
    monkeypatch.setenv("TEACHER_LLM_BASE_URL", "https://mimo.test/v1")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "hermes-test-key")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "mimo-v2.5-pro")
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)

    def blocking_call():
        started.set()
        finish.wait(timeout=2)
        return "late-result"

    async def run():
        task = asyncio.create_task(hermes_config.run_hermes_with_model_lease(
            SimpleNamespace(id=17),
            blocking_call,
        ))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert limiter.released == 0
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
    assert limiter.released == 1


def test_hermes_worker_threads_are_bounded_even_when_limiter_is_noop(monkeypatch):
    limiter = _Limiter()
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()
    monkeypatch.setenv("TEACHER_LLM_BASE_URL", "https://mimo.test/v1")
    monkeypatch.setenv("TEACHER_LLM_API_KEY", "hermes-test-key")
    monkeypatch.setenv("TEACHER_LLM_MODEL", "mimo-v2.5-pro")
    monkeypatch.setattr(hermes_config, "get_model_limiter", lambda: limiter)
    from services import mimo_lane

    monkeypatch.setattr(mimo_lane, "MIMO_SUBSCRIPTION_GLOBAL_CONCURRENCY", 2)
    monkeypatch.setattr(mimo_lane, "_lane", None)
    monkeypatch.setattr(mimo_lane, "_lane_loop", None)

    def blocking_call():
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        return "done"

    async def run():
        return await asyncio.gather(*(
            hermes_config.run_hermes_with_model_lease(
                SimpleNamespace(id=index),
                blocking_call,
            )
            for index in range(6)
        ))

    assert asyncio.run(run()) == ["done"] * 6
    assert state["peak"] == 2


def test_chat_route_runs_hermes_through_shared_lease_helper(monkeypatch):
    from routers import chat as chat_router

    observed = {}

    class FakeAgent:
        def run_conversation(self, *, user_message, conversation_history):
            observed["conversation"] = (user_message, conversation_history)
            return {
                "completed": False,
                "final_response": "ok",
                "messages": [],
                "session_id": "session-1",
            }

    def fake_create_agent(_user):
        observed["agent_created"] = True
        return FakeAgent()

    async def fake_run_with_lease(user, call):
        observed["teacher_id"] = user.id
        observed["call"] = call
        assert "agent_created" not in observed
        return call()

    monkeypatch.setattr(chat_router, "HERMES_ENABLED", True)
    monkeypatch.setattr(chat_router, "check_llm_rate", lambda _user_id: None)
    monkeypatch.setattr(chat_router, "create_agent", fake_create_agent)
    monkeypatch.setattr(
        chat_router,
        "run_hermes_with_model_lease",
        fake_run_with_lease,
    )

    result = asyncio.run(chat_router.chat(
        chat_router.ChatRequest(message="hello"),
        db=object(),
        user=SimpleNamespace(id=23, education_stage="senior"),
    ))

    assert result["response"] == "ok"
    assert result["session_id"] == "session-1"
    assert observed["teacher_id"] == 23
    assert observed["agent_created"] is True
    assert observed["conversation"] == ("hello", None)
