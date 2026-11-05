import asyncio
from io import BytesIO
import json
import os

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base, get_db

from models_user import User
from models_rate_limit import TeacherClass
from routers.auth import _require_user
from services.quick_grader import QuickBatch, batch_ocr_summary, mark_ocr_outcome
from services.template_session import TemplateSession


def _events(response):
    events = []
    current = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current = line[7:]
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line[6:])))
            current = None
    return events


def _setup(monkeypatch, tmp_path, *, education_stage=""):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(id=1, name="老师", role="teacher", education_stage=education_stage)
    db.add(user)
    db.add(TeacherClass(id=1, teacher_id=1, class_name="一班"))
    db.commit()

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    import routers.grade as grade_router
    from routers import grade_review
    from routers import grade_template
    test_app = FastAPI()
    test_app.include_router(grade_router.router, prefix="/api")
    test_app.include_router(grade_template.router, prefix="/api")
    test_app.include_router(grade_review.router, prefix="/api")
    auth_user = User(id=1, name="老师", role="teacher", current_class_id=1, education_stage=education_stage)
    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[_require_user] = lambda: auth_user
    monkeypatch.setattr(grade_router, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(grade_router, "SessionLocal", Session)
    monkeypatch.setattr(
        grade_router,
        "_validate_client_ocr_ready_upload",
        lambda filename, path: {"safe": True, "reason": ""},
    )
    monkeypatch.setattr(
        grade_router, "validate_upload_path",
        lambda filename, content: {"safe": True, "reason": ""},
    )
    # These tests exercise batching and recovery state, not browser image
    # canonicalization. Model the accepted client artifact boundary explicitly.
    monkeypatch.setattr(
        grade_router,
        "_client_ocr_ready_orientation",
        lambda _filename: {
            "orientation_status": "auto_passed",
            "orientation_reason": "client_ocr_ready_v1",
            "orientation_confidence": 1.0,
            "detected_tilt_degrees": 0.0,
            "exif_orientation": 1,
            "orientation_coordinate_space": "client_normalized_pixels",
        },
    )
    monkeypatch.setattr(grade_router, "_check_limits_or_raise", lambda db, user_id: None)
    # Recognition now grades finalized papers too. Keep that new model boundary
    # synthetic while exercising the real pipeline and durable result handling.
    async def fake_judge_paper(questions, detail_level="detailed", *, teacher_material=None):
        return {str(question["question_no"]): {
            "result": "correct", "explanation": "", "hint": "",
            "model_answer": "", "standard_answer": question.get("standard_answer", ""),
            "error_type": "",
        } for question in questions}

    monkeypatch.setattr("services.grader.judge_paper", fake_judge_paper)
    async def available(_user):
        return {"status": "available", "message": "批改服务可用"}
    monkeypatch.setattr(grade_router, "batch_grade_availability", available)
    async def hardware_ready(_teacher_id, **_contract):
        return {"status": "ready", "message": "加速节点已就绪"}
    monkeypatch.setattr(
        grade_router,
        "get_public_hardware_readiness",
        hardware_ready,
    )
    monkeypatch.setattr(grade_template,
        "get_public_hardware_readiness",
        hardware_ready,
    )
    return TestClient(test_app, raise_server_exceptions=False), Session


def _confirmed_template(tmp_path, template_id: str) -> TemplateSession:
    session = TemplateSession(template_id, str(tmp_path), teacher_id=1)
    session.status = "confirmed"
    session.category = "daily"
    session.add_page(0, [{
        "question_number": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    session.confirm_current_contract(1)
    session.save_meta()
    return session


def test_collection_source_paths_survive_upload_ocr_checkpoint_and_identity_plan(monkeypatch, tmp_path):
    """Real multipart/state pipeline; only image/OCR/model boundaries are mocked."""
    client, Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    _confirmed_template(tmp_path, "collection-template")
    from models import Student
    import routers.grade as grade_router

    with Session() as db:
        db.add_all([
            Student(id=1, teacher_id=1, class_id=1, name="张三", student_no="08"),
            Student(id=2, teacher_id=1, class_id=1, name="李四", student_no="56"),
        ])
        db.commit()

    async def blank_identity(*_args, **_kwargs):
        return {"name": "", "student_no": "", "confidence": 0.0, "candidate_names": [], "match_status": "no_match"}

    async def recognized_answers(*_args, **_kwargs):
        return {"student_name": "", "student_no": "", "questions": [{
            "question_number": "1", "question_text": "1+1=?", "question_type": "fill", "student_answer": "2",
        }]}

    monkeypatch.setattr(grade_router, "recognize_student_info_with_roster", blank_identity)
    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", recognized_answers)
    paths = [
        "收集.zip/张三/abc.jpg", "收集.zip/李四/abc.jpg",
        "收集.zip/随机目录1/张三_1.jpg", "收集.zip/随机目录2/李四_1.jpg",
        "收集.zip/张三_123456.jpg", "收集.zip/张三/李四_2.jpg",
    ]
    response = client.post("/api/batch-grade/recognize", files=[
        ("files", (f"{path.rsplit('/', 1)[-1][:-4]}.ocr-ready-v1.jpg", f"synthetic-{index}".encode(), "image/jpeg"))
        for index, path in enumerate(paths)
    ], data={"template_id": "collection-template", "mode": "auto", "category": "daily", "source_paths": json.dumps(paths)})
    assert response.status_code == 200
    done = [payload for event, payload in _events(response) if event == "done"]
    assert len(done) == 1, response.text
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert [upload["source_relative_path"] for upload in batch.uploads] == paths
    assert len({upload["image_path"] for upload in batch.uploads}) == len(paths)
    papers = {paper["identity"]["source_relative_path"]: paper for paper in batch.papers}
    assert [papers[path]["identity"]["student_id"] for path in paths] == [1, 2, 1, 2, 1, None]
    assert papers[paths[-1]]["identity"]["reason"] == "identity_name_conflict"
    assert all(not papers[path]["identity"]["number_used"] for path in paths[:-1])
    with Session() as db:
        plan = grade_router._prepare_identity_plan(db, batch, [
            grade_router._IdentityResolution(paper_id=papers[paths[-1]]["paper_id"], action="existing", student_id=2),
        ], 1)
        assert [plan[papers[path]["paper_id"]]["student_id"] for path in paths] == [1, 2, 1, 2, 1, 2]
        assert db.query(Student).count() == 2


def test_bad_collection_metadata_is_rejected_before_upload_or_model_work(monkeypatch, tmp_path):
    client, _ = _setup(monkeypatch, tmp_path, education_stage="senior")
    import routers.grade as grade_router
    from routers import grade_template

    async def must_not_save(*_args, **_kwargs):
        raise AssertionError("invalid source metadata must be rejected before saving uploads")

    monkeypatch.setattr(grade_router, "_save_batch_uploads", must_not_save)
    monkeypatch.setattr(grade_template, "_save_batch_uploads", must_not_save)
    for paths in [["../张三/abc.jpg"], ["a.jpg", "b.jpg"], ["C:/users/a.jpg"]]:
        response = client.post("/api/batch-grade/recognize", files=[
            ("files", ("abc.ocr-ready-v1.jpg", b"synthetic", "image/jpeg")),
        ], data={"template_id": "not-needed", "source_paths": json.dumps(paths)})
        assert response.status_code == 400
    assert not list(tmp_path.glob("quick_batch_*"))


def test_initial_recognition_publishes_batch_id_and_unlocks_on_disconnect(
    monkeypatch, tmp_path,
):
    _client, Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    import routers.grade as grade_router
    from services.quick_grader import BatchOperationBusy, batch_operation_lock
    _confirmed_template(tmp_path, "disconnect-template")

    async def scenario():
        db = Session()
        batch_id = ""
        try:
            response = await grade_router.batch_grade_recognize(
                files=[UploadFile(
                    filename="paper.ocr-ready-v1.jpg",
                    file=BytesIO(b"image"),
                )],
                category="daily",
                exam_name="",
                template_id="disconnect-template",
                mode="auto",
                user=User(id=1, name="老师", role="teacher", current_class_id=1, education_stage="senior"),
                db=db,
            )
            iterator = response.body_iterator
            first_event = await anext(iterator)
            if isinstance(first_event, bytes):
                first_event = first_event.decode("utf-8")
            assert first_event.startswith("event: progress\n")
            payload = json.loads(
                next(line[6:] for line in first_event.splitlines() if line.startswith("data: "))
            )
            batch_id = payload["batch_id"]
            assert payload["current"] == 0
            checkpoint = QuickBatch.load_meta(batch_id, str(tmp_path))
            assert len(checkpoint.uploads[0]["source_sha256"]) == 64
            assert (
                checkpoint.uploads[0]["working_sha256"]
                == checkpoint.uploads[0]["source_sha256"]
            )

            try:
                with batch_operation_lock(batch_id, str(tmp_path)):
                    raise AssertionError("recognition stream must hold its batch lock")
            except BatchOperationBusy:
                pass

            await iterator.aclose()
        finally:
            db.close()

        interrupted = QuickBatch.load_meta(batch_id, str(tmp_path))
        assert interrupted.status == "recognition_interrupted"
        assert [job["status"] for job in interrupted.ocr_jobs] == ["queued"]
        with batch_operation_lock(batch_id, str(tmp_path)):
            pass

    asyncio.run(scenario())


def test_resume_disconnect_does_not_consume_second_ocr_attempt(monkeypatch, tmp_path):
    _client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router
    from routers import grade_template

    batch = QuickBatch("resume-second-attempt-disconnect", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "resume-disconnect").template_id
    batch.status = "recognition_interrupted"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    image_path = batch_dir / "paper.png"
    image_path.write_bytes(b"image")
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": image_path.name,
        "image_path": str(image_path),
        "source_sha256": "source-hash",
        "working_sha256": "working-hash",
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.ocr_jobs[0].update({"status": "queued", "attempts": 1})
    batch.save_meta()

    started = asyncio.Event()

    async def blocked_recognition(_path, _batch_id, _paper_cache, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", blocked_recognition)
    async def forbidden_hardware(*_args, **_kwargs):
        raise AssertionError("recognition recovery must not probe accelerator hardware")
    monkeypatch.setattr(
        grade_router,
        "get_public_hardware_readiness",
        forbidden_hardware,
    )
    monkeypatch.setattr(grade_template,
        "get_public_hardware_readiness",
        forbidden_hardware,
    )

    async def scenario():
        response = await grade_router.batch_grade_resume_recognition(
            batch.batch_id,
            User(id=1, name="老师", role="teacher", current_class_id=1),
        )
        iterator = response.body_iterator

        async def consume():
            async for _chunk in iterator:
                pass

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("disconnect must cancel the recognition stream")

    asyncio.run(scenario())

    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "recognition_interrupted"
    assert restored.ocr_jobs[0]["status"] == "queued"
    assert restored.ocr_jobs[0]["attempts"] == 1
    assert [item["image_path"] for item in restored.pending_uploads] == [
        str(image_path)
    ]


def test_accelerator_wait_does_not_consume_ocr_attempt(tmp_path):
    batch = QuickBatch("accelerator-wait-attempt", str(tmp_path))
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "paper.jpg",
        "image_path": "paper.jpg",
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job("paper.jpg", "running")

    failed, can_retry = mark_ocr_outcome(
        batch,
        "paper.jpg",
        {
            "error": "加速节点不可用，请使用客户端",
            "_accelerator_state": "unavailable",
            "_accelerator_reason_code": "accelerator_node_unavailable",
        },
    )

    assert failed is True
    assert can_retry is True
    assert batch.ocr_jobs[0]["status"] == "queued"
    assert batch.ocr_jobs[0]["attempts"] == 0
    assert batch.ocr_jobs[0]["error_code"] == "accelerator_node_unavailable"
    assert batch.ocr_jobs[0]["_accelerator_state"] == "unavailable"
    assert (
        batch.ocr_jobs[0]["_accelerator_reason_code"]
        == "accelerator_node_unavailable"
    )


def test_accelerator_wait_keeps_batch_resumable_without_failure_placeholder(
    monkeypatch,
    tmp_path,
):
    client, _Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    import routers.grade as grade_router
    _confirmed_template(tmp_path, "accelerator-wait-template")

    async def wait_for_accelerator(
        _path,
        _batch_id,
        _paper_cache,
        teacher_id="",
        paper_id="",
    ):
        return {
            "questions": [],
            "error": "加速节点不可用，请使用客户端",
            "_accelerator_state": "unavailable",
            "_accelerator_reason_code": "accelerator_node_unavailable",
        }

    monkeypatch.setattr(
        grade_router,
        "recognize_paper_with_cache",
        wait_for_accelerator,
    )
    response = client.post(
        "/api/batch-grade/recognize",
        files=[(
            "files",
            ("paper.ocr-ready-v1.jpg", b"image", "image/jpeg"),
        )],
        data={
            "template_id": "accelerator-wait-template",
            "mode": "auto",
            "category": "daily",
        },
    )

    assert response.status_code == 200
    events = _events(response)
    errors = [payload for event, payload in events if event == "error"]
    assert errors[-1]["code"] == "ACCELERATOR_UNAVAILABLE"
    assert errors[-1]["message"] == "加速节点不可用，请使用客户端"
    assert not [payload for event, payload in events if event == "paper"]
    assert not [payload for event, payload in events if event == "done"]

    batch_id = errors[-1]["batch_id"]
    restored = QuickBatch.load_meta(batch_id, str(tmp_path))
    assert restored.status == "recognition_interrupted"
    assert restored.papers == []
    assert restored.ocr_jobs[0]["status"] == "queued"
    assert restored.ocr_jobs[0]["attempts"] == 0
    assert restored.ocr_jobs[0]["_accelerator_state"] == "unavailable"
    assert (
        restored.ocr_jobs[0]["_accelerator_reason_code"]
        == "accelerator_node_unavailable"
    )
    assert [item["image_path"] for item in restored.pending_uploads] == [
        restored.uploads[0]["image_path"]
    ]


def test_accelerator_wait_during_resume_preserves_success_and_queued_work(
    monkeypatch,
    tmp_path,
):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("accelerator-resume-wait", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "accelerator-resume-wait").template_id
    batch.status = "recognition_interrupted"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    for index in (1, 2):
        image_path = batch_dir / f"paper-{index}.ocr-ready-v1.jpg"
        image_path.write_bytes(b"image")
        batch.uploads.append({
            "paper_id": f"paper-{index}",
            "filename": image_path.name,
            "image_path": str(image_path),
            "original_image_path": str(image_path),
            "source_sha256": f"source-{index}",
            "working_sha256": f"working-{index}",
            "orientation_status": "auto_passed",
            "orientation_reason": "client_ocr_ready_v1",
        })
    batch.ensure_ocr_jobs(batch.uploads)
    batch.save_meta()

    async def mixed_result(path, _batch_id, _paper_cache, **_kwargs):
        if path.endswith("paper-2.ocr-ready-v1.jpg"):
            return {
                "questions": [],
                "error": "等待教师客户端加速节点完成图片处理",
                "_accelerator_state": "pending",
                "_accelerator_reason_code": "accelerator_wait_timeout",
            }
        return {
            "student_name": "学生一",
            "student_no": "01",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
            }],
        }

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", mixed_result)

    response = client.post(
        f"/api/batch-grade/{batch.batch_id}/recognize/resume"
    )
    assert response.status_code == 200
    events = _events(response)
    errors = [payload for event, payload in events if event == "error"]
    assert errors[-1]["code"] == "ACCELERATOR_WAITING"
    assert not [payload for event, payload in events if event == "done"]

    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "recognition_interrupted"
    assert len(restored.papers) == 1
    assert [job["status"] for job in restored.ocr_jobs] == [
        "succeeded",
        "queued",
    ]
    assert [job["attempts"] for job in restored.ocr_jobs] == [1, 0]
    assert restored.ocr_jobs[1]["_accelerator_state"] == "pending"
    assert (
        restored.ocr_jobs[1]["_accelerator_reason_code"]
        == "accelerator_wait_timeout"
    )
    assert [item["filename"] for item in restored.pending_uploads] == [
        "paper-2.ocr-ready-v1.jpg"
    ]


def test_resume_done_survives_immediate_stream_close(monkeypatch, tmp_path):
    _client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-close-after-done", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "resume-close-after-done").template_id
    batch.status = "recognition_interrupted"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    image_path = batch_dir / "paper.png"
    image_path.write_bytes(b"image")
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": image_path.name,
        "image_path": str(image_path),
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.save_meta()

    async def recognize(_path, _batch_id, _paper_cache, **_kwargs):
        return {
            "student_name": "student",
            "student_no": "001",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
            }],
        }

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", recognize)

    async def scenario():
        response = await grade_router.batch_grade_resume_recognition(
            batch.batch_id,
            User(id=1, name="老师", role="teacher", current_class_id=1),
        )
        iterator = response.body_iterator
        async for chunk in iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            if text.startswith("event: done\n"):
                await iterator.aclose()
                return
        raise AssertionError("recognition resume did not emit done")

    asyncio.run(scenario())

    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "ocr_done"
    assert restored.error is None
    assert restored.ocr_jobs[0]["status"] == "succeeded"


def test_recognition_resume_only_runs_unfinished_jobs(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-small", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "resume-unfinished-only").template_id
    batch.category = "daily"
    batch.grade_mode = "auto"
    batch.status = "recognition_interrupted"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    uploads = []
    for index in (1, 2):
        image_path = batch_dir / f"paper-{index}.png"
        image_path.write_bytes(b"test-image")
        uploads.append({
            "paper_id": f"paper-{index}",
            "filename": image_path.name,
            "image_path": str(image_path),
            "source_sha256": f"source-{index}",
            "working_sha256": f"working-{index}",
            "orientation_status": "auto_passed",
            "orientation_reason": "normal_angle",
            "orientation_confidence": 1.0,
            "detected_tilt_degrees": 0.0,
        })
    batch.uploads = uploads
    batch.questions = [{
        "question_no": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
    }]
    batch.papers = [{
        "paper_id": "paper-1",
        "filename": "paper-1.png",
        "image_path": uploads[0]["image_path"],
        "student_name": "张三",
        "student_no": "01",
        "questions": [{
            "question_no": "1",
            "student_answer": "2",
            "question_type": "fill",
        }],
    }]
    batch.ensure_ocr_jobs(uploads)
    batch.ocr_jobs[0]["status"] = "succeeded"
    batch.ocr_jobs[1]["status"] = "running"
    batch.save_meta()

    calls = []
    phases = []

    async def fake_full(path, _batch_id, _paper_cache, **_kwargs):
        from services.ocr_service import _vision_phase_key

        calls.append(os.path.basename(path))
        phases.append(_vision_phase_key.get())
        return {
            "student_name": "李四",
            "student_no": "02",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
            }],
        }

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", fake_full)

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    assert response.status_code == 200
    events = _events(response)
    assert calls == ["paper-2.png"]
    assert phases == ["recognition-resume:1"]
    assert len([payload for event, payload in events if event == "done"]) == 1

    papers = [payload for event, payload in events if event == "paper"]
    assert {payload["paper_id"] for payload in papers} == {"paper-1", "paper-2"}
    completed = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert completed.status == "ocr_done"
    assert completed.recognition_resume_generation == 1
    assert {job["status"] for job in completed.ocr_jobs} == {"succeeded"}
    assert [paper["paper_id"] for paper in completed.papers] == ["paper-1", "paper-2"]


def test_busy_recognition_resume_does_not_advance_generation(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router
    from routers import grade_template
    from services.quick_grader import batch_operation_lock
    hardware_calls = []

    async def hardware_must_not_be_required(*_args, **_kwargs):
        hardware_calls.append(True)
        raise AssertionError("template recovery readiness must not require an accelerator")

    monkeypatch.setattr(
        grade_router,
        "get_public_hardware_readiness",
        hardware_must_not_be_required,
    )
    monkeypatch.setattr(grade_template,
        "get_public_hardware_readiness",
        hardware_must_not_be_required,
    )

    batch = QuickBatch("resume-busy", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = "busy-template"
    batch.status = "recognition_interrupted"
    batch.save_meta()

    with batch_operation_lock(batch.batch_id, str(tmp_path)):
        response = client.post(
            f"/api/batch-grade/{batch.batch_id}/recognize/resume"
        )

    assert response.status_code == 200
    errors = [payload for event, payload in _events(response) if event == "error"]
    assert [payload["code"] for payload in errors] == ["BATCH_RECOGNITION_BUSY"]
    assert hardware_calls == []
    reloaded = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert reloaded.recognition_resume_generation == 0


def test_recognition_resume_requeues_one_transient_failure(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-transient", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "resume-transient-failure").template_id
    batch.status = "failed"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    image_path = batch_dir / "paper.png"
    image_path.write_bytes(b"image")
    upload = {
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(image_path),
        "source_sha256": "source-hash",
        "working_sha256": "working-hash",
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
        "orientation_confidence": 1.0,
        "detected_tilt_degrees": 0.0,
    }
    batch.uploads = [upload]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(str(image_path), "running")
    batch.mark_ocr_job(
        str(image_path),
        "failed",
        "provider timeout",
        retryable=True,
        error_code="provider_timeout",
    )
    batch.save_meta()
    calls = []

    async def recognize(path, _batch_id, _paper_cache, **_kwargs):
        calls.append(os.path.basename(path))
        return {
            "student_name": "张三",
            "student_no": "01",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
            }],
        }

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", recognize)

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    assert response.status_code == 200
    assert _events(response)[-1][0] == "done"
    assert calls == ["paper.png"]
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.ocr_jobs[0]["status"] == "succeeded"
    assert restored.ocr_jobs[0]["attempts"] == 2


def test_second_transient_failure_is_not_run_a_third_time(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-second-failure", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    # With two template pages a failed sheet cannot be attributed to a page, so
    # an exhausted failure stays a failure instead of becoming review placeholders.
    template = _confirmed_template(tmp_path, "second-transient-failure")
    template.add_page(1, [{
        "question_number": "2",
        "question_text": "2+2=?",
        "question_type": "fill",
        "standard_answer": "4",
    }])
    template.confirm_current_contract(1)
    template.save_meta()
    batch.template_id = template.template_id
    batch.status = "failed"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    image_path = batch_dir / "paper.png"
    image_path.write_bytes(b"image")
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(image_path),
        "source_sha256": "source-hash",
        "working_sha256": "working-hash",
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(str(image_path), "running")
    batch.mark_ocr_job(
        str(image_path),
        "failed",
        "first provider timeout",
        retryable=True,
        error_code="provider_timeout",
    )
    batch.save_meta()
    calls = []

    async def transient_failure(path, _batch_id, _paper_cache, **_kwargs):
        calls.append(os.path.basename(path))
        return {
            "questions": [],
            "student_name": "",
            "student_no": "",
            "error": "second provider timeout",
            "_ocr_retryable": True,
            "_ocr_error_code": "provider_timeout",
        }

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", transient_failure)

    second_attempt = client.post(
        f"/api/batch-grade/{batch.batch_id}/recognize/resume"
    )
    assert second_attempt.status_code == 200
    second_errors = [
        payload for event, payload in _events(second_attempt) if event == "error"
    ]
    assert [payload["code"] for payload in second_errors] == [
        "OCR_RETRY_EXHAUSTED"
    ]
    assert calls == ["paper.png"]

    after_second = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert after_second.ocr_jobs[0]["status"] == "failed"
    assert after_second.ocr_jobs[0]["attempts"] == 2
    assert after_second.ocr_jobs[0]["retryable"] is False
    assert after_second.ocr_jobs[0]["error_code"] == "provider_timeout"

    third_attempt = client.post(
        f"/api/batch-grade/{batch.batch_id}/recognize/resume"
    )
    assert third_attempt.status_code == 200
    third_errors = [
        payload for event, payload in _events(third_attempt) if event == "error"
    ]
    assert [payload["code"] for payload in third_errors] == [
        "OCR_RETRY_EXHAUSTED"
    ]
    assert calls == ["paper.png"]
    after_third_request = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert after_third_request.ocr_jobs[0]["attempts"] == 2


def test_recognition_resume_does_not_requeue_exhausted_failure(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-exhausted", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = _confirmed_template(tmp_path, "resume-exhausted-failure").template_id
    batch.status = "failed"
    batch_dir = tmp_path / f"quick_batch_{batch.batch_id}"
    batch_dir.mkdir()
    image_path = batch_dir / "paper.png"
    image_path.write_bytes(b"image")
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(image_path),
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.ocr_jobs[0].update({
        "status": "failed",
        "attempts": 2,
        "retryable": True,
        "error_code": "provider_timeout",
    })
    batch.save_meta()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("an exhausted OCR job must not run again")

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", forbidden)

    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")
    assert response.status_code == 200
    errors = [payload for event, payload in _events(response) if event == "error"]
    assert [payload["code"] for payload in errors] == ["OCR_RETRY_EXHAUSTED"]
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.ocr_jobs[0]["attempts"] == 2
    assert restored.ocr_jobs[0]["status"] == "failed"


def test_readiness_fault_does_not_mutate_resume_checkpoint(monkeypatch, tmp_path):
    client, _Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("resume-readiness-fault", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.source_mode = "template"
    batch.template_id = "readiness-template"
    batch.status = "failed"
    image_path = tmp_path / f"quick_batch_{batch.batch_id}" / "paper.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"image")
    batch.uploads = [{
        "paper_id": "paper-1",
        "filename": "paper.png",
        "image_path": str(image_path),
    }]
    batch.ensure_ocr_jobs(batch.uploads)
    batch.mark_ocr_job(str(image_path), "running")
    batch.mark_ocr_job(
        str(image_path),
        "failed",
        "provider timeout",
        retryable=True,
        error_code="provider_timeout",
    )
    batch.save_meta()
    meta_path = image_path.parent / "meta.json"
    before = meta_path.read_bytes()

    async def unavailable(_user):
        return {
            "status": "unavailable",
            "message": "图片处理线程已阻塞",
        }

    monkeypatch.setattr(grade_router, "batch_grade_availability", unavailable)
    response = client.post(f"/api/batch-grade/{batch.batch_id}/recognize/resume")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "GRADING_CHAIN_UNAVAILABLE"
    assert meta_path.read_bytes() == before
    restored = QuickBatch.load_meta(batch.batch_id, str(tmp_path))
    assert restored.status == "failed"
    assert restored.recognition_resume_generation == 0
    assert restored.ocr_jobs[0]["status"] == "failed"
    assert restored.ocr_jobs[0]["attempts"] == 1
    assert restored.ocr_jobs[0]["retryable"] is True


def test_template_batch_respects_ocr_dispatch_concurrency(monkeypatch, tmp_path):
    client, _ = _setup(monkeypatch, tmp_path, education_stage="senior")
    _confirmed_template(tmp_path, "concurrency-template")
    calls = []
    active = 0
    peak = 0

    async def fake_cached(
        path,
        _batch_id,
        _paper_cache,
        teacher_id="",
        paper_id="",
    ):
        nonlocal active, peak
        calls.append(path)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return {
                "student_name": "张三",
                "student_no": "01",
                "questions": [{
                    "question_number": "1",
                    "question_text": "1+1=?",
                    "question_type": "fill",
                    "student_answer": "2",
                }],
            }
        finally:
            active -= 1

    import routers.grade as grade_router
    from routers import grade_recognition
    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", fake_cached)
    monkeypatch.setattr(grade_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", 3)
    monkeypatch.setattr(grade_recognition, "OCR_TEACHER_WAVE_SIZE", 7)
    async def fake_identity(*_args, **_kwargs):
        return {
            "name": "张三",
            "student_no": "01",
            "confidence": 1.0,
            "candidate_names": ["张三"],
            "match_status": "matched",
        }
    monkeypatch.setattr(
        grade_router,
        "recognize_student_info_with_roster",
        fake_identity,
    )
    files = [
        ("files", (f"small_{i}.ocr-ready-v1.jpg", b"image", "image/jpeg"))
        for i in range(7)
    ]
    response = client.post(
        "/api/batch-grade/recognize",
        files=files,
        data={
            "template_id": "concurrency-template",
            "mode": "auto",
            "category": "daily",
        },
    )
    events = _events(response)
    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    assert done[0]["total_papers"] == 7
    assert len(calls) == 7
    assert peak == 3
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert batch.status == "ocr_done"


def test_template_resume_after_accelerator_wait_dispatches_at_batch_window(
    monkeypatch,
    tmp_path,
):
    """2026-10-03 incident shape: 15 recognized + 8 accelerator-pending papers.

    The resume must run the pending papers at the batch dispatch window (the
    retired template lane, still exported as 1 by the production drop-in, used
    to serialize whole-paper model I/O), end with 23 gradable papers, and keep
    the paper-event contract the browser merge relies on.
    """
    client, _Session = _setup(monkeypatch, tmp_path, education_stage="senior")
    _confirmed_template(tmp_path, "resume-window-template")
    import routers.grade as grade_router
    from routers import grade_recognition

    window = 4
    monkeypatch.setattr(grade_recognition, "OCR_BATCH_DISPATCH_CONCURRENT", window)
    monkeypatch.setattr(
        grade_router, "OCR_TEMPLATE_LOCAL_DISPATCH_CONCURRENT", 1, raising=False,
    )
    waiting_message = "等待教师客户端加速节点完成图片处理"
    phase = {"name": "initial"}
    initial_calls = []
    pending_paths = set()
    resume_calls = []
    active = 0
    peak = 0
    window_full = asyncio.Event()

    def recognized():
        return {
            "student_name": "",
            "student_no": "",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
                "answer_state": "visible",
            }],
        }

    async def fake_cached(path, _batch_id, _paper_cache, **_kwargs):
        nonlocal active, peak
        if phase["name"] == "initial":
            initial_calls.append(path)
            if len(initial_calls) > 15:
                pending_paths.add(path)
                return {
                    "questions": [],
                    "error": waiting_message,
                    "_accelerator_state": "pending",
                    "_accelerator_reason_code": "accelerator_wait_timeout",
                }
            return recognized()
        resume_calls.append(path)
        active += 1
        peak = max(peak, active)
        try:
            # Hold each slot until the window fills; a serial dispatcher
            # times out here once per paper and never exceeds one slot.
            if active >= window:
                window_full.set()
            try:
                await asyncio.wait_for(window_full.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            return recognized()
        finally:
            active -= 1

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", fake_cached)
    files = [
        (
            "files",
            (f"paper_{index:02d}.ocr-ready-v1.jpg", f"image-{index}".encode(), "image/jpeg"),
        )
        for index in range(23)
    ]
    first = client.post(
        "/api/batch-grade/recognize",
        files=files,
        data={
            "template_id": "resume-window-template",
            "mode": "auto",
            "category": "daily",
            "anonymous": "true",
        },
    )
    assert first.status_code == 200, first.text
    first_events = _events(first)
    errors = [payload for event, payload in first_events if event == "error"]
    assert errors and errors[-1]["code"] == "ACCELERATOR_WAITING", first_events[-3:]
    batch_id = errors[-1]["batch_id"]

    # The browser restores this snapshot after ACCELERATOR_WAITING: waiting rows
    # carry the job's last_error while succeeded rows carry an empty error.
    snapshot = client.get(f"/api/batch-grade/{batch_id}/orientation").json()
    waiting_rows = [row for row in snapshot["papers"] if row["error"]]
    assert len(snapshot["papers"]) == 23
    assert len(waiting_rows) == 8
    assert {row["error"] for row in waiting_rows} == {waiting_message}
    assert {row["ocr_status"] for row in waiting_rows} == {"queued"}
    assert not any(row["requires_manual_review"] for row in waiting_rows)
    assert snapshot["can_grade"] is False

    phase["name"] = "resume"
    resumed = client.post(f"/api/batch-grade/{batch_id}/recognize/resume")
    assert resumed.status_code == 200, resumed.text
    resume_events = _events(resumed)
    assert len(initial_calls) == 23 and len(pending_paths) == 8
    assert sorted(resume_calls) == sorted(pending_paths)
    assert peak == window, f"resume dispatch peaked at {peak}, expected {window}"

    done = [payload for event, payload in resume_events if event == "done"]
    assert len(done) == 1, resume_events[-3:]
    assert done[0]["total_papers"] == 23
    assert done[0]["succeeded_papers"] == 23
    assert done[0]["unresolved_papers"] == 0
    assert done[0]["failed_papers"] == 0
    assert done[0]["can_grade"] is True
    # Every paper is re-emitted before `done`; a successful outcome omits the
    # failure fields entirely, so the client must drop stale snapshot values.
    paper_events = [payload for event, payload in resume_events if event == "paper"]
    assert len({payload["paper_id"] for payload in paper_events}) == 23
    for payload in paper_events:
        assert not {"error", "error_info", "requires_manual_review", "ocr_status"} & set(payload)

    refreshed = client.get(f"/api/batch-grade/{batch_id}/orientation").json()
    assert refreshed["can_grade"] is True
    assert [row["error"] for row in refreshed["papers"]] == [""] * 23


def test_anonymous_template_batch_disables_identity_during_ocr(monkeypatch, tmp_path):
    client, _ = _setup(monkeypatch, tmp_path, education_stage="senior")
    _confirmed_template(tmp_path, "anonymous-template")
    ocr_calls = []

    async def fake_cached(
        _path,
        _batch_id,
        _paper_cache,
        teacher_id="",
        paper_id="",
    ):
        ocr_calls.append(_path)
        return {
            "student_name": "张三",
            "student_no": "01",
            "questions": [{
                "question_number": "1",
                "question_text": "1+1=?",
                "question_type": "fill",
                "student_answer": "2",
                "answer_state": "visible",
            }],
        }

    import routers.grade as grade_router
    monkeypatch.setattr(
        grade_router,
        "recognize_paper_with_cache",
        fake_cached,
    )

    response = client.post(
        "/api/batch-grade/recognize",
        files=[(
            "files",
            ("anonymous.ocr-ready-v1.jpg", b"image", "image/jpeg"),
        )],
        data={
            "template_id": "anonymous-template",
            "mode": "auto",
            "category": "daily",
            "anonymous": "true",
        },
    )

    assert response.status_code == 200
    done = [payload for event, payload in _events(response) if event == "done"]
    assert len(done) == 1
    assert len(ocr_calls) == 1
    batch = QuickBatch.load_meta(done[0]["batch_id"], str(tmp_path))
    assert batch.identity_mode == "anonymous"
    assert batch.class_id is None
    assert batch.papers[0]["student_name"] == "匿名页 1"
    assert batch.papers[0]["student_no"] == ""
    assert batch.papers[0]["identity_ocr_source"] == "disabled"


def test_single_page_template_ocr_failures_become_review_placeholders(monkeypatch, tmp_path):
    """A known one-page layout can preserve failed sheets for manual review."""
    client, _ = _setup(monkeypatch, tmp_path, education_stage="senior")
    import routers.grade as grade_router
    from routers import grade_template
    from services.template_session import TemplateSession

    session = TemplateSession("confirmed-template", str(tmp_path), teacher_id=1)
    session.status = "confirmed"
    session.category = "daily"
    session.add_page(0, [{
        "question_number": "1",
        "question_text": "1+1=?",
        "question_type": "fill",
        "standard_answer": "2",
    }])
    session.confirm_current_contract(1)
    session.save_meta()

    async def failing_ocr(
        _path, _batch_id, paper_cache, teacher_id="", paper_id="",
    ):
        assert paper_cache._baselines
        return {"error": "OCR超时", "questions": []}

    monkeypatch.setattr(grade_router, "recognize_paper_with_cache", failing_ocr)

    async def hardware_ready(_teacher_id, **_contract):
        return {"status": "ready", "message": "加速节点已就绪"}

    monkeypatch.setattr(
        grade_router,
        "get_public_hardware_readiness",
        hardware_ready,
    )
    monkeypatch.setattr(grade_template,
        "get_public_hardware_readiness",
        hardware_ready,
    )
    response = client.post(
        "/api/batch-grade/recognize",
        files=[
            ("files", ("a.ocr-ready-v1.jpg", b"image-a", "image/jpeg")),
            ("files", ("b.ocr-ready-v1.jpg", b"image-b", "image/jpeg")),
        ],
        data={"template_id": "confirmed-template", "mode": "precise", "category": "daily"},
    )

    assert response.status_code == 200
    events = _events(response)
    progress = [payload for event, payload in events if event == "progress"]
    papers = [payload for event, payload in events if event == "paper"]
    errors = [payload for event, payload in events if event == "error"]
    done = [payload for event, payload in events if event == "done"]
    assert [payload["current"] for payload in progress][-2:] == [1, 2]
    assert len(papers) == 2
    assert all(payload["error"] == "OCR超时" for payload in papers)
    assert all(payload["requires_manual_review"] is True for payload in papers)
    assert all(payload["batch_id"] for payload in papers)
    assert all(payload["paper_id"] for payload in papers)
    image_response = client.get(
        f"/api/batch-grade/{papers[0]['batch_id']}/paper/{papers[0]['paper_id']}/image"
    )
    assert image_response.status_code == 200
    assert image_response.content == b"image-a"
    assert errors == []
    assert len(done) == 1
    done_payload = done[0]
    assert done_payload["batch_id"] == papers[0]["batch_id"]
    assert done_payload["mode"] == "precise"
    assert done_payload["total_papers"] == 2
    assert done_payload["uploaded_papers"] == 2
    assert done_payload["succeeded_papers"] == 0
    assert done_payload["manual_review_papers"] == 2
    assert done_payload["failed_papers"] == 0
    assert done_payload["unresolved_papers"] == 0
    assert done_payload["can_grade"] is True
    assert len(done_payload["questions"]) == 1
    assert done_payload["questions"][0]["question_no"] == "1"
    assert done_payload["questions"][0]["content"] == "1+1=?"
    assert done_payload["questions"][0]["question_type"] == "fill"

    batch = QuickBatch.load_meta(papers[0]["batch_id"], str(tmp_path))
    assert batch.status == "ocr_done"
    assert [job["status"] for job in batch.ocr_jobs] == ["needs_review", "needs_review"]
    assert all(paper["ocr_failure"] is True for paper in batch.papers)
    assert all(paper["requires_manual_review"] is True for paper in batch.papers)
    assert [paper["questions"] for paper in batch.papers] == [
        [{
            "question_no": "1",
            "student_answer": "[无法识别]",
            "question_type": "fill",
            "slot_identity_error": "ocr_failed",
        }],
        [{
            "question_no": "1",
            "student_answer": "[无法识别]",
            "question_type": "fill",
            "slot_identity_error": "ocr_failed",
        }],
    ]


def test_batch_ocr_summary_counts_mixed_terminal_and_unresolved_jobs(tmp_path):
    batch = QuickBatch("mixed-ocr-summary", str(tmp_path))
    batch.uploads = [
        {"paper_id": f"paper-{index}", "image_path": f"paper-{index}.png"}
        for index in range(1, 6)
    ]
    batch.ocr_jobs = [
        {"image_path": "paper-1.png", "status": "succeeded"},
        {"image_path": "paper-2.png", "status": "succeeded"},
        {"image_path": "paper-3.png", "status": "needs_review"},
        {"image_path": "paper-4.png", "status": "failed"},
        {"image_path": "paper-5.png", "status": "queued"},
    ]
    batch.papers = [{"paper_id": f"paper-{index}"} for index in range(1, 4)]

    assert batch_ocr_summary(batch) == {
        "uploaded_papers": 5,
        "succeeded_papers": 2,
        "manual_review_papers": 1,
        "failed_papers": 1,
        "excluded_papers": 0,
        "retryable_failed_papers": 0,
        "unresolved_papers": 1,
        "can_grade": False,
    }


def test_batch_ocr_summary_grades_the_rest_after_a_teacher_exclusion(tmp_path):
    batch = QuickBatch("excluded-ocr-summary", str(tmp_path))
    batch.uploads = [
        {"paper_id": f"paper-{index}", "image_path": f"paper-{index}.png"}
        for index in range(1, 4)
    ]
    batch.ocr_jobs = [
        {"image_path": "paper-1.png", "status": "succeeded"},
        {"image_path": "paper-2.png", "status": "needs_review"},
        {"image_path": "paper-3.png", "status": "excluded"},
    ]
    batch.papers = [{"paper_id": "paper-1"}, {"paper_id": "paper-2"}]

    summary = batch_ocr_summary(batch)
    assert summary["excluded_papers"] == 1
    assert summary["unresolved_papers"] == 0
    assert summary["can_grade"] is True

    # Excluding every page leaves nothing to grade.
    batch.ocr_jobs = [{"image_path": f"paper-{index}.png", "status": "excluded"} for index in range(1, 4)]
    batch.papers = []
    assert batch_ocr_summary(batch)["can_grade"] is False


def test_ingest_preserves_unexpected_page_type_for_grading(monkeypatch, tmp_path):
    _client, Session = _setup(monkeypatch, tmp_path)
    import routers.grade as grade_router

    batch = QuickBatch("wrong-page", str(tmp_path))
    batch.teacher_id = 1
    batch.class_id = 1
    batch.uploads = [{
        "paper_id": "foreign-paper",
        "image_path": "foreign.png",
        "orientation_status": "auto_passed",
        "orientation_reason": "normal_angle",
        "orientation_confidence": 1.0,
        "detected_tilt_degrees": 0.0,
        "exif_orientation": 1,
        "orientation_coordinate_space": "exif_display_pixels",
    }]
    db = Session()
    try:
        grade_router._ingest_recognized_paper(
            batch, db, 1, "foreign.png", "foreign.png", {
                "student_name": "张三",
                "student_no": "01",
                "_unexpected_page_type": True,
                "page_type_error": "未匹配本批次已确认页型",
                "_answers_only": True,
                "_cache_type_label": "page_1",
                "_local_template_evidence": True,
                "_full_page_transcription": True,
                "questions": [{
                    "question_number": "9",
                    "question_text": "外来试卷题目",
                    "question_type": "solve",
                    "student_answer": "3",
                    "answer_state": "visible",
                }],
            },
        )
    finally:
        db.close()

    paper = batch.papers[0]
    assert paper["unexpected_page_type"] is True
    assert paper["page_type_error"] == "未匹配本批次已确认页型"
    assert paper["ocr_answers_only"] is True
    assert paper["ocr_page_type"] == "page_1"
    assert paper["ocr_local_template_evidence"] is True
    assert paper["ocr_full_page_transcription"] is True
    assert "visual_grading" not in paper
    assert paper["questions"][0]["answer_state"] == "visible"
    assert paper["orientation_status"] == "auto_passed"


def test_missing_ingest_orientation_fails_closed_without_image_io():
    import routers.grade as grade_router
    orientation = grade_router._stored_paper_orientation({}, {})
    assert orientation["orientation_status"] == "needs_review"
    assert orientation["orientation_reason"] == "orientation_metadata_missing"
