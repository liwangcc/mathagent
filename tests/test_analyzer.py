"""Tests for services/analyzer.py — JSON parsing and judgment."""

import asyncio
import json

import httpx
import pytest
import config
from services import analyzer
from services.analyzer import parse_llm_json
from services.flash_grading import build_text_messages
from services.text_grading_rules import TEXT_GRADING_DETAIL_INSTRUCTIONS, TEXT_GRADING_RULES
from services.key_pool import KeyPool


class TestParseLlmJson:
    def test_pure_json_object(self):
        result = parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_pure_json_array(self):
        result = parse_llm_json('[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_markdown_fence_object(self):
        result = parse_llm_json('```json\n{"x": 1}\n```')
        assert result == {"x": 1}

    def test_markdown_fence_array(self):
        result = parse_llm_json('```\n[1, 2, 3]\n```')
        assert result == [1, 2, 3]

    def test_control_characters_stripped(self):
        result = parse_llm_json('{\x00"key"\x1f:\x00"value"\x1f}')
        assert result == {"key": "value"}

    def test_thinking_before_json(self):
        result = parse_llm_json('这是一段思考文字...\n{"answer": 42}\n以上是结果。')
        assert result == {"answer": 42}

    def test_thinking_before_array(self):
        result = parse_llm_json('一些前置内容\n[{"q": 1}, {"q": 2}]\n后置内容')
        assert result == [{"q": 1}, {"q": 2}]

    def test_nested_json(self):
        result = parse_llm_json('{"outer": {"inner": [1, 2, 3]}}')
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_chinese_characters(self):
        result = parse_llm_json('{"题目": "计算1+1", "答案": "2"}')
        assert result == {"题目": "计算1+1", "答案": "2"}

    def test_empty_input(self):
        with pytest.raises(ValueError):
            parse_llm_json("")

    def test_non_json_text(self):
        with pytest.raises(ValueError):
            parse_llm_json("这完全不是JSON")

    def test_invalid_json_with_braces(self):
        with pytest.raises(ValueError):
            parse_llm_json("{not valid json at all}")

    # ── LaTeX 反斜杠规范化（题库损坏根因回归测试）──
    def test_latex_begin_array(self):
        # \begin (\b开头) 不被解码成退格符 + egin
        r = parse_llm_json(r'{"q": "\left\{\begin{array}{l}4x\end{array}\right."}')
        assert r["q"] == r"\left\{\begin{array}{l}4x\end{array}\right."

    def test_latex_frac(self):
        r = parse_llm_json(r'{"q": "z=\frac{1}{2}"}')
        assert r["q"] == r"z=\frac{1}{2}"

    def test_latex_bar(self):
        r = parse_llm_json(r'{"q": "z\cdot\bar{z}"}')
        assert r["q"] == r"z\cdot\bar{z}"

    def test_latex_misc_commands(self):
        r = parse_llm_json(r'{"q": "\therefore x\neq 2 \times 3"}')
        assert r["q"] == r"\therefore x\neq 2 \times 3"

    def test_real_newline_preserved(self):
        # 真换行（\n + 非ASCII字母）保留为换行，不当 LaTeX
        r = parse_llm_json('{"q": "第一行\\n第二行"}')
        assert r["q"] == "第一行\n第二行"

    def test_normalized_backslash_idempotent(self):
        # 已规范的双反斜杠 LaTeX 不被破坏
        r = parse_llm_json(r'{"q": "z=\\frac{1}{2}"}')
        assert r["q"] == r"z=\frac{1}{2}"

    def test_latex_with_newline_mixed(self):
        r = parse_llm_json('{"q": "解:\\n$\\frac{1}{2}$\\n答案"}')
        assert r["q"] == "解:\n$\\frac{1}{2}$\n答案"

    def test_latex_in_array(self):
        r = parse_llm_json(r'[{"question": "\sqrt{x}", "answer": "\frac{1}{2}"}]')
        assert r[0]["question"] == r"\sqrt{x}"
        assert r[0]["answer"] == r"\frac{1}{2}"

    def test_latex_with_markdown_fence(self):
        r = parse_llm_json('```json\n{"q": "\\beta\\gamma"}\n```')
        assert r["q"] == r"\beta\gamma"

    # ── 换行后跟拉丁字母不得变成字面 \n ──
    def test_newline_before_option_letters_stays_a_line_break(self):
        r = parse_llm_json(r'{"q": "25\\frac{4}{5}^\\circ C\nB 4^\\circ C\nC 24^\\circ C"}')
        assert r["q"] == "25\\frac{4}{5}^\\circ C\nB 4^\\circ C\nC 24^\\circ C"

    def test_newline_before_variable_without_latex_stays_a_line_break(self):
        r = parse_llm_json(r'{"q": "x=1\ny=2\nA"}')
        assert r["q"] == "x=1\ny=2\nA"

    def test_escaped_latex_response_trusts_newline_before_command_like_words(self):
        # \nu \ne \ni would be commands, but this model escaped its LaTeX itself.
        r = parse_llm_json(r'{"q": "\\vec{a}=1\nu=2\ne^x>0\ni^2=-1\ttab"}')
        assert r["q"] == "\\vec{a}=1\nu=2\ne^x>0\ni^2=-1\ttab"

    def test_single_backslash_response_keeps_line_break_before_non_command(self):
        r = parse_llm_json(r'{"q": "\sqrt{2}\nB\ny=\frac{1}{2}\nx\neq 1"}')
        assert r["q"] == "\\sqrt{2}\nB\ny=\\frac{1}{2}\nx\\neq 1"

    def test_single_backslash_i_line_is_not_read_as_ni(self):
        r = parse_llm_json(r'{"q": "\sqrt{-1}\ni^2=-1"}')
        assert r["q"] == "\\sqrt{-1}\ni^2=-1"

    def test_u_commands_and_unicode_escapes(self):
        r = parse_llm_json(r'{"q": "\underline{x}\nB", "c": "\u4e2d\u6587"}')
        assert r["q"] == "\\underline{x}\nB"
        assert r["c"] == "中文"


def _flash_system_prompt(questions, detail_level):
    return build_text_messages(questions, detail_level)[0]["content"]


class TestTextGradingPolicy:
    def test_grading_prompts_do_not_request_semantic_error_type_output(self):
        question = {
            "question_no": "1", "question_text": "1+1=?",
            "student_answer": "3", "standard_answer": "2",
        }
        prompts = []
        for detail_level in ("simple", "detailed"):
            prompts.extend(message["content"] for message in build_text_messages([question], detail_level))
        assert all('"error_type"' not in prompt for prompt in prompts)
        assert all("计算/概念/审题/方法" not in prompt for prompt in prompts)

    def test_common_rules_include_confirmed_scoring_policy(self):
        rules = TEXT_GRADING_RULES

        # The approved semantic policy replaces overlapping hard-coded cases.
        assert "correct：满足要求且正确" in rules
        assert "partial：有可计分的正确部分，但有错误或遗漏" in rules
        assert "wrong：空答或无可计分的正确部分" in rules
        assert "题干和教师明确评分要求" in rules
        assert "多个所求答案分别判断" in rules
        assert "needs_review" not in rules

    def test_whole_paper_prompt_reuses_confirmed_policy(self):
        prompt = _flash_system_prompt([
            {"question_no": "1", "question_text": "题目", "standard_answer": "36元"},
        ], "simple")

        assert TEXT_GRADING_RULES in prompt
        assert "result只允许correct、partial、wrong" in prompt
        assert "needs_review" not in prompt

    def test_detailed_wrong_contract_allows_complete_error_sheet_solution(self):
        rules = TEXT_GRADING_RULES + "\n" + TEXT_GRADING_DETAIL_INSTRUCTIONS

        assert "不要直接给完整答案" not in rules
        assert "model_answer、standard_answer" in rules
        assert "简短提示、完整正确解答和最终答案" in rules

        prompt = _flash_system_prompt([
            {"question_no": "1", "question_text": "题目", "standard_answer": "2"},
        ], "detailed")
        assert "wrong/partial填写具体判分理由、简短提示、完整正确解答和最终答案" in prompt


class TestModelProxyRouting:
    @staticmethod
    def _response(content: str = "ok"):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": content}}]}

        return Response()

    def test_mimo_text_call_bypasses_inherited_proxy(self, monkeypatch):
        from services.model_concurrency import NoopModelLimiter

        captured = {}

        async def fake_post(**kwargs):
            captured.update(kwargs)
            return self._response()

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer, "_llm_pool", None)
        monkeypatch.setattr(analyzer, "get_model_limiter", lambda: NoopModelLimiter())
        monkeypatch.setenv("LLM_API_KEY", "llm-test-key")
        monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.invalid/v1")

        assert asyncio.run(analyzer._call_llm(
            [{"role": "user", "content": "ping"}],
            teacher_id="teacher-8",
        )) == "ok"
        assert captured["trust_env"] is False
        assert captured["shared_provider"] == analyzer.MIMO_SUBSCRIPTION_PROVIDER
        assert captured["teacher_id"] == "teacher-8"
        assert captured["global_capacity"] == analyzer.LLM_KEY_CONCURRENCY
        assert captured["credential_capacity"] == analyzer.LLM_KEY_CONCURRENCY

    def test_mimo_capacity_tracks_enabled_pool_credentials(self, monkeypatch):
        pool = KeyPool(
            ["disabled-key", "healthy-a", "healthy-b"],
            max_concurrent_per_key=4,
            base_urls=["https://mimo.test/v1"] * 3,
        )
        captured = {}

        class Limiter:
            async def is_credential_invalid(self, **credential):
                return credential["api_key"] == "disabled-key"

        async def fake_post(**kwargs):
            captured.update(kwargs)
            return self._response()

        monkeypatch.setattr(analyzer, "_llm_pool", pool)
        monkeypatch.setattr(analyzer, "get_model_limiter", lambda: Limiter())
        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)

        assert asyncio.run(analyzer._call_llm(
            [{"role": "user", "content": "ping"}],
            teacher_id="teacher-10",
        )) == "ok"
        assert pool.enabled_key_count() == 2
        assert captured["global_capacity"] == 2 * analyzer.LLM_KEY_CONCURRENCY
        assert captured["credential_capacity"] == analyzer.LLM_KEY_CONCURRENCY
        assert captured["teacher_id"] == "teacher-10"

    def test_mimo_auth_failover_recomputes_provider_capacity(self, monkeypatch):
        pool = KeyPool(
            ["bad-key", "healthy-a", "healthy-b"],
            max_concurrent_per_key=4,
            weights=[100, 1, 1],
            base_urls=["https://mimo.test/v1"] * 3,
        )
        capacities = []

        class Limiter:
            def __init__(self):
                self.invalid = set()

            async def is_credential_invalid(self, **credential):
                return credential["api_key"] in self.invalid

            async def mark_credential_invalid(self, **credential):
                self.invalid.add(credential["api_key"])

        limiter = Limiter()

        async def fake_post(**kwargs):
            capacities.append(kwargs["global_capacity"])
            request = httpx.Request("POST", kwargs["url"])
            if kwargs["api_key"] == "bad-key":
                return httpx.Response(401, request=request, text="unauthorized")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        monkeypatch.setattr(analyzer, "_llm_pool", pool)
        monkeypatch.setattr(analyzer, "get_model_limiter", lambda: limiter)
        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)

        assert asyncio.run(analyzer._call_llm(
            [{"role": "user", "content": "ping"}],
            teacher_id="teacher-11",
        )) == "ok"
        assert capacities == [
            3 * analyzer.LLM_KEY_CONCURRENCY,
            2 * analyzer.LLM_KEY_CONCURRENCY,
        ]
        assert limiter.invalid == {"bad-key"}
        assert pool.enabled_key_count() == 2


class TestModelCredentialFailover:
    @staticmethod
    def _call(pool):
        return analyzer._call_api(
            pool=pool,
            messages=[{"role": "user", "content": "ping"}],
            model="test-model",
            single_key_env="TEST_MODEL_API_KEY",
            single_base_env="TEST_MODEL_BASE_URL",
            single_key_default="single-key",
            single_base_default="https://example.invalid/v1",
        )

    def test_unauthorized_pool_key_switches_to_healthy_key(self, monkeypatch):
        pool = KeyPool(["bad-key", "good-key"], weights=[100, 1])
        seen = []

        async def fake_post(**kwargs):
            api_key = kwargs["api_key"]
            seen.append(api_key)
            request = httpx.Request("POST", kwargs["url"])
            if api_key == "bad-key":
                return httpx.Response(401, request=request, text="invalid token")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)

        assert asyncio.run(self._call(pool)) == "ok"
        assert seen == ["bad-key", "good-key"]
        assert pool.readiness()["enabled_keys"] == 1
        assert pool.status()[0]["invalid_status_code"] == 401

    def test_four_rejected_credentials_do_not_hide_the_fifth_healthy_key(
        self,
        monkeypatch,
    ):
        keys = ["bad-1", "bad-2", "bad-3", "bad-4", "good-5"]
        pool = KeyPool(keys)
        seen = []

        async def fake_post(**kwargs):
            api_key = kwargs["api_key"]
            seen.append(api_key)
            request = httpx.Request("POST", kwargs["url"])
            if api_key != "good-5":
                status = 401 if len(seen) % 2 else 403
                return httpx.Response(status, request=request, text="rejected")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)

        assert asyncio.run(self._call(pool)) == "ok"
        assert seen == keys
        assert pool.readiness()["enabled_keys"] == 1

    def test_rejected_credential_is_skipped_by_another_worker_pool(
        self,
        monkeypatch,
    ):
        class SharedHealth:
            def __init__(self):
                self.invalid = set()

            async def is_credential_invalid(self, **credential):
                return credential["api_key"] in self.invalid

            async def mark_credential_invalid(self, **credential):
                self.invalid.add(credential["api_key"])

        shared = SharedHealth()
        seen = []

        async def fake_post(**kwargs):
            api_key = kwargs["api_key"]
            seen.append(api_key)
            request = httpx.Request("POST", kwargs["url"])
            if api_key == "bad-key":
                return httpx.Response(401, request=request, text="rejected")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        async def call(pool):
            return await analyzer._call_api(
                pool=pool,
                messages=[{"role": "user", "content": "ping"}],
                model="test-model",
                single_key_env="TEST_MODEL_API_KEY",
                single_base_env="TEST_MODEL_BASE_URL",
                single_key_default="single-key",
                single_base_default="https://example.invalid/v1",
                shared_provider="mimo",
            )

        monkeypatch.setattr(analyzer, "get_model_limiter", lambda: shared)
        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)

        assert asyncio.run(call(KeyPool(["bad-key", "good-key"], weights=[100, 1]))) == "ok"
        assert seen == ["bad-key", "good-key"]
        seen.clear()
        second_worker_pool = KeyPool(["bad-key", "good-key"], weights=[100, 1])
        assert asyncio.run(call(second_worker_pool)) == "ok"
        assert seen == ["good-key"]
        assert second_worker_pool.enabled_key_count() == 1

    def test_429_cooldown_is_shared_before_switching_to_healthy_key(
        self,
        monkeypatch,
    ):
        class SharedHealth:
            def __init__(self):
                self.cooldowns = {}

            async def is_credential_invalid(self, **_credential):
                return False

            async def credential_cooldown_seconds(self, **credential):
                return self.cooldowns.get(credential["api_key"], 0.0)

            async def mark_credential_cooldown(self, **credential):
                self.cooldowns[credential["api_key"]] = credential[
                    "cooldown_seconds"
                ]

        shared = SharedHealth()
        pool = KeyPool(
            ["limited-key", "healthy-key"],
            max_concurrent_per_key=4,
            weights=[100, 1],
        )
        seen = []
        capacities = []

        async def fake_post(**kwargs):
            api_key = kwargs["api_key"]
            seen.append(api_key)
            capacities.append(kwargs["global_capacity"])
            request = httpx.Request("POST", kwargs["url"])
            if api_key == "limited-key":
                return httpx.Response(
                    429,
                    request=request,
                    headers={"retry-after": "3600"},
                )
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ok"}}]},
            )

        async def no_wait(_delay):
            return None

        async def call():
            return await analyzer._call_api(
                pool=pool,
                messages=[{"role": "user", "content": "ping"}],
                model="test-model",
                single_key_env="TEST_MODEL_API_KEY",
                single_base_env="TEST_MODEL_BASE_URL",
                single_key_default="single-key",
                single_base_default="https://example.invalid/v1",
                shared_provider="mimo",
                per_key_capacity=4,
            )

        monkeypatch.setattr(analyzer, "get_model_limiter", lambda: shared)
        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", no_wait)

        assert asyncio.run(call()) == "ok"
        assert seen == ["limited-key", "healthy-key"]
        assert capacities == [8, 4]
        assert shared.cooldowns == {"limited-key": 3600.0}
        assert pool.enabled_key_count() == 2
        assert pool.healthy_key_count() == 1
        assert all(item["in_flight"] == 0 for item in pool.status())

    def test_single_unauthorized_key_fails_without_retry_sleep(self, monkeypatch):
        calls = 0
        sleeps = 0

        async def fake_post(**kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", kwargs["url"])
            return httpx.Response(403, request=request, text="forbidden")

        async def unexpected_sleep(_delay):
            nonlocal sleeps
            sleeps += 1

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", unexpected_sleep)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(self._call(None))
        assert exc_info.value.response.status_code == 403
        assert calls == 1
        assert sleeps == 0

    def test_read_error_uses_transport_retry_budget(self, monkeypatch):
        calls = 0

        async def fake_post(**kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", kwargs["url"])
            if calls == 1:
                raise httpx.ReadError("connection dropped", request=request)
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "recovered"}}]},
            )

        async def no_wait(_delay):
            return None

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", no_wait)

        assert asyncio.run(self._call(None)) == "recovered"
        assert calls == 2

    def test_request_timeout_uses_http_retry_budget(self, monkeypatch):
        calls = 0
        pool = KeyPool(["test-key"])

        async def fake_post(**kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", kwargs["url"])
            if calls == 1:
                return httpx.Response(408, request=request, text="request timeout")
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "recovered"}}]},
            )

        async def no_wait(_delay):
            return None

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", no_wait)

        assert asyncio.run(self._call(pool)) == "recovered"
        assert calls == 2
        assert pool.status()[0]["in_flight"] == 0

    def test_invalid_success_envelope_uses_bounded_retry_budget(
        self,
        monkeypatch,
    ):
        calls = 0
        pool = KeyPool(["test-key"])

        async def fake_post(**kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", kwargs["url"])
            if calls < 2:
                return httpx.Response(200, request=request, json={"choices": []})
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "recovered"}}]},
            )

        async def no_wait(_delay):
            return None

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", no_wait)

        assert asyncio.run(self._call(pool)) == "recovered"
        assert calls == 2
        assert pool.status()[0]["in_flight"] == 0

    def test_key_pool_capacity_wait_has_a_finite_deadline(self, monkeypatch):
        pool = KeyPool(["busy-key"], max_concurrent_per_key=1)
        key, _, _ = pool.get_key()
        assert key == "busy-key"
        monkeypatch.setattr(
            analyzer,
            "MODEL_LIMITER_ACQUIRE_TIMEOUT_SECONDS",
            0.01,
        )

        try:
            with pytest.raises(TimeoutError, match="capacity wait timed out"):
                asyncio.run(self._call(pool))
        finally:
            pool.release(key)

    @pytest.mark.parametrize("status", [429, 503])
    def test_final_http_failure_preserves_error_without_extra_sleep(
        self,
        monkeypatch,
        status,
    ):
        calls = 0
        sleeps = []

        async def fake_post(**kwargs):
            nonlocal calls
            calls += 1
            request = httpx.Request("POST", kwargs["url"])
            return httpx.Response(
                status,
                request=request,
                text="provider failed",
                headers={"retry-after": "3600"},
            )

        async def record_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(analyzer, "_post_model_request", fake_post)
        monkeypatch.setattr(analyzer.asyncio, "sleep", record_sleep)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            asyncio.run(self._call(None))

        assert exc_info.value.response.status_code == status
        assert calls == 1
        assert sleeps == []
