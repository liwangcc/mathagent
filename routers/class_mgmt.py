"""班级管理模块 API — 班级表格 / 学生详情 / 月考导入 / 知识点热力图。

契约：班级管理模块_设计与契约.md 第七节。所有接口挂 /api 前缀。
归属隔离：班级与学生均按 teacher_id 校验，杜绝越权。
"""

import asyncio
import io
import re
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import Student, ExamScore, WeaknessOverride
from models_correction import StudentObservationPeriod
from models_rate_limit import TeacherClass
from models_user import User
from routers.auth import _require_user
import services.class_analytics as analytics
from services.security import read_upload_capped
from services.student_service import student_to_dict
from config import MAX_IMPORT_FILE_SIZE

logger = logging.getLogger(__name__)

router = APIRouter()


# ────────────────────────── 归属校验 ──────────────────────────

def _owned_class(db: Session, class_id: int, user: User) -> TeacherClass:
    tc = db.query(TeacherClass).filter(
        TeacherClass.id == class_id,
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None),
    ).first()
    if not tc:
        raise HTTPException(404, detail="班级不存在")
    return tc


def _owned_student(db: Session, student_id: int, user: User) -> Student:
    s = db.query(Student).filter(
        Student.id == student_id,
        Student.teacher_id == user.id,
    ).first()
    if not s:
        raise HTTPException(404, detail="学生不存在")
    return s


# ────────────────────────── 请求体 ──────────────────────────

class _StudentItem(BaseModel):
    student_no: str | None = Field(None, max_length=50)
    exam_no: str | None = Field(None, max_length=64)
    name: str = Field(..., min_length=1, max_length=50)
    phone: str | None = Field(None, max_length=20)


class AddStudentsRequest(BaseModel):
    students: list[_StudentItem] = Field(..., min_length=1, max_length=200)


class CorrectWeaknessRequest(BaseModel):
    status: str = Field("corrected", pattern="^(corrected|pending)$")


class ObservationModeRequest(BaseModel):
    enabled: bool
    reason: str = Field(..., min_length=1, max_length=200)
    note: str = Field("", max_length=1000)
    started_at: datetime | None = None


# ────────────────────────── 班级表格 / 学生 ──────────────────────────

@router.get("/class/{class_id}/students")
def class_students(class_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user), education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None):
    """班级表格视图（主界面，契约 7.2）。"""
    education_stage = education_stage or None
    tc = _owned_class(db, class_id, user)
    students = analytics.class_table(db, class_id, education_stage)
    observed = sum(1 for student in students if student.get("observation_mode"))
    return {
        "class_id": tc.id,
        "education_stage": education_stage,
        "class_name": tc.class_name,
        "students": students,
        "coverage": {
            "class_size": len(students),
            "effective_students": len(students) - observed,
            "observation_students": observed,
        },
    }


@router.post("/class/{class_id}/students")
def add_students(class_id: int, req: AddStudentsRequest,
                 db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """手动加学生（单条/批量，契约 7.2）。学号在本老师内去重。"""
    teacher_class = _owned_class(db, class_id, user)
    added, skipped, errors = 0, 0, []
    # The session does not autoflush, so rows added earlier in this request
    # are invisible to the duplicate queries below; track them here.
    seen_no: set[str] = set()
    seen_exam_no: set[str] = set()
    for it in req.students:
        no = (it.student_no or "").strip() or None
        exam_no = (it.exam_no or "").strip() or None
        if no and no in seen_no:
            skipped += 1
            errors.append(f"学号 {no} 在本次录入中重复（{it.name.strip()}）")
            continue
        if exam_no and exam_no in seen_exam_no:
            skipped += 1
            errors.append(f"准考证号 {exam_no} 在本次录入中重复（{it.name.strip()}）")
            continue
        if no:
            dup = db.query(Student).filter(
                Student.teacher_id == user.id, Student.student_no == no,
            ).first()
            if dup:
                skipped += 1
                errors.append(f"学号 {no} 已存在（{dup.name}）")
                continue
        if exam_no:
            dup = db.query(Student).filter(
                Student.teacher_id == user.id,
                Student.class_id == class_id,
                Student.exam_no == exam_no,
            ).first()
            if dup:
                skipped += 1
                errors.append(f"准考证号 {exam_no} 已存在（{dup.name}）")
                continue
        if not it.name.strip():
            skipped += 1
            errors.append("姓名不能为空")
            continue
        db.add(Student(teacher_id=user.id, class_id=class_id, class_name=teacher_class.class_name,
                       name=it.name.strip(), student_no=no, exam_no=exam_no,
                       phone=(it.phone or "").strip() or None))
        if no:
            seen_no.add(no)
        if exam_no:
            seen_exam_no.add(exam_no)
        added += 1
    _commit_or_conflict(db)
    return {"added": added, "skipped": skipped, "errors": errors}


@router.delete("/class/{class_id}/students/{student_id}")
def remove_student(class_id: int, student_id: int,
                   db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """移出班级（仅解绑班级，不删学生历史数据）。"""
    _owned_class(db, class_id, user)
    s = _owned_student(db, student_id, user)
    if s.class_id != class_id:
        raise HTTPException(400, detail="该学生不在此班级")
    s.class_id = None
    s.class_name = ""
    db.commit()
    return {"ok": True}


class UpdateStudentRequest(BaseModel):
    name: str | None = Field(None, max_length=50)
    student_no: str | None = Field(None, max_length=50)
    exam_no: str | None = Field(None, max_length=64)
    phone: str | None = Field(None, max_length=20)


@router.put("/class/{class_id}/students/{student_id}")
def update_student(class_id: int, student_id: int, req: UpdateStudentRequest,
                   db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """编辑学生信息（姓名/学号/手机号）。"""
    _owned_class(db, class_id, user)
    s = _owned_student(db, student_id, user)
    if s.class_id != class_id:
        raise HTTPException(400, detail="该学生不在此班级")
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(422, detail="姓名不能为空")
        s.name = req.name.strip()
    if req.student_no is not None:
        student_no = req.student_no.strip() or None
        if student_no:
            duplicate = db.query(Student).filter(
                Student.teacher_id == user.id,
                Student.student_no == student_no,
                Student.id != s.id,
            ).first()
            if duplicate:
                raise HTTPException(409, detail=f"学号 {student_no} 已存在（{duplicate.name}）")
        s.student_no = student_no
    if req.exam_no is not None:
        exam_no = req.exam_no.strip() or None
        if exam_no:
            duplicate = db.query(Student).filter(
                Student.teacher_id == user.id,
                Student.class_id == class_id,
                Student.exam_no == exam_no,
                Student.id != s.id,
            ).first()
            if duplicate:
                raise HTTPException(409, detail=f"准考证号 {exam_no} 已存在（{duplicate.name}）")
        s.exam_no = exam_no
    if req.phone is not None:
        s.phone = req.phone.strip() or None
    _commit_or_conflict(db)
    return {"ok": True, "student": student_to_dict(db, s)}


@router.post("/class/{class_id}/students/{student_id}/observation")
def set_student_observation_mode(
    class_id: int,
    student_id: int,
    req: ObservationModeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    """Open/close an audited observation interval without rewriting history."""
    _owned_class(db, class_id, user)
    student = _owned_student(db, student_id, user)
    if student.class_id != class_id:
        raise HTTPException(400, detail="该学生不在此班级")
    active = db.query(StudentObservationPeriod).filter(
        StudentObservationPeriod.student_id == student_id,
        StudentObservationPeriod.teacher_id == user.id,
        StudentObservationPeriod.ended_at.is_(None),
    ).order_by(StudentObservationPeriod.id.desc()).first()
    if req.enabled:
        if active:
            return {"ok": True, "idempotent": True, "period": active.to_dict()}
        started_at = req.started_at or datetime.now()
        if started_at > datetime.now():
            raise HTTPException(400, detail="观察模式开始时间不能晚于当前时间")
        if started_at < student.created_at:
            raise HTTPException(400, detail="观察模式开始时间不能早于学生建档时间")
        period = StudentObservationPeriod(
            student_id=student.id,
            class_id=class_id,
            teacher_id=user.id,
            started_at=started_at,
            reason=req.reason.strip(),
            note=req.note.strip(),
            created_by=user.id,
        )
        db.add(period)
        db.commit()
        db.refresh(period)
        return {"ok": True, "period": period.to_dict()}
    if not active:
        return {"ok": True, "idempotent": True, "period": None}
    active.ended_at = datetime.now()
    active.ended_by = user.id
    active.note = req.note.strip() or active.note
    db.commit()
    return {"ok": True, "period": active.to_dict()}


@router.get("/student/{student_id}/observation-history")
def student_observation_history(
    student_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    _owned_student(db, student_id, user)
    periods = db.query(StudentObservationPeriod).filter(
        StudentObservationPeriod.student_id == student_id,
        StudentObservationPeriod.teacher_id == user.id,
    ).order_by(StudentObservationPeriod.started_at.desc(), StudentObservationPeriod.id.desc()).all()
    return {"periods": [period.to_dict() for period in periods]}


# ────────────────────────── 学生详情 ──────────────────────────

@router.get("/student/{student_id}/detail")
def student_detail(student_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user), education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None):
    """学生详情页（契约 7.3）。"""
    education_stage = education_stage or None
    s = _owned_student(db, student_id, user)
    data = analytics.student_detail(db, student_id, education_stage)
    data["student"] = student_to_dict(db, s)
    return data


@router.post("/student/{student_id}/weakness/correct")
def correct_weakness(student_id: int, topic_key: str,
                     req: CorrectWeaknessRequest,
                     db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """老师手动标记某知识点已纠正 / 撤销（契约 7.3）。topic_key=查询参数 "chapter||topic"。"""
    _owned_student(db, student_id, user)
    parts = topic_key.split("||")
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[0] not in {"primary", "junior", "senior"}):
        raise HTTPException(422, detail="知识点标识无效")
    stage = parts[0] if len(parts) == 3 else ""
    ovr = db.query(WeaknessOverride).filter(
        WeaknessOverride.student_id == student_id,
        WeaknessOverride.topic_key == topic_key,
    ).first()
    # The teacher's choice always becomes the visible status: when it matches
    # the automatic judgement the override is dropped, otherwise it is stored
    # (including "pending" for a topic the system had marked corrected).
    target = next(
        (item for item in analytics.weakness_list(db, student_id) if item["topic_key"] == topic_key),
        None,
    )
    auto_status = target["auto_status"] if target else "pending"
    if req.status == auto_status:
        if ovr:
            db.delete(ovr)
    elif ovr:
        ovr.status = req.status
    else:
        db.add(WeaknessOverride(student_id=student_id, teacher_id=user.id,
                                topic_key=topic_key, status=req.status))
    db.commit()
    return {
        "ok": True,
        "status": req.status,
        "manual": req.status != auto_status,
        "education_stage": stage,
        "correction_rate": analytics.correction_rate(db, student_id, stage),
    }


# ────────────────────────── 班级热力图 ──────────────────────────

@router.get("/class/{class_id}/heatmap")
def class_heatmap(class_id: int, db: Session = Depends(get_db), user: User = Depends(_require_user), education_stage: Literal["primary", "junior", "senior", "legacy", ""] | None = None):
    """知识点热力图（章→知识点→学生，契约 7.5 / 规划 3.7）。"""
    education_stage = education_stage or None
    _owned_class(db, class_id, user)
    return {"education_stage": education_stage, "tree": analytics.class_heatmap(db, class_id, education_stage)}


# ────────────────────────── Excel 导入 ──────────────────────────

def _cell_text(cell) -> str:
    """把一个单元格规范成字符串，供后续统一按文本处理。

    老师的名单表极少是"干净"的，这里吸收几种最常见的脏数据：
      * ``None``（空单元格）→ 空串，免得下游到处写 ``or ""``。
      * 学号被 Excel 存成数字：``2021000008`` 读出来是 int，``str()`` 尚可，
        但存成 float 时会变成 ``2021000008.0``；再长一点直接是科学计数法
        ``1.23456789012345e+11``，那样入库的学号就是错的。故整数值的 float
        一律按整数取字符串。
      * 日期等其它类型统一 ``str()`` 兜底，不抛异常中断整份导入。
    """
    if cell is None:
        return ""
    if isinstance(cell, bool):          # bool 是 int 的子类，需先拦
        return str(cell)
    if isinstance(cell, int):
        return str(cell)
    if isinstance(cell, float):
        return str(int(cell)) if cell.is_integer() else str(cell)
    return str(cell).strip()


def _read_xlsx_rows(raw: bytes) -> list[list]:
    """读 xlsx 全部数据行（含表头）。openpyxl 缺失则报错。"""
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(500, detail="服务器缺少 openpyxl，无法解析 Excel")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, detail="无法解析该文件，请确认是 .xlsx 格式")
    ws = wb.active
    rows = [[_cell_text(cell) for cell in row] for row in ws.iter_rows(values_only=True)]
    wb.close()
    # 只裁掉末尾空行（Excel 常带一片空尾行，会让"表格为空"判断失效）。
    # 中间的空行**必须保留**：调用方用 enumerate 的下标给老师报"第N行…"，
    # 删掉中间任何一行都会让后面所有行号错位，老师照着去改会改错人。
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def _find_cols(header: list, wanted: dict, exclude: dict | None = None) -> dict:
    """按关键词匹配列索引。wanted={'no':['学号','编号'], 'name':['姓名','名字'], ...}。

    - 表头与关键词完全相同的列优先，其次才按"包含关键词"匹配；
    - 每一列只分给一个字段（按 wanted 顺序），"考试成绩"不会同时成为成绩列和考试名列；
    - exclude 给出某字段表头不能包含的词，如电话列不能是"家长姓名"。
    """
    exclude = exclude or {}
    cells = [str(cell or "").strip() for cell in header]
    idx: dict = {}
    used: set[int] = set()
    for exact in (True, False):
        for key, kws in wanted.items():
            if key in idx:
                continue
            for i, text in enumerate(cells):
                if i in used or not text or any(bad in text for bad in exclude.get(key, ())):
                    continue
                if (text in kws) if exact else any(k in text for k in kws):
                    idx[key] = i
                    used.add(i)
                    break
    return idx


_ROSTER_COLUMNS = {
    "no": ["学号", "编号"], "exam_no": ["准考证号", "考号"],
    "name": ["姓名", "名字"], "phone": ["手机", "电话", "家长"],
}
# 家长姓名不是学生姓名，也不是手机号。
_ROSTER_EXCLUDE = {"name": ["家长"], "phone": ["姓名", "名字"]}
_SCORE_COLUMNS = {
    "no": ["学号", "编号"], "name": ["姓名", "名字"],
    "score": ["成绩", "分数", "得分"], "exam": ["考试", "考试名"],
}
_SCORE_EXCLUDE = {"name": ["家长"]}
_PREVIEW_LIMIT = 300


def _column_labels(header: list, cols: dict) -> dict:
    return {key: str(header[index] or "").strip() for key, index in cols.items()}


def _clean_phone(raw: str) -> tuple[str | None, str]:
    """家长手机号：必须含 4 位以上数字，否则视为错列/无效并忽略。"""
    phone = (raw or "").strip()
    if not phone:
        return None, ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4 or len(phone) > 20:
        return None, f"手机号“{phone[:20]}”无效，已忽略"
    return phone, ""


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail="学号或准考证号与已有学生重复，未保存任何修改；请刷新后重试") from exc


def _row_value(row: list, cols: dict, key: str) -> str:
    """取一行里某个逻辑列的文本；列不存在或该行偏短时返回空串。

    老师的名单表常有"某几行只填了前两列"的情况，此时 openpyxl 给出的行会比
    表头短，直接 ``row[idx]`` 会 IndexError 把整份导入打断——一行脏数据废掉
    整张表，是最不该有的失败方式。
    """
    if key not in cols:
        return ""
    index = cols[key]
    if index >= len(row):
        return ""
    return _cell_text(row[index])


@router.post("/class/{class_id}/students/import-excel")
async def import_students_excel(class_id: int, file: UploadFile = File(...),
                                dry_run: bool = Form(False),
                                db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """Excel 导入学生名单（列：学号/姓名/手机号，契约 7.2）。

    dry_run=true 只返回逐行预览（新增/跳过及原因），不写库。
    """
    teacher_class = _owned_class(db, class_id, user)
    raw = await read_upload_capped(file, MAX_IMPORT_FILE_SIZE)
    rows = await asyncio.to_thread(_read_xlsx_rows, raw)
    if len(rows) < 2:
        raise HTTPException(400, detail="表格为空或无数据行")
    cols = _find_cols(rows[0], _ROSTER_COLUMNS, _ROSTER_EXCLUDE)
    if "name" not in cols:
        raise HTTPException(400, detail="未找到'姓名'列")

    added, skipped, errors, warnings, preview = 0, 0, [], [], []
    seen_no: dict[str, int] = {}
    seen_exam_no: dict[str, int] = {}
    for ri, row in enumerate(rows[1:], start=2):
        name = _row_value(row, cols, "name")
        if not name:
            continue
        no = _row_value(row, cols, "no") or None
        exam_no = _row_value(row, cols, "exam_no") or None
        phone, phone_warning = _clean_phone(_row_value(row, cols, "phone"))
        reason = ""
        if len(name) > 50:
            reason = f"第{ri}行姓名超过 50 字"
        elif no and no in seen_no:
            reason = f"第{ri}行学号 {no} 与第{seen_no[no]}行重复"
        elif no and (dup := db.query(Student).filter(
            Student.teacher_id == user.id, Student.student_no == no,
        ).first()):
            reason = f"第{ri}行学号 {no} 已存在（{dup.name}）"
        elif exam_no and exam_no in seen_exam_no:
            reason = f"第{ri}行准考证号 {exam_no} 与第{seen_exam_no[exam_no]}行重复"
        elif exam_no and (dup := db.query(Student).filter(
            Student.teacher_id == user.id,
            Student.class_id == class_id,
            Student.exam_no == exam_no,
        ).first()):
            reason = f"第{ri}行准考证号 {exam_no} 已存在（{dup.name}）"
        if no:
            seen_no.setdefault(no, ri)
        if exam_no:
            seen_exam_no.setdefault(exam_no, ri)
        if len(preview) < _PREVIEW_LIMIT:
            preview.append({
                "row": ri, "student_no": no or "", "exam_no": exam_no or "", "name": name,
                "phone": phone or "", "status": "skip" if reason else "add",
                "reason": reason or (f"第{ri}行{phone_warning}" if phone_warning else ""),
            })
        if reason:
            skipped += 1
            errors.append(reason)
            continue
        if phone_warning:
            warnings.append(f"第{ri}行{phone_warning}")
        if not dry_run:
            db.add(Student(teacher_id=user.id, class_id=class_id, class_name=teacher_class.class_name,
                           name=name, student_no=no, exam_no=exam_no, phone=phone))
        added += 1
    if not dry_run:
        _commit_or_conflict(db)
    return {
        "added": added, "skipped": skipped, "errors": errors, "warnings": warnings,
        "columns": _column_labels(rows[0], cols), "preview": preview, "dry_run": dry_run,
    }


_SCORE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*分?")


def _parse_score(raw: str) -> float | None:
    """"95" / "95.5" / "95分" → 数值；"缺考"、"-"、"90-95" 等无法确定的成绩返回 None。"""
    match = _SCORE_PATTERN.fullmatch((raw or "").strip())
    return float(match.group(1)) if match else None


def _parse_exam_date(raw: str):
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(422, detail="考试日期格式应为 YYYY-MM-DD") from exc


@router.post("/class/{class_id}/exam-scores/import")
async def import_exam_scores(class_id: int, file: UploadFile = File(...),
                             exam_name: str = Form(""),
                             exam_date: str = Form(""),
                             dry_run: bool = Form(False),
                             db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """月考成绩 Excel 导入（列：学号/姓名/成绩/可选考试名，契约 7.4）。按学号优先匹配。

    身份不确定（同名、学号与姓名不一致）的行不会被猜测匹配，而是列入未匹配；
    dry_run=true 只返回逐行预览，不写库。
    """
    from services.teaching_context import make_teaching_context, require_teacher_stage
    context = make_teaching_context(require_teacher_stage(user))
    _owned_class(db, class_id, user)
    parsed_exam_date = _parse_exam_date(exam_date)
    raw = await read_upload_capped(file, MAX_IMPORT_FILE_SIZE)
    rows = await asyncio.to_thread(_read_xlsx_rows, raw)
    if len(rows) < 2:
        raise HTTPException(400, detail="表格为空或无数据行")
    cols = _find_cols(rows[0], _SCORE_COLUMNS, _SCORE_EXCLUDE)
    if "score" not in cols:
        raise HTTPException(400, detail="未找到'成绩'列")

    # 班级学生索引：学号 → student；姓名 → 同名学生列表（同名不猜）
    roster = db.query(Student).filter(Student.class_id == class_id).all()
    by_no = {s.student_no: s for s in roster if s.student_no}
    by_name: dict[str, list[Student]] = {}
    for s in roster:
        by_name.setdefault(s.name, []).append(s)

    unmatched, skipped, duplicates, preview = [], [], [], []
    default_exam = exam_name.strip() or "月考"
    planned: dict[tuple[int, str], tuple[int, Student, float]] = {}
    for ri, row in enumerate(rows[1:], start=2):
        raw_score = _row_value(row, cols, "score")
        no = _row_value(row, cols, "no")
        nm = _row_value(row, cols, "name")
        label = nm or no
        if not raw_score:
            if label:
                skipped.append(f"第{ri}行 {label}：未填写成绩")
            continue
        score = _parse_score(raw_score)
        if score is None:
            skipped.append(f"第{ri}行 {label or ''}：成绩“{raw_score[:20]}”无法识别，已跳过")
            continue
        student, reason = None, ""
        if no and no in by_no:
            student = by_no[no]
            if nm and nm != student.name:
                student, reason = None, f"学号 {no} 对应 {student.name}，与表中姓名 {nm} 不一致"
        elif nm:
            candidates = by_name.get(nm, [])
            if len(candidates) == 1:
                student = candidates[0]
            elif len(candidates) > 1:
                reason = f"班级有 {len(candidates)} 名同名学生，请在表中填写学号"
        if student is None:
            reason = reason or "无匹配学生"
            unmatched.append(f"第{ri}行 {label or '(空)'}：{reason}")
            if len(preview) < _PREVIEW_LIMIT:
                preview.append({"row": ri, "student_no": no, "name": nm, "score": score,
                                "exam_name": "", "status": "unmatched", "reason": reason})
            continue
        ename = _row_value(row, cols, "exam") or default_exam
        key = (student.id, ename)
        if key in planned:
            duplicates.append(f"第{ri}行与第{planned[key][0]}行是 {student.name} 的同一场考试，以第{ri}行为准")
        planned[key] = (ri, student, score)
        if len(preview) < _PREVIEW_LIMIT:
            preview.append({"row": ri, "student_no": student.student_no or "", "name": student.name,
                            "score": score, "exam_name": ename, "status": "match", "reason": ""})

    if not dry_run:
        active_observation = {
            period.student_id: period
            for period in db.query(StudentObservationPeriod).filter(
                StudentObservationPeriod.class_id == class_id,
                StudentObservationPeriod.ended_at.is_(None),
            ).all()
        }
        for (student_id, ename), (_ri, student, score) in planned.items():
            existing = db.query(ExamScore).filter(
                ExamScore.teacher_id == user.id,
                ExamScore.student_id == student_id,
                ExamScore.exam_name == ename,
                ExamScore.education_stage == context["education_stage"],
                ExamScore.catalog_version == context["curriculum_version"],
            ).first()
            if existing:
                existing.class_id = class_id
                existing.score = score
                if parsed_exam_date:
                    existing.exam_date = parsed_exam_date
            else:
                period = active_observation.get(student_id)
                db.add(ExamScore(
                    student_id=student_id, class_id=class_id, teacher_id=user.id,
                    exam_name=ename, score=score,
                    **({"exam_date": parsed_exam_date} if parsed_exam_date else {}),
                    education_stage=context["education_stage"], catalog_version=context["curriculum_version"],
                    analytics_included=period is None,
                    inclusion_reason="included" if period is None else "observation_mode",
                    observation_period_id=period.id if period else None,
                ))
        db.commit()
    return {
        "matched": len(planned), "unmatched": unmatched, "skipped": skipped, "duplicates": duplicates,
        "exam_name": default_exam, "exam_names": sorted({ename for _sid, ename in planned}),
        "exam_date": str(parsed_exam_date) if parsed_exam_date else None,
        "columns": _column_labels(rows[0], cols), "preview": preview, "dry_run": dry_run,
        "teaching_context": context,
    }


# ────────────────────────── 图片识别导入（qwen3.8-flash 识图）──────────────────────────

@router.post("/class/{class_id}/students/import-image")
async def import_students_image(class_id: int, file: UploadFile = File(...),
                                db: Session = Depends(get_db), user: User = Depends(_require_user)):
    """图片识别名单（qwen3.8-flash 识图，契约 7.2）。返回待确认列表，不直接入库。"""
    _owned_class(db, class_id, user)
    from services.analyzer import parse_llm_json
    from services.ocr_service import read_image

    raw = await read_upload_capped(file, MAX_IMPORT_FILE_SIZE)
    if not raw:
        raise HTTPException(400, detail="文件为空")
    mime = file.content_type or "image/jpeg"

    prompt = (
        "识别这张学生名单图片中的每一行，提取可确认的学号、姓名和家长手机号。\n"
        "无法确认的字段返回空字符串，不要猜测或合并相邻行。\n"
        "严格返回 JSON 数组，不要其他文字：\n"
        '[{"student_no":"","name":"","phone":""}]'
    )
    try:
        content = await read_image(
            raw,
            mime,
            prompt,
            max_tokens=2048,
            teacher_id=str(user.id),
        )
        recognized = parse_llm_json(content)
    except Exception as e:
        logger.error("名单图片识别失败: %s", e, exc_info=True)
        raise HTTPException(502, detail="图片识别失败，请重试或改用手动/Excel录入")

    if not isinstance(recognized, list):
        recognized = []
    cleaned = []
    for it in recognized:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        if not name:
            continue
        cleaned.append({"student_no": str(it.get("student_no", "")).strip(), "name": name, "phone": str(it.get("phone", "")).strip()})
    return {"recognized": cleaned, "need_confirm": True}
