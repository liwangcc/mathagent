"""Student self-upload through a class link, without student accounts.

A teacher creates a link for one class with a deadline.  A student opens it,
types name + student number (both must match the same student on that class's
roster) and, only if the teacher set one in class management, a password.
Until the deadline the same identity can add, replace, reorder or delete the
student's pages; the student endpoints never return an image.

Duplicates are prevented by construction: a page is keyed by
(link, student, page number), so re-submitting replaces instead of adding, and
the same photo cannot be stored twice for one student.  The teacher sees who
has submitted, who has not, and identical photos under different students.

The typed identity is a clue, not proof.  Imported pages carry it as the
folder name, and the grading identity gate still compares it with the name
written on the paper (services/collection_identity.py); a conflict goes to the
teacher.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from models import Student
from models_rate_limit import TeacherClass
from models_submission import (
    StudentSubmissionLink,
    StudentSubmissionPage,
    StudentSubmissionThrottle,
    StudentSubmitPassword,
)
from services.auth_service import hash_password, verify_password
from services.capture_session import (
    IMAGE_EXTENSIONS,
    IMAGE_MEDIA_TYPES,
    CaptureInvalidImage,
    inspect_image,
)

logger = logging.getLogger(__name__)

MAX_PAGES = 20
MAX_OPEN_DAYS = 60
# Photos are deleted this long after the deadline; the link stops working too.
RETENTION_DAYS = 30
PASSWORD_MIN_LENGTH = 4
PASSWORD_MAX_LENGTH = 32
# A whole class often shares one school address, so the per-address limit is
# loose; it only stops someone working through the roster.
ADDRESS_WINDOW = timedelta(minutes=10)
ADDRESS_MAX_FAILURES = 30
PASSWORD_MAX_FAILURES = 5
PASSWORD_LOCK = timedelta(minutes=10)

_MSG_LINK_GONE = "链接无效或已失效，请向老师索取新链接"
_MSG_CLASS_GONE = "班级不存在"
_MSG_LINK_NOT_FOUND = "链接不存在"
_MSG_STUDENT_NOT_FOUND = "学生不存在"
# One answer for an unknown name, an unknown number, a mismatched pair and a
# wrong password, so the page cannot be used to test who is on the roster.
_MSG_IDENTITY = "姓名或学号与班级名单不一致，请核对；如果老师给过你提交密码，也请填写"
_MSG_TOO_MANY = "尝试次数过多，请 10 分钟后再试"
_MSG_CLOSED = "已过截止时间，不能再修改"


class SubmissionError(Exception):
    """Base class; ``message`` is safe to show to the person who asked."""

    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SubmissionInvalid(SubmissionError):
    status = 400


class SubmissionForbidden(SubmissionError):
    status = 403


class SubmissionNotFound(SubmissionError):
    status = 404


class SubmissionConflict(SubmissionError):
    status = 409


class SubmissionThrottled(SubmissionError):
    status = 429


# ── Shared helpers ──


def normalize_name(value: str | None) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split())


def normalize_student_no(value: str | None) -> str:
    return normalize_name(value).casefold()


def _iso(value: datetime | None) -> str | None:
    # Stored times are naive server-local, like the rest of the schema.
    return value.astimezone().isoformat() if value else None


def _local(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _root() -> str:
    return os.path.abspath(os.path.join(config.UPLOAD_DIR, "student_submissions"))


def _link_dir(link_id: int) -> str:
    return os.path.join(_root(), str(int(link_id)))


def _page_path(page: StudentSubmissionPage) -> str:
    return os.path.join(_link_dir(page.link_id), str(int(page.student_id)), os.path.basename(page.file))


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _class_students(db: Session, teacher_id: int, class_id: int) -> list[Student]:
    return db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.class_id == class_id,
    ).order_by(Student.student_no.asc(), Student.id.asc()).all()


def _is_open(link: StudentSubmissionLink, now: datetime) -> bool:
    return now < link.closes_at


def _is_purged(link: StudentSubmissionLink, now: datetime) -> bool:
    return now >= link.closes_at + timedelta(days=RETENTION_DAYS)


def _page_view(page: StudentSubmissionPage) -> dict:
    return {
        "page_no": int(page.page_no),
        "bytes": int(page.bytes or 0),
        "width": int(page.width or 0),
        "height": int(page.height or 0),
        "uploaded_at": _iso(page.uploaded_at),
    }


def _delete_link(db: Session, link: StudentSubmissionLink) -> None:
    link_id = link.id
    db.query(StudentSubmissionPage).filter(StudentSubmissionPage.link_id == link_id).delete()
    db.delete(link)
    db.commit()
    shutil.rmtree(_link_dir(link_id), ignore_errors=True)


# ── Teacher: links ──


def _owned_class(db: Session, teacher_id: int, class_id: int) -> TeacherClass:
    owned = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == teacher_id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if owned is None:
        raise SubmissionNotFound(_MSG_CLASS_GONE)
    return owned


def _owned_link(db: Session, teacher_id: int, link_id: int) -> StudentSubmissionLink:
    link = db.query(StudentSubmissionLink).filter(
        StudentSubmissionLink.id == link_id,
        StudentSubmissionLink.teacher_id == teacher_id,
    ).first()
    if link is None:
        raise SubmissionNotFound(_MSG_LINK_NOT_FOUND)
    return link


def _checked_deadline(closes_at: datetime, now: datetime) -> datetime:
    closes_at = _local(closes_at)
    if closes_at > now + timedelta(days=MAX_OPEN_DAYS):
        raise SubmissionInvalid(f"截止时间最多设在 {MAX_OPEN_DAYS} 天后")
    return closes_at


def _link_views(db: Session, links: list[StudentSubmissionLink], roster_size: int, now: datetime) -> list[dict]:
    counts = {
        link_id: (students, pages)
        for link_id, students, pages in db.query(
            StudentSubmissionPage.link_id,
            func.count(func.distinct(StudentSubmissionPage.student_id)),
            func.count(StudentSubmissionPage.id),
        ).filter(
            StudentSubmissionPage.link_id.in_([link.id for link in links]),
        ).group_by(StudentSubmissionPage.link_id)
    } if links else {}
    views = []
    for link in links:
        students, pages = counts.get(link.id, (0, 0))
        views.append({
            "id": link.id,
            "class_id": link.class_id,
            "token": link.token,
            "title": link.title or "",
            "closes_at": _iso(link.closes_at),
            "created_at": _iso(link.created_at),
            "delete_after": _iso(link.closes_at + timedelta(days=RETENTION_DAYS)),
            "open": _is_open(link, now),
            "submitted_students": int(students),
            "page_count": int(pages),
            "roster_size": roster_size,
        })
    return views


def _link_view(db: Session, link: StudentSubmissionLink, now: datetime) -> dict:
    roster_size = len(_class_students(db, link.teacher_id, link.class_id))
    return _link_views(db, [link], roster_size, now)[0]


def purge_expired_links(db: Session, teacher_id: int, now: datetime | None = None) -> int:
    """Delete the teacher's links (and photos) past the retention period."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    expired = db.query(StudentSubmissionLink).filter(
        StudentSubmissionLink.teacher_id == teacher_id,
        StudentSubmissionLink.closes_at <= cutoff,
    ).all()
    for link in expired:
        _delete_link(db, link)
    return len(expired)


def list_links(db: Session, teacher_id: int, class_id: int, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    _owned_class(db, teacher_id, class_id)
    purge_expired_links(db, teacher_id, now)
    links = db.query(StudentSubmissionLink).filter(
        StudentSubmissionLink.teacher_id == teacher_id,
        StudentSubmissionLink.class_id == class_id,
    ).order_by(StudentSubmissionLink.created_at.desc(), StudentSubmissionLink.id.desc()).all()
    roster_size = len(_class_students(db, teacher_id, class_id))
    return _link_views(db, links, roster_size, now)


def create_link(
    db: Session, teacher_id: int, class_id: int, title: str, closes_at: datetime,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    _owned_class(db, teacher_id, class_id)
    closes_at = _checked_deadline(closes_at, now)
    if closes_at <= now:
        raise SubmissionInvalid("截止时间必须晚于现在")
    link = StudentSubmissionLink(
        token=secrets.token_urlsafe(16),
        teacher_id=teacher_id,
        class_id=class_id,
        title=str(title or "").strip()[:100],
        closes_at=closes_at,
        created_at=now,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _link_view(db, link, now)


def update_link(
    db: Session, teacher_id: int, link_id: int, *,
    title: str | None = None, closes_at: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    """Rename, extend, or close a link (a deadline in the past closes it now)."""
    now = now or datetime.now()
    link = _owned_link(db, teacher_id, link_id)
    if title is not None:
        link.title = str(title).strip()[:100]
    if closes_at is not None:
        # Clamped so that closing a link never starts its retention clock early.
        link.closes_at = max(_checked_deadline(closes_at, now), now)
    db.commit()
    return _link_view(db, link, now)


def delete_link(db: Session, teacher_id: int, link_id: int) -> None:
    _delete_link(db, _owned_link(db, teacher_id, link_id))


def link_submissions(db: Session, teacher_id: int, link_id: int, now: datetime | None = None) -> dict:
    """Who submitted which pages, who is missing, and repeated photos."""
    now = now or datetime.now()
    link = _owned_link(db, teacher_id, link_id)
    roster = _class_students(db, teacher_id, link.class_id)
    pages = db.query(StudentSubmissionPage).filter(
        StudentSubmissionPage.link_id == link.id,
    ).order_by(StudentSubmissionPage.student_id, StudentSubmissionPage.page_no).all()
    pages_by_student: dict[int, list[StudentSubmissionPage]] = defaultdict(list)
    students_by_photo: dict[str, set[int]] = defaultdict(set)
    for page in pages:
        pages_by_student[page.student_id].append(page)
        students_by_photo[page.sha256].add(page.student_id)

    # Students who submitted and were later moved out of the class stay visible.
    departed_ids = set(pages_by_student) - {student.id for student in roster}
    departed = db.query(Student).filter(
        Student.id.in_(departed_ids),
    ).order_by(Student.student_no.asc(), Student.id.asc()).all() if departed_ids else []
    names = {student.id: student.name for student in [*roster, *departed]}
    protected = {
        student_id for (student_id,) in db.query(StudentSubmitPassword.student_id).filter(
            StudentSubmitPassword.student_id.in_(list(names)),
        )
    } if names else set()

    def row(student: Student, in_class: bool) -> dict:
        own = pages_by_student.get(student.id, [])
        return {
            "student_id": student.id,
            "name": student.name,
            "student_no": student.student_no or "",
            "in_class": in_class,
            "can_submit": in_class and bool(normalize_student_no(student.student_no)),
            "has_password": student.id in protected,
            "last_uploaded_at": _iso(max((page.uploaded_at for page in own), default=None)),
            "pages": [
                {
                    **_page_view(page),
                    "id": page.id,
                    "same_photo_as": sorted(
                        names.get(other, "") for other in students_by_photo[page.sha256] if other != student.id
                    ),
                }
                for page in own
            ],
        }

    rows = [row(student, True) for student in roster] + [row(student, False) for student in departed]
    return {
        "link": _link_views(db, [link], len(roster), now)[0],
        "students": rows,
        "missing_student_no": sum(1 for student in roster if not normalize_student_no(student.student_no)),
    }


def teacher_page_file(db: Session, teacher_id: int, link_id: int, page_id: int) -> tuple[str, str]:
    """Return ``(path, media_type)`` of one page of the teacher's link."""
    link = _owned_link(db, teacher_id, link_id)
    page = db.query(StudentSubmissionPage).filter(
        StudentSubmissionPage.id == page_id,
        StudentSubmissionPage.link_id == link.id,
    ).first()
    if page is not None:
        path = _page_path(page)
        if os.path.isfile(path):
            return path, IMAGE_MEDIA_TYPES.get(os.path.splitext(path)[1], "image/jpeg")
    raise SubmissionNotFound("该页不存在或已删除")


def clear_student_pages(db: Session, teacher_id: int, link_id: int, student_id: int) -> None:
    """Teacher reset: remove every page one student uploaded to the link."""
    link = _owned_link(db, teacher_id, link_id)
    pages = db.query(StudentSubmissionPage).filter(
        StudentSubmissionPage.link_id == link.id,
        StudentSubmissionPage.student_id == student_id,
    ).all()
    paths = [_page_path(page) for page in pages]
    for page in pages:
        db.delete(page)
    db.commit()
    for path in paths:
        _remove_file(path)


# ── Teacher: optional per-student password ──


def _owned_class_student(db: Session, teacher_id: int, class_id: int, student_id: int) -> Student:
    _owned_class(db, teacher_id, class_id)
    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == teacher_id,
        Student.class_id == class_id,
    ).first()
    if student is None:
        raise SubmissionNotFound(_MSG_STUDENT_NOT_FOUND)
    return student


def password_state(db: Session, teacher_id: int, class_id: int, student_id: int) -> dict:
    student = _owned_class_student(db, teacher_id, class_id, student_id)
    return {"set": db.get(StudentSubmitPassword, student.id) is not None}


def set_password(db: Session, teacher_id: int, class_id: int, student_id: int, password: str) -> dict:
    student = _owned_class_student(db, teacher_id, class_id, student_id)
    password = str(password or "").strip()
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise SubmissionInvalid(f"提交密码需 {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} 个字符")
    if len(password.encode("utf-8")) > 72:  # bcrypt's input limit
        raise SubmissionInvalid("提交密码过长，请改短一些")
    secret = db.get(StudentSubmitPassword, student.id)
    if secret is None:
        secret = StudentSubmitPassword(student_id=student.id)
        db.add(secret)
    secret.password_hash = hash_password(password)
    secret.fail_count = 0
    secret.locked_until = None
    secret.updated_at = datetime.now()
    db.commit()
    return {"set": True}


def clear_password(db: Session, teacher_id: int, class_id: int, student_id: int) -> dict:
    student = _owned_class_student(db, teacher_id, class_id, student_id)
    secret = db.get(StudentSubmitPassword, student.id)
    if secret is not None:
        db.delete(secret)
        db.commit()
    return {"set": False}


# ── Students ──


def _live_link(db: Session, token: str, now: datetime) -> StudentSubmissionLink:
    link = db.query(StudentSubmissionLink).filter(
        StudentSubmissionLink.token == str(token or ""),
    ).first() if token else None
    if link is None or _is_purged(link, now):
        raise SubmissionNotFound(_MSG_LINK_GONE)
    owned_class = db.get(TeacherClass, link.class_id)
    if owned_class is None or owned_class.deleted_at is not None:
        raise SubmissionNotFound(_MSG_LINK_GONE)
    return link


def public_link(db: Session, token: str, now: datetime | None = None) -> dict:
    """What a student sees before identifying: no roster or teacher data."""
    now = now or datetime.now()
    link = _live_link(db, token, now)
    return {
        "title": link.title or "",
        "class_name": db.get(TeacherClass, link.class_id).class_name,
        "closes_at": _iso(link.closes_at),
        "open": _is_open(link, now),
        "max_pages": MAX_PAGES,
    }


def _check_address(db: Session, key: str, now: datetime) -> None:
    row = db.get(StudentSubmissionThrottle, key)
    if row and now - row.window_started_at < ADDRESS_WINDOW and row.failures >= ADDRESS_MAX_FAILURES:
        raise SubmissionThrottled(_MSG_TOO_MANY)


def _record_address_failure(db: Session, key: str, now: datetime) -> None:
    for _ in range(2):  # a concurrent first failure may insert the row first
        row = db.get(StudentSubmissionThrottle, key)
        if row is None:
            row = StudentSubmissionThrottle(key=key, window_started_at=now, failures=0)
            db.add(row)
        elif now - row.window_started_at >= ADDRESS_WINDOW:
            row.window_started_at = now
            row.failures = 0
        row.failures += 1
        db.query(StudentSubmissionThrottle).filter(
            StudentSubmissionThrottle.window_started_at < now - timedelta(days=1),
        ).delete()
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()
    logger.warning("student submission throttle row kept conflicting: %s", key)


def authorize(
    db: Session, token: str, *, name: str, student_no: str, password: str = "",
    client: str = "", now: datetime | None = None,
) -> tuple[StudentSubmissionLink, Student]:
    """Find the roster student a submission request speaks for, or refuse."""
    now = now or datetime.now()
    link = _live_link(db, token, now)
    address_key = f"{link.id}:{client or 'unknown'}"[:128]
    _check_address(db, address_key, now)

    name_key, number_key = normalize_name(name), normalize_student_no(student_no)
    student = next((
        candidate for candidate in _class_students(db, link.teacher_id, link.class_id)
        if name_key and number_key
        and normalize_name(candidate.name) == name_key
        and normalize_student_no(candidate.student_no) == number_key
    ), None)
    if student is None:
        _record_address_failure(db, address_key, now)
        raise SubmissionForbidden(_MSG_IDENTITY)

    secret = db.get(StudentSubmitPassword, student.id)
    if secret is None:
        return link, student  # no password set: whatever was typed is ignored
    if secret.locked_until and secret.locked_until > now:
        raise SubmissionThrottled(_MSG_TOO_MANY)
    password = str(password or "").strip()
    accepted = bool(password) and len(password.encode("utf-8")) <= 72 and verify_password(password, secret.password_hash)
    if not accepted:
        secret.fail_count = int(secret.fail_count or 0) + 1
        if secret.fail_count >= PASSWORD_MAX_FAILURES:
            secret.fail_count = 0
            secret.locked_until = now + PASSWORD_LOCK
        _record_address_failure(db, address_key, now)  # commits the count too
        raise SubmissionForbidden(_MSG_IDENTITY)
    if secret.fail_count or secret.locked_until:
        secret.fail_count = 0
        secret.locked_until = None
        db.commit()
    return link, student


def _student_pages(db: Session, link: StudentSubmissionLink, student: Student) -> list[StudentSubmissionPage]:
    return db.query(StudentSubmissionPage).filter(
        StudentSubmissionPage.link_id == link.id,
        StudentSubmissionPage.student_id == student.id,
    ).order_by(StudentSubmissionPage.page_no).all()


def student_status(db: Session, link: StudentSubmissionLink, student: Student, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    return {
        "student_name": student.name,
        "student_no": student.student_no or "",
        "open": _is_open(link, now),
        "closes_at": _iso(link.closes_at),
        "max_pages": MAX_PAGES,
        "pages": [_page_view(page) for page in _student_pages(db, link, student)],
    }


def _require_open(link: StudentSubmissionLink, now: datetime) -> None:
    if not _is_open(link, now):
        raise SubmissionConflict(_MSG_CLOSED)


def _checked_page_no(page_no: int) -> int:
    page_no = int(page_no)
    if not 1 <= page_no <= MAX_PAGES:
        raise SubmissionInvalid(f"页码应在 1–{MAX_PAGES} 之间")
    return page_no


def upload_page(
    db: Session, link: StudentSubmissionLink, student: Student, page_no: int, data: bytes,
    now: datetime | None = None,
) -> dict:
    """Store or replace one page; the same photo under another page is refused."""
    now = now or datetime.now()
    _require_open(link, now)
    page_no = _checked_page_no(page_no)
    try:
        image_format, width, height = inspect_image(data)
    except CaptureInvalidImage as exc:
        raise SubmissionInvalid(exc.message) from exc
    digest = hashlib.sha256(data).hexdigest()

    pages = _student_pages(db, link, student)
    same_photo = next((page for page in pages if page.sha256 == digest), None)
    if same_photo is not None:
        if same_photo.page_no == page_no:
            return student_status(db, link, student, now)  # a retried upload
        raise SubmissionConflict(f"这张照片已经作为第 {same_photo.page_no} 页上传过了")

    directory = os.path.join(_link_dir(link.id), str(int(student.id)))
    os.makedirs(directory, exist_ok=True)
    name = f"p{page_no:02d}-{secrets.token_hex(8)}{IMAGE_EXTENSIONS[image_format]}"
    target = os.path.join(directory, name)
    with open(f"{target}.tmp", "wb") as stream:
        stream.write(data)
    os.replace(f"{target}.tmp", target)

    page = next((page for page in pages if page.page_no == page_no), None)
    replaced = _page_path(page) if page is not None else None
    if page is None:
        page = StudentSubmissionPage(link_id=link.id, student_id=student.id, page_no=page_no)
        db.add(page)
    page.file = name
    page.sha256 = digest
    page.bytes = len(data)
    page.width = width
    page.height = height
    page.uploaded_at = now
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _remove_file(target)
        raise SubmissionConflict("这一页刚刚在别处上传过，请刷新后再试") from exc
    except Exception:
        db.rollback()
        _remove_file(target)
        raise
    if replaced:
        _remove_file(replaced)
    return student_status(db, link, student, now)


def delete_page(
    db: Session, link: StudentSubmissionLink, student: Student, page_no: int,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    _require_open(link, now)
    page_no = _checked_page_no(page_no)
    page = next((page for page in _student_pages(db, link, student) if page.page_no == page_no), None)
    if page is not None:
        path = _page_path(page)
        db.delete(page)
        db.commit()
        _remove_file(path)
    return student_status(db, link, student, now)


def move_page(
    db: Session, link: StudentSubmissionLink, student: Student, page_no: int, to_page_no: int,
    now: datetime | None = None,
) -> dict:
    """Give a page another number; a page already there takes the old number."""
    now = now or datetime.now()
    _require_open(link, now)
    page_no, to_page_no = _checked_page_no(page_no), _checked_page_no(to_page_no)
    pages = {page.page_no: page for page in _student_pages(db, link, student)}
    moving = pages.get(page_no)
    if moving is None:
        raise SubmissionConflict(f"第 {page_no} 页还没有上传")
    if to_page_no != page_no:
        other = pages.get(to_page_no)
        try:
            # Page 0 is never valid, so it can park a row while the pair swaps.
            moving.page_no = 0
            db.flush()
            if other is not None:
                other.page_no = page_no
                db.flush()
            moving.page_no = to_page_no
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise SubmissionConflict("页面刚刚在别处改动过，请刷新后再试") from exc
    return student_status(db, link, student, now)

