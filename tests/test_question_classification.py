"""Regression tests for the durable, single-lane question classifier."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import database
import models
import models_correction
from database import Base
from services import analyzer
from services import question_classification as classification


@pytest.fixture
def classification_sessions(monkeypatch, tmp_path):
    """Give the worker a private SQLite database without touching app.db."""
    # Importing all model modules registers the foreign-key targets before
    # creating the isolated schema.
    import models_accelerator  # noqa: F401
    import models_correction  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'classification.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(classification, "SessionLocal", sessions)
    return sessions


def _add_questions(sessions, count: int = 1):
    db = sessions()
    rows = [models.QuestionBank(question=f"题目{index}", answer="") for index in range(count)]
    db.add_all(rows)
    db.commit()
    db.close()
    return rows


def _job(sessions, question_id: int):
    db = sessions()
    row = (
        db.query(models.QuestionClassificationJob)
        .filter(models.QuestionClassificationJob.question_id == question_id)
        .one()
    )
    db.expunge(row)
    db.close()
    return row


def test_claim_is_globally_single_lane_and_stale_result_is_ignored(
    classification_sessions,
):
    sessions = classification_sessions
    questions = _add_questions(sessions, 2)
    assert classification.reconcile_classification_jobs_once() == 2

    first = classification.claim_classification_batch("worker-a", batch_size=1)
    assert len(first) == 1
    assert classification.claim_classification_batch("worker-b", batch_size=1) == []

    # Keep the other question out of the way so the same job is reclaimed.
    db = sessions()
    other_job = (
        db.query(models.QuestionClassificationJob)
        .filter(models.QuestionClassificationJob.question_id == questions[1].id)
        .one()
    )
    other_job.next_attempt_at = classification._now() + timedelta(days=1)
    old_job = db.get(models.QuestionClassificationJob, first[0]["job_id"])
    old_job.status = classification.STATUS_PENDING
    old_job.worker_id = ""
    old_job.locked_until = None
    old_job.next_attempt_at = classification._now()
    db.commit()
    db.close()

    second = classification.claim_classification_batch("worker-b", batch_size=1)
    assert len(second) == 1
    assert second[0]["attempts"] == first[0]["attempts"] + 1

    classification.finish_classification_batch(
        first,
        [{"id": str(first[0]["question_id"]), "status": "suggested", "chapter": "错误", "topic": "错误"}],
    )
    db = sessions()
    question = db.get(models.QuestionBank, questions[0].id)
    assert question.chapter == ""
    assert question.topic == ""
    assert db.get(models.QuestionClassificationJob, second[0]["job_id"]).status == classification.STATUS_RUNNING
    db.close()


def test_retry_schedule_reaches_one_hour_then_manual(classification_sessions, monkeypatch):
    sessions = classification_sessions
    question = _add_questions(sessions)[0]
    classification.reconcile_classification_jobs_once()

    clock = [datetime.now()]
    monkeypatch.setattr(classification, "_now", lambda: clock[0])
    request = httpx.Request("POST", "https://classifier.invalid/responses")
    timeout = httpx.ReadTimeout("upstream timeout", request=request)
    expected_delays = (60, 300, 900, 1800, 3600)

    for expected in expected_delays:
        claimed = classification.claim_classification_batch("worker", batch_size=1)
        assert len(claimed) == 1
        classification.fail_classification_batch(claimed, timeout)
        row = _job(sessions, question.id)
        assert row.status == classification.STATUS_RETRY_WAITING
        assert row.next_attempt_at == clock[0] + timedelta(seconds=expected)
        clock[0] = row.next_attempt_at

    sixth = classification.claim_classification_batch("worker", batch_size=1)
    assert sixth[0]["attempts"] == classification.MAX_ATTEMPTS
    classification.fail_classification_batch(sixth, timeout)
    row = _job(sessions, question.id)
    assert row.status == classification.STATUS_MANUAL

    db = sessions()
    question_row = db.get(models.QuestionBank, question.id)
    assert question_row.classification_status == "failed"
    db.close()


def test_non_retryable_http_error_goes_to_manual(classification_sessions):
    sessions = classification_sessions
    question = _add_questions(sessions)[0]
    classification.reconcile_classification_jobs_once()
    claimed = classification.claim_classification_batch("worker", batch_size=1)
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://classifier.invalid/responses"),
    )
    error = httpx.HTTPStatusError("bad request", request=response.request, response=response)
    classification.fail_classification_batch(claimed, error)
    row = _job(sessions, question.id)
    assert row.status == classification.STATUS_MANUAL
    assert row.attempts == 1


def test_local_classifier_configuration_error_is_not_retried():
    error = analyzer.KnowledgeClassifierConfigurationError("配置无效")
    assert classification.is_retryable_classification_error(error) is False


@pytest.mark.parametrize(
    ("effort", "enabled", "wire_effort"),
    [("none", False, "none"), ("low", True, "low"),
     ("medium", True, "medium"), ("high", True, "xhigh"), ("xhigh", True, "xhigh")],
)
def test_flash_classifier_uses_exact_text_route_and_explicit_thinking(
    monkeypatch, effort, enabled, wire_effort,
):
    from services import flash_grading as flash

    calls = []
    defaults = dict(flash.FLASH_PARAMETERS)
    monkeypatch.setattr(analyzer, "KNOWLEDGE_CLASSIFIER_REASONING_EFFORT", effort)
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_MAX_TOKENS", "2048")
    # Legacy classifier overrides cannot re-route the request or select MiMo.
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_MODEL", "mimo-test-only")
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_API_PROTOCOL", "responses")
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_REASONING_EFFORT", "high")
    monkeypatch.setattr(flash, "get_flash_readiness", lambda: {"configured": True, "ready": True})

    async def forbidden_chat(*args, **kwargs):
        pytest.fail("Knowledge classification must never call the chat model")

    async def fake_flash(messages, **kwargs):
        payload = flash._payload(messages, kwargs["phase"], **{
            key: kwargs[key] for key in (
                "enable_thinking", "reasoning_effort", "max_tokens", "json_object",
            )
        })
        calls.append((messages, kwargs, payload))
        return {"content": '{"results": []}', "model": flash.FLASH_MODEL, "receipt": {}}

    monkeypatch.setattr(analyzer, "_call_llm", forbidden_chat)
    monkeypatch.setattr(flash, "call_flash", fake_flash)
    messages = [{"role": "system", "content": "JSON only"}, {"role": "user", "content": "题干"}]
    result = asyncio.run(analyzer._call_knowledge_classifier_text(messages))

    assert result == '{"results": []}'
    assert len(calls) == 1
    sent, kwargs, payload = calls[0]
    assert sent is messages
    assert kwargs == {
        "teacher_id": "question-classifier",
        "phase": "text",
        "json_object": True,
        "enable_thinking": enabled,
        "reasoning_effort": wire_effort,
        "max_tokens": 2048,
        "diagnostic_phase": "knowledge-classification",
    }
    assert payload["model"] == "qwen3.8-flash"
    assert payload["enable_thinking"] is enabled
    assert payload["reasoning_effort"] == wire_effort
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert payload["stream"] is True
    assert payload["messages"] == messages
    assert flash.FLASH_PARAMETERS == defaults


def test_classifier_validated_thinking_setting_is_explicit_low():
    import config

    assert config.KNOWLEDGE_CLASSIFIER_REASONING_EFFORT == "low"
    parameters = analyzer._knowledge_classifier_runtime_config()
    assert parameters["enable_thinking"] is True
    assert parameters["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("question_count", "configured_budget", "expected_budget"),
    [(1, 4096, 4096), (25, 2048, 2048 + 384 * 25),
     (100, 2048, 32768), (25, 40000, 40000)],
)
def test_flash_classifier_preserves_whole_request_token_allowance(
    monkeypatch, question_count, configured_budget, expected_budget,
):
    from services import flash_grading as flash

    budgets = []
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_MAX_TOKENS", str(configured_budget))
    monkeypatch.setattr(flash, "get_flash_readiness", lambda: {"configured": True, "ready": True})

    async def fake_flash(_messages, **kwargs):
        budgets.append(kwargs["max_tokens"])
        return {"content": '{"results": []}'}

    monkeypatch.setattr(flash, "call_flash", fake_flash)
    asyncio.run(analyzer._call_knowledge_classifier_text(
        [{"role": "user", "content": "题干"}], question_count=question_count,
    ))
    assert budgets == [expected_budget]


@pytest.mark.parametrize("budget", ["0", "-1", "nan", "not-a-number"])
def test_classifier_rejects_invalid_token_budget(monkeypatch, budget):
    monkeypatch.setenv("KNOWLEDGE_CLASSIFIER_MAX_TOKENS", budget)
    with pytest.raises(analyzer.KnowledgeClassifierConfigurationError):
        analyzer._knowledge_classifier_runtime_config()


@pytest.mark.parametrize("effort", ["", "minimal", "max", "invalid"])
def test_classifier_rejects_unsupported_reasoning(monkeypatch, effort):
    monkeypatch.setattr(analyzer, "KNOWLEDGE_CLASSIFIER_REASONING_EFFORT", effort)
    with pytest.raises(analyzer.KnowledgeClassifierConfigurationError, match="思考档位"):
        analyzer._knowledge_classifier_runtime_config()


@pytest.mark.parametrize("readiness", [
    {"configured": False, "ready": False, "reason": "credentials_not_configured"},
    {"configured": True, "ready": False, "reason": "endpoint_not_configured"},
])
def test_unconfigured_flash_is_a_local_error_without_any_request(monkeypatch, readiness):
    from services import flash_grading as flash

    monkeypatch.setattr(flash, "get_flash_readiness", lambda: readiness)

    async def forbidden(*args, **kwargs):
        pytest.fail("An unconfigured classifier must not send a request")

    monkeypatch.setattr(flash, "call_flash", forbidden)
    monkeypatch.setattr(analyzer, "_call_llm", forbidden)
    with pytest.raises(analyzer.KnowledgeClassifierConfigurationError, match="未配置"):
        asyncio.run(analyzer._call_knowledge_classifier_text([
            {"role": "user", "content": "题干"},
        ]))


def test_flash_error_is_not_retried_or_fallen_back_inside_classifier(monkeypatch):
    from services import flash_grading as flash

    calls = []
    failure = flash.FlashGradingError("Flash grading HTTP 429")
    failure.status_code = 429
    monkeypatch.setattr(flash, "get_flash_readiness", lambda: {"configured": True, "ready": True})

    async def fail_flash(*args, **kwargs):
        calls.append(kwargs)
        raise failure

    async def forbidden_chat(*args, **kwargs):
        pytest.fail("Flash failure must never fall back to the chat model")

    monkeypatch.setattr(flash, "call_flash", fail_flash)
    monkeypatch.setattr(analyzer, "_call_llm", forbidden_chat)
    with pytest.raises(flash.FlashGradingError) as caught:
        asyncio.run(analyzer._call_knowledge_classifier_text([
            {"role": "user", "content": "题干"},
        ]))
    assert caught.value is failure
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (403, False), (408, True), (429, True), (500, True)],
)
def test_flash_http_status_retains_worker_retry_semantics(status, retryable):
    from services.flash_grading import FlashGradingError

    error = FlashGradingError(f"Flash grading HTTP {status}")
    error.status_code = status
    assert classification.is_retryable_classification_error(error) is retryable


@pytest.mark.parametrize(
    ("enabled", "readiness", "expected"),
    [
        (True, {"configured": True, "ready": True}, True),
        (True, {"configured": False, "ready": False}, False),
        (True, {"configured": True, "ready": False}, False),
        (False, {"configured": True, "ready": True}, False),
    ],
)
def test_worker_readiness_uses_flash_not_mimo(monkeypatch, enabled, readiness, expected):
    from services import flash_grading as flash

    probes = []
    monkeypatch.setattr(classification, "KNOWLEDGE_CLASSIFIER_WORKER_ENABLED", enabled)
    # The chat model can be configured while Flash is not, or absent while Flash is ready.
    monkeypatch.setenv("LLM_BASE_URL", "" if expected else "https://chat.invalid")
    monkeypatch.setenv("LLM_API_KEY", "" if expected else "synthetic-not-a-key")

    def probe():
        probes.append(True)
        return readiness

    monkeypatch.setattr(flash, "get_flash_readiness", probe)
    assert classification.classifier_worker_configured() is expected
    assert len(probes) == int(enabled)


def test_worker_not_ready_leaves_durable_jobs_and_status_unchanged(
    classification_sessions, monkeypatch,
):
    from services import flash_grading as flash

    sessions = classification_sessions
    question = _add_questions(sessions)[0]
    classification.reconcile_classification_jobs_once()
    with sessions() as db:
        before = classification.get_classification_status(db)
    stop = asyncio.Event()

    def unavailable():
        stop.set()
        return {"configured": False, "ready": False}

    monkeypatch.setattr(classification, "KNOWLEDGE_CLASSIFIER_WORKER_ENABLED", True)
    monkeypatch.setattr(flash, "get_flash_readiness", unavailable)
    asyncio.run(classification.question_classification_worker_loop(stop))
    job = _job(sessions, question.id)
    assert job.status == classification.STATUS_PENDING
    assert job.attempts == 0
    with sessions() as db:
        assert classification.get_classification_status(db) == before


@pytest.mark.parametrize("stage", ["primary", "junior", "senior"])
def test_builder_is_pure_and_shared_by_production_with_full_stage_catalog(monkeypatch, stage):
    from copy import deepcopy
    from services.math_curriculum import get_math_catalog
    from services.teaching_context import make_teaching_context, stage_prompt, use_teaching_context

    questions = [
        {"id": " 1.2 ", "question": " 求结果 ", "image_url": "ignored-image", "answer": "ignored-answer"},
        {"question_id": "leaf-7", "text": "计算"},
        {"text": "说明理由"},
    ]
    original = deepcopy(questions)
    sent = []
    context = make_teaching_context(stage)
    with use_teaching_context(context):
        messages, ids, catalog, version = analyzer.build_knowledge_classification_messages(questions)
        assert sent == []
        assert questions == original
        assert ids == ["1.2", "leaf-7", "2"]
        assert catalog == get_math_catalog(stage)
        assert version == context["curriculum_version"]
        assert messages[0]["content"] == stage_prompt(
            "你是高中数学题库知识点分类器。使用 Qwen Flash 文本路由，"
            "只输出严格 JSON；知识点目录是唯一允许的分类集合。"
        )
        prompt = messages[-1]["content"]
        assert json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) in prompt
        assert all(isinstance(message["content"], str) for message in messages)
        assert "ignored-image" not in prompt and "ignored-answer" not in prompt
        assert "MiMo" not in str(messages)
        first_chapter = catalog[0]

        async def fake_call(actual_messages, **kwargs):
            sent.append((actual_messages, kwargs))
            return json.dumps({"results": [
                {"id": item_id, "chapter": first_chapter["chapter"], "topic": first_chapter["topics"][0]}
                for item_id in reversed(ids)
            ]})

        monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
        results = asyncio.run(analyzer.classify_questions_knowledge(questions))
    assert sent == [(messages, {"question_count": 3})]
    assert [item["id"] for item in results] == ids
    assert all(item["education_stage"] == stage and item["classification_version"] == version for item in results)
    assert questions == original


@pytest.mark.parametrize("questions", [
    [{"id": " ", "question": "题干"}],
    [{"id": "1", "question": "题干"}, {"question_id": " 1 ", "text": "另一题"}],
    [{"id": "1", "question": " "}],
    [{"id": "1", "image_url": "data:image/png;base64,c3ludGhldGlj"}],
    [{"id": "1", "question": [{"type": "image_url", "image_url": {"url": "test-image"}}]}],
    [{"id": "1", "question": {"image_url": "test-image"}}],
    ["not-an-object"],
])
def test_classifier_rejects_invalid_ids_or_non_text_before_calling(monkeypatch, questions):
    monkeypatch.setattr(analyzer, "_knowledge_catalog", lambda: (
        [{"chapter": "函数", "topics": ["定义域"]}], "catalog:test",
    ))

    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid inputs must be rejected before a model request")

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(analyzer.classify_questions_knowledge(questions))


def test_flash_text_boundary_rejects_images_before_transport(monkeypatch):
    from services import flash_grading as flash

    monkeypatch.setattr(flash, "get_flash_readiness", lambda: {"configured": True, "ready": True})

    def forbidden_route():
        pytest.fail("An image must be rejected before acquiring credentials or sending a request")

    monkeypatch.setattr(flash, "_route", forbidden_route)
    with pytest.raises(flash.FlashGradingError, match="cannot receive images"):
        asyncio.run(analyzer._call_knowledge_classifier_text([
            {"role": "user", "content": [
                {"type": "text", "text": "题干"},
                {"type": "image_url", "image_url": {"url": "test-image"}},
            ]},
        ]))


@pytest.mark.parametrize("returned_ids", [["1"], ["1", "1"], ["1", "9"], ["1", "3", "9"]])
def test_classifier_requires_exact_output_ids(monkeypatch, returned_ids):
    monkeypatch.setattr(analyzer, "_knowledge_catalog", lambda: (
        [{"chapter": "函数", "topics": ["定义域"]}], "catalog:test",
    ))

    async def fake_call(*args, **kwargs):
        return json.dumps({"results": [
            {"id": item_id, "chapter": "函数", "topic": "定义域"} for item_id in returned_ids
        ]})

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    with pytest.raises(ValueError, match="omitted|duplicate or unknown"):
        asyncio.run(analyzer.classify_questions_knowledge([
            {"id": "1", "question": "题干1"}, {"id": "3", "question": "题干3"},
        ]))


def test_builder_rejects_empty_catalog(monkeypatch):
    monkeypatch.setattr(analyzer, "_knowledge_catalog", lambda: ([], "empty"))
    with pytest.raises(RuntimeError, match="目录为空"):
        analyzer.build_knowledge_classification_messages([{"id": "1", "question": "题干"}])


def test_empty_classifier_input_skips_prompt_and_request(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Empty classification must not build a prompt")

    monkeypatch.setattr(analyzer, "build_knowledge_classification_messages", forbidden)
    assert asyncio.run(analyzer.classify_questions_knowledge([])) == []


def test_catalog_validation_rejects_unknown_pairs(monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "_knowledge_catalog",
        lambda: ([{"chapter": "函数", "topics": ["定义域"]}], "catalog:v1"),
    )

    async def fake_call(_messages):
        return '{"results":[{"id":"1","chapter":"函数","topic":"不存在","confidence":"high"}]}'

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    with pytest.raises(ValueError, match="outside the catalog"):
        asyncio.run(
            analyzer.classify_questions_knowledge([{"id": "1", "question": "求定义域"}])
        )


def test_per_question_mode_keeps_valid_rows_and_rejects_only_off_catalog(monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "_knowledge_catalog",
        lambda: ([{"chapter": "函数", "topics": ["定义域"]}], "catalog:v1"),
    )

    async def fake_call(*args, **kwargs):
        return json.dumps({"results": [
            {"id": "1", "chapter": "函数", "topic": "定义域"},
            {"id": "2", "chapter": "函数", "topic": "不存在"},
            {"id": "3", "knowledge_points": [
                {"chapter": "函数", "topic": "定义域"}, {"chapter": "函数", "topic": "定义域"},
            ]},
        ]}, ensure_ascii=False)

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    questions = [{"id": str(i), "question": f"题干{i}"} for i in (1, 2, 3)]
    with pytest.raises(ValueError, match="outside the catalog|exactly one point"):
        asyncio.run(analyzer.classify_questions_knowledge(questions))
    rows = asyncio.run(analyzer.classify_questions_knowledge(questions, per_question=True))

    assert [row["id"] for row in rows] == ["1", "2", "3"]
    assert rows[0]["status"] == "suggested" and rows[0]["topic"] == "定义域"
    assert [row["status"] for row in rows[1:]] == ["rejected", "rejected"]
    assert all("chapter" not in row and "topic" not in row for row in rows[1:])


def test_per_question_mode_still_rejects_malformed_ids(monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "_knowledge_catalog",
        lambda: ([{"chapter": "函数", "topics": ["定义域"]}], "catalog:v1"),
    )

    async def fake_call(*args, **kwargs):
        return '{"results":[{"id":"9","chapter":"函数","topic":"定义域"}]}'

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    with pytest.raises(ValueError, match="duplicate or unknown"):
        asyncio.run(analyzer.classify_questions_knowledge(
            [{"id": "1", "question": "求定义域"}], per_question=True,
        ))


def test_classifier_sends_full_catalog_and_accepts_exactly_one_point(monkeypatch):
    catalog = [
        {"chapter": "函数", "topics": ["定义域", "单调性"]},
        {"chapter": "平面向量", "topics": ["向量的模"]},
    ]
    captured = {}
    monkeypatch.setattr(
        analyzer,
        "_knowledge_catalog",
        lambda: (catalog, "catalog:v2"),
    )

    async def fake_call(messages):
        captured["prompt"] = messages[-1]["content"]
        return (
            '{"results":[{"id":"1","knowledge_points":['
            '{"chapter":"函数","topic":"定义域"}],"confidence":"high"}]}'
        )

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    result = asyncio.run(
        analyzer.classify_questions_knowledge([{"id": "1", "question": "求定义域"}])
    )

    assert "必须且只能选择一个" in captured["prompt"]
    assert json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) in captured["prompt"]
    assert result[0]["knowledge_points"] == [{"chapter": "函数", "topic": "定义域"}]


def test_catalog_validation_rejects_multiple_points(monkeypatch):
    monkeypatch.setattr(
        analyzer,
        "_knowledge_catalog",
        lambda: ([
            {"chapter": "函数", "topics": ["定义域", "单调性"]},
            {"chapter": "导数", "topics": ["导数的运算"]},
        ], "catalog:v2"),
    )

    async def fake_call(_messages):
        return (
            '{"results":[{"id":"1","knowledge_points":['
            '{"chapter":"函数","topic":"单调性"},'
            '{"chapter":"导数","topic":"导数的运算"},'
            '{"chapter":"函数","topic":"不存在"}],"confidence":"high"}]}'
        )

    monkeypatch.setattr(analyzer, "_call_knowledge_classifier_text", fake_call)
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(
            analyzer.classify_questions_knowledge([{"id": "1", "question": "求导判断单调性"}])
        )


def test_clearing_a_classification_requeues_job(classification_sessions):
    sessions = classification_sessions
    question = _add_questions(sessions)[0]
    db = sessions()
    question = db.get(models.QuestionBank, question.id)
    question.chapter = "函数"
    question.topic = "定义域"
    db.commit()
    classification.mark_question_classified(db, question)
    db.commit()
    question.chapter = ""
    question.topic = ""
    classification.queue_question_classification(db, question)
    db.commit()
    job = db.query(models.QuestionClassificationJob).filter_by(question_id=question.id).one()
    assert job.status == classification.STATUS_PENDING
    assert job.attempts == 0
    assert job.locked_until is None
    assert question.classification_status == classification.STATUS_PENDING
    db.close()


def test_placeholder_knowledge_is_reopened_and_queued(classification_sessions):
    sessions = classification_sessions
    db = sessions()
    question = models.QuestionBank(
        question="求函数定义域",
        answer="",
        chapter="未知",
        topic="综合",
        classification_status="confirmed",
        classification_source="legacy",
    )
    db.add(question)
    db.flush()
    old_job = models.QuestionClassificationJob(
        question_id=question.id,
        status=classification.STATUS_COMPLETED,
        attempts=1,
        next_attempt_at=datetime.now(),
        worker_id="",
        completed_at=datetime.now(),
    )
    db.add(old_job)
    db.commit()
    question_id = question.id
    db.close()

    classification.reconcile_classification_jobs_once()

    db = sessions()
    question = db.get(models.QuestionBank, question_id)
    job = db.query(models.QuestionClassificationJob).filter_by(question_id=question_id).one()
    assert question.classification_status == classification.STATUS_PENDING
    assert question.classification_source == ""
    assert job.status == classification.STATUS_PENDING
    assert job.attempts == 0
    from services.answer_import import get_unclassified_questions

    assert [row["id"] for row in get_unclassified_questions(db)] == [question_id]
    db.close()


def _add_historical_attempt(
    db,
    *,
    grading_result_id: int,
    student_id: int,
    fingerprint: str,
    question_no: str = "1",
    question_bank_id: int | None = None,
    chapter: str = "未知",
    topic: str = "未分类",
):
    attempt = models_correction.QuestionAttemptEvent(
        student_id=student_id,
        class_id=None,
        teacher_id=7,
        grading_result_id=grading_result_id,
        question_bank_id=question_bank_id,
        question_no=question_no,
        question_identity=f"fingerprint:{fingerprint}",
        identity_method="fingerprint",
        question_fingerprint=fingerprint,
        question_type="solve",
        question_snapshot_json=json.dumps({
            "question_no": question_no,
            "question_text": "求函数 f(x)=x+1 的定义域",
            "chapter": chapter,
            "topic": topic,
            "knowledge_point": topic,
        }, ensure_ascii=False),
        student_answer_snapshot_json="{}",
        final_result="wrong",
        analytics_included=True,
        inclusion_reason="included",
        source_category="daily",
        assessment_purpose="standard",
    )
    db.add(attempt)
    db.flush()
    return attempt


def test_historical_jobs_deduplicate_fingerprint_and_share_global_lane(
    classification_sessions,
):
    sessions = classification_sessions
    db = sessions()
    first_result = models.GradingResult(student_id=1, total_questions=1)
    second_result = models.GradingResult(student_id=2, total_questions=1)
    db.add_all([first_result, second_result])
    db.flush()
    fingerprint = "a" * 64
    _add_historical_attempt(
        db,
        grading_result_id=first_result.id,
        student_id=1,
        fingerprint=fingerprint,
    )
    _add_historical_attempt(
        db,
        grading_result_id=second_result.id,
        student_id=2,
        fingerprint=fingerprint,
    )
    bank_question = models.QuestionBank(question="另一道题", answer="")
    db.add(bank_question)
    db.commit()
    db.close()

    assert classification.reconcile_knowledge_event_classification_jobs_once() == 1
    classification.reconcile_classification_jobs_once()
    claimed = classification.claim_knowledge_event_classification("history-worker")
    assert len(claimed) == 1
    assert claimed[0]["question_fingerprint"] == fingerprint
    assert classification.claim_knowledge_event_classification("other-worker") == []
    assert classification.claim_classification_batch("bank-worker", batch_size=1) == []

    db = sessions()
    assert db.query(models_correction.KnowledgeEventClassificationJob).count() == 1
    db.close()


def test_historical_result_updates_matching_durable_projections(
    classification_sessions,
):
    sessions = classification_sessions
    db = sessions()
    student = models.Student(teacher_id=7, name="测试学生", class_name="高一1班")
    db.add(student)
    db.flush()
    question = models.QuestionBank(
        teacher_id=7,
        question="求函数 f(x)=x+1 的定义域",
        answer="R",
        chapter="未知",
        topic="综合",
    )
    db.add(question)
    db.flush()
    details = [
        {
            "question_no": "1",
            "question": question.question,
            "result": "wrong",
            "chapter": "未知",
            "topic": "未分类",
        },
        {
            "question_no": "2",
            "question": "圆的标准方程",
            "result": "wrong",
            "chapter": "平面解析几何",
            "topic": "圆的方程",
        },
    ]
    grading_result = models.GradingResult(student_id=student.id, total_questions=2)
    grading_result.set_details(details)
    db.add(grading_result)
    db.flush()
    profile = models.StudentProfile(
        student_id=student.id,
        grading_result_id=grading_result.id,
        total_questions=2,
    )
    profile.set_details(details)
    db.add(profile)
    first_event = models.KnowledgeEvent(
        student_id=student.id,
        teacher_id=7,
        grading_result_id=grading_result.id,
        question_bank_id=question.id,
        chapter="未知",
        topic="未分类",
        result="wrong",
        error_type="概念",
    )
    second_event = models.KnowledgeEvent(
        student_id=student.id,
        teacher_id=7,
        grading_result_id=grading_result.id,
        chapter="平面解析几何",
        topic="圆的方程",
        result="wrong",
        error_type="计算",
    )
    db.add_all([first_event, second_event])
    db.flush()
    first_attempt = _add_historical_attempt(
        db,
        grading_result_id=grading_result.id,
        student_id=student.id,
        fingerprint="b" * 64,
        question_no="1",
        question_bank_id=question.id,
    )
    _add_historical_attempt(
        db,
        grading_result_id=grading_result.id,
        student_id=student.id,
        fingerprint="c" * 64,
        question_no="2",
        chapter="平面解析几何",
        topic="圆的方程",
    )
    db.commit()
    attempt_id = first_attempt.id
    result_id = grading_result.id
    profile_id = profile.id
    first_event_id = first_event.id
    second_event_id = second_event.id
    question_id = question.id
    db.close()

    classification.reconcile_knowledge_event_classification_jobs_once()
    claimed = classification.claim_knowledge_event_classification("worker")
    assert len(claimed) == 1
    classification.finish_knowledge_event_classification(
        claimed,
        [{
            "id": str(attempt_id),
            "status": "suggested",
            "chapter": "函数",
            "topic": "函数的定义域",
            "confidence": "high",
            "classification_version": "catalog:test",
        }],
    )

    db = sessions()
    attempt = db.get(models_correction.QuestionAttemptEvent, attempt_id)
    assert attempt.question_snapshot()["topic"] == "函数的定义域"
    stored_details = db.get(models.GradingResult, result_id).get_details()
    assert stored_details[0]["topic"] == "函数的定义域"
    assert stored_details[1]["topic"] == "圆的方程"
    profile_details = db.get(models.StudentProfile, profile_id).get_details()
    assert profile_details == stored_details
    assert db.get(models.KnowledgeEvent, first_event_id).topic == "函数的定义域"
    assert db.get(models.KnowledgeEvent, second_event_id).topic == "圆的方程"
    stored_question = db.get(models.QuestionBank, question_id)
    assert stored_question.topic == "函数的定义域"
    assert stored_question.classification_status == "suggested"
    history_job = db.query(
        models_correction.KnowledgeEventClassificationJob
    ).filter_by(question_fingerprint="b" * 64).one()
    assert history_job.status == classification.STATUS_COMPLETED
    status = classification.get_classification_status(db, teacher_id=7)
    assert status["historical"]["completed"] >= 1
    assert status["historical"]["unclassified_attempts"] == 0
    db.close()


def test_question_bank_result_completes_matching_historical_job(
    classification_sessions,
):
    sessions = classification_sessions
    db = sessions()
    question = models.QuestionBank(
        teacher_id=7,
        question="求函数定义域",
        answer="R",
    )
    grading_result = models.GradingResult(student_id=1, total_questions=1)
    grading_result.set_details([{
        "question_no": "1",
        "question": question.question,
        "result": "wrong",
        "chapter": "未知",
        "topic": "未分类",
    }])
    db.add_all([question, grading_result])
    db.flush()
    attempt = _add_historical_attempt(
        db,
        grading_result_id=grading_result.id,
        student_id=1,
        fingerprint="d" * 64,
        question_bank_id=question.id,
    )
    db.commit()
    attempt_id = attempt.id
    question_id = question.id
    db.close()

    classification.reconcile_classification_jobs_once()
    classification.reconcile_knowledge_event_classification_jobs_once()
    db = sessions()
    history_job = db.query(
        models_correction.KnowledgeEventClassificationJob
    ).filter_by(question_fingerprint="d" * 64).one()
    history_job.status = classification.STATUS_MANUAL
    history_job.completed_at = datetime.now()
    db.commit()
    db.close()
    claimed = classification.claim_classification_batch("bank-worker", batch_size=1)
    assert len(claimed) == 1
    classification.finish_classification_batch(
        claimed,
        [{
            "id": str(question_id),
            "status": "suggested",
            "chapter": "函数",
            "topic": "函数的定义域",
            "confidence": "high",
            "classification_version": "catalog:test",
        }],
    )

    db = sessions()
    assert db.get(
        models_correction.QuestionAttemptEvent,
        attempt_id,
    ).question_snapshot()["topic"] == "函数的定义域"
    history_job = db.query(
        models_correction.KnowledgeEventClassificationJob
    ).filter_by(question_fingerprint="d" * 64).one()
    assert history_job.status == classification.STATUS_COMPLETED
    db.close()


def test_legacy_question_bank_migration_adds_pending_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE question_bank (id INTEGER PRIMARY KEY, chapter VARCHAR(100), topic VARCHAR(100))"
        )
        connection.exec_driver_sql(
            "INSERT INTO question_bank(id, chapter, topic) VALUES "
            "(1, '函数', '定义域'), (2, '', ''), (3, '未知', '综合')"
        )
        database._migrate_question_bank_classification(connection.connection.cursor())

    columns = {column["name"] for column in inspect(engine).get_columns("question_bank")}
    assert {"classification_status", "classification_source", "classified_at"} <= columns
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, classification_status, classification_source FROM question_bank ORDER BY id"
        ).all()
    assert rows == [
        (1, "confirmed", "legacy"),
        (2, "pending", ""),
        (3, "pending", ""),
    ]
