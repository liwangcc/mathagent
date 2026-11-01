"""Stage-aware teacher bank access without loading any production data."""

from types import SimpleNamespace
import asyncio
import io
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import Base
import models_user  # noqa: F401
from models import QuestionBank
from routers import teacher
from services.math_curriculum import catalog_version, get_knowledge_point, get_math_catalog


@pytest.fixture
def bank():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for row_id, stage, owner in ((1, "junior", 7), (2, "senior", 7), (3, "", 7), (4, "junior", 8)):
            session.add(QuestionBank(id=row_id, teacher_id=owner, is_public=False, education_stage=stage, question=f"题目{row_id}", answer="2", source="auto"))
        session.commit()
        yield session


def _user(stage="junior"):
    return SimpleNamespace(id=7, name="模拟老师", role="teacher", education_stage=stage)


def test_bank_lists_only_profile_stage_without_other_teacher_or_legacy(bank):
    for profile, selected, expected in (("junior", None, [1]), ("junior", "junior", [1]), ("senior", None, [2])):
        result = teacher.list_bank(page=1, page_size=50, db=bank, user=_user(profile), education_stage=selected)
        assert [row["id"] for row in result["items"]] == expected
    for selected in ("senior", "legacy"):
        with pytest.raises(HTTPException) as caught:
            teacher.list_bank(page=1, page_size=50, db=bank, user=_user(), education_stage=selected)
        assert caught.value.status_code == 409
    with pytest.raises(HTTPException) as caught:
        teacher.list_bank(page=1, page_size=50, db=bank, user=_user(None), education_stage="senior")
    assert caught.value.status_code == 428


def test_private_export_follows_profile_and_rejects_stale_stage(bank):
    for stage, expected in (("junior", "题目1"), ("senior", "题目2")):
        response = teacher.export_private_bank(db=bank, user=_user(stage))
        payload = json.loads(response.body)
        assert payload["count"] == 1
        assert payload["items"][0]["question"] == expected
        assert payload["items"][0]["education_stage"] == stage
    for selected in ("senior", "legacy"):
        with pytest.raises(HTTPException) as caught:
            teacher.export_private_bank(db=bank, user=_user("junior"), education_stage=selected)
        assert caught.value.status_code == 409


def test_explicit_legacy_pending_work_keeps_its_existing_contract(bank):
    # Pending historical work is not the profile-scoped Bank page.
    result = teacher.list_auto_answers(page=1, page_size=50, db=bank, user=_user(), education_stage="legacy")
    assert [row["id"] for row in result["items"]] == [3]


def test_http_bank_list_export_and_default_catalog_share_verified_profile_scope():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from database import get_db
    from routers.auth import _require_user
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    user = _user("junior")
    with Session(engine) as session:
        for row_id, stage, owner, public in (
            (1, "junior", 7, False), (2, "senior", 7, False), (3, "", 7, False),
            (4, "junior", 8, False), (5, "junior", None, True), (6, "senior", None, True),
        ):
            session.add(QuestionBank(id=row_id, question=f"synthetic {row_id}", answer="2", education_stage=stage,
                                     teacher_id=owner, is_public=public))
        session.commit()
        app = FastAPI()
        app.include_router(teacher.router, prefix="/api")
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[_require_user] = lambda: user
        with TestClient(app) as client:
            for stage, expected in (("junior", [5, 1]), ("senior", [6, 2])):
                user.education_stage = stage
                response = client.get("/api/teacher/bank")
                assert response.status_code == 200
                assert [row["id"] for row in response.json()["items"]] == expected
                exported = client.get("/api/bank/export").json()
                assert exported["count"] == 1
                assert exported["items"][0]["education_stage"] == stage
                assert client.get("/api/teacher/chapters").json() == get_math_catalog(stage)
                for stale in ({"junior", "senior", "legacy"} - {stage}):
                    for route in ("/api/teacher/bank", "/api/bank/export"):
                        response = client.get(route, params={"education_stage": stale})
                        assert response.status_code == 409
                        assert response.json()["detail"]["code"] == "teaching_profile_changed"
            user.education_stage = ""
            for route in ("/api/teacher/bank", "/api/bank/export", "/api/teacher/chapters"):
                assert client.get(route).status_code == 428
    engine.dispose()


def test_manual_bank_create_uses_profile_catalog_and_stage_local_deduplication(bank):
    payload = teacher.BankQuestionCreateRequest(question="题目2", answer="2", chapter="有理数与实数", topic="有理数与数轴")
    result = teacher.add_to_bank(payload, db=bank, user=_user())
    row = bank.get(QuestionBank, result["id"])
    assert row.education_stage == "junior"
    assert row.catalog_version == catalog_version("junior")
    assert row.knowledge_point_id == get_knowledge_point("junior", payload.chapter, payload.topic)["id"]
    with pytest.raises(HTTPException) as caught:
        teacher.add_to_bank(payload, db=bank, user=_user())
    assert caught.value.status_code == 409
    with pytest.raises(HTTPException) as caught:
        teacher.add_to_bank(teacher.BankQuestionCreateRequest(question="无效范围", chapter="导数及其应用", topic="导数的运算"), db=bank, user=_user())
    assert caught.value.status_code == 422


def test_editing_existing_question_never_reassigns_it_to_new_profile_stage(bank, monkeypatch):
    monkeypatch.setattr("services.question_classification.queue_question_classification", lambda *_args: None)
    teacher.update_bank(2, teacher.BankQuestionUpdateRequest(answer="新答案"), db=bank, user=_user("junior"))
    assert bank.get(QuestionBank, 2).education_stage == "senior"
    teacher.update_bank(3, teacher.BankQuestionUpdateRequest(answer="历史答案"), db=bank, user=_user("junior"))
    assert bank.get(QuestionBank, 3).education_stage == ""


def test_teacher_chapter_endpoint_is_exact_selected_catalog():
    assert teacher.get_chapters(user=_user("junior")) == get_math_catalog("junior")
    assert teacher.get_chapters(user=_user("senior")) == get_math_catalog("senior")
    assert teacher.get_chapters(user=_user("junior"), education_stage="senior") == get_math_catalog("senior")
    assert teacher.get_chapters(user=_user("junior"), education_stage="legacy") == []


def test_old_confirmation_lookup_does_not_match_new_stage_same_text(bank):
    bank.get(QuestionBank, 1).question = "同题"
    bank.get(QuestionBank, 2).question = "同题"
    bank.get(QuestionBank, 3).question = "同题"
    bank.commit()
    for stage, expected in (("junior", 1), ("senior", 2), ("", 3)):
        result = teacher._same_record_stage(bank.query(QuestionBank), stage).filter(QuestionBank.question == "同题").all()
        assert [row.id for row in result] == [expected]


def test_json_import_same_stage_export_roundtrip_and_full_scope_validation(bank, tmp_path):
    from services.answer_import import import_from_json, _build_question_index
    from services.teaching_context import make_teaching_context, use_teaching_context
    source = tmp_path / "questions.json"
    context = make_teaching_context("junior")
    valid = {"question": "全新题目求和", "answer": "9", "education_stage": "junior", "subject": "数学"}
    with use_teaching_context(context):
        for invalid in ({"subject": "英语"}, {"education_stage": "senior"}, {"education_stage": ""}, {"teaching_context": {"education_stage": "senior"}}):
            source.write_text(json.dumps({"items": [valid, {**valid, **invalid}]}), encoding="utf-8")
            with pytest.raises(ValueError):
                import_from_json(bank, source, teacher_id=7)
            assert bank.query(QuestionBank).count() == 4
        source.write_text(json.dumps({"items": [valid, valid]}), encoding="utf-8")
        assert import_from_json(bank, source, teacher_id=7) == {"imported": 1, "skipped": 1}
        row = bank.query(QuestionBank).filter_by(question=valid["question"]).one()
        assert row.education_stage == "junior"
        assert row.catalog_version == context["curriculum_version"]
        assert _build_question_index(bank, 7).get(valid["question"]) is row
        assert import_from_json(bank, source, teacher_id=7) == {"imported": 0, "skipped": 2}


def test_excel_scope_is_checked_before_first_insert(bank, tmp_path):
    from openpyxl import Workbook
    from services.answer_import import import_from_excel
    from services.teaching_context import make_teaching_context, use_teaching_context
    source = tmp_path / "questions.xlsx"
    workbook = Workbook()
    workbook.active.append(["题目", "答案", "学科", "学段"])
    workbook.active.append(["新题一", "3", "数学", "junior"])
    workbook.active.append(["新题二", "4", "数学", "senior"])
    workbook.save(source)
    with use_teaching_context(make_teaching_context("junior")):
        with pytest.raises(ValueError):
            import_from_excel(bank, source, teacher_id=7)
    assert bank.query(QuestionBank).count() == 4


def test_import_routes_require_profile_and_copy_context_to_worker(monkeypatch):
    from fastapi import UploadFile
    from routers import answer
    from services.teaching_context import current_teaching_context
    calls = []
    def imported(_db, _path, **_kwargs):
        calls.append(current_teaching_context())
        return {"imported": 1, "skipped": 0}
    monkeypatch.setattr(answer, "import_from_json", imported)
    monkeypatch.setattr(answer, "import_from_excel", imported)
    for suffix, route in (("json", answer.import_answer_json), ("xlsx", answer.import_answer_excel)):
        upload = UploadFile(filename=f"mock.{suffix}", file=io.BytesIO(b"mock"))
        with pytest.raises(HTTPException) as caught:
            asyncio.run(route(upload, db=None, user=_user(None)))
        assert caught.value.status_code == 428
        asyncio.run(route(upload, db=None, user=_user("junior")))
    assert len(calls) == 2 and all(item["education_stage"] == "junior" for item in calls)
    assert current_teaching_context() == {}
