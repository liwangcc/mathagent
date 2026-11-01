"""Tests for services/paper_cache.py: answer-only recognition of student pages
against a teacher-confirmed template."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from services.paper_cache import (
    PaperCache,
    normalize_questions,
    question_number_signature,
    _enforce_question_type_answer_constraints,
    _normalize_question_number,
    _question_number_sort_key,
    RECOGNIZE_PAGE_AND_ANSWERS_PROMPT,
    RECOGNIZE_ANSWERS_ONLY_PROMPT,
    RECOGNIZE_EVIDENCE_ONLY_PROMPT,
)


def test_local_evidence_marks_only_incomplete_answer_regions_unreadable(
    monkeypatch, tmp_path
):
    from services.accelerator_business import AcceleratorEvidence
    from services.template_evidence import EvidenceResult

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "x=____",
            "question_type": "fill",
        }],
    }])
    evidence_path = tmp_path / "evidence-deadbeefdeadbeefdeadbeefdeadbeef.png"
    evidence_path.write_bytes(b"evidence")
    evidence = EvidenceResult(
        page_type_id="page_1",
        evidence_path=str(evidence_path),
        question_numbers=["1"],
        blank_question_numbers=[],
        diagnostics={
            "ownership": "confirmed_bbox_neighbor_clamped",
            "answer_source_complete": False,
            "incomplete_answer_regions": ["1"],
            "accelerator": {
                "evidence_authority": "trusted_client_evidence",
                "server_source_transform_replayed": False,
            },
        },
    )

    def must_not_match(*_args, **_kwargs):
        raise AssertionError("server template matcher must not run")

    async def fake_accelerator(**_kwargs):
        return AcceleratorEvidence(evidence, state="ready")

    cache._template_evidence_matcher = SimpleNamespace(match_and_build=must_not_match)
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"

    captured = {}

    async def fake_vision(*_args, **kwargs):
        captured.update(kwargs)
        return {"questions": [{
            "question_number": "1",
            "student_answer": "[无法识别]",
            "answer_state": "unreadable",
        }]}

    monkeypatch.setattr(cache, "_recognize_answers_only", fake_vision)
    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence",
        fake_accelerator,
    )

    result = asyncio.run(cache._recognize_with_local_evidence(
        "paper.png",
        teacher_id="7",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert "error" not in result
    assert captured["program_unreadable_qns"] == {"1"}
    assert not evidence_path.exists()


@pytest.mark.parametrize("mode", ["blank", "marked"])
def test_drawing_baseline_is_original_hash_bound_template(tmp_path, mode):
    import hashlib
    import numpy as np
    from PIL import Image
    path = tmp_path / "template.png"
    Image.new("RGB", (20, 30), (111, 111, 111)).save(path)
    cache = PaperCache()
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_artifacts = {"page_1": {
        "template_mode": mode, "template_image": "template.png",
        "difference_template_image": "whiteout-must-not-be-used.png",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "width": 20, "height": 30,
    }}
    # A marked page is loaded the same way; the gate uses it only as a
    # printed-content mask, never as the standard figure.
    actual_mode, page = cache._drawing_template_context("page_1")
    assert actual_mode == mode
    assert page.shape == (30, 20, 3)
    assert np.all(page == 111)
    page[:] = 0
    assert np.all(cache._drawing_template_context("page_1")[1] == 111)


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("failure", ["missing", "hash", "dimensions", "escape", "absolute"])
def test_invalid_template_baseline_is_unavailable_not_whiteout(tmp_path, failure, mode):
    import hashlib
    from PIL import Image
    path = tmp_path / "template.png"
    Image.new("RGB", (20, 30), "white").save(path)
    artifact = {"template_mode": mode, "template_image": "template.png",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "width": 20, "height": 30}
    if failure == "missing":
        artifact["template_image"] = "missing.png"
    elif failure == "hash":
        artifact["sha256"] = "0" * 64
    elif failure == "dimensions":
        artifact["width"] = 21
    elif failure == "escape":
        artifact["template_image"] = "../template.png"
    else:
        artifact["template_image"] = str(path)
    cache = PaperCache()
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_artifacts = {"page_1": artifact}
    assert cache._drawing_template_context("page_1") == (mode, None)


@pytest.mark.parametrize("mode", ["blank", "marked"])
@pytest.mark.parametrize("answer_kind", ["non_graphic", "drawing", "mixed"])
def test_paper_cache_passes_drawing_mode_only_for_graphics(monkeypatch, tmp_path, answer_kind, mode):
    from PIL import Image
    from services.accelerator_business import AcceleratorEvidence
    from services.template_evidence import EvidenceResult
    cache = PaperCache.from_confirmed_template_pages([{"page_index": 0, "questions": [{
        "question_no": "1", "question_type": "solve", "question_text": "演示",
        "answer_kind": answer_kind,
        "drawing_regions": [[100, 200, 800, 900]] if answer_kind != "non_graphic" else [],
    }]}])
    path = tmp_path / "evidence-deadbeefdeadbeefdeadbeefdeadbeef.png"
    Image.new("RGB", (100, 200), "black").save(path)
    evidence = EvidenceResult(page_type_id="page_1", evidence_path=str(path), question_numbers=["1"],
        blank_question_numbers=[], diagnostics={"answer_source_complete": True,
            "template_width": 100, "template_height": 200, "evidence_columns": [{}],
            "accelerator": {"evidence_authority": "trusted_client_evidence", "server_source_transform_replayed": False}})
    cache._template_evidence_matcher = SimpleNamespace()
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-test"
    async def accelerator(**_kwargs):
        return AcceleratorEvidence(evidence, state="ready")
    async def ocr(*_args, **_kwargs):
        return {"questions": [{"question_number": "1", "student_answer": "测试"}]}
    called = {}
    sentinel = object()
    def baseline(page_type_id):
        assert answer_kind != "non_graphic", "ordinary questions must not load extra image"
        called["page"] = page_type_id
        return mode, sentinel
    async def assess(**kwargs):
        called.update(kwargs)
        return {"1": {"decision": "wrong"}} if answer_kind != "non_graphic" else {}
    monkeypatch.setattr("services.accelerator_business.try_accelerated_evidence", accelerator)
    monkeypatch.setattr(cache, "_recognize_answers_only", ocr)
    monkeypatch.setattr(cache, "_drawing_template_context", baseline)
    monkeypatch.setattr("services.drawing_grading.assess_page", assess)
    result = asyncio.run(cache._recognize_with_local_evidence("paper.png", teacher_id="7", batch_id="b", paper_id="p"))
    assert "error" not in result
    graphic = answer_kind != "non_graphic"
    assert called["template_mode"] == (mode if graphic else "marked")
    assert called["blank_template"] is (sentinel if graphic and mode == "blank" else None)
    assert called["marked_template"] is (sentinel if graphic and mode == "marked" else None)
    if answer_kind != "non_graphic":
        assert result["_drawing_assessments"]["1"]["decision"] == "wrong"


def test_focus_sheet_authority_enters_ocr_without_server_rejection(
    monkeypatch, tmp_path
):
    from services.accelerator_business import AcceleratorEvidence
    from services.template_evidence import EvidenceResult

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "x=____",
            "question_type": "fill",
        }],
    }])
    evidence_path = tmp_path / "evidence_01.png"
    evidence_path.write_bytes(b"focus-sheet")
    evidence = EvidenceResult(
        page_type_id="page_1",
        evidence_path=str(evidence_path),
        evidence_paths=(str(evidence_path),),
        question_numbers=["1"],
        blank_question_numbers=[],
        diagnostics={
            "ownership": "confirmed_bbox_focus_sheets",
            "answer_source_complete": True,
            "evidence_sheets": [{
                "sheet_id": "sheet_01",
                "regions": [{
                    "question_number": "1",
                    "bbox_1000": [100, 100, 900, 900],
                }],
            }],
            "evidence_locations": [{
                "question_number": "1",
                "image_index": 1,
                "answer_bbox_1000": [100, 100, 900, 900],
            }],
            "accelerator": {
                "evidence_authority": "trusted_client_evidence_focus_sheets",
                "server_source_transform_replayed": False,
            },
        },
    )
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"

    async def fake_accelerator(**_kwargs):
        return AcceleratorEvidence(evidence, state="ready")

    captured = {}

    async def fake_vision(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return {
            "questions": [{
                "question_number": "1",
                "student_answer": "2",
                "answer_state": "visible",
            }],
        }

    monkeypatch.setattr(cache, "_recognize_answers_only", fake_vision)
    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence",
        fake_accelerator,
    )

    result = asyncio.run(cache._recognize_with_local_evidence(
        "paper.png",
        teacher_id="7",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    assert result.get("_ocr_source") == "client-evidence-multi-v3", result
    assert captured["kwargs"]["evidence_strategy"] == "confirmed_bbox_focus_sheets"
    assert captured["kwargs"]["evidence_paths"] == (str(evidence_path),)
    assert evidence_path.exists()


@pytest.mark.parametrize("bound_client, quality_validated", [
    (False, True), (True, True), (True, False),
])
def test_server_rendered_full_column_authority_enters_ocr_without_rejection(
    monkeypatch, tmp_path, bound_client, quality_validated
):
    from services.accelerator_business import AcceleratorEvidence
    from services.template_evidence import EvidenceResult

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "x=____",
            "question_type": "fill",
        }],
    }])
    evidence_path = tmp_path / "evidence_01.png"
    evidence_path.write_bytes(b"full-column")
    evidence = EvidenceResult(
        page_type_id="page_1",
        evidence_path=str(evidence_path),
        evidence_paths=(str(evidence_path),),
        question_numbers=["1"],
        blank_question_numbers=[],
        diagnostics={
            "ownership": "full_column_difference",
            "answer_source_complete": True,
            "evidence_sheets": [{
                "sheet_id": "column_01",
                "question_numbers": ["1"],
            }],
            "accelerator": {
                "evidence_authority": (
                    "bound_client_full_columns" if bound_client
                    else "server_rendered_full_columns"
                ),
                "server_source_transform_replayed": not bound_client,
                "server_evidence_rendered": not bound_client,
                "bound_node_identity_validated": bound_client,
                "local_quality_report_validated": quality_validated,
            },
        },
    )
    cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
    cache._accelerator_template_dir = str(tmp_path)
    cache._accelerator_template_id = "template-1"

    captured = {}

    async def fake_accelerator(**_kwargs):
        return AcceleratorEvidence(evidence, state="ready")

    async def fake_vision(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "questions": [{
                "question_number": "1",
                "student_answer": "2",
                "answer_state": "visible",
            }],
        }

    monkeypatch.setattr(cache, "_recognize_answers_only", fake_vision)
    monkeypatch.setattr(
        "services.accelerator_business.try_accelerated_evidence",
        fake_accelerator,
    )

    result = asyncio.run(cache._recognize_with_local_evidence(
        "paper.png",
        teacher_id="7",
        batch_id="batch-1",
        paper_id="paper-1",
    ))

    if bound_client and not quality_validated:
        assert result["_accelerator_reason_code"] == "accelerator_evidence_contract_invalid"
        assert not captured
        return
    assert result.get("_ocr_source") == "client-evidence-columns-v1", result
    assert captured["evidence_strategy"] == "full_column_difference"
    assert captured["evidence_paths"] == (str(evidence_path),)
    assert evidence_path.exists()


def test_server_cpu_evidence_without_accelerator_context_enters_ocr(
    monkeypatch, tmp_path
):
    from services.template_evidence import EvidenceResult

    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "x=____",
            "question_type": "fill",
        }],
    }])
    paper_path = tmp_path / "paper.png"
    evidence_path = tmp_path / "evidence_01.png"

    def fake_matcher(_image_path, output_dir, *, include_evidence):
        assert include_evidence is True
        evidence = tmp_path / "evidence_01.png"
        evidence.write_bytes(b"server-rendered-full-column")
        return EvidenceResult(
            page_type_id="page_1",
            evidence_path=str(evidence),
            evidence_paths=(str(evidence),),
            question_numbers=["1"],
            blank_question_numbers=[],
            diagnostics={
                "ownership": "full_column_difference",
                "answer_source_complete": True,
            },
        )

    captured = {}

    async def fake_vision(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "questions": [{
                "question_number": "1",
                "student_answer": "2",
                "answer_state": "visible",
            }],
        }

    cache._template_evidence_matcher = SimpleNamespace(
        match_and_build=fake_matcher,
    )
    monkeypatch.setattr(cache, "_recognize_answers_only", fake_vision)

    result = asyncio.run(cache._recognize_with_local_evidence(str(paper_path)))

    assert "error" not in result, result
    assert result["_server_template_evidence"] is True
    assert result["_ocr_source"] == "client-evidence-columns-v1"
    assert captured["evidence_strategy"] == "full_column_difference"
    assert captured["evidence_paths"] == (str(evidence_path),)


def make_ocr(qns: list[str], texts: dict[str, str] | None = None) -> dict:
    """Helper: create fake OCR result from question numbers.

    texts: optional {question_number: question_text} override.
    """
    texts = texts or {}
    return {
        "questions": [
            {
                "question_number": q,
                "question_text": texts.get(q, f"Question {q}"),
                "student_answer": "",
                "answer_state": "blank",
            }
            for q in qns
        ]
    }


def test_confirmed_template_pages_create_frozen_answers_only_cache():
    cache = PaperCache.from_confirmed_template_pages([
        {
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "x+1=2",
                "question_type": "fill",
            }],
        },
        {
            "page_index": 1,
            "questions": [{
                "question_no": "2",
                "question_text": "求 x",
                "question_type": "solve",
            }],
        },
    ])

    assert sorted(cache._baselines) == ["page_1", "page_2"]
    page_2 = cache._baselines["page_2"]["ocr_result"]["questions"]
    assert [q["question_number"] for q in page_2] == ["2"]
    assert page_2[0]["question_text"] == "求 x"


def test_confirmed_template_preserves_choice_mode_in_student_ocr_prompt():
    cache = PaperCache.from_confirmed_template_pages([{
        "page_index": 0,
        "questions": [{
            "question_no": "1",
            "question_text": "多选题",
            "question_type": "choice",
            "choice_mode": "multiple",
        }],
    }])

    prompt = cache._build_answer_only_prompt("page_1")

    assert '"1":"choice:multiple"' in prompt
    assert cache._baselines["page_1"]["ocr_result"]["questions"][0]["choice_mode"] == "multiple"


# ═══════════════════════════════════════════════════════
# 题号归一化测试
# ═══════════════════════════════════════════════════════

class TestNormalizeQuestionNumber:
    def test_basic(self):
        assert _normalize_question_number("1") == "1"
        assert _normalize_question_number("2.") == "2"
        assert _normalize_question_number(" 3 ") == "3"

    def test_chinese_prefix(self):
        assert _normalize_question_number("第1题") == "1"
        assert _normalize_question_number("第 2 题") == "2"

    def test_sub_question(self):
        assert _normalize_question_number("1(1)") == "1(1)"
        assert _normalize_question_number("2(2)") == "2(2)"


class TestQuestionNumberSortKey:
    def test_numeric_order(self):
        qns = ["10", "2", "1", "11"]
        assert sorted(qns, key=_question_number_sort_key) == ["1", "2", "10", "11"]

    def test_sub_question_order(self):
        qns = ["1(10)", "1(2)", "1(1)", "2"]
        assert sorted(qns, key=_question_number_sort_key) == ["1(1)", "1(2)", "1(10)", "2"]


class TestNormalizeQuestions:
    def test_basic(self):
        qs = [{"question_number": "第1题"}, {"question_number": "2."}]
        normalize_questions(qs)
        assert qs[0]["question_number"] == "1"
        assert qs[1]["question_number"] == "2"


def test_question_number_signature_is_stable():
    # Part of each template page and so of the confirmed contract hash.
    assert question_number_signature(["2.", " 1 ", "第3题", "", "1"]) == {
        "qn_set": ["1", "2", "3"],
        "qn_hash": "02b12e930c8b",
        "qn_count": 3,
    }
    assert question_number_signature([]) == {
        "qn_set": [], "qn_hash": "d41d8cd98f00", "qn_count": 0,
    }


# ═══════════════════════════════════════════════════════
# PaperCache 测试
# ═══════════════════════════════════════════════════════

class TestPaperCache:
    def test_build_answer_only_prompt(self):
        cache = PaperCache()
        cache._baselines["test"] = {
            "ocr_result": make_ocr(
                ["1", "2", "3"],
                {"1": "已知 M={a, 2a-1}"},
            ),
        }
        prompt = cache._build_answer_only_prompt("test")
        assert '题号清单=["1", "2", "3"]' in prompt
        assert "已知 M={a, 2a-1}" not in prompt
        assert "Question 2" not in prompt
        assert "Question 3" not in prompt
        assert "OCR" in prompt
        assert "只抄图片中可见笔迹" in prompt
        assert "空白为 blank" in prompt
        assert "answer_state" in prompt
        assert "不解题、不判分" in prompt
        assert '作答类型={"1":"solve","2":"solve","3":"solve"}' in prompt
        assert "严格只返回 JSON" in prompt
        assert "已确认分栏规则" in prompt
        assert "一张完整页面" in prompt
        assert "蓝色 Q 题号顺序" in prompt
        assert "题干、标准答案和数学知识不能作为转录依据" in prompt
        assert "MiMo" not in prompt
        assert "Qwen" not in prompt
        assert "DeepSeek" not in prompt
        assert "{page_type_id}" not in prompt
        assert "{question_numbers}" not in prompt
        assert "{question_schema}" not in prompt

    def test_anonymous_full_page_prompts_omit_identity_contract(self):
        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
            }],
        }])

        for prompt in (
            cache._build_answer_only_prompt("page_1"),
            cache._build_page_and_answers_prompt(),
        ):
            assert "忽略试卷上栏和侧边栏" in prompt
            assert '"student_name"' not in prompt
            assert '"student_no"' not in prompt

    def test_alignment_verification_prompt_reanchors_each_printed_question(self):
        cache = PaperCache()
        cache._baselines["test"] = {
            "ocr_result": make_ocr(["1", "2", "3"]),
        }

        prompt = cache._build_answer_only_prompt(
            "test",
            alignment_verification=1,
        )
        tiebreak_prompt = cache._build_answer_only_prompt(
            "test",
            alignment_verification=2,
        )

        assert "第 1 次独立" in prompt
        assert "第 2 次独立" in tiebreak_prompt
        assert prompt != tiebreak_prompt
        assert "独立的题号归属复核" in prompt
        assert "不得沿用任何先前识别结果" in prompt
        assert "找到该题印刷题号" in prompt
        assert "禁止因某题空白而把后续非空答案依次前移或后移" in prompt

    def test_joint_prompt_freezes_compact_contract_without_standard_answers(self):
        pages = [
            {
                "page_index": 0,
                "questions": [
                    {
                        "question_no": "1",
                        "question_text": "secret stem",
                        "standard_answer": "secret answer",
                        "question_type": "choice",
                        "choice_mode": "multiple",
                    },
                    {
                        "question_no": "2",
                        "question_text": "fill stem",
                        "question_type": "fill",
                        "blank_count": 2,
                    },
                ],
            },
            {
                "page_index": 1,
                "questions": [{
                    "question_no": "20",
                    "question_text": "other stem",
                    "question_type": "solve",
                }],
            },
        ]
        cache = PaperCache.from_confirmed_template_pages(pages)
        reordered_cache = PaperCache.from_confirmed_template_pages(
            list(reversed(copy.deepcopy(pages)))
        )

        prompt = cache._build_page_and_answers_prompt()

        assert prompt == cache._build_page_and_answers_prompt()
        assert prompt == reordered_cache._build_page_and_answers_prompt()
        assert "frozen_page_types=" in prompt
        assert '"question_numbers":["1","2"]' in prompt
        assert '"choice_mode":"multiple"' in prompt
        assert '"blank_count":2' in prompt
        assert "secret stem" not in prompt
        assert "secret answer" not in prompt
        assert "standard_answer" not in prompt
        assert "根号、分数" in prompt
        assert "page_type_id" in RECOGNIZE_PAGE_AND_ANSWERS_PROMPT
        assert "按从左到右合起来仍是同一整页" in RECOGNIZE_PAGE_AND_ANSWERS_PROMPT
        assert "模板差分后的学生卷" in RECOGNIZE_EVIDENCE_ONLY_PROMPT
        assert "不完整题干" in RECOGNIZE_EVIDENCE_ONLY_PROMPT
        assert "蓝色 Q 题号标签" in RECOGNIZE_EVIDENCE_ONLY_PROMPT

    def test_build_answer_only_prompt_missing(self):
        cache = PaperCache()
        prompt = cache._build_answer_only_prompt("nonexistent")
        assert prompt == ""


# ═══════════════════════════════════════════════════════
# 答案转录：题干来自已确认模板，答案来自本次识别
# ═══════════════════════════════════════════════════════

class TestAnswerOnlyRecognition:
    """Stems come from the template, answers from one Vision request."""

    def _ready_cache(self) -> PaperCache:
        """A one-page template with questions 1/2/3 and stable stems."""
        return PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {"question_no": number, "question_text": f"完整题干{number}", "question_type": "solve"}
                for number in ("1", "2", "3")
            ],
        }])

    def test_answer_ocr_failure_metadata_survives_frozen_fail_closed(self):
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
            }],
        }])

        timeout = httpx.ReadTimeout(
            "vision timeout",
            request=httpx.Request("POST", "https://vision.invalid"),
        )
        with patch(
            "services.ocr_service._prepare_vision_image",
            new=AsyncMock(return_value="paper.png"),
        ), patch(
            "services.ocr_service.call_vision",
            new=AsyncMock(side_effect=timeout),
        ):
            result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["questions"] == []
        assert "停止自动批改" in result["error"]
        assert result["_ocr_retryable"] is True
        assert result["_ocr_error_code"] == "provider_timeout"

    @staticmethod
    def _constrained(raw, *, question_type="choice", choice_mode="single") -> dict:
        question = {
            "question_number": "11", "question_type": question_type,
            "choice_mode": choice_mode, "student_answer": raw, "answer_state": "visible",
        }
        _enforce_question_type_answer_constraints([question])
        return question

    def test_choice_13_transcription_is_read_as_b(self):
        """选择题中 B 被读成 13 时按 B 批阅，同时保留原始值。"""
        question = self._constrained("13")
        assert question["student_answer"] == "B"
        assert question["answer_state"] == "visible"
        assert question["_ocr_raw_student_answer"] == "13"
        assert question["_ocr_normalization_reason"] == "choice_ocr_confusion_13_to_B"

    def test_other_numeric_choice_transcription_stays_unreadable(self):
        """只有明确的 13 规则化；其他数字仍要求复核。"""
        question = self._constrained("12")
        assert question["student_answer"] == "[无法识别]"
        assert question["answer_state"] == "unreadable"
        assert question["_ocr_raw_student_answer"] == "12"

    @pytest.mark.parametrize("raw,mode,expected", [
        ("C)", "single", "C"), ("（C", "single", "C"), ("(c)", "single", "C"),
        ("(AC)", "multiple", "AC"), ("C?)", "single", "[无法识别]"),
        ("(CC)", "multiple", "[无法识别]"), ("(AC)", "single", "[无法识别]"),
        ("不是C)", "single", "[无法识别]"),
    ])
    def test_choice_printed_parenthesis_does_not_hide_a_clear_option(self, raw, mode, expected):
        result = self._constrained(raw, choice_mode=mode)
        assert result["student_answer"] == expected
        assert result["answer_state"] == ("unreadable" if expected == "[无法识别]" else "visible")
        assert result["_ocr_raw_student_answer"] == raw

    def test_13_is_not_mapped_for_non_choice_questions(self):
        """13 -> B 只适用于模板确认的选择题。"""
        question = self._constrained("13", question_type="fill", choice_mode="unknown")
        assert question["student_answer"] == "13"
        assert question["answer_state"] == "visible"
        assert "_ocr_normalization_reason" not in question

    def test_recognize_answers_only_merges_correctly(self):
        """_recognize_answers_only 正确合并基准题干和识别出的答案。"""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from config import OCR_ANSWER_PREPROCESS_MAX_EDGE

        cache = self._ready_cache()
        label = next(iter(cache._baselines))

        # mock call_vision 返回只有答案的结果
        mock_response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "2", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "甲", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "乙", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "丙", "answer_state": "visible"},
            ],
            "student_name": "张三",
            "student_no": "",
        }, ensure_ascii=False)

        prepare = AsyncMock(return_value="prepared.jpg")
        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=mock_response)):
            with patch("services.ocr_service._prepare_vision_image", new=prepare):
                result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        # 题干来自基准
        qs = {q["question_number"]: q for q in result["questions"]}
        assert qs["1"]["question_text"] == "完整题干1"
        assert qs["2"]["question_text"] == "完整题干2"
        # 答案来自识别
        assert qs["1"]["student_answer"] == "甲"
        assert qs["3"]["student_answer"] == "丙"
        assert result.get("_answers_only") is True
        prepare.assert_awaited_once_with(
            "fake.png",
            max_edge=OCR_ANSWER_PREPROCESS_MAX_EDGE,
        )

    def test_answers_only_never_asks_for_or_keeps_identity(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = self._ready_cache()
        label = next(iter(cache._baselines))
        response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "2", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "甲", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "乙", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "丙", "answer_state": "visible"},
            ],
            "student_name": "张三",
            "student_no": "01",
        }, ensure_ascii=False)
        vision = AsyncMock(return_value=response)

        with patch("services.ocr_service.call_vision", new=vision):
            with patch(
                "services.ocr_service._prepare_vision_image",
                new=AsyncMock(return_value="prepared.jpg"),
            ):
                result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        prompt = vision.await_args.args[1]
        assert '"student_name"' not in prompt
        assert '"student_no"' not in prompt
        assert result["student_name"] == ""
        assert result["student_no"] == ""

    def test_recognize_page_combines_routing_and_transcription(self):
        import asyncio
        from unittest.mock import AsyncMock, call, patch
        from config import OCR_ANSWER_PREPROCESS_MAX_EDGE

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {"question_no": "1", "question_text": "题干1", "question_type": "choice", "choice_mode": "single"},
                {"question_no": "2", "question_text": "题干2", "question_type": "fill"},
            ],
        }])
        joint_response = json.dumps({
            "page_type_id": "page_1",
            "observed_question_numbers": ["1", "2"],
            "questions": [
                {"question_number": "1", "student_answer": "D", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "", "answer_state": "blank"},
            ],
            "student_name": "张三",
            "student_no": "",
        }, ensure_ascii=False)
        vision = AsyncMock(return_value=joint_response)
        prepare = AsyncMock(return_value="prepared.jpg")

        with patch("services.ocr_service.call_vision", new=vision):
            with patch("services.ocr_service._prepare_vision_image", new=prepare):
                result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["_cache_type_label"] == "page_1"
        assert [q["student_answer"] for q in result["questions"]] == ["D", ""]
        assert result["_observed_question_numbers"] == ["1", "2"]
        assert vision.await_count == 1
        assert vision.await_args_list[0].kwargs["prompt_first"] is True
        prompt = vision.await_args_list[0].args[1]
        assert '"student_answer"' in prompt
        assert "frozen_page_types=" in prompt
        assert "题干1" not in prompt
        assert "standard_answer" not in prompt
        assert prepare.await_args_list == [
            call("paper.png", max_edge=OCR_ANSWER_PREPROCESS_MAX_EDGE),
        ]

    def test_joint_route_accepts_one_unique_question_anchor(self):
        from unittest.mock import AsyncMock, call, patch
        from config import OCR_ANSWER_PREPROCESS_MAX_EDGE

        cache = PaperCache.from_confirmed_template_pages([
            {
                "page_index": 0,
                "questions": [
                    {"question_no": "1", "question_text": "p1-1"},
                    {"question_no": "2", "question_text": "p1-2"},
                ],
            },
            {
                "page_index": 1,
                "questions": [
                    {"question_no": "20", "question_text": "p2-20"},
                    {"question_no": "21", "question_text": "p2-21"},
                ],
            },
        ])
        response = json.dumps({
            "page_type_id": "page_2",
            "observed_question_numbers": ["21"],
            "questions": [
                {"question_number": "20", "student_answer": "", "answer_state": "blank"},
                {"question_number": "21", "student_answer": "x", "answer_state": "visible"},
            ],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)
        vision = AsyncMock(return_value=response)
        prepare = AsyncMock(return_value="prepared.jpg")

        with patch("services.ocr_service.call_vision", new=vision), patch(
            "services.ocr_service._prepare_vision_image", new=prepare
        ):
            result = asyncio.run(cache._recognize_answers_only(
                "paper.png",
                auto_route=True,
            ))

        assert result["_cache_type_label"] == "page_2"
        assert result["_observed_question_numbers"] == ["21"]
        assert [q["question_number"] for q in result["questions"]] == ["20", "21"]
        assert vision.await_args_list[0].kwargs["prompt_first"] is True
        assert prepare.await_args_list == [
            call("paper.png", max_edge=OCR_ANSWER_PREPROCESS_MAX_EDGE),
        ]

    @pytest.mark.parametrize(
        "observed_question_numbers",
        [["1"], ["1", "20"]],
    )
    def test_joint_route_rejects_ambiguous_or_cross_page_anchors(
        self, observed_question_numbers
    ):
        from unittest.mock import AsyncMock, patch

        conflicting_pages = [
            {
                "page_index": 0,
                "questions": [
                    {"question_no": "1", "question_text": "p1-1"},
                    {"question_no": "2", "question_text": "p1-2"},
                ],
            },
            {
                "page_index": 1,
                "questions": [
                    {"question_no": "1", "question_text": "p2-1"},
                    {"question_no": "20", "question_text": "p2-20"},
                ],
            },
        ]
        # New teacher-confirmed contracts reject duplicate Q before any OCR.
        with pytest.raises(ValueError, match="duplicate question number"):
            PaperCache.from_confirmed_template_pages(conflicting_pages)

        # A hand-built contract can still carry overlapping printed numbers.
        # Keep testing the routing guard against them without weakening the
        # authoritative-directory construction gate above.
        cache = PaperCache()
        cache._baselines = {
            f"page_{page['page_index'] + 1}": {"ocr_result": {"questions": [
                {"question_number": question["question_no"],
                 "question_text": question["question_text"]}
                for question in page["questions"]
            ]}}
            for page in conflicting_pages
        }
        response = json.dumps({
            "page_type_id": "page_1",
            "observed_question_numbers": observed_question_numbers,
            "questions": [
                {"question_number": "1", "student_answer": "x", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "", "answer_state": "blank"},
            ],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)

        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)), patch(
            "services.ocr_service._prepare_vision_image", new=AsyncMock(return_value="paper.png"),
        ):
            result = asyncio.run(cache._recognize_answers_only(
                "paper.png",
                auto_route=True,
            ))

        assert "error" in result
        # The shared processing-error layer emits public safe text; routing
        # refusal is verified through its structured code, not legacy wording.
        expected_code = "OUTPUT_PARSE_FAILED" if observed_question_numbers == ["1"] else "IMAGE_COORDINATES_INVALID"
        assert result["error_info"]["code"] == expected_code
        assert not result.get("questions")
        assert "_cache_type_label" not in result

    def test_local_template_evidence_bypasses_generative_routing(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        from pathlib import Path
        from services.accelerator_business import AcceleratorEvidence
        from services.template_evidence import EvidenceResult

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "must never enter the evidence prompt",
                "question_type": "choice",
            }],
        }])
        expected_evidence_path = tmp_path / (
            "evidence-0123456789abcdef0123456789abcdef.png"
        )
        expected_evidence_path.write_bytes(b"evidence")
        diagnostics = {
            "inliers": 42,
            "answer_source_complete": True,
            "accelerator": {
                "evidence_authority": "bound_client_artifact",
                "server_source_transform_replayed": False,
            },
        }
        evidence = EvidenceResult(
            page_type_id="page_1",
            evidence_path=str(expected_evidence_path),
            question_numbers=["1"],
            blank_question_numbers=[],
            diagnostics=diagnostics,
        )

        class FakeMatcher:
            def match_and_build(self, *_args, **_kwargs):
                raise AssertionError("server template matcher must not run")

        async def fake_accelerator(**_kwargs):
            return AcceleratorEvidence(evidence, state="ready")

        async def fake_answers(path, label, teacher_id="", **kwargs):
            assert Path(path) == expected_evidence_path
            assert label == "page_1"
            assert kwargs["evidence_mode"] is True
            assert kwargs["evidence_strategy"] == "client-evidence-v1"
            assert kwargs["program_blank_qns"] == set()
            return {
                "questions": [{"question_number": "1", "student_answer": "D"}],
                "_cache_type_label": label,
            }

        cache._template_evidence_matcher = FakeMatcher()
        cache._require_local_evidence = True
        cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
        cache._accelerator_template_dir = str(tmp_path)
        cache._accelerator_template_id = "template-1"
        cache._recognize_answers_only = fake_answers
        monkeypatch.setattr(
            "services.accelerator_business.try_accelerated_evidence",
            fake_accelerator,
        )
        paper_path = tmp_path / "paper.png"
        paper_path.write_bytes(b"paper")

        result = asyncio.run(cache.recognize_page(
            str(paper_path),
            teacher_id="7",
            batch_id="batch-1",
            paper_id="paper-1",
        ))

        assert result["_local_template_evidence"] is True
        assert result["_full_page_transcription"] is False
        assert result["_ocr_source"] == "client-evidence-v1"
        assert result["_template_match_diagnostics"] == diagnostics
        assert not expected_evidence_path.exists()

    def test_original_answer_prompt_preserves_radical_direction(self):
        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "21",
                "question_text": "hidden stem",
                "question_type": "fill",
            }],
        }])

        prompt = cache._build_answer_only_prompt("page_1")

        assert "根号、分数、正负号和括号" in prompt
        assert "无法辨认" in prompt
        assert "unreadable" in prompt

    def test_local_template_evidence_is_cleaned_when_answer_ocr_fails(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path
        from services.accelerator_business import AcceleratorEvidence
        from services.template_evidence import EvidenceResult

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "hidden stem",
                "question_type": "fill",
            }],
        }])
        expected_evidence_path = tmp_path / (
            "evidence-fedcba9876543210fedcba9876543210.png"
        )
        expected_evidence_path.write_bytes(b"evidence")
        evidence = EvidenceResult(
            page_type_id="page_1",
            evidence_path=str(expected_evidence_path),
            question_numbers=["1"],
            blank_question_numbers=[],
            diagnostics={
                "answer_source_complete": True,
                "accelerator": {
                    "evidence_authority": "trusted_client_evidence",
                    "server_source_transform_replayed": False,
                },
            },
        )

        class FakeMatcher:
            def match_and_build(self, *_args, **_kwargs):
                raise AssertionError("server template matcher must not run")

        async def fake_accelerator(**_kwargs):
            return AcceleratorEvidence(evidence, state="ready")

        async def failed_answers(path, label, teacher_id="", **kwargs):
            assert Path(path) == expected_evidence_path
            assert kwargs["evidence_mode"] is True
            return {"error": "OCR timeout"}

        cache._template_evidence_matcher = FakeMatcher()
        cache._accelerator_template_artifacts = {"page_1": {"verified": True}}
        cache._accelerator_template_dir = str(tmp_path)
        cache._accelerator_template_id = "template-1"
        cache._recognize_answers_only = failed_answers
        monkeypatch.setattr(
            "services.accelerator_business.try_accelerated_evidence",
            fake_accelerator,
        )
        paper_path = tmp_path / "paper.png"
        paper_path.write_bytes(b"paper")

        result = asyncio.run(cache._recognize_with_local_evidence(
            str(paper_path),
            teacher_id="7",
            batch_id="batch-1",
            paper_id="paper-1",
        ))

        assert result["error"] == "OCR timeout"
        # The evidence already named the page, so a final failure becomes
        # review placeholders instead of an unroutable batch blocker.
        assert result["_fail_closed_questions"] == [{
            "question_no": "1",
            "student_answer": "[无法识别]",
            "question_type": "fill",
            "slot_identity_error": "ocr_failed",
        }]
        assert not expected_evidence_path.exists()

    def test_required_local_template_evidence_failure_is_fail_closed(self):
        import asyncio
        from services.template_evidence import TemplateEvidenceError

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "hidden stem",
                "question_type": "choice",
            }],
        }])

        class RejectingMatcher:
            def match_and_build(self, *_args, **_kwargs):
                raise TemplateEvidenceError("synthetic local matcher rejection")

        cache._template_evidence_matcher = RejectingMatcher()
        cache._require_local_evidence = True
        result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["questions"] == []
        assert result["error"] == "本次模板答案证据未通过校验，已停止自动批改该页"
        assert result["_accelerator_state"] == "fault"
        assert result["_accelerator_reason_code"] == "server_template_evidence_failed"

    @pytest.mark.parametrize(
        ("state", "message"),
        [
            ("pending", "等待教师客户端加速节点完成图片处理"),
            ("unavailable", "加速节点不可用，请使用客户端"),
        ],
    )
    def test_required_local_evidence_preserves_accelerator_message(
        self, state, message
    ):
        import asyncio

        cache = PaperCache()
        cache._template_evidence_matcher = object()
        cache._require_local_evidence = True

        async def accelerator_result(*_args, **_kwargs):
            return {
                "error": message,
                "_accelerator_state": state,
                "_accelerator_reason_code": "accelerator_test_state",
            }

        cache._recognize_with_local_evidence = accelerator_result

        result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["error"] == message
        assert result["questions"] == []
        assert result["_accelerator_state"] == state
        assert result["_accelerator_reason_code"] == "accelerator_test_state"

    def test_required_local_evidence_fault_remains_fail_closed(self):
        import asyncio

        cache = PaperCache()
        cache._template_evidence_matcher = object()
        cache._require_local_evidence = True

        async def accelerator_fault(*_args, **_kwargs):
            return {
                "error": "底层产物已经损坏",
                "_accelerator_state": "fault",
                "_accelerator_reason_code": "accelerator_evidence_missing",
            }

        cache._recognize_with_local_evidence = accelerator_fault

        result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["error"] == "本次模板答案证据未通过校验，已停止自动批改该页"
        assert result["questions"] == []
        assert result["_accelerator_state"] == "fault"
        assert result["_accelerator_reason_code"] == "accelerator_evidence_missing"

    def test_evidence_only_ocr_skips_preprocess_and_flags_blank_conflicts(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {
                    "question_no": "1",
                    "question_text": "secret stem 1",
                    "question_type": "choice",
                },
                {
                    "question_no": "2",
                    "question_text": "secret stem 2",
                    "question_type": "fill",
                },
            ],
        }])
        response = json.dumps({
            "questions": [
                {"question_number": "1", "student_answer": "DA", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "4", "answer_state": "visible"},
            ],
        })
        vision = AsyncMock(return_value=response)
        prepare = AsyncMock(return_value="must-not-be-used.jpg")

        with patch("services.ocr_service.call_vision", new=vision):
            with patch("services.ocr_service._prepare_vision_image", new=prepare):
                result = asyncio.run(cache._recognize_answers_only(
                    "evidence.png",
                    "page_1",
                    evidence_mode=True,
                    program_blank_qns={"2"},
                ))

        prepare.assert_not_awaited()
        assert [question["student_answer"] for question in result["questions"]] == ["[无法识别]", "[无法识别]"]
        assert result["questions"][0]["answer_state"] == "unreadable"
        assert result["questions"][1]["answer_state"] == "unreadable"
        prompt = vision.await_args.args[1]
        assert "中国高中数学试卷 OCR" in prompt
        assert "secret stem" not in prompt
        assert '作答类型={"1":"choice:unknown","2":"fill"}' in prompt
        assert "question_text" not in prompt
        assert "answer_state" in prompt
        assert "unreadable" in prompt
        assert "student_name" not in prompt
        assert "student_no" not in prompt

    def test_evidence_only_ocr_keeps_blank_when_program_and_model_agree(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "secret stem",
                "question_type": "fill",
            }],
        }])
        response = json.dumps({
            "questions": [{
                "question_number": "1",
                "student_answer": "",
                "answer_state": "blank",
            }],
        })

        with patch(
            "services.ocr_service.call_vision",
            new=AsyncMock(return_value=response),
        ):
            result = asyncio.run(cache._recognize_answers_only(
                "evidence.png",
                "page_1",
                evidence_mode=True,
                program_blank_qns={"1"},
            ))

        assert result["questions"][0]["student_answer"] == ""
        assert result["questions"][0]["answer_state"] == "blank"

    def test_multi_evidence_ocr_uses_one_ordered_call_without_teacher_answers(self):
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {
                    "question_no": "1",
                    "question_text": "secret stem one",
                    "question_type": "fill",
                    "standard_answer": "teacher answer one",
                },
                {
                    "question_no": "2",
                    "question_text": "secret stem two",
                    "question_type": "solve",
                    "standard_answer": "teacher answer two",
                },
            ],
        }])
        paths = ("evidence-01.png", "evidence-02.png", "evidence-03.png")
        response = json.dumps({
            "questions": [
                {"question_number": "1", "student_answer": "1", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "x=0", "answer_state": "visible"},
            ],
        })
        multi = AsyncMock(return_value=response)
        single = AsyncMock(side_effect=AssertionError("single-image OCR must not run"))
        prepare = AsyncMock(side_effect=AssertionError("server image preprocessing must not run"))

        with patch("services.ocr_service.call_vision_images", new=multi):
            with patch("services.ocr_service.call_vision", new=single):
                with patch("services.ocr_service._prepare_vision_image", new=prepare):
                    result = asyncio.run(cache._recognize_answers_only(
                        paths[0],
                        "page_1",
                        evidence_mode=True,
                        evidence_paths=paths,
                    ))

        multi.assert_awaited_once()
        assert multi.await_args.args[0] == list(paths)
        assert multi.await_args.kwargs["max_tokens"] == 9000
        assert multi.await_args.kwargs["max_attempts"] == 1
        prompt = multi.await_args.args[1]
        assert "secret stem" not in prompt
        assert "teacher answer" not in prompt
        assert [item["student_answer"] for item in result["questions"]] == [
            "1",
            "x=0",
        ]

    def test_multi_evidence_recovers_q10_from_verified_tail_block(self):
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {
                    "question_no": "9",
                    "question_text": "secret stem nine",
                    "question_type": "choice",
                    "choice_mode": "single",
                    "standard_answer": "D",
                },
                {
                    "question_no": "10",
                    "question_text": "secret stem ten",
                    "question_type": "choice",
                    "choice_mode": "single",
                    "standard_answer": "C",
                },
            ],
        }])
        cache._accelerator_template_artifacts = {
            "page_1": {
                "answer_regions": [
                    {
                        "question_number": "9",
                        "answer_bbox": [100, 680, 450, 760],
                    },
                    {
                        "question_number": "10",
                        "answer_bbox": [100, 800, 450, 900],
                    },
                ],
                "question_crop_plan": {
                    "blocks": [
                        {
                            "block_id": "left-4",
                            "bbox_1000": [0, 640, 512, 1000],
                        },
                        {
                            "block_id": "right-1",
                            "bbox_1000": [488, 0, 1000, 1000],
                        },
                    ],
                },
            },
        }
        diagnostics = {
            "answer_source_complete": True,
            "evidence_blocks": [
                {
                    "block_id": "left-4",
                    "column": "left",
                    # The layout model omitted Q10, but verified geometry still
                    # places its full answer box inside this continuous crop.
                    "question_number_hints": ["9"],
                },
                {
                    "block_id": "right-1",
                    "column": "right",
                    "question_number_hints": [],
                },
            ],
        }
        first = json.dumps({
            "questions": [
                {
                    "question_number": "9",
                    "student_answer": "B",
                    "answer_state": "visible",
                },
                {
                    "question_number": "10",
                    "student_answer": "[无法识别]",
                    "answer_state": "unreadable",
                },
            ],
        })
        local = json.dumps({
            "questions": [
                {
                    "question_number": "9",
                    "student_answer": "B",
                    "answer_state": "visible",
                },
                {
                    "question_number": "10",
                    "student_answer": "A",
                    "answer_state": "visible",
                },
            ],
        })
        vision = AsyncMock(side_effect=[first, local])

        with patch("services.ocr_service.call_vision_images", new=vision):
            result = asyncio.run(cache._recognize_answers_only(
                "left-tail.png",
                "page_1",
                evidence_mode=True,
                evidence_strategy="continuous_question_crop_plan",
                evidence_paths=("left-tail.png", "right-column.png"),
                evidence_diagnostics=diagnostics,
            ))

        assert vision.await_count == 2
        assert vision.await_args_list[0].args[0] == [
            "left-tail.png",
            "right-column.png",
        ]
        assert vision.await_args_list[1].args[0] == ["left-tail.png"]
        assert vision.await_args_list[1].kwargs == {"max_tokens": 2048}
        local_prompt = vision.await_args_list[1].args[1]
        assert "secret stem" not in local_prompt
        assert "standard_answer" not in local_prompt
        assert "题号清单=[\"9\",\"10\"]" in local_prompt
        assert '"question_number"' in local_prompt
        assert result["questions"][1]["student_answer"] == "A"
        assert result["questions"][1]["answer_state"] == "visible"
        assert result["questions"][1]["_ocr_local_recovery"] is True

    def test_focus_sheet_recovers_unreadable_choice_with_same_sheet_witness(self):
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {
                    "question_no": "9",
                    "question_text": "secret stem nine",
                    "question_type": "choice",
                    "choice_mode": "single",
                },
                {
                    "question_no": "10",
                    "question_text": "secret stem ten",
                    "question_type": "choice",
                    "choice_mode": "single",
                },
            ],
        }])
        cache._accelerator_template_artifacts = {
            "page_1": {
                "answer_regions": [
                    {"question_number": "9", "answer_bbox": [100, 680, 450, 760]},
                    {"question_number": "10", "answer_bbox": [100, 800, 450, 900]},
                ],
            },
        }
        answers = [
            {"question_number": "9", "student_answer": "B", "answer_state": "visible"},
            {
                "question_number": "10",
                "student_answer": "[无法识别]",
                "answer_state": "unreadable",
            },
        ]
        diagnostics = {
            "ownership": "confirmed_bbox_focus_sheets",
            "answer_source_complete": True,
            "evidence_locations": [
                {
                    "question_number": "9",
                    "image_index": 1,
                    "answer_bbox_1000": [100, 100, 900, 450],
                },
                {
                    "question_number": "10",
                    "image_index": 1,
                    "answer_bbox_1000": [100, 550, 900, 950],
                },
            ],
        }
        response = json.dumps({
            "questions": [
                {"question_number": "9", "student_answer": "B", "answer_state": "visible"},
                {"question_number": "10", "student_answer": "A", "answer_state": "visible"},
            ],
        })

        with patch(
            "services.ocr_service.call_vision_images",
            new=AsyncMock(return_value=response),
        ) as vision:
            recovered = asyncio.run(cache._recover_unreadable_evidence_answers(
                "page_1",
                answers,
                ("focus-01.png", "focus-02.png"),
                diagnostics,
            ))

        vision.assert_awaited_once()
        assert vision.await_args.args[0] == ["focus-01.png"]
        prompt = vision.await_args.args[1]
        assert "secret stem" not in prompt
        assert recovered == {
            "10": {
                "student_answer": "A",
                "answer_state": "visible",
                "_ocr_local_recovery": True,
            },
        }

    def test_multi_evidence_does_not_recover_incomplete_source_or_witness_conflict(self):
        from unittest.mock import AsyncMock, patch

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [
                {
                    "question_no": "9",
                    "question_type": "choice",
                    "choice_mode": "single",
                },
                {
                    "question_no": "10",
                    "question_type": "choice",
                    "choice_mode": "single",
                },
            ],
        }])
        cache._accelerator_template_artifacts = {
            "page_1": {
                "answer_regions": [
                    {"question_number": "9", "answer_bbox": [10, 700, 400, 760]},
                    {"question_number": "10", "answer_bbox": [10, 800, 400, 900]},
                ],
                "question_crop_plan": {"blocks": [{
                    "block_id": "left-4",
                    "bbox_1000": [0, 650, 512, 1000],
                }]},
            },
        }
        answers = [
            {"question_number": "9", "student_answer": "B", "answer_state": "visible"},
            {
                "question_number": "10",
                "student_answer": "[无法识别]",
                "answer_state": "unreadable",
            },
        ]
        diagnostics = {
            "answer_source_complete": False,
            "evidence_blocks": [{
                "block_id": "left-4",
                "column": "left",
                "question_number_hints": ["9"],
            }],
        }

        with patch(
            "services.ocr_service.call_vision_images",
            new=AsyncMock(side_effect=AssertionError("incomplete pixels must fail closed")),
        ):
            recovered = asyncio.run(cache._recover_unreadable_evidence_answers(
                "page_1", answers, ("tail.png",), diagnostics,
            ))
        assert recovered == {}

        diagnostics["answer_source_complete"] = True
        conflicting = json.dumps({
            "questions": [
                {"question_number": "9", "student_answer": "C", "answer_state": "visible"},
                {"question_number": "10", "student_answer": "A", "answer_state": "visible"},
            ],
        })
        with patch(
            "services.ocr_service.call_vision_images",
            new=AsyncMock(return_value=conflicting),
        ):
            recovered = asyncio.run(cache._recover_unreadable_evidence_answers(
                "page_1", answers, ("tail.png",), diagnostics,
            ))
        assert recovered == {}

    def test_evidence_only_prompt_has_compact_25_question_budget(self):
        questions = []
        for question_number in range(1, 26):
            if question_number <= 15:
                question_type = "choice"
            elif question_number <= 20:
                question_type = "fill"
            else:
                question_type = "solve"
            questions.append({
                "question_no": str(question_number),
                "question_text": f"very long frozen stem {question_number}",
                "question_type": question_type,
            })
        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": questions,
        }])

        prompt = cache._build_evidence_only_prompt("page_1")

        assert len(prompt) < 1200
        assert "very long frozen stem" not in prompt
        assert '"1":"choice:unknown","2":"choice:unknown","3":"choice:unknown"' in prompt
        assert '"16":"fill","17":"fill","18":"fill","19":"fill","20":"fill"' in prompt
        assert '"21":"solve","22":"solve","23":"solve","24":"solve","25":"solve"' in prompt
        anchor_prompt = cache._build_evidence_only_prompt(
            "page_1",
            evidence_strategy="continuous_question_crop_plan",
        )
        assert "题号清单=" in anchor_prompt
        assert '"1":"choice:unknown"' in anchor_prompt
        assert "B/left/right/hints" not in anchor_prompt

    def test_recognize_answers_only_count_mismatch_fallback(self):
        """基准3题，允许差0（a(3)=0.65→floor=0），差2题触发fallback。"""
        import asyncio
        from unittest.mock import patch, AsyncMock

        cache = self._ready_cache()  # 基准3题
        label = next(iter(cache._baselines))

        # mock 只返回 1 题答案（差2，超过允许的0）
        mock_response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1"],
            "questions": [{"question_number": "1", "student_answer": "甲", "answer_state": "visible"}],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)

        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=mock_response)):
            with patch("services.preprocess.preprocess_paper_image", return_value="fake.png"):
                result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        # 应返回 error，提示需要 fallback
        assert "error" in result

    def test_recognize_answers_only_exact_match_pass(self):
        """基准3题，返回3题答案（差0），校验通过。"""
        import asyncio
        from unittest.mock import patch, AsyncMock

        cache = self._ready_cache()  # 基准3题
        label = next(iter(cache._baselines))

        # mock 返回 3 题答案（差0，允许）
        mock_response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "2", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "甲", "answer_state": "visible"},
                {"question_number": "2", "student_answer": "乙", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "丙", "answer_state": "visible"},
            ],
            "student_name": "测试",
            "student_no": "",
        }, ensure_ascii=False)

        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=mock_response)):
            with patch("services.preprocess.preprocess_paper_image", return_value="fake.png"):
                result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        # 应成功，无 error
        assert "error" not in result
        assert len(result["questions"]) == 3

    def test_recognize_answers_only_rejects_reordered_question_numbers(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = self._ready_cache()
        label = next(iter(cache._baselines))
        response = json.dumps({
            "questions": [
                {"question_number": "2", "student_answer": "乙", "answer_state": "visible"},
                {"question_number": "1", "student_answer": "甲", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "丙", "answer_state": "visible"},
            ],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)

        with patch(
            "services.ocr_service.call_vision",
            new=AsyncMock(return_value=response),
        ):
            result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        assert result["error"] == "答案题号或顺序与已确认页型不一致，需教师复核"

    def test_answers_only_fails_closed_on_answer_that_claims_blank_visual_state(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = self._ready_cache()
        label = next(iter(cache._baselines))
        response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "2", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "A", "answer_state": "blank"},
                {"question_number": "2", "student_answer": "", "answer_state": "blank"},
                {"question_number": "3", "student_answer": "", "answer_state": "blank"},
            ],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)

        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)):
            result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        # The contradicted answer is never graded as "A" nor as blank; only
        # that question goes to the teacher, the rest of the page survives.
        assert "error" not in result
        by_number = {q["question_number"]: q for q in result["questions"]}
        assert by_number["1"]["answer_state"] == "unreadable"
        assert by_number["1"]["student_answer"] == "[无法识别]"
        assert by_number["2"]["answer_state"] == "blank"
        assert by_number["3"]["answer_state"] == "blank"

    def test_answers_only_contract_mismatch_keeps_one_recovery_attempt(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = self._ready_cache()
        label = next(iter(cache._baselines))
        response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "A", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "C", "answer_state": "visible"},
            ],
            "student_name": "",
            "student_no": "",
        }, ensure_ascii=False)

        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)):
            result = asyncio.run(cache._recognize_answers_only("fake.png", label))

        assert result["error"] == "答案题号或顺序与已确认页型不一致，需教师复核"
        assert result["_ocr_retryable"] is True
        assert result["_ocr_error_code"] == "transcription_contract_mismatch"

    def test_answers_only_fails_closed_on_fraction_joining_blank_neighbor(self):
        """Do not grade a denominator from Q23 as part of Q22."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        cache = self._ready_cache()
        label = next(iter(cache._baselines))
        response = json.dumps({
            "page_type_id": label,
            "observed_question_numbers": ["1", "2", "3"],
            "questions": [
                {"question_number": "1", "student_answer": "A", "answer_state": "visible"},
                {"question_number": "2", "student_answer": r"\\frac{4}{1}", "answer_state": "visible"},
                {"question_number": "3", "student_answer": "", "answer_state": "blank"},
            ],
            "student_name": "", "student_no": "",
        }, ensure_ascii=False)
        with patch("services.ocr_service.call_vision", new=AsyncMock(return_value=response)):
            result = asyncio.run(cache._recognize_answers_only("fake.png", label))
        assert result["_answer_ambiguity_qns"] == ["2", "3"]
        assert result["questions"][1]["answer_state"] == "unreadable"
        assert result["questions"][2]["answer_state"] == "unreadable"

    def test_confirmed_template_never_falls_back_to_full_ocr(self):
        import asyncio

        cache = PaperCache.from_confirmed_template_pages([{
            "page_index": 0,
            "questions": [{
                "question_no": "1",
                "question_text": "不会发送给答案OCR的题干",
                "question_type": "choice",
            }],
        }])

        async def rejected_answers_only(*_args, **_kwargs):
            return {"error": "视觉证据状态矛盾"}

        cache._recognize_answers_only = rejected_answers_only
        result = asyncio.run(cache.recognize_page("paper.png"))

        assert result["questions"] == []
        assert "停止自动批改" in result["error"]
