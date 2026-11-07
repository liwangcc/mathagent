"""多页一次性模板 step 2 —— 数据结构 + recognize/confirm 端点测试。

覆盖：
  - TemplateSession add_page/make_page 归一化 + 签名 + 存取
  - /batch-grade/template/recognize：统一模板流水线 OCR N 页→未确认会话落盘
  - /batch-grade/template/confirm：老师可反复保存本批权威模板
  - /batch-grade/template/{id}/bank-import：独立、一次性、幂等写入私库
"""

import asyncio
import copy
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from PIL import Image, ImageDraw

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db
from models import GradingResult, QuestionBank, Student
from models_correction import (
    QuestionAttemptEvent,
    QuestionCorrectionRelation,
    TemplateQuestionAuthorization,
    WrongQuestionCycle,
)
from models_user import User
from routers.auth import _require_user
import services.template_session as template_session
from services.template_session import (
    CURRENT_ANSWER_CONTRACT_VERSION,
    CURRENT_ARTIFACT_HASH_VERSION,
    TemplateSession,
)
from services.template_evidence import CanonicalTemplate, canonicalize_template_image
from tests.grade_family import set_grade_global
from routers import grade_availability
from routers import grade_report
from routers import grade_papers
from routers import grade_recognition
from routers import grade_template_import_steps
from routers import grade_template_import_form
from routers import grade_template_import


@pytest.fixture(autouse=True)
def _ready_batch_grade_chain(monkeypatch):
    accelerator_calls = []

    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}

    async def template_available():
        return {"status": "available", "message": "模板识别服务可用"}

    monkeypatch.setattr(
        "routers.grade_availability.batch_grade_availability",
        available,
    )
    monkeypatch.setattr(
        "routers.grade_availability._batch_grade_service_availability",
        available,
    )
    monkeypatch.setattr(
        "routers.grade_template_availability._batch_grade_service_availability",
        available,
    )
    monkeypatch.setattr(
        "routers.grade_template_availability.get_column_layout_readiness",
        template_available,
    )

    async def hardware_ready(_teacher_id, **_contract):
        return {"status": "ready", "message": "加速节点已就绪"}

    monkeypatch.setattr(
        "routers.grade_availability.get_public_hardware_readiness",
        hardware_ready,
    )
    monkeypatch.setattr(
        "routers.grade_template_availability.get_public_hardware_readiness",
        hardware_ready,
    )

    return accelerator_calls


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    cur = None
    for line in text.splitlines():
        if line.startswith("event:"):
            cur = line[len("event:"):].strip()
        elif line.startswith("data:"):
            events.append((cur, json.loads(line[len("data:"):].strip())))
    return events


def _legacy_error_with_diagnostic(errors):
    """Verify the new diagnostic contract, then retain legacy field assertions."""
    expected = {
        'TEMPLATE_OCR_FAILED': 'PROCESSING_FAILED',
        'TEMPLATE_CANONICAL_CANDIDATE_INVALID': 'IMAGE_CANDIDATE_INVALID',
        'TEMPLATE_CANONICALIZATION_FAILED': 'IMAGE_CANONICALIZATION_FAILED',
        'VISION_AUTH_FAILED': 'PROVIDER_AUTH_FAILED',
        'TEMPLATE_NO_QUESTIONS': 'TRANSCRIPTION_EMPTY',
        'TEMPLATE_OUTPUT_INVALID': 'OUTPUT_PARSE_FAILED',
        'VISION_TIMEOUT': 'PROVIDER_TIMEOUT',
    }
    assert len(errors) == 1
    result = []
    for error in errors:
        info = error['error_info']
        assert info['code'] == expected[error['code']]
        assert info['stage'] == 'template'
        for key in ('error_id', 'correlation_id'):
            assert len(info[key]) == 32
            assert all(c in '0123456789abcdef' for c in info[key])
        assert info['message'] and info['action']
        assert isinstance(info['retryable'], bool)
        result.append({k: v for k, v in error.items() if k != 'error_info'})
    return result


def _template_png_bytes(marker: str = "A") -> bytes:
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, 575, 775), outline="black", width=3)
    draw.text((55, 50), f"MATH TEMPLATE {marker}", fill="black")
    for row in range(8):
        top = 125 + row * 72
        draw.line((60, top, 540, top), fill=(50, 50, 50), width=2)
        draw.rectangle((390, top + 12, 530, top + 48), outline=(80, 80, 80), width=2)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _ocr_ready_jpeg_bytes(marker: str = "A") -> bytes:
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 50), f"OCR READY {marker}", fill="black")
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=85)
    return stream.getvalue()


def _canonical_submission(tmp_path, raw: bytes, name: str = "page") -> tuple[bytes, dict]:
    source = tmp_path / "desktop-source" / f"{name}.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(raw)
    canonical = canonicalize_template_image(
        source,
        tmp_path / "desktop-output" / name,
        0,
    )
    metadata = dict(canonical.canonicalization)
    metadata.pop("source_image")
    return Path(canonical.image_path).read_bytes(), metadata


# ── 数据结构层 ──

def test_make_page_normalizes_and_signs():
    page = TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": " 求 f(x) ", "question_type": "SOLVE",
         "standard_answer": " 3 "},
        {"question_number": "2", "question_text": "选择", "question_type": "weird"},
    ])
    assert page["page_index"] == 0
    q1, q2 = page["questions"]
    assert q1["question_no"] == "1"
    assert q1["question_text"] == "求 f(x)"          # 去首尾空白
    assert q1["question_type"] == "solve"            # 小写
    assert q1["standard_answer"] == "3"
    assert q2["question_type"] == "solve"            # 非法值兜底
    # 签名基于题号集合
    assert set(page["signature"]["qn_set"]) == {"1", "2"}
    assert page["signature"]["qn_count"] == 2


def test_insert_question_shifts_the_rest_of_the_page_and_moves_content():
    """The teacher's case: a missed first question left the page reading 1234.

    Inserting at the front must renumber 1234 to 2345 and carry each question's
    text and answer along with it. The question the model labelled "1" really is
    the paper's second question, so its content belongs to "2"; leaving the
    content behind would grade every answer against the wrong question.
    """
    pages = [TemplateSession.make_page(0, [
        {"question_number": str(index), "question_text": f"题{index}",
         "standard_answer": f"答{index}"}
        for index in range(1, 5)
    ])]

    updated, new_number, renames = template_session.insert_question(
        pages, page_index=0, before_question_number="1",
    )

    assert new_number == "1"
    # The locating artifacts key their boxes by number, and those boxes still
    # mark the same ink, so the caller is told how the numbers moved.
    assert renames == {"1": "2", "2": "3", "3": "4", "4": "5"}
    questions = updated[0]["questions"]
    assert [q["question_no"] for q in questions] == ["1", "2", "3", "4", "5"]
    # The inserted question is empty and awaits the teacher; every original
    # question keeps its own text and answer under its new number.
    assert questions[0]["question_text"] == ""
    assert questions[0]["standard_answer"] == ""
    assert [(q["question_text"], q["standard_answer"]) for q in questions[1:]] == [
        (f"题{index}", f"答{index}") for index in range(1, 5)
    ]
    assert set(updated[0]["signature"]["qn_set"]) == {"1", "2", "3", "4", "5"}


def test_insert_question_continues_the_shift_onto_later_pages():
    """Question numbers are unique for the whole paper, so the shift continues.

    build_answer_map flattens every page into one table keyed by question
    number, and resolves a collision by silently keeping one of the pair. A
    shift that stopped at the page boundary would therefore drop an answer.
    """
    pages = [
        TemplateSession.make_page(0, [
            {"question_number": "1", "question_text": "一", "standard_answer": "A"},
            {"question_number": "2", "question_text": "二", "standard_answer": "B"},
        ]),
        TemplateSession.make_page(1, [
            {"question_number": "3", "question_text": "三", "standard_answer": "C"},
            {"question_number": "4", "question_text": "四", "standard_answer": "D"},
        ]),
    ]

    updated, new_number, renames = template_session.insert_question(
        pages, page_index=0, before_question_number="1",
    )

    assert new_number == "1"
    assert renames == {"1": "2", "2": "3", "3": "4", "4": "5"}
    assert [q["question_no"] for q in updated[0]["questions"]] == ["1", "2", "3"]
    assert [q["question_no"] for q in updated[1]["questions"]] == ["4", "5"]
    # Page two's content followed its own questions rather than staying put.
    assert [q["question_text"] for q in updated[1]["questions"]] == ["三", "四"]
    numbers = [q["question_no"] for page in updated for q in page["questions"]]
    assert len(numbers) == len(set(numbers))


def test_insert_question_appends_without_disturbing_existing_numbers():
    pages = [TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": "一", "standard_answer": "A"},
        {"question_number": "2", "question_text": "二", "standard_answer": "B"},
    ])]

    updated, new_number, renames = template_session.insert_question(pages, page_index=0)

    # Appending shifts nothing, so no existing box needs renaming.
    assert new_number == "3"
    assert renames == {}
    assert [q["question_no"] for q in updated[0]["questions"]] == ["1", "2", "3"]
    assert [q["question_text"] for q in updated[0]["questions"][:2]] == ["一", "二"]


def test_insert_question_uses_the_teacher_typed_number_without_renumbering():
    """Teacher annotation: the new number is typed by hand (digits and dots)."""
    pages = [TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": "一", "standard_answer": "A"},
        {"question_number": "2", "question_text": "二", "standard_answer": "B"},
        {"question_number": "3", "question_text": "三", "standard_answer": "C"},
    ])]

    updated, new_number, renames = template_session.insert_question(
        pages, page_index=0, before_question_number="3", new_question_number="2.4",
    )

    assert new_number == "2.4"
    assert renames == {}
    assert [q["question_no"] for q in updated[0]["questions"]] == ["1", "2", "2.4", "3"]

    for bad in (".3", "2.", "2..3", "2a", "a.2"):
        with pytest.raises(ValueError, match="数字和点"):
            template_session.insert_question(
                pages, page_index=0, before_question_number="3", new_question_number=bad,
            )
    with pytest.raises(ValueError, match="已存在"):
        template_session.insert_question(
            pages, page_index=0, before_question_number="3", new_question_number="2",
        )


def test_insert_question_refuses_to_guess_a_non_numeric_successor():
    """A paper may label a question 11(1) or 二.3, which has no next number.

    Guessing one would silently mislabel the tail of the paper, so the teacher
    is told to adjust those numbers by hand instead.
    """
    pages = [TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": "一", "standard_answer": "A"},
        {"question_number": "2(1)", "question_text": "二", "standard_answer": "B"},
    ])]

    with pytest.raises(ValueError, match="不是纯数字"):
        template_session.insert_question(
            pages, page_index=0, before_question_number="1",
        )


def test_delete_question_leaves_the_remaining_numbers_alone():
    """Deletion removes a spurious annotation; the other numbers are correct.

    Shifting them down, as the mirror of insertion, would corrupt a paper whose
    numbering was already right.
    """
    pages = [TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": "一", "standard_answer": "A"},
        {"question_number": "2", "question_text": "二", "standard_answer": "B"},
        {"question_number": "3", "question_text": "三", "standard_answer": "C"},
    ])]

    updated = template_session.delete_question(
        pages, page_index=0, question_number="2",
    )

    assert [q["question_no"] for q in updated[0]["questions"]] == ["1", "3"]
    assert [q["question_text"] for q in updated[0]["questions"]] == ["一", "三"]
    assert set(updated[0]["signature"]["qn_set"]) == {"1", "3"}


def test_delete_question_keeps_at_least_one_question():
    pages = [TemplateSession.make_page(0, [
        {"question_number": "1", "question_text": "一", "standard_answer": "A"},
    ])]

    with pytest.raises(ValueError, match="至少要保留一道题"):
        template_session.delete_question(
            pages, page_index=0, question_number="1",
        )


def test_session_save_load_roundtrip(tmp_path):
    sess = TemplateSession("tpl1", str(tmp_path))
    sess.has_answers = False
    sess.category = "exam"
    sess.exam_name = "期中考试"
    sess.recognition_started_at = "2026-08-10T01:00:00+00:00"
    sess.recognition_deadline_at = "2026-08-10T01:05:00+00:00"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": "A"}])
    artifact = {
        "version": 1,
        "template_image": "artifacts/page_000/template.png",
        "sha256": "0" * 64,
        "width": 100,
        "height": 100,
        "answer_regions": [{
            "question_number": "1",
            "question_type": "solve",
            "answer_bbox": [10, 10, 90, 90],
        }],
    }
    sess.page_artifacts["page_1"] = artifact
    sess.status = "confirmed"
    sess.confirm_current_contract(7)
    sess.save_meta()

    loaded = TemplateSession.load_meta("tpl1", str(tmp_path))
    assert loaded.status == "confirmed"
    assert loaded.has_answers is False
    assert loaded.category == "exam"
    assert loaded.exam_name == "期中考试"
    assert loaded.recognition_started_at == "2026-08-10T01:00:00+00:00"
    assert loaded.recognition_deadline_at == "2026-08-10T01:05:00+00:00"
    assert len(loaded.pages) == 1
    assert loaded.pages[0]["questions"][0]["standard_answer"] == "A"
    assert loaded.page_artifacts == {"page_1": artifact}
    assert loaded.answer_contract_version == CURRENT_ANSWER_CONTRACT_VERSION
    assert loaded.confirmed_contract_hash == loaded.content_hash()
    assert loaded.confirmed_artifact_hash == loaded.artifact_hash()
    assert loaded.confirmed_artifact_hash_version == CURRENT_ARTIFACT_HASH_VERSION
    assert loaded.requires_reconfirmation is False
    assert loaded.confirmed_by == 7
    assert loaded.confirmed_at


def test_derived_knowledge_metadata_does_not_invalidate_confirmed_contract(tmp_path):
    sess = TemplateSession("knowledge-overlay", str(tmp_path), teacher_id=7)
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "求函数 f(x)=x² 的单调区间",
        "question_type": "solve",
        "standard_answer": "略",
    }])
    sess.status = "confirmed"
    sess.confirm_current_contract(7)
    original_hash = sess.confirmed_contract_hash
    sess.knowledge_classifications["0:1"] = {
        "question_text_hash": sess.question_text_hash("求函数 f(x)=x² 的单调区间"),
        "chapter": "函数",
        "topic": "函数的单调性",
        "confidence": "high",
        "classification_version": "catalog:test",
    }
    sess.save_meta()

    loaded = TemplateSession.load_meta("knowledge-overlay", str(tmp_path))
    answer = loaded.build_answer_map()["1"]

    assert loaded.confirmed_contract_hash == original_hash == loaded.content_hash()
    assert loaded.recognition_contract_error() == ""
    assert answer["chapter"] == "函数"
    assert answer["topic"] == "函数的单调性"


def test_confirmed_template_background_classifies_only_missing_knowledge(monkeypatch, tmp_path):
    import routers.grade as gr_router
    from routers import grade_template

    sess = TemplateSession("knowledge-background", str(tmp_path), teacher_id=7)
    sess.category = "daily"
    sess.add_page(0, [
        {
            "question_number": "1",
            "question_text": "求函数定义域",
            "question_type": "solve",
            "standard_answer": "x>0",
        },
        {
            "question_number": "2",
            "question_text": "计算向量长度",
            "question_type": "solve",
            "standard_answer": "2",
        },
    ])
    sess.status = "confirmed"
    sess.confirm_current_contract(7)
    sess.save_meta()
    original_hash = sess.confirmed_contract_hash
    calls = []

    async def classify(items):
        calls.append(items)
        return [
            {
                "id": item["id"],
                "status": "suggested",
                "chapter": "函数" if item["question_no"] == "1" else "平面向量",
                "topic": "定义域" if item["question_no"] == "1" else "向量的模",
                "confidence": "high",
                "classification_version": "catalog:test",
            }
            for item in items
        ]

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    set_grade_global(monkeypatch, "_template_knowledge_classifier", classify)

    asyncio.run(grade_template._classify_confirmed_template_knowledge(
        sess.template_id, 7, original_hash,
    ))

    loaded = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert len(calls) == 1
    assert [item["question"] for item in calls[0]] == ["求函数定义域", "计算向量长度"]
    assert loaded.build_answer_map()["1"]["topic"] == "定义域"
    assert loaded.build_answer_map()["2"]["topic"] == "向量的模"
    assert loaded.confirmed_contract_hash == original_hash == loaded.content_hash()
    assert loaded.recognition_contract_error() == ""


def test_load_legacy_confirmed_template_requires_reconfirmation(tmp_path):
    sess = TemplateSession("legacy-contract", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "旧模板题目",
        "question_type": "choice",
        "standard_answer": "A",
    }])
    sess.save_meta()

    loaded = TemplateSession.load_meta("legacy-contract", str(tmp_path))

    assert loaded.answer_contract_version == 0
    assert loaded.requires_reconfirmation is True
    assert "重新确认" in loaded.reconfirmation_reason


def test_confirmed_contract_detects_content_changed_after_confirmation(tmp_path):
    sess = TemplateSession("changed-contract", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "确认时题干",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    sess.confirm_current_contract(1)
    assert sess.recognition_contract_error() == ""

    sess.pages[0]["questions"][0]["standard_answer"] = "3"

    assert "发生变化" in sess.recognition_contract_error()


def test_recognize_rejects_legacy_contract_until_teacher_reconfirms(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    sess = TemplateSession("legacy-route", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "旧模板选择题 A.1 B.2 C.3 D.4",
        "question_type": "choice",
        "standard_answer": "A",
    }])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    rejected = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "legacy-route"},
        files=[("files", ("student.png", b"\x89PNG\r\n\x1a\nimg", "image/png"))],
    )

    assert rejected.status_code == 409
    assert "重新确认" in rejected.json()["detail"]
    blocked = TemplateSession.load_meta("legacy-route", str(tmp_path))
    assert blocked.requires_reconfirmation is True

    confirmed = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "legacy-route",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1",
            "question_text": "旧模板选择题 A.1 B.2 C.3 D.4",
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "A",
        }]}],
    })

    assert confirmed.status_code == 200, confirmed.text
    upgraded = TemplateSession.load_meta("legacy-route", str(tmp_path))
    assert upgraded.answer_contract_version == CURRENT_ANSWER_CONTRACT_VERSION
    assert upgraded.requires_reconfirmation is False
    assert upgraded.confirmed_contract_hash == upgraded.content_hash()
    assert upgraded.confirmed_artifact_hash == upgraded.artifact_hash()

    async def recognized_student(
        _path, _batch_id, paper_cache, teacher_id="", paper_id="",
    ):
        assert paper_cache._baselines
        return {"student_name": "张三", "questions": [{
            "question_number": "1",
            "student_answer": "A",
            "question_type": "choice",
        }]}

    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognized_student)
    set_grade_global(monkeypatch, "_validate_client_ocr_ready_upload", lambda _filename, _content: {"safe": True})
    accepted = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "legacy-route"},
        files=[(
            "files",
            (
                "student.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("LEGACY"),
                "image/jpeg",
            ),
        )],
    )
    assert accepted.status_code == 200, accepted.text
    assert TemplateSession.load_meta("legacy-route", str(tmp_path)).status == "frozen"


def test_recognize_rejects_confirmed_contract_whose_content_changed(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    sess = TemplateSession("changed-route", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "确认题目",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    sess.confirm_current_contract(1)
    sess.pages[0]["questions"][0]["standard_answer"] = "3"
    sess.save_meta()

    rejected = _client(Session, monkeypatch).post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "changed-route"},
        files=[("files", ("student.png", b"\x89PNG\r\n\x1a\nimg", "image/png"))],
    )

    assert rejected.status_code == 409
    assert "发生变化" in rejected.json()["detail"]
    blocked = TemplateSession.load_meta("changed-route", str(tmp_path))
    assert blocked.requires_reconfirmation is True
    assert "发生变化" in blocked.reconfirmation_reason


def test_confirmed_contract_detects_visual_artifact_changed_after_confirmation(tmp_path):
    sess = TemplateSession("changed-artifact", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "确认题目",
        "question_type": "fill",
        "standard_answer": "2",
    }], artifact={
        "version": 1,
        "sha256": "1" * 64,
        "width": 100,
        "height": 100,
        "question_schema": [{
            "question_number": "1",
            "question_type": "fill",
            "choice_mode": "unknown",
        }],
        "question_numbers": ["1"],
    })
    sess.confirm_current_contract(1)
    assert sess.recognition_contract_error() == ""

    sess.page_artifacts["page_1"]["question_schema"][0]["question_type"] = "choice"

    assert "发生变化" in sess.recognition_contract_error()


def test_artifact_hash_binds_schema_and_visual_overrides(tmp_path):
    sess = TemplateSession("legacy-artifact-hash", str(tmp_path), teacher_id=1)
    artifact = {
        "version": 1,
        "sha256": "1" * 64,
        "width": 100,
        "height": 100,
        "question_schema": [{
            "question_number": "1",
            "question_type": "fill",
            "choice_mode": "unknown",
        }],
        "question_numbers": ["1"],
    }
    sess.page_artifacts["page_1"] = artifact
    initial_hash = sess.artifact_hash()

    artifact["question_anchors"] = []
    assert sess.artifact_hash() != initial_hash

    hash_with_anchors = sess.artifact_hash()
    artifact["template_handwriting_regions"] = []
    assert sess.artifact_hash() != hash_with_anchors


def test_load_meta_missing_returns_pending(tmp_path):
    sess = TemplateSession.load_meta("nope", str(tmp_path))
    assert sess.status == "pending"
    assert sess.pages == []


def test_blank_template_confirm_preserves_server_artifact_and_layout(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    artifact = {
        "version": 1,
        "template_image": "artifacts/page_000/template.png",
        "sha256": "0" * 64,
        "width": 100,
        "height": 100,
        "question_schema": [{
            "question_number": "1",
            "question_type": "fill",
            "choice_mode": "unknown",
        }],
        "question_numbers": ["1"],
    }
    sess = TemplateSession("blank-layout", str(tmp_path), teacher_id=1)
    sess.has_answers = False
    sess.template_mode = "blank"
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "题干",
        "question_type": "fill",
        "standard_answer": "2",
    }], artifact=artifact)
    sess.save_meta()

    client = _client(Session, monkeypatch)
    response = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "blank-layout",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1",
            "question_text": "老师修正题干",
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "A",
        }]}],
    })
    assert response.status_code == 200

    saved = TemplateSession.load_meta("blank-layout", str(tmp_path))
    artifact = saved.page_artifacts["page_1"]
    assert "answer_regions" not in artifact
    assert artifact["question_schema"][0]["question_type"] == "choice"
    assert saved.pages[0]["questions"][0]["question_type"] == "choice"

    changed_number = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "blank-layout",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "2",
            "question_text": "篡改题号",
            "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "A",
        }]}],
    })
    assert changed_number.status_code == 409
    assert "题号或顺序不能修改" in changed_number.json()["detail"]


def test_blank_template_unknown_choice_cannot_be_confirmed(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    sess = TemplateSession("unknown-choice", str(tmp_path), teacher_id=1)
    sess.has_answers = False
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "4",
        "question_text": "结果为（ ） A.1 B.2 C.3 D.4",
        "question_type": "choice",
        "choice_mode": "unknown",
        "standard_answer": "",
    }])
    sess.save_meta()

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "unknown-choice",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "4",
            "question_text": "结果为（ ） A.1 B.2 C.3 D.4",
            "question_type": "choice",
            "choice_mode": "unknown",
            "standard_answer": "",
        }]}],
    })

    assert response.status_code == 400
    saved = TemplateSession.load_meta("unknown-choice", str(tmp_path))
    assert saved.status == "ocr_done"


def test_answered_template_confirm_cannot_drop_a_registered_page(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    sess = TemplateSession("answered-pages", str(tmp_path), teacher_id=1)
    sess.has_answers = True
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "第一页",
        "question_type": "fill",
        "standard_answer": "1",
    }])
    sess.add_page(1, [{
        "question_number": "2",
        "question_text": "第二页",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    sess.save_meta()

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "answered-pages",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1",
            "question_text": "只提交第一页",
            "question_type": "fill",
            "standard_answer": "1",
        }]}],
    })

    assert response.status_code == 409
    assert "模板页序不能修改" in response.json()["detail"]
    saved = TemplateSession.load_meta("answered-pages", str(tmp_path))
    assert saved.status == "ocr_done"
    assert [page["page_index"] for page in saved.pages] == [0, 1]


def test_expired_or_invalid_correction_candidates_cannot_authorize_history_or_block_template_save(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("invalid-correction", str(tmp_path), teacher_id=1)
    sess.has_answers = True
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1",
        "question_text": "原始题干",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    sess.save_meta()

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "invalid-correction",
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1",
            "question_text": "老师当前确认的题干",
            "question_type": "fill",
            "standard_answer": "3",
            "authorized_wrong_cycle_ids": [999999],
        }]}],
    })

    assert response.status_code == 200, response.text
    saved = TemplateSession.load_meta("invalid-correction", str(tmp_path))
    assert saved.status == "confirmed"
    assert saved.pages[0]["questions"][0]["question_text"] == "老师当前确认的题干"
    assert saved.pages[0]["questions"][0]["standard_answer"] == "3"
    with Session() as db:
        assert db.query(TemplateQuestionAuthorization).count() == 0
        assert db.query(QuestionCorrectionRelation).count() == 0


def test_saving_template_authorizes_discovered_history_unless_question_is_excluded(
    monkeypatch, tmp_path,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from services.correction_tracking import question_fingerprint

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    current = {
        "question_no": "8",
        "question_text": "判断新函数在区间上的单调性",
        "question_type": "solve",
        "standard_answer": "递增",
        "chapter": "函数",
        "topic": "单调性",
    }
    sess = TemplateSession("default-correction", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [current])
    sess.save_meta()

    db = Session()
    student = Student(
        id=10, teacher_id=1, class_id=1, name="学生甲", student_no="001",
    )
    grading = GradingResult(id=100, student_id=10, wrong_count=1)
    db.add_all([student, grading])
    db.flush()
    # Same wording with a different answer: a similar question, not the original.
    historical = {
        "question_text": "判断新函数在区间上的单调性。",
        "question_type": "solve",
        "standard_answer": "递减",
        "chapter": "函数",
        "topic": "单调性",
        "knowledge_point": "单调性",
    }
    attempt = QuestionAttemptEvent(
        id=100,
        student_id=10,
        class_id=1,
        teacher_id=1,
        grading_result_id=100,
        question_no="3",
        question_identity="history:100",
        identity_method="fingerprint",
        question_fingerprint=question_fingerprint(historical),
        question_type="solve",
        question_snapshot_json=json.dumps(historical, ensure_ascii=False),
        student_answer_snapshot_json="{}",
        final_result="wrong",
    )
    cycle = WrongQuestionCycle(
        id=100,
        student_id=10,
        class_id=1,
        teacher_id=1,
        question_identity="history:100",
        question_fingerprint=attempt.question_fingerprint,
        cycle_no=1,
        status="pending",
        opened_attempt_id=100,
    )
    db.add_all([attempt, cycle])
    db.commit()
    db.close()

    client = _client(Session, monkeypatch, raise_server_exceptions=True)
    candidates = client.post(
        "/api/batch-grade/template/correction-candidates",
        json={
            "template_id": "default-correction",
            "pages": [{"page_index": 0, "questions": [current]}],
        },
    )
    assert candidates.status_code == 200, candidates.text
    comparison = candidates.json()["candidates"][0]
    assert comparison["current_question"] == current["question_text"]
    assert comparison["candidates"][0]["historical_question"] == historical["question_text"]

    unseen = client.post(
        "/api/batch-grade/template/confirm",
        json={
            "template_id": "default-correction",
            "category": "daily",
            "pages": [{"page_index": 0, "questions": [current]}],
        },
    )
    assert unseen.status_code == 200, unseen.text
    with Session() as db:
        # Unseen discoveries never become authorized just because saving works.
        assert db.query(TemplateQuestionAuthorization).count() == 0
        assert db.query(QuestionCorrectionRelation).count() == 0

    saved = client.post(
        "/api/batch-grade/template/confirm",
        json={
            "template_id": "default-correction",
            "category": "daily",
            "pages": [{"page_index": 0, "questions": [{
                **current,
                "authorized_wrong_cycle_ids": [100],
            }]}],
        },
    )
    assert saved.status_code == 200, saved.text
    db = Session()
    assert db.query(TemplateQuestionAuthorization).filter_by(
        template_id="default-correction", target_cycle_id=100,
    ).one().revoked_at is None
    relation = db.query(QuestionCorrectionRelation).filter_by(target_cycle_id=100).one()
    assert relation.revoked_at is None
    assert relation.relation_type == "teacher_authorized_similar"
    assert relation.similarity_score == 1.0
    db.close()

    excluded_question = {**current, "correction_excluded": True}
    excluded = client.post(
        "/api/batch-grade/template/confirm",
        json={
            "template_id": "default-correction",
            "category": "daily",
            "pages": [{"page_index": 0, "questions": [excluded_question]}],
        },
    )
    assert excluded.status_code == 200, excluded.text
    persisted = TemplateSession.load_meta("default-correction", str(tmp_path))
    assert persisted.pages[0]["questions"][0]["correction_excluded"] is True
    db = Session()
    assert db.query(TemplateQuestionAuthorization).one().revoked_at is not None
    assert db.query(QuestionCorrectionRelation).one().revoked_at is not None
    db.close()


def test_revoking_one_template_keeps_shared_correction_relation(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from services.correction_tracking import question_fingerprint

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    question = {
        "question_no": "1",
        "question_text": "共享授权题",
        "question_type": "fill",
        "standard_answer": "2",
    }
    fingerprint = question_fingerprint(question)
    for template_id in ("shared-a", "shared-b"):
        sess = TemplateSession(template_id, str(tmp_path), teacher_id=1)
        sess.has_answers = True
        sess.status = "confirmed"
        sess.category = "daily"
        sess.add_page(0, [question])
        sess.save_meta()

    db = Session()
    db.add(WrongQuestionCycle(
        id=100,
        student_id=10,
        class_id=1,
        teacher_id=1,
        question_identity="shared-original",
        question_fingerprint="f" * 64,
        cycle_no=1,
        status="pending",
        opened_attempt_id=100,
    ))
    db.add_all([
        TemplateQuestionAuthorization(
            teacher_id=1,
            template_id=template_id,
            source_question_fingerprint=fingerprint,
            target_cycle_id=100,
            authorized_by=1,
        )
        for template_id in ("shared-a", "shared-b")
    ])
    db.add(QuestionCorrectionRelation(
        teacher_id=1,
        target_cycle_id=100,
        source_question_identity="template:shared-a:1",
        source_question_fingerprint=fingerprint,
        authorized_by=1,
    ))
    db.commit()
    db.close()

    client = _client(Session, monkeypatch, raise_server_exceptions=True)
    payload = {
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [question]}],
    }
    removed_a = client.post(
        "/api/batch-grade/template/confirm",
        json={"template_id": "shared-a", **payload},
    )
    assert removed_a.status_code == 200, removed_a.text

    db = Session()
    authorizations = {
        row.template_id: row.revoked_at
        for row in db.query(TemplateQuestionAuthorization).all()
    }
    assert authorizations["shared-a"] is not None
    assert authorizations["shared-b"] is None
    assert db.query(QuestionCorrectionRelation).one().revoked_at is None
    db.close()

    removed_b = client.post(
        "/api/batch-grade/template/confirm",
        json={"template_id": "shared-b", **payload},
    )
    assert removed_b.status_code == 200, removed_b.text
    db = Session()
    assert db.query(QuestionCorrectionRelation).one().revoked_at is not None
    db.close()


# ── 端点层 ──

def _make_app_and_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Base.metadata is shared across the suite; another migration test may have
    # created an intentionally legacy students table on the same metadata object.
    # Rebind this helper to the current Student contract before create_all.
    import models as current_models
    current_models.Student.__table__.create(bind=engine, checkfirst=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="老师", role="teacher", education_stage="senior"))
    from models_rate_limit import TeacherClass
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.commit()
    db.close()
    return engine, Session


def _client(Session, monkeypatch, *, raise_server_exceptions=False):
    import routers.grade as gr_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    # Through monkeypatch, so the test database is gone again after the test;
    # both modules that open sessions (recognition, grading) use it.
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade_recognition, "SessionLocal", Session)
    app = FastAPI()
    app.include_router(gr_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[_require_user] = lambda: User(id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior")
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_client_leaves_the_real_session_factory_in_place_after_the_test(monkeypatch):
    # _client used to assign routers.grade.SessionLocal outright: every later
    # test in the process then talked to this test's in-memory database, and
    # grade_pipeline's own binding (split out of grade.py) never saw it at all.
    import database
    from routers import grade_pipeline, grade_recognition

    _engine, Session = _make_app_and_session()
    with monkeypatch.context() as patch:
        _client(Session, patch)
        assert grade_recognition.SessionLocal is Session
        assert grade_pipeline.SessionLocal is Session
    assert grade_recognition.SessionLocal is database.SessionLocal
    assert grade_pipeline.SessionLocal is database.SessionLocal


def test_template_recognize_rejects_removed_review_category(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"category": "review"},
        files=[("files", ("template.png", _template_png_bytes(), "image/png"))],
    )

    assert response.status_code == 400


def _patch_template_layout(monkeypatch, grade_router):
    from routers import grade_template
    events = []

    async def fake_layout(path, *, teacher_id="system", column_mode=None):
        assert teacher_id == "1"
        events.append(("layout", path))
        return {
            "column_layout": {
                "column_count": 2,
                "columns": [
                    {"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000},
                ],
            },
            "pages": [{
                "candidates": [
                    {
                        "candidate_index": 0,
                        "coordinate_trust": "original",
                        "usable_for_original_overlay": True,
                        "template_bbox_1000": [0, 100, 500, 101],
                        "question_number_hint": "1",
                        "raw_sha256": "a" * 64,
                    },
                    {
                        "candidate_index": 1,
                        "coordinate_trust": "original",
                        "usable_for_original_overlay": True,
                        "template_bbox_1000": [500, 100, 1000, 101],
                        "question_number_hint": "2",
                        "raw_sha256": "b" * 64,
                    },
                ],
            }],
            "summary": {"total": 2},
        }

    set_grade_global(monkeypatch, "recognize_column_layout", fake_layout)
    return events


def _patch_fast_template_pre_ocr(monkeypatch, grade_router):
    from routers import grade_template
    _patch_template_layout(monkeypatch, grade_router)

    def local_canonical(source_path, _template_dir, _page_index):
        source = Path(source_path)
        with Image.open(source) as image:
            width, height = image.size
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return CanonicalTemplate(
            image_path=str(source),
            canonicalization={
                "version": "paper-canonical-v1",
                "canonical_sha256": digest,
                "canonical_width": int(width),
                "canonical_height": int(height),
                "source_image": str(source),
            },
        )

    monkeypatch.setattr(grade_template_import, "canonicalize_template_image", local_canonical)


class _ImportDeadlineClock:
    """The template import deadline's clock; it moves only when the test advances it.

    Machine load cannot use up a short deadline before the step a test stalls
    has even started: the deadline passes exactly where the test says.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _template_status_on_disk(upload_dir) -> str:
    template_id = next(Path(upload_dir).glob("template_*")).name.removeprefix("template_")
    return TemplateSession.load_meta(template_id, str(upload_dir)).status


def test_template_recognize_ocrs_each_page(
    monkeypatch,
    tmp_path,
    _ready_batch_grade_chain,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    # 关掉限流依赖（内存库无限流表数据也可，这里直接放行）
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    layout_events = _patch_template_layout(monkeypatch, gr_router)

    ocr_paths: list[str] = []

    async def _mock_with_answers(path, **kwargs):
        assert "ocr_image_path" not in kwargs
        assert "prebuilt_column_images" not in kwargs
        ocr_paths.append(path)
        return {"questions": [
            {"question_number": "1", "question_text": "1+1=?", "question_type": "fill",
             "standard_answer": "2", "answer_bbox": [500, 400, 850, 500]},
        ]}
    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _mock_with_answers)
    client = _client(Session, monkeypatch)
    r = client.post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "exam", "exam_name": "期中考试"},
        files=[("files", ("p1.png", _template_png_bytes(), "image/png"))],
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    done = [p for e, p in events if e == "done"]
    assert done, f"no done event: {events}"
    tid = done[0]["template_id"]
    assert r.headers["x-template-id"] == tid
    assert done[0]["page_count"] == 1

    state = client.get(f"/api/batch-grade/template/{tid}/recognition")
    assert state.status_code == 200
    assert state.json()["status"] == "ocr_done"
    assert state.json()["terminal"] is True
    assert state.json()["pages"] == done[0]["pages"]

    # 会话已落盘且未确认
    sess = TemplateSession.load_meta(tid, str(tmp_path))
    assert sess.status == "ocr_done"
    assert sess.category == "exam"
    assert sess.exam_name == "期中考试"
    assert sess.pages[0]["questions"][0]["standard_answer"] == "2"
    assert set(sess.page_artifacts) == {"page_1"}
    artifact = sess.page_artifacts["page_1"]
    assert artifact["blank_detection_enabled"] is False
    assert artifact["canonicalization"]["version"] == "paper-canonical-v1"
    assert artifact["canonicalization"]["canonical_sha256"] == artifact["sha256"]
    assert len(ocr_paths) == 1
    assert Path(ocr_paths[0]).is_file()
    assert layout_events and layout_events[0][0] == "layout"
    assert artifact["canonicalization"]["source_image"].endswith("source.png")
    assert _ready_batch_grade_chain == []
    assert not any(
        payload.get("accelerator_status") == "waiting_accelerator"
        for event, payload in events
        if event == "progress"
    )
    cache = gr_router._build_template_paper_cache(sess)
    assert cache._template_evidence_matcher is not None
    assert cache._require_local_evidence is True


def test_template_recognize_builds_one_box_collection_for_both_roles(
    monkeypatch,
    tmp_path,
    _ready_batch_grade_chain,
):
    """The imported artifact carries the unified boxes in both roles.

    Regression: the blank import used to persist an empty handwriting
    contract, so student batches had no exemption baseline and diffed the
    student answers away.
    """
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _mock_with_unified_boxes(path, **_kwargs):
        boxes = [{"question_number": "1", "regions": [[500, 400, 850, 500]]}]
        return {
            "questions": [
                {"question_number": "1", "question_text": "1+1=?", "question_type": "fill",
                 "standard_answer": "2", "answer_bbox": [500, 400, 850, 500]},
            ],
            "template_answer_regions": boxes,
        }

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _mock_with_unified_boxes)
    client = _client(Session, monkeypatch)
    r = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "blank", "category": "exam", "exam_name": "期中考试"},
        files=[("files", ("p1.png", _template_png_bytes(), "image/png"))],
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    done = [p for e, p in events if e == "done"]
    assert done, f"no done event: {events}"
    sess = TemplateSession.load_meta(done[0]["template_id"], str(tmp_path))
    artifact = sess.page_artifacts["page_1"]
    assert artifact["template_answer_regions"] == [
        {"question_number": "1", "regions": [[500, 400, 850, 500]]},
    ]
    assert artifact["template_handwriting_regions"] == artifact["template_answer_regions"]


def test_template_anchor_review_keeps_questions_and_persists_candidates(
    monkeypatch,
    tmp_path,
):
    """A semantic anchor conflict is reviewable, not a failed OCR session."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    anchor_candidates = [{
        "question_number": "1",
        "question_number_bbox": [80, 110, 135, 145],
        "confidence": 0.35,
        "human_confirmed": False,
        "confidence_source": "model",
    }]
    handwriting_candidates = [{
        "question_number": "1",
        "regions": [[520, 210, 610, 275]],
        "confidence": 0.30,
        "human_confirmed": False,
        "confidence_source": "model",
    }]
    anchor_diagnostics = {
        "strategy": "parallel_frozen_columns",
        "needs_review": True,
        "review_reason": "low_confidence",
        "confidence": {
            "low_confidence_question_anchors": ["1"],
            "low_confidence_handwriting_regions": ["1"],
        },
    }
    template_ocr_diagnostics = {
        "strategy": "parallel_frozen_columns",
        "column_count": 2,
    }

    async def reviewable_pipeline(_path, **kwargs):
        return {
            "error": "模板题号定位失败：置信度不足，需人工复核",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=？",
                "question_type": "fill",
                "standard_answer": "2",
            }],
            "layout_result": kwargs["layout_result"],
            "question_anchors": [],
            "template_handwriting_regions": [],
            "question_anchor_candidates": anchor_candidates,
            "template_handwriting_region_candidates": handwriting_candidates,
            "anchor_diagnostics": anchor_diagnostics,
            "template_ocr_diagnostics": template_ocr_diagnostics,
            "needs_review": True,
            "anchor_review_pending": True,
        }

    monkeypatch.setattr(
        grade_template_import,
        "recognize_template_pipeline",
        reviewable_pipeline,
    )
    client = _client(Session, monkeypatch)
    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "marked", "category": "daily"},
        files=[("files", ("review.png", _template_png_bytes(), "image/png"))],
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"]
    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    template_id = done[0]["template_id"]

    saved = TemplateSession.load_meta(template_id, str(tmp_path))
    assert saved.status == "ocr_done"
    assert saved.pages[0]["questions"][0]["standard_answer"] == "2"
    artifact = saved.page_artifacts["page_1"]
    assert "question_anchors" not in artifact
    assert "template_handwriting_regions" not in artifact
    assert isinstance(artifact.get("question_crop_plan"), dict)

    run = saved.layout_cut_runs[f"template-import-{template_id}"]
    page_run = run["pages"]["page_1"]
    assert page_run["anchor_review_pending"] is True
    assert page_run["geometry_review_pending"] is True
    assert page_run["question_anchor_candidates"] == anchor_candidates
    assert page_run["template_handwriting_region_candidates"] == (
        handwriting_candidates
    )
    assert page_run["anchor_diagnostics"] == anchor_diagnostics
    assert page_run["template_ocr_diagnostics"] == template_ocr_diagnostics

    cut_state = client.get(
        f"/api/batch-grade/template/{template_id}/cut-candidates"
    )
    assert cut_state.status_code == 200
    payload = cut_state.json()
    assert payload["anchor_review_pending_pages"] == [0]
    assert payload["geometry_review_pending_pages"] == [0]
    assert payload["pages"][0]["anchor_review_pending"] is True
    assert payload["pages"][0]["question_anchors"] == []
    assert payload["pages"][0]["template_handwriting_regions"] == []
    assert payload["pages"][0]["question_anchor_candidates"] == anchor_candidates
    assert payload["pages"][0]["template_handwriting_region_candidates"] == (
        handwriting_candidates
    )


def test_template_multi_image_association_review_keeps_answers_and_blocks_confirm(
    monkeypatch,
    tmp_path,
):
    """A multi-image association conflict keeps text, but never approves geometry."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    questions = [
        {
            "question_number": "1",
            "question_text": "1+1=？",
            "question_type": "fill",
            "standard_answer": "2",
        },
        {
            "question_number": "2",
            "question_text": "2+2=？",
            "question_type": "fill",
            "standard_answer": "4",
        },
    ]
    question_anchors = [
        {
            "question_number": "1",
            "question_number_bbox": [80, 110, 135, 145],
            "confidence": 0.98,
        },
        {
            "question_number": "2",
            "question_number_bbox": [80, 210, 135, 245],
            "confidence": 0.97,
        },
    ]
    handwriting_regions = [
        {
            "question_number": "1",
            "regions": [[520, 210, 610, 275]],
            "confidence": 0.96,
        },
        {
            "question_number": "2",
            "regions": [[520, 310, 610, 375]],
            "confidence": 0.95,
        },
    ]
    association_diagnostics = {
        "version": "template-image-association-v1",
        "status": "needs_review",
        "needs_review": True,
        "image_count": 2,
        "accepted_prefix": [2],
        "conflict": {
            "question_number": "2",
            "status": "reversed",
            "reported_value": 1,
            "parsed_index": 1,
        },
    }
    template_ocr_diagnostics = {
        "strategy": "single_multi_image_fallback",
        "column_count": 2,
        "request_count": 1,
        "column_ids": [],
        "image_association": [2],
        "image_association_diagnostics": association_diagnostics,
        "model": "qwen3.8-flash",
        "needs_review": True,
        "review_reason": "image_association_conflict",
        "image_association_error": "第 2 题的 image_index 与阅读顺序颠倒",
    }

    async def reviewable_pipeline(_path, **kwargs):
        return {
            "error": "模板多图关联需人工复核",
            "questions": questions,
            "layout_result": kwargs["layout_result"],
            "needs_review": True,
            "template_ocr_review_pending": True,
            # Anchor recognition itself succeeded. The association failure is
            # independent, so these boxes are retained only as review candidates.
            "question_anchors": [],
            "template_handwriting_regions": [],
            "question_anchor_candidates": question_anchors,
            "template_handwriting_region_candidates": handwriting_regions,
            "anchor_diagnostics": {
                "strategy": "parallel_frozen_columns",
                "needs_review": False,
            },
            "template_ocr_diagnostics": template_ocr_diagnostics,
        }

    monkeypatch.setattr(
        grade_template_import,
        "recognize_template_pipeline",
        reviewable_pipeline,
    )
    client = _client(Session, monkeypatch)
    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "marked", "category": "daily"},
        files=[("files", ("multi-image-review.png", _template_png_bytes(), "image/png"))],
    )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    assert not [payload for event, payload in events if event == "error"]
    template_id = done[0]["template_id"]
    assert [
        question["standard_answer"] for question in done[0]["pages"][0]["questions"]
    ] == ["2", "4"]
    assert all(
        "image_index" not in question
        for question in done[0]["pages"][0]["questions"]
    )

    saved = TemplateSession.load_meta(template_id, str(tmp_path))
    assert saved.status == "ocr_done"
    assert [
        question["standard_answer"] for question in saved.pages[0]["questions"]
    ] == ["2", "4"]
    assert all("image_index" not in question for question in saved.pages[0]["questions"])
    artifact = saved.page_artifacts["page_1"]
    assert "question_anchors" not in artifact
    assert "template_handwriting_regions" not in artifact

    run = saved.layout_cut_runs[f"template-import-{template_id}"]
    page_run = run["pages"]["page_1"]
    assert page_run["geometry_review_pending"] is True
    assert page_run["question_anchor_candidates"] == question_anchors
    assert page_run["template_handwriting_region_candidates"] == handwriting_regions
    assert page_run["template_ocr_diagnostics"] == template_ocr_diagnostics

    cut_response = client.get(
        f"/api/batch-grade/template/{template_id}/cut-candidates"
    )
    assert cut_response.status_code == 200, cut_response.text
    cut_payload = cut_response.json()
    assert cut_payload["geometry_review_pending_pages"] == [0]
    assert cut_payload["pages"][0]["question_anchor_candidates"] == question_anchors
    assert cut_payload["pages"][0]["template_handwriting_region_candidates"] == (
        handwriting_regions
    )
    assert cut_payload["pages"][0]["template_ocr_diagnostics"] == (
        template_ocr_diagnostics
    )
    assert cut_payload["pages"][0]["template_ocr_diagnostics"][
        "image_association_diagnostics"
    ] == association_diagnostics
    assert cut_payload["pages"][0]["question_anchors"] == []
    assert cut_payload["pages"][0]["template_handwriting_regions"] == []

    confirm_response = client.post(
        "/api/batch-grade/template/confirm",
        json={
            "template_id": template_id,
            "category": "daily",
            "pages": [{
                "page_index": 0,
                "questions": [
                    {
                        "question_no": "1",
                        "question_text": "1+1=？",
                        "question_type": "fill",
                        "standard_answer": "2",
                    },
                    {
                        "question_no": "2",
                        "question_text": "2+2=？",
                        "question_type": "fill",
                        "standard_answer": "4",
                    },
                ],
            }],
        },
    )
    assert confirm_response.status_code == 409, confirm_response.text
    assert "仍需人工确认" in confirm_response.json()["detail"]


def test_template_nonsemantic_error_with_questions_still_fails(
    monkeypatch,
    tmp_path,
):
    """Questions alone cannot turn a provider failure into a review draft."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def failed_pipeline(_path, **kwargs):
        return {
            "error": "provider transport failed",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=？",
                "question_type": "fill",
                "standard_answer": "2",
            }],
            "layout_result": kwargs["layout_result"],
        }

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", failed_pipeline)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "marked", "category": "daily"},
        files=[("files", ("failed.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "done"]
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "TEMPLATE_OCR_FAILED",
        "message": "模板题目识别失败，请稍后重试；若持续发生请联系管理员检查视觉识别服务",
    }]
    template_dir = next(tmp_path.glob("template_*"))
    saved = TemplateSession.load_meta(
        template_dir.name.removeprefix("template_"),
        str(tmp_path),
    )
    assert saved.status == "failed"


def test_template_recognize_sends_canonical_page_without_legacy_ocr_crops(
    monkeypatch,
    tmp_path,
):
    """The router must not delay full-page Qwen on obsolete transcription crops."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    def local_canonical(source_path, _template_dir, _page_index):
        source = Path(source_path)
        with Image.open(source) as image:
            width, height = image.size
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return CanonicalTemplate(
            image_path=str(source),
            canonicalization={
                "version": "paper-canonical-v1",
                "canonical_sha256": digest,
                "canonical_width": int(width),
                "canonical_height": int(height),
                "source_image": str(source),
            },
        )

    async def stable_layout(path, *, teacher_id="system", **_kwargs):
        assert teacher_id == "1"
        source = Path(path)
        with Image.open(source) as image:
            width, height = image.size
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        columns = []
        for index, (left, right) in enumerate(((0, 500), (500, 1000)), 1):
            crop_left = round(left * width / 1000)
            crop_right = round(right * width / 1000)
            columns.append({
                "column_index": index,
                "column_order": index,
                "column_id": f"stable-{index}",
                "x1_1000": left,
                "x2_1000": right,
                "crop_left_px": crop_left,
                "crop_right_px": crop_right,
                "crop_box_px": [crop_left, 0, crop_right, height],
                "actual_pixel_endpoints": {"left": crop_left, "right": crop_right},
                "crop_coordinate_space": "source_pixels",
                "crop_endpoint_version": "rounded_source_pixels_v1",
                "source_width": width,
                "source_height": height,
                "image_sha256": digest,
                "layout_revision": 1,
            })
        return {
            "column_layout": {
                "column_count": 2,
                "columns": columns,
                "source_width": width,
                "source_height": height,
                "image_sha256": digest,
                "layout_revision": 1,
            },
            "pages": [{"candidates": []}],
        }

    monkeypatch.setattr(grade_template_import, "canonicalize_template_image", local_canonical)
    set_grade_global(monkeypatch, "recognize_column_layout", stable_layout)
    observed = {}

    async def fake_pipeline(path, **kwargs):
        observed["path"] = path
        observed.update(kwargs)
        assert Path(path).is_file()
        assert "ocr_image_path" not in kwargs
        assert "prebuilt_column_images" not in kwargs
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", fake_pipeline)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("stable.png", _template_png_bytes(), "image/png"))],
    )

    assert response.status_code == 200, response.text
    assert Path(observed["path"]).is_file()
    assert observed["layout_result"]["column_layout"]["column_count"] == 2


def test_template_recognition_status_is_owner_scoped(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    sess = TemplateSession("owned-recognition", str(tmp_path), teacher_id=1)
    sess.status = "recognizing"
    sess.recognition_started_at = "2026-08-10T01:00:00+00:00"
    sess.recognition_deadline_at = "2099-08-10T01:05:00+00:00"
    sess.save_meta()

    client = _client(Session, monkeypatch)
    own = client.get("/api/batch-grade/template/owned-recognition/recognition")
    assert own.status_code == 200
    assert own.json()["status"] == "recognizing"
    assert own.json()["terminal"] is False

    client.app.dependency_overrides[_require_user] = lambda: User(
        id=2,
        name="其他老师",
        role="teacher",
        current_class_id=1,
    )
    foreign = client.get("/api/batch-grade/template/owned-recognition/recognition")
    assert foreign.status_code == 403


def test_stale_template_recognition_is_reported_unusable(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    sess = TemplateSession("stale-recognition", str(tmp_path), teacher_id=1)
    sess.status = "recognizing"
    sess.recognition_started_at = "2020-01-01T00:00:00+00:00"
    sess.recognition_deadline_at = "2020-01-01T00:05:00+00:00"
    sess.save_meta()

    state = _client(Session, monkeypatch).get(
        "/api/batch-grade/template/stale-recognition/recognition"
    )
    assert state.status_code == 200
    assert state.json()["status"] == "failed"
    assert state.json()["terminal"] is True
    assert state.json()["error_code"] == "TEMPLATE_IMPORT_TIMEOUT"
    assert state.json()["pages"] == []


def test_template_recognize_accepts_desktop_canonical_bytes_without_server_fallback(
    monkeypatch,
    tmp_path,
    _ready_batch_grade_chain,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    raw = _template_png_bytes("DESKTOP")
    canonical_bytes, metadata = _canonical_submission(tmp_path, raw, "normal")
    ocr_bytes = []

    async def fake_ocr(path, **kwargs):
        assert "ocr_image_path" not in kwargs
        assert "prebuilt_column_images" not in kwargs
        ocr_bytes.append(Path(path).read_bytes())
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", fake_ocr)
    async def hardware_probe_forbidden(_user):
        raise AssertionError("client candidate queried accelerator readiness")

    monkeypatch.setattr(
        grade_availability,
        "batch_grade_availability",
        hardware_probe_forbidden,
    )
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={
            "has_answers": "true",
            "category": "daily",
            "canonical_metadata": json.dumps([metadata]),
        },
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )

    assert response.status_code == 200
    done = [payload for event, payload in _parse_sse(response.text) if event == "done"]
    assert len(done) == 1
    session = TemplateSession.load_meta(done[0]["template_id"], str(tmp_path / "uploads"))
    artifact = session.page_artifacts["page_1"]
    artifact_path = Path(session.template_dir) / artifact["template_image"]
    assert len(ocr_bytes) == 1
    assert ocr_bytes[0] != raw
    with Image.open(io.BytesIO(ocr_bytes[0])) as ocr_image, Image.open(
        io.BytesIO(canonical_bytes)
    ) as canonical_image:
        assert ocr_image.size == canonical_image.size
    assert artifact_path.read_bytes() == canonical_bytes
    assert artifact["sha256"] == metadata["canonical_sha256"]
    assert artifact["canonicalization"]["source_image"].startswith("canonical/page_000/")
    assert _ready_batch_grade_chain == []


def test_template_recognize_uses_local_canonicalization_without_accelerator(
    monkeypatch,
    tmp_path,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def fake_ocr(_path, **_kwargs):
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", fake_ocr)

    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[("files", ("raw.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert not any(
        payload.get("accelerator_status") == "waiting_accelerator"
        for event, payload in events
        if event == "progress"
    )
    assert [payload for event, payload in events if event == "done"]


@pytest.mark.parametrize("state", ["unavailable", "fault"])
def test_template_local_canonicalization_failure_keeps_source(
    monkeypatch,
    tmp_path,
    state,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    upload_dir = tmp_path / "uploads"
    raw = _template_png_bytes(state)
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    def fail_local(*_args, **_kwargs):
        raise OSError(f"local canonicalization {state}")

    monkeypatch.setattr(grade_template_import, "canonicalize_template_image", fail_local)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[("files", ("raw.png", raw, "image/png"))],
    )

    errors = [
        payload for event, payload in _parse_sse(response.text)
        if event == "error"
    ]
    assert len(errors) == 1
    assert errors[0]["code"] == "TEMPLATE_CANONICALIZATION_FAILED"
    assert "页面矫正失败" in errors[0]["message"]
    template_dir = next(upload_dir.glob("template_*"))
    session = TemplateSession.load_meta(
        template_dir.name.removeprefix("template_"),
        str(upload_dir),
    )
    assert session.status == "failed"
    assert any(
        path.read_bytes() == raw
        for path in template_dir.glob("*.png")
    )


def test_template_full_page_model_skips_legacy_ocr_evidence_accelerator(
    monkeypatch,
    tmp_path,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    upload_dir = tmp_path / "uploads"
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)
    seen_paths = []

    async def fake_ocr(path, **kwargs):
        seen_paths.append(path)
        assert "ocr_image_path" not in kwargs
        assert "prebuilt_column_images" not in kwargs
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", fake_ocr)

    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[("files", ("raw.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert len(seen_paths) == 1
    assert Path(seen_paths[0]).is_file()
    assert not any(
        payload.get("accelerator_status") == "waiting_accelerator"
        for event, payload in events
        if event == "progress"
    )
    assert [payload for event, payload in events if event == "done"]


def test_template_recognize_rejects_desktop_hash_mismatch_without_fallback(
    monkeypatch,
    tmp_path,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    raw = _template_png_bytes("HASH")
    canonical_bytes, metadata = _canonical_submission(tmp_path, raw, "hash")
    metadata["canonical_sha256"] = "0" * 64
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={
            "has_answers": "true",
            "category": "daily",
            "canonical_metadata": json.dumps([metadata]),
        },
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "TEMPLATE_CANONICAL_CANDIDATE_INVALID",
        "message": "客户端标准模板候选校验失败；本次模板不可用，请重新导入",
    }]
    assert not [payload for event, payload in events if event == "done"]


def test_template_recognize_rejects_malformed_desktop_candidate_pair(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    raw = _template_png_bytes("PAIR")
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", raw, "image/png")),
        ],
    )

    assert response.status_code == 422
    assert "canonical_metadata" in response.json()["detail"]

    count_mismatch = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={
            "has_answers": "true",
            "category": "daily",
            "canonical_metadata": "[]",
        },
        files=[
            ("files", ("raw-1.png", raw, "image/png")),
            ("files", ("raw-2.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", raw, "image/png")),
        ],
    )
    assert count_mismatch.status_code == 422
    assert "数量" in count_mismatch.json()["detail"]

    canonical_bytes, metadata = _canonical_submission(tmp_path, raw, "path-field")
    metadata["source_image"] = "C:/client/must-not-cross-wire.png"
    client_path = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={
            "has_answers": "true",
            "category": "daily",
            "canonical_metadata": json.dumps([metadata]),
        },
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )
    assert client_path.status_code == 422
    assert "invalid fields" in client_path.json()["detail"]


def test_template_candidate_files_do_not_reduce_the_template_page_limit(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(grade_template_import_form, "MAX_TEMPLATE_PAGES", 1)
    raw = _template_png_bytes()
    canonical_bytes, metadata = _canonical_submission(tmp_path, raw, "count-limit")
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={
            "has_answers": "true",
            "category": "daily",
            "canonical_metadata": json.dumps([metadata]),
        },
        files=[
            ("files", ("raw.png", raw, "image/png")),
            ("canonical_files", ("canonical.png", canonical_bytes, "image/png")),
        ],
    )

    assert response.status_code == 200

    over_limit = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[
            ("files", ("raw-1.png", raw, "image/png")),
            ("files", ("raw-2.png", raw, "image/png")),
        ],
    )
    assert over_limit.status_code == 413
    assert "最多上传 1 页模板原图" in over_limit.json()["detail"]


def test_blank_template_recognize_builds_server_owned_visual_artifact(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    from services.template_evidence import verify_template_artifacts

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def fake_blank(_path, **_kwargs):
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=____",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }], "question_anchors": [{
            "question_number": "1",
            "question_number_bbox": [100, 100, 140, 125],
        }], "template_handwriting_regions": [{
            "question_number": "1",
            "regions": [[500, 400, 850, 500]],
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", fake_blank)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    session = TemplateSession.load_meta(done[0]["template_id"], str(tmp_path))
    assert set(session.page_artifacts) == {"page_1"}
    verified = verify_template_artifacts(
        session.template_dir,
        session.page_artifacts,
    )
    assert "answer_regions" not in verified["page_1"]
    assert verified["page_1"]["question_schema"][0]["question_number"] == "1"
    # All template handwriting is handled as differential exemption; the old
    # blank-template detector is disabled for every new template variant.
    assert verified["page_1"]["blank_detection_enabled"] is False
    assert verified["page_1"]["canonicalization"]["version"] == "paper-canonical-v1"


def test_template_recognize_rejects_blank_before_ocr(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    ocr_called = False

    async def _unexpected_ocr(_path, **_kwargs):
        nonlocal ocr_called
        ocr_called = True
        return {"questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _unexpected_ocr)
    stream = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(stream, format="PNG")
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "true", "category": "daily"},
        files=[("files", ("blank.png", stream.getvalue(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert ocr_called is False
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "TEMPLATE_CANONICALIZATION_FAILED",
        "message": "模板页面矫正失败，请确认图片边界完整、方向正确且清晰后重新导入",
    }]
    assert not [payload for event, payload in events if event == "done"]


def test_blank_template_vision_auth_failure_never_creates_confirmable_empty_template(monkeypatch, tmp_path):
    """A failed vision request must be visible to the teacher, not become page 1 (0 questions)."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _vision_auth_failed(_path, **_kwargs):
        return {"error": "Client error '401 Unauthorized' for vision endpoint", "questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _vision_auth_failed)
    client = _client(Session, monkeypatch)

    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "exam", "exam_name": "期末考试"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    errors = [payload for event, payload in events if event == "error"]
    assert _legacy_error_with_diagnostic(errors) == [{
        "code": "VISION_AUTH_FAILED",
        "message": "视觉识别服务认证失败，请联系管理员检查视觉识别服务的密钥配置",
    }]
    assert not [payload for event, payload in events if event == "done"]
    saved = TemplateSession.load_meta(next(tmp_path.glob("template_*")).name.removeprefix("template_"), str(tmp_path))
    assert saved.status == "failed"
    assert saved.error_code == "VISION_AUTH_FAILED"
    assert saved.error == "视觉识别服务认证失败，请联系管理员检查视觉识别服务的密钥配置"
    state = client.get(f"/api/batch-grade/template/{saved.template_id}/recognition")
    assert state.status_code == 200
    assert state.json()["status"] == "failed"
    assert state.json()["error_code"] == "VISION_AUTH_FAILED"
    assert state.json()["pages"] == []


def test_blank_template_empty_model_result_never_creates_confirmable_empty_template(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _empty_result(_path, **_kwargs):
        return {"questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _empty_result)
    client = _client(Session, monkeypatch)
    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "TEMPLATE_NO_QUESTIONS",
        "message": "模板未识别到任何题目，请检查图片是否完整、清晰后重新上传",
    }]
    assert not [payload for event, payload in events if event == "done"]


def test_template_contract_error_is_not_reported_as_visual_service_failure(monkeypatch, tmp_path):
    """A model-output contract error must identify the bad field, not blame the service."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _invalid_contract(_path, **_kwargs):
        return {"error": "模板 OCR 第 10 题题型无效", "questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _invalid_contract)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "TEMPLATE_OUTPUT_INVALID",
        "message": "模板识别结果格式异常，请重新识别；若仍失败请联系管理员",
    }]
    assert not [payload for event, payload in events if event == "done"]


def test_template_pool_timeout_has_a_truthful_retryable_error(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _timed_out(_path, **_kwargs):
        return {"error": "absolute_attempt_ttl", "questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _timed_out)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "VISION_TIMEOUT",
        "message": "视觉识别服务响应超时，本次未返回完整题目；请重新识别",
    }]
    assert not [payload for event, payload in events if event == "done"]


def test_template_total_deadline_reports_unusable_and_cleans_ocr_task(
    monkeypatch,
    tmp_path,
):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    clock = _ImportDeadlineClock()
    set_grade_global(monkeypatch, "_template_import_clock", clock)
    monkeypatch.setattr(grade_template_import, "_template_import_deadline_seconds", lambda _count: 0.1)
    monkeypatch.setattr(grade_template_import_steps, "_TEMPLATE_COMPONENT_HEARTBEAT_S", 0.01)
    _patch_fast_template_pre_ocr(monkeypatch, gr_router)
    cancelled = threading.Event()
    status_when_cancelled = []

    async def hanging_ocr(_path, **_kwargs):
        # The import's time runs out while OCR is running.
        clock.advance(0.1)
        try:
            await asyncio.Event().wait()
        finally:
            status_when_cancelled.append(_template_status_on_disk(tmp_path))
            cancelled.set()

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", hanging_ocr)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    errors = [
        payload for event, payload in _parse_sse(response.text) if event == "error"
    ]
    assert len(errors) == 1
    assert errors[0]["code"] == "TEMPLATE_IMPORT_TIMEOUT"
    assert "本次模板不可用" in errors[0]["message"]
    assert cancelled.is_set()
    # The step was stopped before the template was reported unusable.
    assert status_when_cancelled == ["recognizing"]
    template_id = next(tmp_path.glob("template_*")).name.removeprefix("template_")
    failed = TemplateSession.load_meta(template_id, str(tmp_path))
    assert failed.status == "failed"
    assert failed.error == errors[0]["message"]


def test_template_deadline_during_layout_is_reported_as_the_import_timeout(
    monkeypatch,
    tmp_path,
):
    # The deadline is a TimeoutError (an OSError); the layout step must not
    # report it as a column-crop failure.
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    clock = _ImportDeadlineClock()
    set_grade_global(monkeypatch, "_template_import_clock", clock)
    monkeypatch.setattr(grade_template_import, "_template_import_deadline_seconds", lambda _count: 0.1)
    monkeypatch.setattr(grade_template_import_steps, "_TEMPLATE_COMPONENT_HEARTBEAT_S", 0.01)
    _patch_fast_template_pre_ocr(monkeypatch, gr_router)
    cancelled = threading.Event()
    status_when_cancelled = []

    async def hanging_layout(_path, **_kwargs):
        # The import's time runs out while the layout step is running.
        clock.advance(0.1)
        try:
            await asyncio.Event().wait()
        finally:
            status_when_cancelled.append(_template_status_on_disk(tmp_path))
            cancelled.set()

    set_grade_global(monkeypatch, "recognize_column_layout", hanging_layout)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    errors = [
        payload for event, payload in _parse_sse(response.text) if event == "error"
    ]
    assert [error["code"] for error in errors] == ["TEMPLATE_IMPORT_TIMEOUT"]
    assert cancelled.is_set()
    # The step was stopped before the template was reported unusable.
    assert status_when_cancelled == ["recognizing"]


def test_template_stream_disconnect_keeps_background_ocr_running(monkeypatch, tmp_path):
    import routers.grade as gr_router
    from routers import grade_template

    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    monkeypatch.setattr(grade_template_import, "_template_import_deadline_seconds", lambda _count: 60.0)
    monkeypatch.setattr(grade_template_import_steps, "_TEMPLATE_COMPONENT_HEARTBEAT_S", 0.01)
    _patch_template_layout(monkeypatch, gr_router)
    cancelled = asyncio.Event()
    ocr_started = asyncio.Event()
    release = asyncio.Event()

    async def hanging_ocr(_path, **_kwargs):
        ocr_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
            "answer_bbox": [500, 400, 850, 500],
        }], "question_anchors": [{
            "question_number": "1",
            "question_number_bbox": [100, 100, 140, 125],
        }], "template_handwriting_regions": [{
            "question_number": "1",
            "regions": [[500, 400, 850, 500]],
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", hanging_ocr)

    async def scenario():
        upload = UploadFile(
            file=io.BytesIO(_template_png_bytes()),
            filename="blank.png",
            headers=Headers({"content-type": "image/png"}),
        )
        response = await grade_template.batch_grade_template_recognize(
            files=[upload],
            canonical_files=None,
            canonical_metadata=None,
            has_answers=False,
            category="daily",
            exam_name="",
            user=SimpleNamespace(id=1, education_stage="senior"),
            db=object(),
        )
        template_id = response.headers["x-template-id"]
        iterator = response.body_iterator.__aiter__()
        first_chunk = await anext(iterator)
        assert f'"template_id": "{template_id}"' in first_chunk
        async with asyncio.timeout(2):
            await ocr_started.wait()
        await iterator.aclose()
        await asyncio.sleep(0.02)
        assert not cancelled.is_set()
        active = TemplateSession.load_meta(template_id, str(tmp_path))
        assert active.status == "recognizing"

        release.set()
        async with asyncio.timeout(2):
            while True:
                completed = TemplateSession.load_meta(template_id, str(tmp_path))
                if completed.status != "recognizing":
                    break
                await asyncio.sleep(0.01)
        assert completed.status == "ocr_done"
        assert completed.pages[0]["questions"][0]["standard_answer"] == "2"
        assert not cancelled.is_set()
        return template_id

    template_id = asyncio.run(scenario())
    completed = TemplateSession.load_meta(template_id, str(tmp_path))
    assert completed.status == "ocr_done"


def test_template_recognition_progress_discloses_page_scaled_stop(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    async def _success(_path, **_kwargs):
        return {"questions": [{
            "question_number": "1",
            "question_text": "1+1=?",
            "question_type": "fill",
            "standard_answer": "2",
        }]}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _success)
    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[("files", ("blank.png", _template_png_bytes(), "image/png"))],
    )

    events = _parse_sse(response.text)
    first_progress = next(payload for event, payload in events if event == "progress")
    assert "正在识别 1 页模板" in first_progress["message"]
    deadline_minutes = max(
        1,
        (int(grade_template._template_import_deadline_seconds(1)) + 59) // 60,
    )
    assert f"本次总时限为 {deadline_minutes} 分钟" in first_progress["message"]
    assert "处理中会持续显示进度" in first_progress["message"]


def test_partial_template_ocr_failure_can_never_be_confirmed(monkeypatch, tmp_path):
    """A later-page OCR failure must block confirm even if an earlier page was saved."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    from routers import grade_template
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)
    _patch_template_layout(monkeypatch, gr_router)

    attempts = 0

    async def _first_page_then_auth_failure(_path, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"questions": [{
                "question_number": "1", "question_text": "1+1=?",
                "question_type": "fill", "standard_answer": "2",
            }]}
        return {"error": "401 Unauthorized", "questions": []}

    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", _first_page_then_auth_failure)
    client = _client(Session, monkeypatch)
    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"has_answers": "false", "category": "daily"},
        files=[
            ("files", ("page-1.png", _template_png_bytes("A"), "image/png")),
            ("files", ("page-2.png", _template_png_bytes("B"), "image/png")),
        ],
    )
    events = _parse_sse(response.text)
    assert _legacy_error_with_diagnostic([payload for event, payload in events if event == "error"]) == [{
        "code": "VISION_AUTH_FAILED",
        "message": "视觉识别服务认证失败，请联系管理员检查视觉识别服务的密钥配置",
    }]
    assert not [payload for event, payload in events if event == "done"]

    template_id = next(tmp_path.glob("template_*")).name.removeprefix("template_")
    failed = TemplateSession.load_meta(template_id, str(tmp_path))
    assert failed.status == "failed"
    assert len(failed.pages) == 1

    confirm = client.post("/api/batch-grade/template/confirm", json={
        "template_id": template_id,
        "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "1+1=?",
            "question_type": "fill", "standard_answer": "2",
        }]}],
    })
    assert confirm.status_code == 409
    assert TemplateSession.load_meta(template_id, str(tmp_path)).status == "failed"


def test_template_confirm_rejects_foreign_owner(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    foreign = TemplateSession("foreign-template", str(tmp_path))
    foreign.status = "ocr_done"
    foreign.add_page(0, [{
        "question_number": "1", "question_text": "1+1=?",
        "question_type": "fill", "standard_answer": "2",
    }])
    foreign.save_meta()
    with open(foreign.meta_path, "r", encoding="utf-8") as f:
        foreign_meta = json.load(f)
    foreign_meta["teacher_id"] = 2
    with open(foreign.meta_path, "w", encoding="utf-8") as f:
        json.dump(foreign_meta, f)

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "foreign-template", "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "1+1=?",
            "question_type": "fill", "standard_answer": "2",
        }]}],
    })
    assert response.status_code == 403
    assert TemplateSession.load_meta("foreign-template", str(tmp_path)).status == "ocr_done"


def test_template_confirm_rejects_path_traversal_template_id(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "/../../outside", "category": "daily", "pages": [],
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "非法的模板标识"


def test_template_save_is_repeatable_and_bank_import_is_separate_idempotent(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("tplc", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.category = "exam"
    sess.exam_name = "期中考试"
    sess.add_page(0, [{"question_number": "1", "question_text": "原始OCR题干", "standard_answer": "旧"}])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    payload = {
        "template_id": "tplc", "category": "exam", "exam_name": "期中考试",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "老师第一次修改",
            "question_type": "solve", "standard_answer": "答案一",
            "subquestion_count": 2, "subquestion_scores": [5, 7, 0],
            "answer_variants": ["等价答案"], "solution_steps": ["第一步", "第二步"],
            "grading_method": "process", "chapter": "代数", "topic": "方程",
        }]}],
    }
    first = client.post("/api/batch-grade/template/confirm", json=payload)
    assert first.status_code == 200
    assert first.json()["bank_imported"] is False
    db = Session()
    assert db.query(QuestionBank).count() == 0
    db.close()

    payload["pages"][0]["questions"][0]["question_text"] = "老师再次修改"
    payload["pages"][0]["questions"][0]["standard_answer"] = "最终答案"
    second = client.post("/api/batch-grade/template/confirm", json=payload)
    assert second.status_code == 200
    saved = TemplateSession.load_meta("tplc", str(tmp_path))
    assert saved.status == "confirmed"
    assert saved.pages[0]["questions"][0]["question_text"] == "老师再次修改"
    assert saved.pages[0]["questions"][0]["standard_answer"] == "最终答案"

    imported = client.post("/api/batch-grade/template/tplc/bank-import")
    assert imported.status_code == 200
    assert imported.json()["already_imported"] is False
    assert imported.json()["added_to_bank"] == 1
    db = Session()
    qb = db.query(QuestionBank).one()
    assert qb.question == "老师再次修改"
    assert qb.answer == "最终答案"
    assert qb.get_variants() == ["等价答案"]
    assert qb.get_solution_steps() == ["第一步", "第二步"]
    db.close()

    repeated = client.post("/api/batch-grade/template/tplc/bank-import")
    assert repeated.status_code == 200
    assert repeated.json()["already_imported"] is True
    assert repeated.json()["added_to_bank"] == 0
    db = Session()
    assert db.query(QuestionBank).count() == 1
    db.close()


def test_template_confirm_skips_answerless_questions(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("tpla", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": ""}])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    r = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "tpla",
        "category": "daily",
        "pages": [{
            "page_index": 0,
            "questions": [
                {"question_no": "1", "question_text": "有题干无答案",
                 "question_type": "solve", "standard_answer": ""},
            ],
        }],
    })
    assert r.status_code == 200
    assert r.json()["bank_imported"] is False
    imported = client.post("/api/batch-grade/template/tpla/bank-import")
    assert imported.status_code == 200
    assert imported.json()["added_to_bank"] == 0
    assert imported.json()["imported_count"] == 0
    # 无答案题不入库，但仍留在模板页里供批改复用
    db = Session()
    assert db.query(QuestionBank).count() == 0
    db.close()
    saved = TemplateSession.load_meta("tpla", str(tmp_path))
    assert saved.pages[0]["questions"][0]["question_text"] == "有题干无答案"


def test_template_confirm_preserves_custom_fill_score_contract(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("tplfill", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.add_page(0, [{
        "question_number": "8", "question_text": "x=___，y=___",
        "question_type": "fill", "standard_answer": "2；3",
    }])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    response = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "tplfill", "category": "exam", "exam_name": "期末",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "8", "question_text": "x=___，y=___", "question_type": "fill",
            "standard_answer": "2；3", "blank_count": 2, "answer_slots": ["2", "3"],
            "fill_scoring_mode": "custom", "blank_scores": [2, 3], "max_score": 5,
        }]}],
    })
    assert response.status_code == 200
    saved = TemplateSession.load_meta("tplfill", str(tmp_path))
    answer = saved.build_answer_map()["8"]
    assert answer["fill_scoring_mode"] == "custom"
    assert answer["blank_scores"] == [2.0, 3.0]
    assert answer["max_score"] == 5.0


# ── step 3：有模板时 recognize 走模板权威路（跳聚类/题库匹配/AI建议）──

def test_build_answer_map_flattens_pages():
    sess = TemplateSession("t", "/tmp")
    sess.pages = [
        TemplateSession.make_page(0, [
            {"question_number": "1", "question_text": "题一", "question_type": "choice",
             "standard_answer": "A"},
        ]),
        TemplateSession.make_page(1, [
            {"question_number": "2", "question_text": "题二", "question_type": "solve",
             "standard_answer": "42"},
        ]),
    ]
    am = sess.build_answer_map()
    assert set(am.keys()) == {"1", "2"}
    assert am["1"]["matched"] is True
    assert am["1"]["source"] == "template_confirmed"
    assert am["1"]["standard_answer"] == "A"
    assert am["2"]["question_text"] == "题二"
    assert am["2"]["question_type"] == "solve"


def test_recognize_with_template_uses_template_authority(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    # 已确认模板：题干+答案是权威
    sess = TemplateSession("tconf", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [
        {"question_number": "1", "question_text": "模板权威题干", "question_type": "fill",
         "standard_answer": "模板答案"},
    ])
    sess.confirm_current_contract(1)
    sess.save_meta()

    # 学生卷 OCR：题干可能有噪声、答案是学生写的；题干与标准答案只取冻结模板。
    async def _mock_recognize_paper(
        path, batch_id, paper_cache, teacher_id="", paper_id="",
    ):
        assert paper_cache._baselines
        assert "[page_type_id=page_1]" in paper_cache._build_answer_only_prompt()
        return {"student_name": "张三", "questions": [
            {"question_number": "1", "question_text": "学生卷噪声题干",
             "student_answer": "学生写的", "question_type": "solve"},
        ]}
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", _mock_recognize_paper)

    client = _client(Session, monkeypatch)
    r = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "mode": "precise", "template_id": "tconf"},
        files=[(
            "files",
            (
                "s1.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("STUDENT"),
                "image/jpeg",
            ),
        )],
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    done = [p for e, p in events if e == "done"]
    assert done, f"no done event: {events}"

    # 1) 题库匹配 / AI 建议均未被调用

    # 2) done 返回的题干/答案是模板权威值，不是学生卷 OCR
    q = done[0]["questions"][0]
    assert q["content"] == "模板权威题干"
    assert q["answer"] == "模板答案"
    assert q["matched"] is True
    assert q["source"] == "template_confirmed"
    assert TemplateSession.load_meta("tconf", str(tmp_path)).status == "frozen"

    frozen_edit = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "tconf", "category": "daily",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "识别后试图改题干",
            "question_type": "fill", "standard_answer": "新答案",
        }]}],
    })
    assert frozen_edit.status_code == 409
    assert "已冻结" in frozen_edit.json()["detail"]


def test_recognize_template_category_mismatch_keeps_confirmed(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("category-mismatch", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "exam"
    sess.exam_name = "期中"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": "A"}])
    sess.save_meta()

    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "category-mismatch"},
        files=[("files", ("s1.png", b"\x89PNG\r\n\x1a\nimg", "image/png"))],
    )

    assert response.status_code == 409
    assert TemplateSession.load_meta("category-mismatch", str(tmp_path)).status == "confirmed"


def test_recognize_template_exam_name_mismatch_remains_editable(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("exam-mismatch", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "exam"
    sess.exam_name = "期中"
    sess.add_page(0, [{"question_number": "1", "question_text": "旧题干", "standard_answer": "A"}])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    rejected = client.post(
        "/api/batch-grade/recognize",
        data={"category": "exam", "exam_name": "期末", "template_id": "exam-mismatch"},
        files=[("files", ("s1.png", b"\x89PNG\r\n\x1a\nimg", "image/png"))],
    )
    assert rejected.status_code == 409
    assert TemplateSession.load_meta("exam-mismatch", str(tmp_path)).status == "confirmed"

    edited = client.post("/api/batch-grade/template/confirm", json={
        "template_id": "exam-mismatch", "category": "exam", "exam_name": "期中",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "1", "question_text": "仍可编辑", "question_type": "choice",
            "choice_mode": "single",
            "standard_answer": "B",
        }]}],
    })
    assert edited.status_code == 200
    saved = TemplateSession.load_meta("exam-mismatch", str(tmp_path))
    assert saved.status == "confirmed"
    assert saved.pages[0]["questions"][0]["question_text"] == "仍可编辑"


def test_recognize_upload_failures_do_not_freeze_template(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    sess = TemplateSession("upload-failure", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": "A"}])
    sess.confirm_current_contract(1)
    sess.save_meta()
    client = _client(Session, monkeypatch)

    set_grade_global(monkeypatch, "_validate_client_ocr_ready_upload", lambda filename, content: {"safe": False, "reason": "测试拒绝"})
    unsafe = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "upload-failure"},
        files=[(
            "files",
            (
                "unsafe.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("UNSAFE"),
                "image/jpeg",
            ),
        )],
    )
    assert unsafe.status_code == 400
    assert TemplateSession.load_meta("upload-failure", str(tmp_path)).status == "confirmed"

    async def _too_large(_upload, _path, _max_size):
        return -1, ""

    set_grade_global(monkeypatch, "_stream_to_disk_capped_with_sha256", _too_large)
    set_grade_global(monkeypatch, "_validate_client_ocr_ready_upload", lambda filename, path: {"safe": True, "reason": ""})
    oversized = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "template_id": "upload-failure"},
        files=[(
            "files",
            ("large.ocr-ready-v1.jpg", b"content", "image/jpeg"),
        )],
    )
    assert oversized.status_code == 413
    assert TemplateSession.load_meta("upload-failure", str(tmp_path)).status == "confirmed"


def test_concurrent_template_recognize_cannot_overwrite_freeze(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    async def _mock_identity(*args, **kwargs):
        return {"name": "张三", "student_no": "", "confidence": 1}

    monkeypatch.setattr(grade_papers, "recognize_student_info_with_roster", _mock_identity)

    sess = TemplateSession("concurrent-freeze", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": "A"}])
    sess.confirm_current_contract(1)
    sess.save_meta()

    async def _mock_recognize_paper(
        _path,
        _batch_id,
        paper_cache,
        teacher_id="",
        paper_id="",
    ):
        assert paper_cache._baselines
        return {"student_name": "张三", "questions": [{
            "question_number": "1", "question_text": "题", "student_answer": "A",
        }]}

    monkeypatch.setattr(
        grade_papers,
        "recognize_paper_with_cache",
        _mock_recognize_paper,
    )
    original_save_meta = TemplateSession.save_meta
    freeze_entered = threading.Event()
    allow_freeze = threading.Event()

    def _delayed_freeze(self):
        if self.template_id == "concurrent-freeze" and self.status == "frozen":
            freeze_entered.set()
            assert allow_freeze.wait(timeout=5)
        return original_save_meta(self)

    monkeypatch.setattr(TemplateSession, "save_meta", _delayed_freeze)

    def _recognize():
        with _client(Session, monkeypatch) as client:
            return client.post(
                "/api/batch-grade/recognize",
                data={"category": "daily", "template_id": "concurrent-freeze"},
                files=[(
                    "files",
                    (
                        "s1.ocr-ready-v1.jpg",
                        _ocr_ready_jpeg_bytes("CONCURRENT"),
                        "image/jpeg",
                    ),
                )],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_recognize)
        assert freeze_entered.wait(timeout=5)
        second = pool.submit(_recognize)
        second_response = second.result(timeout=5)
        allow_freeze.set()
        first_response = first.result(timeout=10)

    assert sorted([first_response.status_code, second_response.status_code]) == [200, 409]
    assert TemplateSession.load_meta("concurrent-freeze", str(tmp_path)).status == "frozen"


def test_recognize_with_template_preserves_custom_fill_scoring_contract(monkeypatch, tmp_path):
    """Template authority must retain custom blank scores through the SSE confirmation payload."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    sess = TemplateSession("tfillcontract", str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "exam"
    sess.exam_name = "期末"
    sess.add_page(0, [{
        "question_number": "8", "question_text": "x=___，y=___",
        "question_type": "fill", "standard_answer": "2；3", "max_score": 5,
        "blank_count": 2, "answer_slots": ["2", "3"],
        "fill_scoring_mode": "custom", "blank_scores": [2, 3],
    }])
    sess.confirm_current_contract(1)
    sess.save_meta()

    async def _mock_recognize_paper(
        _path, _batch_id, paper_cache, teacher_id="", paper_id="",
    ):
        assert paper_cache._baselines
        return {"student_name": "张三", "questions": [{
            "question_number": "8", "question_text": "学生卷题干",
            "student_answer": "2；4", "question_type": "fill",
        }]}
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", _mock_recognize_paper)

    response = _client(Session, monkeypatch).post(
        "/api/batch-grade/recognize",
        data={"category": "exam", "exam_name": "期末", "mode": "precise", "template_id": "tfillcontract"},
        files=[(
            "files",
            (
                "s1.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("FILL"),
                "image/jpeg",
            ),
        )],
    )
    assert response.status_code == 200
    done = [payload for event, payload in _parse_sse(response.text) if event == "done"]
    assert len(done) == 1
    question = done[0]["questions"][0]
    assert question["fill_scoring_mode"] == "custom"
    assert question["blank_scores"] == [2.0, 3.0]
    assert question["max_score"] == 5.0


def test_recognize_rejects_unconfirmed_template(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    sess = TemplateSession("tocr", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"   # 未确认
    sess.category = "daily"
    sess.add_page(0, [{"question_number": "1", "question_text": "题", "standard_answer": "A"}])
    sess.save_meta()

    client = _client(Session, monkeypatch)
    r = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "mode": "precise", "template_id": "tocr"},
        files=[("files", ("s1.png", b"\x89PNG\r\n\x1a\nimg", "image/png"))],
    )
    assert r.status_code == 400


def test_recognize_requires_a_confirmed_template(monkeypatch, tmp_path):
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda db, uid: None)

    client = _client(Session, monkeypatch)
    r = client.post(
        "/api/batch-grade/recognize",
        data={"category": "daily", "mode": "precise"},
        files=[
            ("files", (
                "s1.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("CLUSTER-1"),
                "image/jpeg",
            )),
            ("files", (
                "s2.ocr-ready-v1.jpg",
                _ocr_ready_jpeg_bytes("CLUSTER-2"),
                "image/jpeg",
            )),
        ],
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "请先导入并确认模板后再开始批改"


def test_template_confirm_rejects_non_letter_choice_answer_without_side_effects(monkeypatch, tmp_path):
    """A numeric/set answer must never be frozen as a choice-question answer."""
    _engine, Session = _make_app_and_session()
    import routers.grade as gr_router
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    sess = TemplateSession("invalid-choice", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.add_page(0, [{
        "question_number": "4", "question_text": "求 x 的值", "question_type": "choice",
        "standard_answer": "-3",
    }])
    sess.save_meta()

    response = _client(Session, monkeypatch).post("/api/batch-grade/template/confirm", json={
        "template_id": "invalid-choice", "category": "exam", "exam_name": "单元测验",
        "pages": [{"page_index": 0, "questions": [{
            "question_no": "4", "question_text": "求 x 的值", "question_type": "choice",
            "standard_answer": "-3", "max_score": 4,
        }]}],
    })

    assert response.status_code == 400
    assert "选择题标准答案" in response.json()["detail"]
    assert TemplateSession.load_meta("invalid-choice", str(tmp_path)).status == "ocr_done"
    db = Session()
    assert db.query(QuestionBank).count() == 0
    db.close()


def _directory_confirmation_session(monkeypatch, tmp_path, *, question_type="solve", answer="2", kind="non_graphic", bounded=True):
    """Small persisted teacher draft; model and production storage are never used."""
    import routers.grade as gr_router
    from routers import grade_template

    _engine, Session = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))

    async def no_background_classifier(*_args, **_kwargs):
        return None

    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", no_background_classifier)
    sess = TemplateSession("directory-confirm-gate", str(tmp_path), teacher_id=1)
    sess.status = "ocr_done"
    sess.category = "daily"
    sess.add_page(0, [{
        "question_number": "1", "question_text": "合成印刷题干", "question_type": question_type,
        "standard_answer": answer, "directory_node_id": "n001", "printed_path": ["1"],
        "answer_kind": kind, "drawing_regions": [[650, 350, 950, 600]] if kind != "non_graphic" else [],
        "label_anchor": [640, 350] if kind != "non_graphic" else None,
    }])
    if bounded:
        sess.layout_cut_runs = {"bounded-directory-import": {"pages": {"page_1": {
            "page_index": 0,
            "template_ocr_diagnostics": {
                "needs_review": False, "geometry_review_pending": False,
                "model_stages": {"template-directory-v1": {"status": "completed"}},
            },
        }}}}
    sess.save_meta()
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    return _client(Session, monkeypatch), sess, payload


def _teacher_reference(mode):
    return {
        "version": "drawing-reference-v1", "coordinate_space": "normalized_1000_v1",
        "comparison_mode": mode,
        "variants": [{"strokes": [{"kind": "point", "point": [750, 450]}]}] if mode == "fixed" else [],
        "context_regions": [], "teacher_confirmed": mode == "fixed",
    }


@pytest.mark.parametrize("question_type", ["fill", "solve"])
@pytest.mark.parametrize("answer", ["", " \t "])
def test_new_directory_confirmation_requires_nonempty_standard_answer(monkeypatch, tmp_path, question_type, answer):
    client, sess, payload = _directory_confirmation_session(
        monkeypatch, tmp_path, question_type=question_type, answer=answer,
    )
    before = Path(sess.meta_path).read_bytes()
    blocked = client.post("/api/batch-grade/template/confirm", json=payload)
    assert blocked.status_code == 422, blocked.text
    assert "标准答案" in blocked.json()["detail"]
    assert Path(sess.meta_path).read_bytes() == before
    assert TemplateSession.load_meta(sess.template_id, str(tmp_path)).status == "ocr_done"
    assert client.post(f"/api/batch-grade/template/{sess.template_id}/bank-import").status_code == 409

    payload["pages"][0]["questions"][0]["standard_answer"] = "老师核对后的答案"
    accepted = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize("kind", ["drawing", "mixed"])
def test_new_directory_drawing_can_confirm_without_reference(monkeypatch, tmp_path, kind):
    client, sess, payload = _directory_confirmation_session(monkeypatch, tmp_path, kind=kind)
    accepted = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted.status_code == 200, accepted.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    question = saved.pages[0]["questions"][0]
    assert question["answer_kind"] == kind
    assert question["drawing_regions"] == [[650, 350, 950, 600]]
    assert "drawing_reference" not in question


@pytest.mark.parametrize("question_type,answer,kind", [
    ("fill", "", "non_graphic"), ("solve", "", "non_graphic"),
    ("solve", "2", "drawing"), ("solve", "2", "mixed"),
])
def test_legacy_directory_without_bounded_model_marker_retains_confirmation_contract(
    monkeypatch, tmp_path, question_type, answer, kind,
):
    client, sess, payload = _directory_confirmation_session(
        monkeypatch, tmp_path, question_type=question_type, answer=answer, kind=kind, bounded=False,
    )
    response = client.post("/api/batch-grade/template/confirm", json=payload)
    assert response.status_code == 200, response.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert saved.status == "confirmed"
    assert saved.pages[0]["questions"][0]["standard_answer"] == answer
    assert "drawing_reference" not in saved.pages[0]["questions"][0]


@pytest.mark.parametrize("explicit,expected", [(False, False), (True, True), (None, True)])
def test_template_geometry_review_uses_explicit_flag_before_legacy_review(explicit, expected):
    import routers.grade as gr_router
    from routers import grade_template

    diagnostics = {"needs_review": True, "review_reasons": ["标准图候选尚未完成"]}
    if explicit is not None:
        diagnostics["geometry_review_pending"] = explicit
    assert grade_template._template_geometry_review_state({"template_ocr_diagnostics": diagnostics}) == (False, expected)
    # An explicit content-only diagnostic must never override an independent
    # unresolved anchor or teacher candidate.
    assert grade_template._template_geometry_review_state({
        "template_ocr_diagnostics": diagnostics, "anchor_review_pending": True,
    }) == (True, True)


def test_current_one_three_import_confirm_and_reopen(monkeypatch, tmp_path):
    """Current hierarchy plus three parallel calls contract reaches real import and confirmation routes."""
    from unittest.mock import AsyncMock
    import httpx
    import routers.grade as grade
    from routers import grade_template
    from services import flash_grading, template_directory_pipeline as pipeline

    _engine, session_factory = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda *_args: None)
    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", AsyncMock())
    _patch_template_layout(monkeypatch, grade)
    phases = []
    slot_ids = {"1": ["1[1]", "1[2]", "1[3]"], "2": ["2[1]"]}
    artifact_errors = []
    build_artifact = grade_template_import.build_template_artifact

    def capture_artifact(*args, **kwargs):
        try:
            return build_artifact(*args, **kwargs)
        except Exception as exc:
            artifact_errors.append(str(exc))
            raise

    monkeypatch.setattr(grade_template_import, "build_template_artifact", capture_artifact)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Upstream transport must be mocked")

    async def flash(messages, **kwargs):
        assert kwargs["phase"] == "template_hierarchy"
        assert kwargs["enable_thinking"] is True
        prompt = messages[0]["content"][0]["text"]
        if prompt == pipeline._HIERARCHY_PROMPT:
            assert kwargs["reasoning_effort"] == "low"
            phases.append("hierarchy")
            payload = {"contract_version": 4, "questions": [
                {"question_number": number, "printed_path": [number + "."],
                 "slots": [{"slot_id": sid, "cue": f"第{i}个填写位置"} for i, sid in enumerate(ids, 1)]}
                for number, ids in slot_ids.items()]}
        elif prompt == pipeline._COARSE_LOCATOR_PROMPT:
            assert kwargs["reasoning_effort"] == "low"
            phases.append("coarse")
            payload = {"coordinate_space": "normalized_1000_v1", "questions": [
                {"question_number": number, "bbox": [0, index * 500, 1000, (index + 1) * 500]}
                for index, number in enumerate(slot_ids)]}
        else:
            assert kwargs["reasoning_effort"] == "high"
            assert '"q_id":"Q1"' in prompt
            phases.append("transcribe")
            payload = {"questions": [{"q_id": "Q" + number, "question_text": "印刷题干" + number,
                                       "uncertain": []} for number in slot_ids]}
        return {"model": "qwen3.8-flash", "content": json.dumps(payload), "receipt": {}}

    async def max_call(images, prompt, **kwargs):
        assert kwargs["reasoning_effort"] == "high" and kwargs["max_tokens"] == 32768
        if "normalized_1000_v1" in prompt:
            phases.append("locator")
            rows = [
                {"question_number": "1", "question_type": "fill", "choice_mode": "unknown",
                 "question_number_bbox": [100, 100, 130, 120], "answer_kind": "non_graphic",
                 "regions": [[170, 170, 240, 205], [280, 170, 350, 205], [380, 170, 450, 205]],
                 "slot_ids": slot_ids["1"]},
                {"question_number": "2", "question_type": "solve", "choice_mode": "unknown",
                 "question_number_bbox": [550, 100, 580, 120], "answer_kind": "non_graphic",
                 "regions": [[620, 170, 920, 350]], "slot_ids": slot_ids["2"]}]
            for row in rows:
                row.pop("slot_ids"); row.pop("answer_kind")
                row["regions"] = [{"bbox": box, "kind": "answer", "slot_id": sid}
                                  for box, sid in zip(row["regions"], slot_ids[row["question_number"]])]
                row["label_anchor"] = None
            payload = {"coordinate_space": "normalized_1000_v1", "questions": rows, "needs_review": []}
        else:
            phases.append("solve")
            payload = {"answers": [
                {"q_id": "Q" + number, "standard_answer": "2；3；2" if number == "1" else "4",
                 "answer_items": [{"slot_id": sid, "answer": answer} for sid, answer in zip(ids,
                                  ["2", "3", "2"] if number == "1" else ["4"])],
                 "solution_steps": [], "visual_facts": [], "uncertain": []}
                for number, ids in slot_ids.items()]}
        raw = json.dumps(payload, ensure_ascii=False)
        kwargs["response_validator"](raw)
        return raw

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(flash_grading, "call_flash", flash)
    monkeypatch.setattr(pipeline, "call_gpt_vision", max_call)
    client = _client(session_factory, monkeypatch)
    response = client.post("/api/batch-grade/template/recognize", data={"template_mode": "marked", "category": "daily"},
                           files=[("files", ("synthetic-handoff.png", _template_png_bytes(), "image/png"))])
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert not [value for event, value in events if event == "error"], (events, artifact_errors)
    done = next(value for event, value in events if event == "done")
    saved = TemplateSession.load_meta(done["template_id"], str(tmp_path))
    assert sorted(phases) == ["coarse", "hierarchy", "locator", "solve", "transcribe"]
    assert [q["question_no"] for q in saved.pages[0]["questions"]] == ["1", "2"]
    first = saved.pages[0]["questions"][0]
    assert first["question_text"] == "印刷题干1"
    assert first["blank_count"] == 3 and first["answer_slots"] == ["2", "3", "2"]
    assert len(saved.page_artifacts["page_1"]["template_answer_regions"][0]["regions"]) == 3
    assert saved.question_directories["page_1"]["leaves"][0]["slot_ids"] == slot_ids["1"]
    confirmed = client.post("/api/batch-grade/template/confirm", json={
        "template_id": saved.template_id, "category": "daily", "pages": copy.deepcopy(saved.pages)})
    assert confirmed.status_code == 200, confirmed.text
    reopened = TemplateSession.load_meta(saved.template_id, str(tmp_path))
    assert reopened.status == "confirmed"
    assert reopened.build_answer_map()["1"]["answer_slots"] == ["2", "3", "2"]


def _recognize_directory_timeout_draft(monkeypatch, tmp_path, phase, *,
                                      drawing_question_type="solve", drawing_conflict=False):
    """Exercise persistence/recovery using a synthetic directory, without model work."""
    import httpx
    import routers.grade as gr_router
    from routers import grade_template
    from services import template_directory_pipeline as pipeline
    from services import template_recognition
    from tests.test_template_directory_pipeline import ModelFixture, leaf

    _engine, Session = _make_app_and_session()
    set_grade_global(monkeypatch, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_recognition, "_check_limits_or_raise", lambda _db, _uid: None)
    # ModelFixture represents failed stages with missing output fields. Allow
    # local image processing to finish so recovery tests reach those fields:
    # the import's clock stands still, so machine load cannot use up its time.
    set_grade_global(monkeypatch, "_template_import_clock", _ImportDeadlineClock())
    monkeypatch.setattr(grade_template_import, "_template_import_deadline_seconds", lambda _count: 5.0)
    monkeypatch.setattr(grade_template_import_steps, "_TEMPLATE_MODEL_SAVE_RESERVE_S", 0.3)
    monkeypatch.setattr(grade_template_import_steps, "_TEMPLATE_COMPONENT_HEARTBEAT_S", 0.01)
    _patch_template_layout(monkeypatch, gr_router)

    async def no_background_classifier(*_args, **_kwargs):
        return None

    async def forbidden_network(*_args, **_kwargs):
        raise AssertionError("Real upstream network is forbidden in template timeout tests")

    async def forbidden_flash(*_args, **_kwargs):
        raise AssertionError("Flash reference generation is not part of this timeout fixture")

    monkeypatch.setattr(grade_template, "_classify_confirmed_template_knowledge", no_background_classifier)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_network)
    from services import flash_grading
    monkeypatch.setattr(flash_grading, "call_flash", forbidden_flash)
    tree = {"coordinate_space": "normalized_1000_v1", "needs_review": [], "nodes": [
        leaf("1", question_type="fill", y=100, x=300),
        leaf("2", question_type=drawing_question_type, y=350, regions=[[650, 350, 950, 600]], exemptions=[],
             requires_drawing=True),
    ]}
    fixture = ModelFixture(None, tree)
    if phase is not None:
        fixture.block_phases.add(phase)

    def drawing_geometry(result):
        if not drawing_conflict:
            result["questions"][1].update(
                answer_kind="drawing", drawing_regions=[[650, 350, 950, 600]], label_anchor=[640, 350],
            )
        else:
            result["questions"][1].update(answer_kind="drawing", drawing_regions=[], label_anchor=None)

    fixture.response_edit["drawing"] = drawing_geometry
    if drawing_question_type == "choice":
        def choice_answer(result):
            next(row for row in result["answers"] if row["q_id"] == "Q2")["standard_answer"] = "A"
        fixture.response_edit["solve"] = choice_answer
    monkeypatch.setattr(pipeline, "call_gpt_vision", fixture)
    async def synthetic_directory(image_path, *, template_mode, layout_result, **_kwargs):
        from services.merged_template_receive import receive_merged_template_output
        payload = json.loads(await fixture(image_path, "synthetic fixture"))
        merged = receive_merged_template_output(payload)
        directory, questions = merged["question_directory"], merged["questions"]
        anchors, regions, handwriting, pending = [], [], [], []
        review = list(directory.get("needs_review") or [])
        leaves = {leaf["question_number"]: leaf for leaf in directory["leaves"]}
        for question in questions:
            number = str(question.get("question_number") or question.get("question_no"))
            leaf = leaves[number]
            anchors.append({"question_number": number, "question_number_bbox": leaf["question_number_bbox"]})
            kind = question.get("answer_kind")
            boxes = leaf.get("regions") or []
            drawing = question.get("drawing_regions") or []
            regions.append({"question_number": number, "regions": [] if kind == "drawing" else boxes})
            handwriting.append({"question_number": number, "regions": drawing if kind == "drawing" else boxes + [b for b in drawing if b not in boxes]})
            review.extend(question.get("directory_issues") or [])
            if question.get("answer_status") != "ready":
                pending.append(number)
        diagnostics = {"strategy": "synthetic_directory", "needs_review": bool(review),
                       "geometry_review_pending": bool(review), "review_reasons": review,
                       "model_stages": {"template-fullsolve-v1": {"status": "completed"}}}
        return {"questions": questions, "question_directory": directory, "template_mode": template_mode,
                "layout_result": layout_result, "question_anchors": anchors,
                "template_answer_regions": regions, "template_handwriting_regions": handwriting,
                "requires_teacher_confirmation": pending, "template_ocr_diagnostics": diagnostics,
                "anchor_diagnostics": diagnostics}
    monkeypatch.setattr(pipeline, "recognize_directory_template", synthetic_directory)
    monkeypatch.setattr(grade_template_import, "recognize_template_pipeline", template_recognition.recognize_template_pipeline)
    client = _client(Session, monkeypatch)
    response = client.post(
        "/api/batch-grade/template/recognize",
        data={"template_mode": "marked", "category": "daily"},
        files=[("files", ("synthetic-directory.png", _template_png_bytes(), "image/png"))],
    )
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert not [payload for event, payload in events if event == "error"], events
    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1, events
    sess = TemplateSession.load_meta(done[0]["template_id"], str(tmp_path))
    assert sess.status == "ocr_done"
    if phase is not None:
        assert phase in fixture.cancelled
    assert [row["question_no"] for row in sess.pages[0]["questions"]] == ["1", "2"]
    assert all(row["directory_node_id"] for row in sess.pages[0]["questions"])
    return client, sess, fixture, events


def test_real_directory_import_saves_answers_geometry_without_teacher_reference(monkeypatch, tmp_path):
    """The import saves answers and geometry even when standard-figure generation fails."""
    client, sess, fixture, events = _recognize_directory_timeout_draft(monkeypatch, tmp_path, None)
    import routers.grade as gr_router
    from routers import grade_template

    questions = sess.pages[0]["questions"]
    assert [row["standard_answer"] for row in questions] == ["答案_Q1", "答案_Q2"]
    assert questions[1]["answer_kind"] == "drawing"
    assert questions[1]["drawing_regions"] == [[650, 350, 950, 600]]
    assert questions[1]["label_anchor"] == [640, 350]
    assert "drawing_reference" not in questions[1]
    artifact = sess.page_artifacts["page_1"]
    assert artifact["question_anchors"] and artifact["template_answer_regions"]
    assert artifact["question_schema"][1]["answer_kind"] == "drawing"
    assert grade_template._template_geometry_review_pending_pages(sess) == []
    page_run = sess.layout_cut_runs[f"template-import-{sess.template_id}"]["pages"]["page_1"]
    diagnostics = page_run["template_ocr_diagnostics"]
    assert diagnostics["geometry_review_pending"] is False
    assert diagnostics["strategy"] == "synthetic_directory"
    assert [call["phase"] for call in fixture.calls] == ["directory", "solve", "drawing"]  # fixture compatibility marker
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    accepted = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted.status_code == 200, accepted.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert "drawing_reference" not in saved.pages[0]["questions"][1]


def test_real_directory_drawing_timeout_requires_explicit_complete_teacher_schema(monkeypatch, tmp_path):
    client, sess, _fixture, _events = _recognize_directory_timeout_draft(monkeypatch, tmp_path, "drawing")
    import routers.grade as gr_router
    from routers import grade_template

    assert [row["standard_answer"] for row in sess.pages[0]["questions"]] == ["答案_Q1", "答案_Q2"]
    assert grade_template._template_geometry_review_pending_pages(sess) == [0]
    before = Path(sess.meta_path).read_bytes()
    payload = {
        "page_index": 0,
        "columns": [{"column_index": 1, "x1_1000": 0, "x2_1000": 500},
                    {"column_index": 2, "x1_1000": 500, "x2_1000": 1000}],
        "question_anchors": [
            {"question_number": "1", "question_number_bbox": [130, 100, 160, 120]},
            {"question_number": "2", "question_number_bbox": [130, 350, 160, 370]},
        ],
        "template_answer_regions": [
            {"question_number": "1", "regions": [[300, 100, 420, 145]]},
            {"question_number": "2", "regions": [[650, 350, 950, 600]]},
        ],
        "template_handwriting_regions": [
            {"question_number": "1", "regions": [[320, 110, 370, 130]]},
            {"question_number": "2", "regions": []},
        ],
    }
    schema = [
        {"question_number": "1", "answer_kind": "non_graphic", "drawing_regions": [], "label_anchor": None},
        {"question_number": "2", "answer_kind": "drawing", "drawing_regions": [[650, 350, 950, 600]],
         "label_anchor": [640, 350]},
    ]
    partial = copy.deepcopy(schema)
    partial[1].pop("label_anchor")
    for submitted in (None, schema[:1], partial):
        attempt = copy.deepcopy(payload)
        if submitted is not None:
            attempt["question_schema"] = submitted
        rejected = client.put(f"/api/batch-grade/template/{sess.template_id}/layout-overrides", json=attempt)
        assert rejected.status_code in {200, 422}, rejected.text
        if rejected.status_code == 422:
            assert Path(sess.meta_path).read_bytes() == before
        else:
            before = Path(sess.meta_path).read_bytes()
    payload["question_schema"] = schema
    accepted = client.put(f"/api/batch-grade/template/{sess.template_id}/layout-overrides", json=payload)
    assert accepted.status_code == 200, accepted.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert grade_template._template_geometry_review_pending_pages(saved) == []
    assert saved.pages[0]["questions"][1]["answer_kind"] == "drawing"
    assert saved.pages[0]["questions"][1]["drawing_regions"] == schema[1]["drawing_regions"]
    # Once the teacher resolved drawing geometry, an ordinary repeat layout
    # save must not demand the same subtype confirmation again. It must also
    # retain the new-pipeline marker used by the still-missing reference gate.
    repeat = copy.deepcopy(payload)
    repeat.pop("question_schema")
    resaved = client.put(f"/api/batch-grade/template/{sess.template_id}/layout-overrides", json=repeat)
    assert resaved.status_code == 200, resaved.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert grade_template._template_geometry_review_pending_pages(saved) == []
    confirm = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(saved.pages)}
    confirmed_without_reference = client.post("/api/batch-grade/template/confirm", json=confirm)
    assert confirmed_without_reference.status_code == 200, confirmed_without_reference.text


def test_real_directory_solve_timeout_keeps_directory_but_requires_teacher_answers(monkeypatch, tmp_path):
    client, sess, fixture, _events = _recognize_directory_timeout_draft(monkeypatch, tmp_path, "solve")
    import routers.grade as gr_router
    from routers import grade_template

    assert [row["standard_answer"] for row in sess.pages[0]["questions"]] == ["", ""]
    assert sess.pages[0]["questions"][1]["answer_kind"] == "drawing"
    assert sess.pages[0]["questions"][1]["drawing_regions"] == [[650, 350, 950, 600]]
    assert grade_template._template_geometry_review_pending_pages(sess) == []
    assert [call["phase"] for call in fixture.calls] == ["directory", "solve", "drawing"]  # fixture compatibility marker
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    rejected = client.post("/api/batch-grade/template/confirm", json=payload)
    assert rejected.status_code == 422, rejected.text
    assert "标准答案" in rejected.json()["detail"]
    assert TemplateSession.load_meta(sess.template_id, str(tmp_path)).status == "ocr_done"
    for question in payload["pages"][0]["questions"]:
        question["standard_answer"] = "老师核对后的标准答案"
    payload["pages"][0]["questions"][1]["drawing_reference"] = _teacher_reference("manual")
    accepted = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted.status_code == 200, accepted.text


def test_reference_timeout_confirmation_gate_survives_cut_run_pruning(monkeypatch, tmp_path):
    client, sess, fixture, _events = _recognize_directory_timeout_draft(monkeypatch, tmp_path, None)
    import routers.grade as gr_router
    from routers import grade_template

    original_pages = copy.deepcopy(sess.pages)
    artifact_sha = sess.page_artifacts["page_1"]["sha256"]
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(original_pages)}
    for index in range(6):
        cut = client.post(f"/api/batch-grade/template/{sess.template_id}/cut-candidates", json={
            "action_id": f"bounded-cut-review-{index}", "cut_types": ["question"], "image_type": "photo",
        })
        assert cut.status_code == 200, cut.text

    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert len(saved.layout_cut_runs) == 5
    assert f"template-import-{sess.template_id}" not in saved.layout_cut_runs
    assert "bounded-cut-review-0" not in saved.layout_cut_runs
    assert saved.status == "ocr_done" and saved.pages == original_pages
    assert saved.page_artifacts["page_1"]["sha256"] == artifact_sha
    assert grade_template._template_geometry_review_pending_pages(saved) == []
    for run in saved.layout_cut_runs.values():
        page_run = run["pages"]["page_1"]
        assert page_run["artifact_sha256"] == artifact_sha
        diagnostics = page_run["template_ocr_diagnostics"]
        assert diagnostics["geometry_review_pending"] is False
        # The confirmation gate for a graphic question is carried by the page
        # contract, not by the import run that history rotation prunes.
        assert diagnostics["model_stages"]["template-fullsolve-v1"]["status"] == "completed"
        assert "template-drawing-reference-v1" not in diagnostics["model_stages"]
    # Re-cut reuses the frozen column plan; it neither regenerates standards
    # nor gains authority merely by pushing the original import out of history.
    assert [call["phase"] for call in fixture.calls] == ["directory", "solve", "drawing"]  # fixture compatibility marker
    accepted_without_reference = client.post("/api/batch-grade/template/confirm", json=payload)
    assert accepted_without_reference.status_code == 200, accepted_without_reference.text
    assert client.post(f"/api/batch-grade/template/{sess.template_id}/bank-import").status_code == 200


def _geometry_question():
    return {"question_number": "1", "question_text": "确认时题干", "question_type": "solve",
            "standard_answer": "2", "directory_node_id": "d1", "answer_kind": "mixed",
            "drawing_regions": [[10, 10, 50, 50]], "label_anchor": [8, 8]}


def _geometry_artifact():
    return {"version": 1, "sha256": "1" * 64, "width": 100, "height": 100,
            "question_numbers": ["1"],
            "question_schema": [{"question_number": "1", "question_type": "solve",
                                 "choice_mode": "unknown", "answer_kind": "mixed",
                                 "drawing_regions": [[10, 10, 50, 50]], "label_anchor": [8, 8]}]}


def _confirmed_geometry_session(tmp_path, template_id):
    sess = TemplateSession(template_id, str(tmp_path), teacher_id=1)
    sess.status = "confirmed"
    sess.category = "daily"
    sess.add_page(0, [_geometry_question()], artifact=_geometry_artifact())
    sess.confirm_current_contract(1)
    assert sess.recognition_contract_error() == ""
    return sess


def _move_geometry(sess):
    sess.pages[0]["questions"][0]["label_anchor"] = [40, 60]
    sess.pages[0]["questions"][0]["drawing_regions"] = [[40, 60, 120, 200]]
    schema = sess.page_artifacts["page_1"]["question_schema"][0]
    schema["label_anchor"] = [40, 60]
    schema["drawing_regions"] = [[40, 60, 120, 200]]


def test_moved_geometry_box_keeps_answers_and_reports_geometry_only(tmp_path):
    """A moved box must not cost the teacher the standard answers.

    Geometry says where an answer is read from, which is a separate contract from
    what the student is graded against: a pure box move asks for a geometry review
    instead of invalidating the answers and re-solving the paper.
    """
    sess = _confirmed_geometry_session(tmp_path, "moved-box")
    answers_hash = sess.answers_hash()

    _move_geometry(sess)

    assert sess.answers_hash() == answers_hash
    assert sess.content_hash() != sess.confirmed_contract_hash
    error = sess.recognition_contract_error()
    assert "定位框" in error and "无需重算" in error


def test_answer_side_edit_still_invalidates_the_whole_contract(tmp_path):
    """Changing what the student is graded against still blocks recognition."""
    sess = _confirmed_geometry_session(tmp_path, "edited-answer")

    sess.pages[0]["questions"][0]["standard_answer"] = "3"

    assert "模板内容" in sess.recognition_contract_error()


def test_geometry_change_on_a_pre_split_confirmation_stays_fail_closed(tmp_path):
    """A confirmation that never recorded the answer contract keeps the strict reason."""
    sess = _confirmed_geometry_session(tmp_path, "pre-split")
    sess.confirmed_answers_hash = ""

    _move_geometry(sess)

    assert "模板内容" in sess.recognition_contract_error()


@pytest.mark.parametrize("changed_view", ["page_only", "artifact_only", "both"])
def test_geometry_hash_mismatch_is_fail_closed_without_losing_reusable_answers(tmp_path, changed_view):
    import routers.grade as gr_router
    from fastapi import HTTPException
    sess = _confirmed_geometry_session(tmp_path, "geometry-desync")
    before_answers = sess.answers_hash()
    before_answer = sess.pages[0]["questions"][0]["standard_answer"]
    before_artifact_hash = sess.artifact_hash()
    if changed_view in {"page_only", "both"}:
        sess.pages[0]["questions"][0]["drawing_regions"] = [[40, 60, 120, 200]]
    if changed_view in {"artifact_only", "both"}:
        sess.page_artifacts["page_1"]["question_schema"][0]["drawing_regions"] = [[40, 60, 120, 200]]
    if changed_view == "page_only":
        assert sess.artifact_hash() == before_artifact_hash
    assert sess.requires_reconfirmation is False  # hash gate must work independently
    assert "定位框" in sess.recognition_contract_error()
    assert sess.answers_hash() == before_answers
    assert sess.pages[0]["questions"][0]["standard_answer"] == before_answer
    sess.save_meta()
    reopened = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert "定位框" in reopened.recognition_contract_error()
    with pytest.raises(HTTPException) as blocked:
        gr_router._validate_template_for_recognition(reopened, "daily", "", persist_reconfirmation=True)
    assert blocked.value.status_code == 409
    persisted = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert persisted.requires_reconfirmation is True
    assert persisted.answers_hash() == before_answers


@pytest.mark.parametrize("change", ["move", "remove"])
def test_answer_region_only_edit_invalidates_geometry_signature(tmp_path, change):
    sess = _confirmed_geometry_session(tmp_path, "answer-region-only")
    artifact = sess.page_artifacts["page_1"]
    artifact["template_answer_regions"] = [{"question_number": "1", "regions": [[100, 200, 300, 400]]}]
    sess.confirm_current_contract(1)
    original_answers = sess.answers_hash()
    original_content = sess.content_hash()
    original_artifact = sess.artifact_hash()
    if change == "move":
        artifact["template_answer_regions"][0]["regions"] = [[200, 300, 400, 500]]
    else:
        artifact.pop("template_answer_regions")

    assert sess.content_hash() == original_content
    assert sess.answers_hash() == original_answers
    assert sess.artifact_hash() != original_artifact
    assert "定位框" in sess.recognition_contract_error()
    assert "无需重算" in sess.recognition_contract_error()
    sess.save_meta()
    restored = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert restored.confirmed_artifact_hash_version == CURRENT_ARTIFACT_HASH_VERSION
    assert restored.recognition_contract_error() == sess.recognition_contract_error()
    assert restored.answers_hash() == original_answers


@pytest.mark.parametrize("question_type", ["choice", "fill", "solve"])
@pytest.mark.parametrize("reference_mode", ["manual", "fixed"])
def test_graphic_import_hand_draw_confirm_and_freeze_keep_original_type(
    monkeypatch, tmp_path, question_type, reference_mode,
):
    from services import template_directory_pipeline as pipeline
    from services.paper_cache import PaperCache
    import routers.grade as gr_router
    from routers import grade_template

    client, sess, fixture, _ = _recognize_directory_timeout_draft(
        monkeypatch, tmp_path, None, drawing_question_type=question_type)
    question = sess.pages[0]["questions"][1]
    assert question["question_type"] == question_type
    assert question["answer_kind"] == "drawing"
    assert sess.page_artifacts["page_1"]["question_schema"][1]["question_type"] == question_type
    assert grade_template._template_geometry_review_pending_pages(sess) == []

    reference = _teacher_reference(reference_mode)
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    payload["pages"][0]["questions"][1]["drawing_reference"] = reference
    # 保存模板即确认：固定参考不再 422 或要求二次勾选，保存后自动标记已核对。
    confirmed = client.post("/api/batch-grade/template/confirm", json=payload)
    assert confirmed.status_code == 200, confirmed.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    gr_router._validate_template_for_recognition(saved, "daily", "")
    cache = PaperCache.from_confirmed_template_pages(
        saved.pages, template_artifacts=saved.page_artifacts,
        template_dir=saved.template_dir, template_id=saved.template_id, require_local_evidence=True)
    assert cache._baselines
    assert saved.build_answer_map()["2"]["question_type"] == question_type
    assert len(fixture.calls) == 3  # no implicit re-solve or standard-figure request

    async def recognize_student(
        path, batch_id, paper_cache, teacher_id="", paper_id="",
    ):
        assert paper_cache._baselines
        return {"student_name": "合成学生", "questions": [
            {"question_number": "1", "student_answer": "1", "question_type": "fill"},
            {"question_number": "2", "student_answer": "A" if question_type == "choice" else "绘图",
             "question_type": question_type}]}
    monkeypatch.setattr(grade_papers, "recognize_paper_with_cache", recognize_student)
    frozen = client.post("/api/batch-grade/recognize",
        data={"category": "daily", "mode": "precise", "template_id": sess.template_id},
        files=[("files", ("synthetic.ocr-ready-v1.jpg", _ocr_ready_jpeg_bytes("SYNTHETIC"), "image/jpeg"))])
    assert frozen.status_code == 200, frozen.text
    assert any(event == "done" for event, _ in _parse_sse(frozen.text)), frozen.text
    reopened = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert reopened.status == "frozen" and reopened.recognition_contract_error() == ""
    assert reopened.pages[0]["questions"][1]["question_type"] == question_type
    assert reopened.pages[0]["questions"][1]["drawing_reference"]["comparison_mode"] == reference_mode
    assert bool(reopened.pages[0]["questions"][1]["drawing_reference"]["teacher_confirmed"]) is True
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 409


def test_saving_without_a_graphic_reference_keeps_the_custom_flash_route(monkeypatch, tmp_path):
    """A saved drawing region is enough when standard-figure generation fails."""
    client, sess, fixture, _ = _recognize_directory_timeout_draft(
        monkeypatch, tmp_path, None, drawing_question_type="fill")
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    response = client.post("/api/batch-grade/template/confirm", json=payload)
    assert response.status_code == 200, response.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    question = saved.pages[0]["questions"][1]
    assert question["drawing_regions"] == [[650, 350, 950, 600]]
    assert "drawing_reference" not in question


@pytest.mark.parametrize("needs_review,association", [(True, None), (False, None), (None, None),
                                                     (False, {"needs_review": True}), (False, {"status": "needs_review"})])
def test_one_save_resolves_unnumbered_section_review_and_confirms_unchecked_reference(monkeypatch, tmp_path, needs_review, association):
    import routers.grade as gr_router
    from routers import grade_template
    client, sess, fixture, _ = _recognize_directory_timeout_draft(monkeypatch, tmp_path, None)
    page_run = sess.layout_cut_runs[f"template-import-{sess.template_id}"]["pages"]["page_1"]
    diagnostic = page_run["template_ocr_diagnostics"]
    diagnostic.update(geometry_review_pending=True, review_reasons=["【基础练习】标题没有印刷题号"])
    if needs_review is None:
        diagnostic.pop("needs_review", None)
    else:
        diagnostic["needs_review"] = needs_review
    if association:
        diagnostic.update(geometry_review_pending=False, image_association=association)
    # Historical runs can repeat the same warning. One page write clears all.
    sess.layout_cut_runs["older-preview"] = copy.deepcopy(sess.layout_cut_runs[f"template-import-{sess.template_id}"])
    sess.save_meta()
    if not association:
        assert grade_template._template_geometry_review_pending_pages(sess) == [0]
    artifact = sess.page_artifacts["page_1"]
    saved_layout = client.put(f"/api/batch-grade/template/{sess.template_id}/layout-overrides", json={
        "page_index": 0,
        "columns": artifact["question_crop_plan"]["parameters"]["columns"],
        "question_anchors": artifact["question_anchors"],
        "template_answer_regions": artifact["template_answer_regions"],
        "template_handwriting_regions": artifact["template_handwriting_regions"],
        "question_schema": artifact["question_schema"],
    })
    assert saved_layout.status_code == 200, saved_layout.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert grade_template._template_geometry_review_pending_pages(saved) == []
    for run in saved.layout_cut_runs.values():
        assert run["pages"]["page_1"]["template_ocr_diagnostics"]["teacher_geometry_confirmed"] is True
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(saved.pages)}
    payload["pages"][0]["questions"][1]["drawing_reference"] = {**_teacher_reference("fixed"), "teacher_confirmed": False}
    confirmed = client.post("/api/batch-grade/template/confirm", json=payload)
    assert confirmed.status_code == 200, confirmed.text
    reopened = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert reopened.status == "confirmed"
    assert reopened.pages[0]["questions"][1]["drawing_reference"]["teacher_confirmed"] is True
    gr_router._validate_template_for_recognition(reopened, "daily", "")
    assert len(fixture.calls) == 3  # Saving adds no model calls.


def test_conflicting_graphic_import_requires_geometry_review_then_recovers(monkeypatch, tmp_path):
    import routers.grade as gr_router
    from routers import grade_template
    client, sess, fixture, _ = _recognize_directory_timeout_draft(
        monkeypatch, tmp_path, None, drawing_question_type="fill", drawing_conflict=True)
    before_answers = [question["standard_answer"] for question in sess.pages[0]["questions"]]
    assert grade_template._template_geometry_review_pending_pages(sess) == [0]
    payload = {"template_id": sess.template_id, "category": "daily", "pages": copy.deepcopy(sess.pages)}
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 422
    cut = client.get(f"/api/batch-grade/template/{sess.template_id}/cut-candidates").json()["pages"][0]
    schema = copy.deepcopy(cut["question_schema_candidates"])
    schema[1].update(answer_kind="drawing", drawing_regions=[[650, 350, 950, 600]], label_anchor=[640, 350])
    repaired = client.put(f"/api/batch-grade/template/{sess.template_id}/layout-overrides", json={
        "page_index": 0, "columns": cut["anchor_crop_plan"]["parameters"]["columns"],
        "question_anchors": cut["question_anchor_candidates"],
        "template_answer_regions": cut["template_answer_region_candidates"],
        "template_handwriting_regions": cut["template_handwriting_region_candidates"],
        "question_schema": schema,
    })
    assert repaired.status_code == 200, repaired.text
    saved = TemplateSession.load_meta(sess.template_id, str(tmp_path))
    assert grade_template._template_geometry_review_pending_pages(saved) == []
    assert [question["standard_answer"] for question in saved.pages[0]["questions"]] == before_answers
    assert saved.pages[0]["questions"][1]["question_type"] == "fill"
    payload["pages"] = copy.deepcopy(saved.pages)
    payload["pages"][0]["questions"][1]["drawing_reference"] = _teacher_reference("manual")
    assert client.post("/api/batch-grade/template/confirm", json=payload).status_code == 200
    assert len(fixture.calls) == 3
