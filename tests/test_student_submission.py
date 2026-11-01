"""Student self-upload through class links (services/student_submission.py)."""

from datetime import datetime, timedelta
from io import BytesIO
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401
import models_rate_limit  # noqa: F401
import models_submission  # noqa: F401
import models_user  # noqa: F401
from database import Base, get_db
from models import Student
from models_rate_limit import TeacherClass
from models_submission import StudentSubmissionLink, StudentSubmissionPage
from models_user import User
from services import student_submission as submission

T1, T2 = 1, 2
IDENTITY = {"name": "张三", "student_no": "2024001"}


def _photo(shade: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (300, 400), (shade, shade, shade)).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    db.add_all([User(id=T1, name="师一", role="teacher"), User(id=T2, name="师二", role="teacher")])
    db.flush()
    db.add_all([
        TeacherClass(id=1, teacher_id=T1, class_name="高一1班"),
        TeacherClass(id=2, teacher_id=T2, class_name="别人的班"),
    ])
    db.flush()
    db.add_all([
        Student(id=10, teacher_id=T1, class_id=1, name="张三", student_no="2024001"),
        Student(id=11, teacher_id=T1, class_id=1, name="李四", student_no="2024002"),
        Student(id=12, teacher_id=T1, class_id=1, name="王五", student_no=None),
        Student(id=20, teacher_id=T2, class_id=2, name="张三", student_no="2024001"),
    ])
    db.commit()
    db.close()

    def override_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    from routers import student_submission
    from routers.auth import _require_user

    app = FastAPI()
    app.include_router(student_submission.router, prefix="/api")
    app.dependency_overrides[get_db] = override_db

    def current_teacher(request: Request):
        return User(id=int(request.headers["X-Test-Teacher"]), name="t", role="teacher")

    app.dependency_overrides[_require_user] = current_teacher

    def teacher(teacher_id):
        return TestClient(app, headers={"X-Test-Teacher": str(teacher_id)})

    def public():
        return TestClient(app)

    yield teacher, public, Session


def _create_link(client, class_id=1, hours=24):
    closes_at = (datetime.now() + timedelta(hours=hours)).astimezone().isoformat()
    response = client.post("/api/submission-links", json={"class_id": class_id, "title": "周测三", "closes_at": closes_at})
    assert response.status_code == 200, response.text
    return response.json()


def _upload(client, token, page_no, data, **identity):
    fields = {**IDENTITY, **identity, "page_no": str(page_no)}
    return client.post(
        f"/api/submit/{token}/pages",
        data=fields,
        files={"file": ("page.jpg", data, "image/jpeg")},
    )


def test_link_is_scoped_to_its_teacher(env):
    teacher, public, _ = env
    link = _create_link(teacher(T1))

    assert teacher(T1).get("/api/submission-links", params={"class_id": 1}).json()["links"][0]["id"] == link["id"]
    assert teacher(T2).get("/api/submission-links", params={"class_id": 1}).status_code == 404
    assert teacher(T2).get(f"/api/submission-links/{link['id']}/submissions").status_code == 404
    assert teacher(T2).delete(f"/api/submission-links/{link['id']}").status_code == 404
    assert teacher(T2).post("/api/submission-links", json={
        "class_id": 1, "title": "", "closes_at": (datetime.now() + timedelta(hours=1)).isoformat(),
    }).status_code == 404

    info = public().get(f"/api/submit/{link['token']}").json()
    assert info == {
        "title": "周测三", "class_name": "高一1班", "closes_at": link["closes_at"],
        "open": True, "max_pages": submission.MAX_PAGES,
    }
    assert public().get("/api/submit/not-a-token").status_code == 404


def test_deadline_must_be_in_the_future_and_bounded(env):
    teacher, _, _ = env
    client = teacher(T1)
    past = (datetime.now() - timedelta(minutes=1)).isoformat()
    far = (datetime.now() + timedelta(days=submission.MAX_OPEN_DAYS + 1)).isoformat()
    assert client.post("/api/submission-links", json={"class_id": 1, "closes_at": past}).status_code == 400
    assert client.post("/api/submission-links", json={"class_id": 1, "closes_at": far}).status_code == 400


def test_identity_needs_matching_name_and_number_with_one_answer_for_all_failures(env):
    teacher, public, _ = env
    token = _create_link(teacher(T1))["token"]
    client = public()

    ok = client.post(f"/api/submit/{token}/status", json={"name": " 张 三 ", "student_no": "２０２４００１"})
    assert ok.status_code == 200
    assert ok.json()["student_name"] == "张三" and ok.json()["pages"] == []

    refusals = [
        {"name": "张三", "student_no": "2024002"},   # someone else's number
        {"name": "赵六", "student_no": "2024001"},   # not on the roster
        {"name": "王五", "student_no": ""},          # roster has no number
        {"name": "张三", "student_no": "2024003"},
    ]
    details = set()
    for identity in refusals:
        response = client.post(f"/api/submit/{token}/status", json=identity)
        assert response.status_code == 403
        details.add(response.json()["detail"])
    assert len(details) == 1


def test_resubmitting_replaces_pages_instead_of_adding_duplicates(env, tmp_path):
    teacher, public, Session = env
    token = _create_link(teacher(T1))["token"]
    client = public()

    assert _upload(client, token, 1, _photo(10)).status_code == 200
    assert _upload(client, token, 2, _photo(20)).status_code == 200
    db = Session()
    first_file = submission._page_path(db.query(StudentSubmissionPage).filter_by(page_no=1).one())
    db.close()
    assert os.path.isfile(first_file)

    replaced = _upload(client, token, 1, _photo(30))
    assert replaced.status_code == 200
    assert [page["page_no"] for page in replaced.json()["pages"]] == [1, 2]
    assert not os.path.exists(first_file)

    # Same photo again for the same page is a harmless retry ...
    assert _upload(client, token, 1, _photo(30)).status_code == 200
    # ... but the same photo as a different page is refused.
    duplicate = _upload(client, token, 3, _photo(20))
    assert duplicate.status_code == 409
    assert "第 2 页" in duplicate.json()["detail"]

    again = client.post(f"/api/submit/{token}/status", json=IDENTITY).json()
    assert [page["page_no"] for page in again["pages"]] == [1, 2]

    db = Session()
    assert db.query(StudentSubmissionPage).count() == 2
    db.close()


def test_page_numbers_are_bounded_and_images_validated(env):
    teacher, public, _ = env
    token = _create_link(teacher(T1))["token"]
    client = public()
    assert _upload(client, token, 0, _photo(10)).status_code == 400
    assert _upload(client, token, submission.MAX_PAGES + 1, _photo(10)).status_code == 400
    assert _upload(client, token, 1, b"not an image").status_code == 400


def test_pages_can_be_reordered_and_deleted(env):
    teacher, public, _ = env
    token = _create_link(teacher(T1))["token"]
    client = public()
    _upload(client, token, 1, _photo(10))
    _upload(client, token, 2, _photo(20))

    swapped = client.post(f"/api/submit/{token}/pages/1/move", json={**IDENTITY, "to_page_no": 2}).json()
    assert [page["page_no"] for page in swapped["pages"]] == [1, 2]
    moved = client.post(f"/api/submit/{token}/pages/2/move", json={**IDENTITY, "to_page_no": 5}).json()
    assert [page["page_no"] for page in moved["pages"]] == [1, 5]
    assert client.post(f"/api/submit/{token}/pages/3/move", json={**IDENTITY, "to_page_no": 1}).status_code == 409

    remaining = client.post(f"/api/submit/{token}/pages/5/delete", json=IDENTITY).json()
    assert [page["page_no"] for page in remaining["pages"]] == [1]
    # Deleting a page that is not there just reports the current state.
    assert client.post(f"/api/submit/{token}/pages/5/delete", json=IDENTITY).status_code == 200


def test_closed_link_is_read_only(env):
    teacher, public, _ = env
    teacher_client = teacher(T1)
    link = _create_link(teacher_client)
    client = public()
    _upload(client, link["token"], 1, _photo(10))

    closed = teacher_client.put(f"/api/submission-links/{link['id']}", json={
        "closes_at": (datetime.now() - timedelta(days=3)).isoformat(),
    }).json()
    assert closed["open"] is False
    # Closing early never shortens the retention period.
    assert datetime.fromisoformat(closed["closes_at"]) > datetime.now().astimezone() - timedelta(minutes=1)

    status = client.post(f"/api/submit/{link['token']}/status", json=IDENTITY)
    assert status.status_code == 200 and status.json()["open"] is False
    assert _upload(client, link["token"], 2, _photo(20)).status_code == 409
    assert client.post(f"/api/submit/{link['token']}/pages/1/delete", json=IDENTITY).status_code == 409

    reopened = teacher_client.put(f"/api/submission-links/{link['id']}", json={
        "closes_at": (datetime.now() + timedelta(hours=2)).isoformat(),
    }).json()
    assert reopened["open"] is True
    assert _upload(client, link["token"], 2, _photo(20)).status_code == 200


def test_optional_password_only_guards_students_who_have_one(env):
    teacher, public, _ = env
    teacher_client = teacher(T1)
    token = _create_link(teacher_client)["token"]
    client = public()

    # No password set: anything typed in the optional field is ignored.
    assert client.post(f"/api/submit/{token}/status", json={**IDENTITY, "password": "whatever"}).status_code == 200

    url = "/api/class/1/students/10/submit-password"
    assert teacher_client.get(url).json() == {"set": False}
    assert teacher_client.put(url, json={"password": "12"}).status_code == 400
    assert teacher_client.put(url, json={"password": "8264"}).json() == {"set": True}
    assert teacher(T2).put(url, json={"password": "0000"}).status_code == 404

    blank = client.post(f"/api/submit/{token}/status", json=IDENTITY)
    wrong = client.post(f"/api/submit/{token}/status", json={**IDENTITY, "password": "0000"})
    unknown = client.post(f"/api/submit/{token}/status", json={"name": "赵六", "student_no": "1"})
    assert blank.status_code == wrong.status_code == 403
    assert blank.json()["detail"] == unknown.json()["detail"]
    assert client.post(f"/api/submit/{token}/status", json={**IDENTITY, "password": " 8264 "}).status_code == 200
    assert _upload(client, token, 1, _photo(10)).status_code == 403
    assert _upload(client, token, 1, _photo(10), password="8264").status_code == 200

    # Another student without a password is unaffected.
    assert client.post(f"/api/submit/{token}/status", json={"name": "李四", "student_no": "2024002"}).status_code == 200

    for _ in range(submission.PASSWORD_MAX_FAILURES):
        client.post(f"/api/submit/{token}/status", json={**IDENTITY, "password": "0000"})
    assert client.post(f"/api/submit/{token}/status", json={**IDENTITY, "password": "8264"}).status_code == 429

    # Setting the password again (or clearing it) lifts the lock.
    assert teacher_client.delete(url).json() == {"set": False}
    assert client.post(f"/api/submit/{token}/status", json=IDENTITY).status_code == 200


def test_failed_identity_checks_are_throttled_per_address(env):
    teacher, public, _ = env
    token = _create_link(teacher(T1))["token"]
    client = public()
    for index in range(submission.ADDRESS_MAX_FAILURES):
        assert client.post(f"/api/submit/{token}/status", json={"name": "赵六", "student_no": str(index)}).status_code == 403
    assert client.post(f"/api/submit/{token}/status", json=IDENTITY).status_code == 429


def test_teacher_sees_roster_status_shared_photos_and_can_reset(env):
    teacher, public, Session = env
    teacher_client = teacher(T1)
    link = _create_link(teacher_client)
    client = public()
    same = _photo(40)
    _upload(client, link["token"], 1, same)
    _upload(client, link["token"], 2, _photo(50))
    _upload(client, link["token"], 1, same, name="李四", student_no="2024002")

    view = teacher_client.get(f"/api/submission-links/{link['id']}/submissions").json()
    assert view["link"]["submitted_students"] == 2 and view["link"]["page_count"] == 3
    assert view["link"]["roster_size"] == 3
    assert view["missing_student_no"] == 1
    rows = {row["name"]: row for row in view["students"]}
    assert [page["page_no"] for page in rows["张三"]["pages"]] == [1, 2]
    assert rows["张三"]["pages"][0]["same_photo_as"] == ["李四"]
    assert rows["张三"]["pages"][1]["same_photo_as"] == []
    assert rows["王五"]["pages"] == [] and rows["王五"]["can_submit"] is False

    page_id = rows["张三"]["pages"][1]["id"]
    image = teacher_client.get(f"/api/submission-links/{link['id']}/pages/{page_id}/image")
    assert image.status_code == 200 and image.content == _photo(50)
    assert teacher(T2).get(f"/api/submission-links/{link['id']}/pages/{page_id}/image").status_code == 404

    assert teacher_client.delete(f"/api/submission-links/{link['id']}/students/10").status_code == 200
    rows = {row["name"]: row for row in teacher_client.get(f"/api/submission-links/{link['id']}/submissions").json()["students"]}
    assert rows["张三"]["pages"] == []


def test_students_moved_out_cannot_submit_but_their_pages_stay_visible(env):
    teacher, public, Session = env
    teacher_client = teacher(T1)
    link = _create_link(teacher_client)
    client = public()
    _upload(client, link["token"], 1, _photo(10))

    db = Session()
    db.get(Student, 10).class_id = None
    db.commit()
    db.close()

    assert client.post(f"/api/submit/{link['token']}/status", json=IDENTITY).status_code == 403
    rows = teacher_client.get(f"/api/submission-links/{link['id']}/submissions").json()["students"]
    departed = next(row for row in rows if row["student_id"] == 10)
    assert departed["in_class"] is False and len(departed["pages"]) == 1


def test_deleting_a_student_removes_their_pages(env):
    teacher, public, Session = env
    link = _create_link(teacher(T1))
    _upload(public(), link["token"], 1, _photo(10))
    db = Session()
    db.delete(db.get(Student, 10))
    db.commit()
    assert db.query(StudentSubmissionPage).count() == 0
    db.close()


def test_deleting_or_expiring_a_link_removes_its_photos(env):
    teacher, public, Session = env
    teacher_client = teacher(T1)
    kept = _create_link(teacher_client)
    expired = _create_link(teacher_client)
    client = public()
    _upload(client, kept["token"], 1, _photo(10))
    _upload(client, expired["token"], 1, _photo(10))
    kept_dir = submission._link_dir(kept["id"])
    expired_dir = submission._link_dir(expired["id"])
    assert os.path.isdir(kept_dir) and os.path.isdir(expired_dir)

    db = Session()
    db.get(StudentSubmissionLink, expired["id"]).closes_at = datetime.now() - timedelta(days=submission.RETENTION_DAYS, minutes=1)
    db.commit()
    db.close()
    assert public().get(f"/api/submit/{expired['token']}").status_code == 404
    links = teacher_client.get("/api/submission-links", params={"class_id": 1}).json()["links"]
    assert [link["id"] for link in links] == [kept["id"]]
    assert not os.path.exists(expired_dir)

    assert teacher_client.delete(f"/api/submission-links/{kept['id']}").status_code == 200
    assert not os.path.exists(kept_dir)
    assert public().get(f"/api/submit/{kept['token']}").status_code == 404
    db = Session()
    assert db.query(StudentSubmissionPage).count() == 0
    db.close()


def test_deleted_class_disables_its_link(env):
    teacher, public, Session = env
    link = _create_link(teacher(T1))
    db = Session()
    db.get(TeacherClass, 1).deleted_at = datetime.now()
    db.commit()
    db.close()
    assert public().get(f"/api/submit/{link['token']}").status_code == 404


def test_import_folder_names_feed_the_identity_gate():
    """Paths built by frontend/src/utils/studentSubmission.js::submissionSourcePath."""
    from services.collection_identity import resolve_collection_identity

    roster = [
        {"student_id": 10, "name": "张三", "student_no": "08", "class_id": 1},
        {"student_id": 11, "name": "张三", "student_no": "09", "class_id": 1},
        {"student_id": 12, "name": "李四", "student_no": "2024002", "class_id": 1},
    ]

    def resolve(path, ocr_name=""):
        return resolve_collection_identity(
            roster, ocr_name=ocr_name, filename=path.rsplit("/", 1)[-1], source_relative_path=path,
        )

    # A blank or illegible header: the typed identity decides.
    assert resolve("学生提交/李四_学号2024002/第1页.jpg")["student_id"] == 12
    # Same-name classmates are told apart by the labelled number.
    assert resolve("学生提交/张三_学号09/第2页.jpg")["student_id"] == 11
    # The paper agrees with the typed identity.
    assert resolve("学生提交/李四_学号2024002/第1页.jpg", ocr_name="李四")["status"] == "matched"
    # The paper names someone else: the teacher decides.
    conflict = resolve("学生提交/李四_学号2024002/第1页.jpg", ocr_name="张三")
    assert conflict["status"] == "needs_confirmation"
    assert conflict["reason"] == "identity_name_conflict"
