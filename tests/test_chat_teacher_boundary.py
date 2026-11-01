import asyncio
from types import SimpleNamespace

import routers.chat as chat_router


def test_hermes_exception_does_not_fall_through_to_legacy_actions(monkeypatch):
    legacy_called = False

    async def fail_hermes(_user, _call):
        raise RuntimeError("provider unavailable")

    async def forbidden_legacy(*_args, **_kwargs):
        nonlocal legacy_called
        legacy_called = True
        raise AssertionError("Hermes failures must not enter the legacy intent path")

    monkeypatch.setattr(chat_router, "HERMES_ENABLED", True)
    monkeypatch.setattr(chat_router, "check_llm_rate", lambda _user_id: None)
    monkeypatch.setattr(chat_router, "run_hermes_with_model_lease", fail_hermes)
    monkeypatch.setattr(chat_router, "recognize_intent", forbidden_legacy)

    response = asyncio.run(chat_router.chat(
        chat_router.ChatRequest(message="帮我生成成绩表"),
        db=object(),
        user=SimpleNamespace(id=7, education_stage="senior"),
    ))

    assert response["intent"] == "chat"
    assert "连接模型服务不稳定" in response["response"]
    assert legacy_called is False


def test_disabled_hermes_legacy_path_rejects_action_intents(monkeypatch):
    async def recognize_report(*_args, **_kwargs):
        return {"intent": "report", "params": {"student_name": "张三"}}

    monkeypatch.setattr(chat_router, "HERMES_ENABLED", False)
    monkeypatch.setattr(chat_router, "check_llm_rate", lambda _user_id: None)
    monkeypatch.setattr(chat_router, "recognize_intent", recognize_report)

    response = asyncio.run(chat_router.chat(
        chat_router.ChatRequest(message="帮我生成张三的报告"),
        db=object(),
        user=SimpleNamespace(id=7, education_stage="senior"),
    ))

    assert response["intent"] == "help"
    assert "不会代替您生成或导出" in response["response"]
    assert response["data"] == {}
