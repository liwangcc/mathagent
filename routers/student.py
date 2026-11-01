"""学生管理接口"""

from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import GradingResult, QuestionBank, Student, Weakness, WeaknessOverride
from models_user import User
from routers.auth import _require_user
import services.class_analytics as analytics
from services.chat_utils import student_class_filter
from services.student_service import student_to_dict, students_to_dict

router = APIRouter()


class StudentCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    student_no: str | None = Field(None, max_length=50)
    class_name: str | None = Field(None, max_length=50)
    class_id: int | None = Field(None, ge=1)


class StudentUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=50)
    student_no: str | None = Field(None, max_length=50)
    class_name: str | None = Field(None, max_length=50)
    class_id: int | None = Field(None, ge=1)


def _get_class_filter(db: Session, user: User):
    """获取当前班级过滤条件，返回 class_id 或 None（全部班级）"""
    if user.current_class_id is None:
        return None
    # 验证班级存在且未删除
    from models_rate_limit import TeacherClass
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == user.current_class_id,
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    return tc.id if tc else None


def _owned_student(db: Session, student_id: int, user: User) -> Student:
    """获取属于当前老师的学生,否则 404。"""
    student = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user.id,
    ).first()
    if not student:
        raise HTTPException(404, detail="学生不存在")
    return student


@router.get("/student/list")
def list_students(
    class_name: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """获取学生列表（根据当前选中班级过滤）"""
    q = db.query(Student).filter(Student.teacher_id == user.id)

    # 优先使用 class_id 过滤（方案B）
    class_id = _get_class_filter(db, user)
    if class_id is not None:
        q = q.filter(Student.class_id == class_id)
    elif class_name:
        # 兼容旧的 class_name 参数
        q = q.filter(student_class_filter(db, user.id, class_name))

    if keyword:
        from services.security import escape_like
        like = f"%{escape_like(keyword.strip())}%"
        q = q.filter((Student.name.like(like, escape="\\")) | (Student.student_no.like(like, escape="\\")))

    total = q.count()
    students = q.order_by(Student.id).offset((page - 1) * page_size).limit(page_size).all()
    items = students_to_dict(db, students)
    return {
        "items": items,
        "students": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/student/add")
def add_student(
    data: StudentCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """添加学生（自动关联当前选中班级）"""
    from models_rate_limit import TeacherClass

    payload = data.model_dump(exclude_unset=True)
    student_no = (payload.get("student_no") or "").strip()
    class_name = (payload.get("class_name") or "").strip()

    # 确定 class_id
    class_id = payload.get("class_id")
    if class_id is None and user.current_class_id:
        # 如果未指定 class_id，使用当前选中的班级
        class_id = user.current_class_id

    # 验证 class_id 有效性
    if class_id:
        tc = db.query(TeacherClass).filter(
            TeacherClass.id == class_id,
            TeacherClass.teacher_id == user.id,
            TeacherClass.deleted_at.is_(None),
        ).first()
        if not tc:
            raise HTTPException(400, "指定的班级不存在")
        class_name = tc.class_name  # 同步 class_name

    student = Student(
        teacher_id=user.id,
        class_id=class_id,
        name=data.name.strip(),
        student_no=student_no or None,
        class_name=class_name,
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return student_to_dict(db, student)


@router.put("/student/{student_id}")
def update_student(
    student_id: int,
    data: StudentUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """更新学生信息"""
    from models_rate_limit import TeacherClass

    student = _owned_student(db, student_id, user)
    payload = data.model_dump(exclude_unset=True)

    if "name" in payload:
        student.name = payload["name"].strip()
    if "student_no" in payload:
        val = (payload["student_no"] or "").strip()
        student.student_no = val if val else None
    if "class_id" in payload:
        new_class_id = payload["class_id"]
        if new_class_id:
            tc = db.query(TeacherClass).filter(
                TeacherClass.id == new_class_id,
                TeacherClass.teacher_id == user.id,
                TeacherClass.deleted_at.is_(None),
            ).first()
            if not tc:
                raise HTTPException(400, "指定的班级不存在")
            student.class_id = tc.id
            student.class_name = tc.class_name
        else:
            student.class_id = None
            student.class_name = ""
    elif "class_name" in payload:
        # 兼容旧接口
        student.class_name = (payload["class_name"] or "").strip()

    db.commit()
    return student_to_dict(db, student)


@router.delete("/student/{student_id}")
def delete_student(
    student_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """删除学生"""
    student = _owned_student(db, student_id, user)
    db.delete(student)
    db.commit()
    return {"deleted": student_id}


@router.get("/student/{student_id}/weaknesses")
def get_student_weaknesses(
    student_id: int,
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
    education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None,
):
    """查看学生薄弱点"""
    education_stage = education_stage or None
    _owned_student(db, student_id, user)
    canonical = analytics.weakness_list(db, student_id, education_stage)
    if status:
        requested_status = "corrected" if status in {"resolved", "corrected"} else "pending"
        canonical = [row for row in canonical if row["status"] == requested_status]

    legacy_rows = analytics.filter_education_stage(db.query(Weakness).filter(Weakness.student_id == student_id), Weakness, education_stage).all()
    legacy_by_key: dict[str, Weakness] = {}
    legacy_by_topic: dict[str, list[Weakness]] = {}
    for row in legacy_rows:
        key = analytics._topic_key_str(row.chapter, row.topic, row.education_stage or "")
        current = legacy_by_key.get(key)
        if current is None or (current.status != "pending" and row.status == "pending"):
            legacy_by_key[key] = row
        legacy_by_topic.setdefault((row.education_stage or "", row.catalog_version or "", row.topic or "未知"), []).append(row)

    # Build chapter lookup: topic -> chapter (from QuestionBank)
    topics = {w["topic"] for w in canonical if w.get("topic")}
    chapter_map: dict[str, str] = {}
    if topics:
        from services.bank_scope import visible_filter
        rows = (
            db.query(QuestionBank.topic, QuestionBank.chapter, QuestionBank.education_stage, QuestionBank.catalog_version)
            .filter(QuestionBank.topic.in_(topics), QuestionBank.chapter != "")
            .filter(visible_filter(user.id))
            .all()
        )
        for topic, chapter, stage, version in rows:
            key = (stage or "", version or "", topic)
            if topic and chapter and key not in chapter_map:
                chapter_map[key] = chapter

    # Pull all grading details for this student once, then filter per weakness
    grading_results = (
        db.query(GradingResult).filter(GradingResult.student_id == student_id).all()
    )
    all_details: list[dict] = []
    for gr in grading_results:
        for d in gr.get_details():
            if d.get("result") == "correct":
                continue
            all_details.append({**d, **analytics.row_stage_metadata(gr)})

    def _details_for(topic: str, subtopic: str, stage: str, version: str) -> list[dict]:
        if not topic and not subtopic:
            return []
        needles = [t for t in (topic, subtopic) if t]
        matched = []
        for d in all_details:
            if (d["education_stage"], d["catalog_version"]) != (stage, version):
                continue
            haystack = " ".join(
                str(d.get(k, "")) for k in ("question", "explanation", "hint")
            )
            if any(n and n in haystack for n in needles):
                matched.append({
                    "question": d.get("question", ""),
                    "student_answer": d.get("student_answer", ""),
                    "correct_answer": d.get("correct_answer", ""),
                    "result": d.get("result", ""),
                    "explanation": d.get("explanation", ""),
                    "image_path": d.get("image_path", ""),
                })
        return matched[:10]

    out = []
    for w in canonical:
        item = dict(w)
        legacy = legacy_by_key.get(w["topic_key"])
        if legacy is not None and (legacy.catalog_version or "") != w["catalog_version"]:
            legacy = None
        if legacy is None:
            topic_rows = legacy_by_topic.get((w["education_stage"], w["catalog_version"], w["topic"]), [])
            if len(topic_rows) == 1:
                legacy = topic_rows[0]
        # Old clients pass this id to /resolve/{id} or /weakness/{id}.
        item["id"] = legacy.id if legacy is not None else None
        # Prefer stored chapter; fall back to QuestionBank lookup
        if not item.get("chapter"):
            item["chapter"] = chapter_map.get((w["education_stage"], w["catalog_version"], w["topic"]), "")
        item["related_details"] = _details_for(w["topic"], w["topic"], w["education_stage"], w["catalog_version"])
        out.append(item)
    return out


@router.post("/student/{student_id}/resolve/{weakness_id}")
def resolve_weakness(
    student_id: int,
    weakness_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """标记薄弱点已解决"""
    _owned_student(db, student_id, user)
    w = db.query(Weakness).filter(
        Weakness.id == weakness_id,
        Weakness.student_id == student_id,
    ).first()
    if not w:
        raise HTTPException(404, detail="薄弱点不存在")
    topic_key = analytics._topic_key_str(w.chapter, w.topic, w.education_stage or "")
    override = db.query(WeaknessOverride).filter(
        WeaknessOverride.student_id == student_id,
        WeaknessOverride.topic_key == topic_key,
    ).first()
    if override is None:
        override = WeaknessOverride(
            student_id=student_id,
            teacher_id=user.id,
            topic_key=topic_key,
            status="corrected",
        )
        db.add(override)
    else:
        override.status = "corrected"
    db.commit()
    return next(
        (
            item for item in analytics.weakness_list(db, student_id, w.education_stage or "")
            if item["topic"] == w.topic and item["chapter"] == (w.chapter or "")
        ),
        {"topic": w.topic, "chapter": w.chapter or "", "status": "corrected", **analytics.row_stage_metadata(w)},
    )


@router.delete("/student/{student_id}/weakness/{weakness_id}")
def delete_weakness(
    student_id: int,
    weakness_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """兼容旧接口：从待纠错列表移除，但保留历史审计事件。"""
    _owned_student(db, student_id, user)
    w = db.query(Weakness).filter(
        Weakness.id == weakness_id,
        Weakness.student_id == student_id,
    ).first()
    if not w:
        raise HTTPException(404, detail="薄弱点不存在")
    topic_key = analytics._topic_key_str(w.chapter, w.topic, w.education_stage or "")
    override = db.query(WeaknessOverride).filter(
        WeaknessOverride.student_id == student_id,
        WeaknessOverride.topic_key == topic_key,
    ).first()
    if override is None:
        db.add(WeaknessOverride(
            student_id=student_id,
            teacher_id=user.id,
            topic_key=topic_key,
            status="corrected",
        ))
    else:
        override.status = "corrected"
    db.delete(w)
    db.commit()
    return {"deleted": weakness_id, "status": "corrected"}


@router.post("/student/migrate-classes")
def migrate_student_classes(
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """迁移学生数据：将 class_name 转换为 class_id（一次性操作）"""
    from models_rate_limit import TeacherClass

    # 获取当前老师的所有学生（有 class_name 但没有 class_id 的）
    students = db.query(Student).filter(
        Student.teacher_id == user.id,
        Student.class_id.is_(None),
        Student.class_name != "",
    ).all()

    if not students:
        return {"ok": True, "migrated": 0, "message": "没有需要迁移的学生"}

    # 收集所有不同的 class_name
    class_names = set(s.class_name for s in students if s.class_name)

    # 为每个 class_name 创建或获取 TeacherClass
    name_to_class: dict[str, TeacherClass] = {}
    for name in class_names:
        tc = db.query(TeacherClass).filter(
            TeacherClass.teacher_id == user.id,
            TeacherClass.class_name == name,
            TeacherClass.deleted_at.is_(None),
        ).first()
        if not tc:
            # 检查班级数量限制
            active_count = db.query(TeacherClass).filter(
                TeacherClass.teacher_id == user.id,
                TeacherClass.deleted_at.is_(None),
            ).count()
            if active_count >= TeacherClass.MAX_CLASSES_PER_TEACHER:
                return {
                    "ok": False,
                    "message": f"班级数量已达上限({TeacherClass.MAX_CLASSES_PER_TEACHER}个)，无法自动创建班级'{name}'",
                    "migrated": 0,
                }
            tc = TeacherClass(teacher_id=user.id, class_name=name)
            db.add(tc)
            db.flush()
        name_to_class[name] = tc

    # 更新学生的 class_id
    migrated = 0
    for student in students:
        if student.class_name and student.class_name in name_to_class:
            student.class_id = name_to_class[student.class_name].id
            migrated += 1

    # 更新班级学生数
    for tc in name_to_class.values():
        tc.student_count = db.query(Student).filter(
            Student.class_id == tc.id,
        ).count()

    db.commit()
    return {
        "ok": True,
        "migrated": migrated,
        "classes_created": len([tc for tc in name_to_class.values() if tc.id]),
        "message": f"成功迁移{migrated}名学生",
    }
