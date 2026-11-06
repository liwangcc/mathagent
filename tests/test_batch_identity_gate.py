import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db
from models import GradingResult, Student
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
from services.quick_grader import QuickBatch
from services.student_service import resolve_student_identity


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    current = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            current = line[len("event:"):].strip()
        elif line.startswith("data:"):
            events.append((current, json.loads(line[len("data:"):].strip())))
    return events


@pytest.fixture()
def identity_app(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    db = Session()
    db.add(User(id=1, name="老师", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.add(TeacherClass(id=2, teacher_id=1, class_name="二班"))
    db.commit()
    db.close()

    import routers.grade as grade_router
    from routers import grade_pipeline
    from routers import grade_review
    from routers import grade_template
    import services.grading_persistence as persistence

    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}

    monkeypatch.setattr(grade_router, "batch_grade_availability", available)
    monkeypatch.setattr(
        grade_router,
        "_batch_grade_scoring_availability",
        available,
    )
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(grade_pipeline, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_router, "UPLOAD_DIR", str(upload_dir))
    monkeypatch.setattr(grade_pipeline, "SessionLocal", Session)
    monkeypatch.setattr(grade_router, "SessionLocal", Session)
    monkeypatch.setattr(persistence, "ARCHIVE_DIR", str(tmp_path / "archive"))

    async def _offline_grade(*, paper, student_name, **_kwargs):
        details = [{
            "question_no": str(question.get("question_no") or ""),
            "student_answer": str(question.get("student_answer") or ""),
            "result": "correct",
            "error_type": "",
        } for question in paper.get("questions", [])]
        return {
            "student_name": student_name,
            "score": None,
            "max_score": None,
            "correct_rate": 1.0,
            "correct_count": len(details),
            "partial_count": 0,
            "wrong_count": 0,
            "needs_review_count": 0,
            "total": len(details),
            "details": details,
        }

    # Identity tests must not depend on configured or reachable model keys.
    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", _offline_grade)

    app = FastAPI()
    app.state.upload_dir = str(upload_dir)
    app.include_router(grade_router.router, prefix="/api")
    app.include_router(grade_template.router, prefix="/api")
    app.include_router(grade_review.router, prefix="/api")

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    # UI has switched to class 2 after upload.  The batch must remain frozen to class 1.
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[_require_user] = lambda: User(
        id=1, name="老师", role="teacher", current_class_id=2,
    )

    def make_batch(batch_id: str, identity: dict):
        paper_path = upload_dir / f"quick_batch_{batch_id}" / "p1.png"
        paper_path.parent.mkdir(parents=True, exist_ok=True)
        paper_path.write_bytes(b"identity-paper")
        batch = QuickBatch(batch_id, str(upload_dir))
        batch.teacher_id = 1
        batch.class_id = 1
        batch.source_mode = "template"
        batch.template_id = "identity-template"
        batch.status = "awaiting_confirmation"
        batch.papers = [{
            "paper_id": "paper-1",
            "filename": "p1.png",
            "student_name": identity.get("ocr_name", ""),
            "student_no": identity.get("ocr_student_no", ""),
            "identity": identity,
            "image_path": str(paper_path),
            "orientation_status": "auto_passed",
            "orientation_reason": "normal_angle",
            "questions": [{
                "question_no": "1",
                "student_answer": "A",
                "question_type": "choice",
            }],
        }]
        batch.questions = [{"question_no": "1", "question_text": "选择A"}]
        # Grading answers come from the frozen template snapshot.
        batch.answer_map = {"1": {
            "matched": True,
            "question_text": "选择A",
            "standard_answer": "A",
        }}
        batch.save_meta()
        return batch

    return TestClient(app, raise_server_exceptions=False), Session, make_batch


def _confirm_payload(batch_id: str, identity_resolutions=None):
    return {
        "batch_id": batch_id,
        "mode": "auto",
        "category": "daily",
        "exam_name": "",
        "confirmed_answers": [{
            "question_no": "1",
            "content": "选择A",
            "answer": "A",
            "matched": True,
        }],
        "identity_resolutions": identity_resolutions or [],
    }


def test_unresolved_identity_blocks_before_student_or_result_write(identity_app):
    client, Session, make_batch = identity_app
    make_batch("pending1", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "missing_identity",
        "ocr_name": "",
        "ocr_student_no": "",
        "candidates": [],
    })

    response = client.post("/api/batch-grade/confirm", json=_confirm_payload("pending1"))

    assert response.status_code == 400
    assert "身份" in response.json()["detail"]
    db = Session()
    assert db.query(Student).count() == 0
    assert db.query(GradingResult).count() == 0
    db.close()


def test_incomplete_ocr_batch_blocks_confirm_before_grader(identity_app, monkeypatch):
    client, Session, make_batch = identity_app
    db = Session()
    student = Student(teacher_id=1, class_id=1, name="张三", student_no="01")
    db.add(student)
    db.commit()
    student_id = student.id
    db.close()

    batch = make_batch("partial-ocr", {
        "status": "matched",
        "student_id": student_id,
        "ocr_name": "张三",
        "ocr_student_no": "01",
        "candidates": [],
    })
    batch_dir = Path(batch.batch_dir)
    second_path = batch_dir / "p2.png"
    failed_path = batch_dir / "p3.png"
    second_path.write_bytes(b"recognized-paper")
    failed_path.write_bytes(b"failed-paper")
    first_path = batch.papers[0]["image_path"]
    identity = dict(batch.papers[0]["identity"])
    batch.papers.append({
        **batch.papers[0],
        "paper_id": "paper-2",
        "filename": "p2.png",
        "image_path": str(second_path),
        "identity": identity,
    })
    batch.uploads = [
        {"paper_id": "paper-1", "filename": "p1.png", "image_path": first_path},
        {"paper_id": "paper-2", "filename": "p2.png", "image_path": str(second_path)},
        {"paper_id": "paper-3", "filename": "p3.png", "image_path": str(failed_path)},
    ]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(first_path, "succeeded")
    batch.mark_ocr_job(str(second_path), "succeeded")
    batch.mark_ocr_job(str(failed_path), "failed", "OCR超时")
    batch.status = "ocr_partial_failed"
    batch.save_meta()

    grader_calls = []

    async def fake_grade_paper_unified(**kwargs):
        grader_calls.append(kwargs)
        return {
            "score": 0,
            "max_score": 4,
            "correct_rate": 0,
            "correct_count": 0,
            "partial_count": 0,
            "wrong_count": 1,
            "needs_review_count": 0,
            "total": 1,
            "details": [],
        }

    from routers import grade_pipeline
    monkeypatch.setattr(grade_pipeline, "grade_paper_unified", fake_grade_paper_unified)

    response = client.post(
        "/api/batch-grade/confirm",
        json=_confirm_payload("partial-ocr"),
    )

    assert response.status_code == 409
    assert "不能开始部分批阅" in response.json()["detail"]
    assert grader_calls == []
    persisted = QuickBatch.load_meta("partial-ocr", client.app.state.upload_dir)
    assert persisted.status == "ocr_partial_failed"
    assert persisted.review_results == []


def test_explicit_create_resolution_remains_available_for_roster_no_match(identity_app):
    client, Session, make_batch = identity_app
    make_batch("create1", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "roster_name_not_found",
        "ocr_name": "新同学",
        "ocr_student_no": "8",
        "candidates": [],
    })

    response = client.post("/api/batch-grade/confirm", json=_confirm_payload("create1", [{
        "paper_id": "paper-1",
        "action": "create",
        "confirmed_name": "新同学",
        "confirmed_student_no": "8",
    }]))

    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert any(event == "result" for event, _ in events), events
    db = Session()
    assert db.query(Student).count() == 0
    assert db.query(GradingResult).count() == 0
    db.close()
    from services.quick_grader import QuickBatch
    staged = QuickBatch.load_meta("create1", client.app.state.upload_dir)
    assert staged.status == "review_ready"
    assignment = staged.review_results[0]["identity_assignment"]
    assert assignment["action"] == "create"
    assert assignment["name"] == "新同学"
    assert assignment["student_no"] == "08"

    review_response = client.get("/api/batch-grade/review/create1")
    assert review_response.status_code == 200
    review_row = review_response.json()["results"][0]
    assert review_row["paper_id"] == "paper-1"
    assert review_row["details"][0]["student_answer"] == "A"

    image_response = client.get(
        f"/api/batch-grade/create1/paper/{review_row['paper_id']}/image"
    )
    assert image_response.status_code == 200
    assert image_response.content == b"identity-paper"


def test_explicit_create_resolution_allows_missing_student_no(identity_app):
    client, Session, make_batch = identity_app
    make_batch("create-no-number", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "roster_name_not_found",
        "ocr_name": "无学号新同学",
        "ocr_student_no": "",
        "candidates": [],
    })

    response = client.post("/api/batch-grade/confirm", json=_confirm_payload(
        "create-no-number", [{
            "paper_id": "paper-1",
            "action": "create",
            "confirmed_name": "无学号新同学",
            "confirmed_student_no": "",
        }],
    ))

    assert response.status_code == 200, response.text
    staged = QuickBatch.load_meta("create-no-number", client.app.state.upload_dir)
    assignment = staged.review_results[0]["identity_assignment"]
    assert assignment["action"] == "create"
    assert assignment["name"] == "无学号新同学"
    assert assignment["student_no"] == ""

    archived = client.post(
        "/api/batch-grade/review/archive-all",
        json={"batch_id": "create-no-number"},
    )
    assert archived.status_code == 200, archived.text
    db = Session()
    created = db.query(Student).filter(Student.name == "无学号新同学").one()
    assert created.student_no is None
    db.close()


def test_explicit_existing_resolution_never_creates_placeholder(identity_app):
    client, Session, make_batch = identity_app
    db = Session()
    existing = Student(teacher_id=1, class_id=1, name="王伟", student_no="001")
    db.add(existing)
    db.commit()
    existing_id = existing.id
    db.close()

    make_batch("existing1", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "name_number_conflict",
        "ocr_name": "王炜",
        "ocr_student_no": "001",
        "candidates": [{"student_id": existing_id, "name": "王伟", "student_no": "001", "class_id": 1}],
    })

    response = client.post("/api/batch-grade/confirm", json=_confirm_payload("existing1", [{
        "paper_id": "paper-1",
        "action": "existing",
        "student_id": existing_id,
    }]))

    assert response.status_code == 200, response.text
    db = Session()
    assert db.query(Student).count() == 1
    assert db.query(GradingResult).count() == 0
    db.close()
    staged = QuickBatch.load_meta("existing1", client.app.state.upload_dir)
    assignment = staged.review_results[0]["identity_assignment"]
    assert assignment["action"] == "existing"
    assert assignment["student_id"] == existing_id


def test_stale_empty_candidates_can_resolve_to_existing_batch_student(identity_app):
    client, Session, make_batch = identity_app
    db = Session()
    existing = Student(teacher_id=1, class_id=1, name="赵六", student_no="")
    db.add(existing)
    db.commit()
    existing_id = existing.id
    db.close()
    make_batch("stale-empty-candidates", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "missing_identity",
        "ocr_name": "",
        "ocr_student_no": "",
        "candidates": [],
    })

    response = client.post(
        "/api/batch-grade/confirm",
        json=_confirm_payload("stale-empty-candidates", [{
            "paper_id": "paper-1",
            "action": "existing",
            "student_id": existing_id,
        }]),
    )

    assert response.status_code == 200, response.text
    staged = QuickBatch.load_meta(
        "stale-empty-candidates", client.app.state.upload_dir,
    )
    assignment = staged.review_results[0]["identity_assignment"]
    assert assignment["action"] == "existing"
    assert assignment["student_id"] == existing_id


def test_roster_ambiguous_candidates_survive_batch_ingest_for_teacher_confirmation(identity_app):
    _client, Session, _make_batch = identity_app
    db = Session()
    li_shuo = Student(teacher_id=1, class_id=1, name="周涛", student_no="01")
    wang_yihan = Student(teacher_id=1, class_id=1, name="吴静", student_no="02")
    db.add_all([li_shuo, wang_yihan])
    db.commit()

    from routers.grade import _ingest_recognized_paper

    batch = QuickBatch("roster-ingest", "unused")
    batch.teacher_id = 1
    batch.class_id = 1
    _ingest_recognized_paper(
        batch,
        db,
        1,
        "ambiguous.png",
        "unused/ambiguous.png",
        {
            "questions": [{
                "question_number": "1",
                "question_text": "选择A",
                "student_answer": "A",
                "question_type": "choice",
            }],
            "student_name": "李",
            "student_no": "",
            "identity_roster_candidates": ["周涛", "吴静"],
            "identity_roster_match_status": "ambiguous",
        },
    )
    db.close()

    identity = batch.papers[0]["identity"]
    assert identity["status"] == "needs_confirmation"
    assert identity["reason"] == "roster_name_ambiguous"
    assert [item["name"] for item in identity["candidates"][:2]] == ["周涛", "吴静"]
    assert batch.papers[0]["identity_roster_match_status"] == "ambiguous"


def test_unresolved_identity_does_not_run_server_roster_crop(
    identity_app,
    monkeypatch,
):
    """Uncertain names stay in teacher confirmation without another image decode."""
    _client, Session, _make_batch = identity_app
    db = Session()
    db.add_all([
        Student(teacher_id=1, class_id=1, name="周涛", student_no="01"),
        Student(teacher_id=1, class_id=1, name="吴静", student_no="02"),
    ])
    db.commit()

    import routers.grade as grade_router

    identity_crops = []

    async def unreadable_identity(image_path, roster_names, **_kwargs):
        identity_crops.append((image_path, list(roster_names)))
        return {
            "name": "",
            "student_no": "",
            "confidence": 0.0,
            "candidate_names": [],
            "match_status": "",
        }

    async def recognized_answers(_image_path, _batch_id, _paper_cache, **_kwargs):
        return {"questions": [{"question_number": "1"}]}

    monkeypatch.setattr(
        grade_router,
        "recognize_student_info_with_roster",
        unreadable_identity,
    )
    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", recognized_answers)
    batch = QuickBatch("unresolved-identity", grade_router.UPLOAD_DIR)
    result = asyncio.run(grade_router._recognize_batch_upload(
        batch,
        "unused.png",
        object(),  # template cache placeholder; the OCR fake ignores it
        teacher_id="1",
        paper_id="paper-1",
        roster_names=["周涛", "吴静"],
    ))
    # The identity preflight is the only server-side identity crop: an
    # unmatched name must not trigger a second roster pass.
    assert identity_crops == [("unused.png", ["周涛", "吴静"])]
    identity = resolve_student_identity(
        db,
        1,
        result["student_name"],
        result["student_no"],
        1,
        roster_candidate_names=result.get("identity_roster_candidates") or [],
        roster_match_status=str(result.get("identity_roster_match_status") or ""),
    )
    db.close()

    assert result["student_no"] == ""
    assert identity["status"] == "needs_confirmation"
    assert identity["reason"] == "missing_identity"


def test_unique_name_can_resolve_when_ocr_student_no_is_wrong(identity_db):
    target = Student(teacher_id=1, class_id=1, name="按姓名匹配", student_no="01")
    identity_db.add(target)
    identity_db.commit()

    identity = resolve_student_identity(
        identity_db, 1, "按姓名匹配", "99", 1,
    )

    assert identity["status"] == "matched"
    assert identity["student_id"] == target.id
    assert identity["reason"] == "unique_name"


def test_unique_student_no_can_resolve_when_ocr_name_is_wrong(identity_db):
    target = Student(teacher_id=1, class_id=1, name="按学号匹配", student_no="01")
    identity_db.add(target)
    identity_db.commit()

    identity = resolve_student_identity(
        identity_db, 1, "姓名误读", "01", 1,
    )

    assert identity["status"] == "matched"
    assert identity["student_id"] == target.id
    assert identity["reason"] == "unique_student_no"


def test_batch_ocr_reads_identity_before_template_answer_ocr(identity_app, monkeypatch):
    """The top/left identity crop is completed before reusable column OCR."""
    _client, Session, _make_batch = identity_app
    import routers.grade as grade_router

    events = []

    async def fake_identity(path, roster, **kwargs):
        events.append(("identity", path, tuple(roster), kwargs))
        return {
            "name": "周涛",
            "student_no": "01",
            "confidence": 0.98,
            "candidate_names": ["周涛"],
            "match_status": "unique_high_confidence",
        }

    async def fake_answer_ocr(path, _batch_id, _cache, **kwargs):
        events.append(("answers", path, kwargs))
        return {"questions": []}

    monkeypatch.setattr(
        grade_router, "recognize_student_info_with_roster", fake_identity,
    )
    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", fake_answer_ocr)

    from services.quick_grader import QuickBatch
    batch = QuickBatch("identity-order", _client.app.state.upload_dir)
    batch.identity_mode = "identified"
    result = asyncio.run(grade_router._recognize_batch_upload(
        batch,
        "paper.png",
        object(),
        teacher_id="1",
        paper_id="paper-1",
        roster_names=["周涛"],
    ))
    Session().close()

    assert [event[0] for event in events] == ["identity", "answers"]
    assert result["student_name"] == "周涛"
    assert result["student_no"] == "01"
    assert result["identity_ocr_source"] == "top_left_crop"


def test_identity_image_endpoint_serves_owned_batch_paper(identity_app):
    client, Session, make_batch = identity_app
    db = Session()
    db.add(Student(teacher_id=1, class_id=1, name="赵六", student_no="01"))
    db.add(Student(teacher_id=1, class_id=1, name="吴静", student_no="02"))
    db.commit()
    identity = resolve_student_identity(db, 1, "", "", 1)
    db.close()
    make_batch("image1", identity)

    response = client.get("/api/batch-grade/image1/paper/paper-1/image")

    assert response.status_code == 200
    assert response.content == b"identity-paper"
    assert response.headers["cache-control"] == "private, no-store"
    assert [item["name"] for item in identity["candidates"]] == ["赵六", "吴静"]


def test_identity_options_use_batch_frozen_class_for_existing_batches(identity_app):
    client, Session, make_batch = identity_app
    db = Session()
    first = Student(teacher_id=1, class_id=1, name="赵六", student_no="01")
    second = Student(teacher_id=1, class_id=1, name="吴静", student_no="")
    other_class = Student(teacher_id=1, class_id=2, name="孙七", student_no="02")
    db.add_all([first, second, other_class])
    db.commit()
    expected_ids = [first.id, second.id]
    db.close()
    make_batch("identity-options", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "missing_identity",
        "ocr_name": "",
        "ocr_student_no": "",
        "candidates": [],
    })

    response = client.get("/api/batch-grade/identity-options/identity-options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["class_id"] == 1
    assert [item["student_id"] for item in payload["students"]] == expected_ids
    assert [item["name"] for item in payload["students"]] == ["赵六", "吴静"]


def test_identity_image_endpoint_normalizes_exif_orientation(identity_app):
    client, _Session, make_batch = identity_app
    batch = make_batch("oriented-image", {
        "status": "matched", "student_id": 1, "ocr_name": "赵六",
        "ocr_student_no": "01", "candidates": [],
    })
    source = Path(batch.papers[0]["image_path"])
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 40), "white").save(source, format="JPEG", exif=exif)

    response = client.get("/api/batch-grade/oriented-image/paper/paper-1/image")

    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as oriented:
        assert oriented.size == (40, 80)
        assert oriented.getexif().get(274, 1) == 1


def test_identity_image_endpoint_rejects_unknown_paper(identity_app):
    client, _Session, make_batch = identity_app
    make_batch("image2", {
        "status": "needs_confirmation", "student_id": None,
        "reason": "missing_identity", "ocr_name": "",
        "ocr_student_no": "", "candidates": [],
    })

    response = client.get("/api/batch-grade/image2/paper/not-found/image")
    assert response.status_code == 404


def test_anonymous_batch_grades_without_class_or_identity_and_rejects_archive(identity_app):
    client, Session, make_batch = identity_app
    batch = make_batch("anonymous-grade", {
        "status": "needs_confirmation",
        "student_id": None,
        "reason": "missing_identity",
        "ocr_name": "不应保留的姓名",
        "ocr_student_no": "99",
        "candidates": [],
    })
    batch.class_id = None
    batch.identity_mode = "anonymous"
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "p1.png",
        "image_path": batch.papers[0]["image_path"],
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(batch.papers[0]["image_path"], "succeeded")
    batch.save_meta()

    response = client.post(
        "/api/batch-grade/confirm",
        json=_confirm_payload("anonymous-grade"),
    )

    assert response.status_code == 200, response.text
    result_events = [payload for event, payload in _parse_sse(response.text) if event == "result"]
    assert [item["student_name"] for item in result_events] == ["匿名页 1"]

    staged = QuickBatch.load_meta("anonymous-grade", client.app.state.upload_dir)
    assert staged.class_id is None
    assert staged.identity_mode == "anonymous"
    assert staged.review_results[0]["identity_assignment"] == {
        "action": "anonymous",
        "student_id": None,
        "name": "匿名页 1",
        "student_no": "",
    }
    assert staged.papers[0]["student_name"] == "匿名页 1"
    assert staged.papers[0]["student_no"] == ""

    review = client.get("/api/batch-grade/review/anonymous-grade")
    assert review.status_code == 200
    assert review.json()["identity_mode"] == "anonymous"

    identity_options = client.get(
        "/api/batch-grade/anonymous-grade/identity-options"
    )
    assert identity_options.status_code == 409

    archive = client.post(
        "/api/batch-grade/review/archive-all",
        json={"batch_id": "anonymous-grade"},
    )
    assert archive.status_code == 409
    assert "不能写入学生档案" in archive.json()["detail"]

    db = Session()
    assert db.query(Student).count() == 0
    assert db.query(GradingResult).count() == 0
    db.close()


def test_anonymous_identity_plan_keeps_same_name_pages_independent(tmp_path):
    from routers.grade import _prepare_anonymous_identity_plan

    batch = QuickBatch("anonymous-pages", str(tmp_path))
    batch.identity_mode = "anonymous"
    batch.uploads = [
        {"paper_id": "paper-a"},
        {"paper_id": "paper-b"},
    ]
    batch.papers = [
        {"paper_id": "paper-a", "student_name": "同一个OCR姓名", "student_no": "1"},
        {"paper_id": "paper-b", "student_name": "同一个OCR姓名", "student_no": "1"},
    ]

    plan = _prepare_anonymous_identity_plan(batch)

    assert plan["paper-a"]["name"] == "匿名页 1"
    assert plan["paper-b"]["name"] == "匿名页 2"
    assert plan["paper-a"]["action"] == "anonymous"
    assert plan["paper-b"]["action"] == "anonymous"


@pytest.fixture()
def identity_db():
    """一个只装了老师和班级的空库，用于直接测身份解析（不起 HTTP 层）。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add(User(id=1, name="师一", role="teacher"))
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.commit()
    yield db
    db.close()


class TestFilenameCluesOnlyAdd:
    """姓名优先：数字噪声不干扰归属；明确的姓名冲突由老师确认。"""

    @staticmethod
    def _resolve(db, *, ocr_name, ocr_no, filename):
        import routers.grade as grade_router

        name, no, identity = grade_router._resolve_identity_with_filename_clues(
            db, 1, 1,
            ocr_name=ocr_name,
            ocr_student_no=ocr_no,
            filename=filename,
        )
        return name, no, identity

    def test_name_only_ocr_still_auto_matches_despite_filename_serial(self, identity_db):
        """核心回归：文件名里的流水号不得阻断唯一姓名的自动匹配。"""
        target = Student(teacher_id=1, class_id=1, name="马飞", student_no="2021000008")
        identity_db.add(target)
        identity_db.commit()

        _name, _no, identity = self._resolve(
            identity_db, ocr_name="马飞", ocr_no="", filename="收集件_20240301_0007.jpg",
        )

        assert identity["status"] == "matched"
        assert identity["reason"] == "unique_name"

    def test_another_known_name_in_filename_requires_confirmation(self, identity_db):
        """新约定：不是纯数字噪声，而是明确指向另一学生的姓名，必须核对。"""
        identity_db.add(Student(teacher_id=1, class_id=1, name="马飞", student_no="2021000008"))
        identity_db.add(Student(teacher_id=1, class_id=1, name="赵六", student_no="2021591199"))
        identity_db.commit()

        _name, _no, identity = self._resolve(
            identity_db, ocr_name="马飞", ocr_no="", filename="赵六2021591199.jpg",
        )

        assert identity["status"] == "needs_confirmation"
        assert identity["reason"] == "identity_name_conflict"

    def test_filename_number_disambiguates_same_name_students(self, identity_db):
        """应有的收益：同名两人时，文件名补出的学号把身份定下来。"""
        first = Student(teacher_id=1, class_id=1, name="马飞", student_no="2021000008")
        second = Student(teacher_id=1, class_id=1, name="马飞", student_no="2021591109")
        identity_db.add_all([first, second])
        identity_db.commit()

        _name, no, identity = self._resolve(
            identity_db, ocr_name="马飞", ocr_no="", filename="马飞2021591109_1eax24.jpg",
        )

        assert identity["status"] == "matched"
        assert identity["student_id"] == second.id
        assert no == "2021591109"

    def test_filename_carries_identity_when_ocr_read_nothing(self, identity_db):
        """OCR 全空时，文件名是唯一线索，应当能独立完成匹配。"""
        target = Student(teacher_id=1, class_id=1, name="马飞", student_no="2021000008")
        identity_db.add(target)
        identity_db.commit()

        _name, _no, identity = self._resolve(
            identity_db, ocr_name="", ocr_no="", filename="马飞2021000008_1eax24.jpg",
        )

        assert identity["status"] == "matched"
        assert identity["student_id"] == target.id

    def test_unmatched_baseline_keeps_its_own_reason(self, identity_db):
        """兜底失败时保留真实原因，不被文件名噪声掩盖成 conflict。"""
        identity_db.add(Student(teacher_id=1, class_id=1, name="赵六", student_no="2021591199"))
        identity_db.commit()

        _name, _no, identity = self._resolve(
            identity_db, ocr_name="马飞", ocr_no="", filename="收集件_20240301_0007.jpg",
        )

        assert identity["status"] == "needs_confirmation"
        assert identity["reason"] == "name_not_found"


def test_collection_identity_cannot_select_other_teachers_or_classes(identity_db):
    import routers.grade as grade_router

    identity_db.add(User(id=2, name="其他老师", role="teacher"))
    identity_db.add_all([
        TeacherClass(id=2, teacher_id=1, class_name="另一班"),
        TeacherClass(id=3, teacher_id=2, class_name="其他老师班级"),
    ])
    identity_db.flush()
    identity_db.add_all([
        Student(id=1, teacher_id=1, class_id=1, name="张三", student_no="08"),
        Student(id=2, teacher_id=1, class_id=2, name="李四", student_no="56"),
        Student(id=3, teacher_id=2, class_id=3, name="王平", student_no="09"),
    ])
    identity_db.commit()
    for path in ["收集.zip/另一班/李四56/abc.jpg", "收集.zip/王平09/abc.jpg"]:
        _, _, identity = grade_router._resolve_identity_with_filename_clues(
            identity_db, 1, 1, ocr_name="", ocr_student_no="", filename="abc.jpg", source_relative_path=path,
        )
        assert identity["status"] == "needs_confirmation"
        assert all(student["student_id"] == 1 for student in identity["candidates"])
    _, _, no_class = grade_router._resolve_identity_with_filename_clues(
        identity_db, 1, None, ocr_name="张三", ocr_student_no="08", filename="张三08.jpg",
    )
    assert no_class["status"] == "needs_confirmation"
    assert no_class["candidates"] == []
