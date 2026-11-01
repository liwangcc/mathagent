"""拍照导入题库 — 题干图 + 答案图 → 配对 → 入题库。

流程：
  recognize : 题干图走 recognize_paper，答案图走 recognize_answer_sheet，按题号配对
  save      : 老师确认后批量入题库（按 question 文本去重）
"""

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Optional
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import MAX_UPLOAD_FILE_SIZE, UPLOAD_DIR
from database import get_db
from models import QuestionBank
from models_user import User
from routers.auth import _require_admin, _require_user
from services.ocr_service import (
    recognize_answer_sheet,
    recognize_paper,
    reset_vision_teacher_id,
    set_vision_teacher_id,
)
from services.exercise_bank_selector import normalize_question_type
from services.security import read_upload_capped, validate_upload_file
from services.answer_import import _validate_import_scope
from services.teaching_context import (
    make_teaching_context, record_teaching_fields,
    require_teacher_stage, use_teaching_context, validate_teaching_context,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_BYTES = MAX_UPLOAD_FILE_SIZE


def _import_metadata_path(import_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", import_id or ""):
        raise HTTPException(404, detail="导入批次不存在或已过期")
    root = (Path(UPLOAD_DIR) / "bank_import").resolve()
    path = (root / import_id / "metadata.json").resolve()
    if not path.is_relative_to(root):
        raise HTTPException(404, detail="导入批次不存在或已过期")
    return path


def _write_import_metadata(import_id: str, metadata: dict) -> None:
    path = _import_metadata_path(import_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"metadata.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _owned_import_metadata(import_id: str, user: User) -> dict:
    path = _import_metadata_path(import_id)
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, detail="导入批次不存在或已过期")
    if not isinstance(metadata, dict) or metadata.get("teacher_id") != user.id:
        raise HTTPException(404, detail="导入批次不存在或已过期")
    metadata["teaching_context"] = validate_teaching_context(metadata.get("teaching_context") or {})
    return metadata


def _normalize_question(text: str) -> str:
    """标准化题目文本用于去重：去掉题号、分值、多余空格。

    比较完整题干：只比前 100 字会把共用长题干、只有后半部分不同的
    两道几何/应用题误判为重复而静默跳过。
    """
    text = re.sub(r'【.*?】\d+\.\s*', '', text)
    text = re.sub(r'[（(]\d+\s*分[)）]', '', text)
    return re.sub(r'\s+', '', text)

async def _save_upload(f: UploadFile, sub_dir: str) -> str:
    raw = await read_upload_capped(f, _MAX_BYTES)
    # ── 安全验证 ──
    validation = validate_upload_file(f.filename, raw)
    if not validation["safe"]:
        raise HTTPException(400, detail=f"文件安全检查失败: {validation['reason']}")
    ext = os.path.splitext(f.filename or "")[1].lower() or ".png"
    os.makedirs(sub_dir, exist_ok=True)
    save_path = os.path.join(sub_dir, f"{uuid.uuid4().hex}{ext}")
    with open(save_path, "wb") as out:
        out.write(raw)
    return save_path


@router.post("/bank/photo-import/recognize")
async def photo_import_recognize(
    question_files: list[UploadFile] = File(default=[]),
    answer_files: list[UploadFile] = File(default=[]),
    chapter: str = Form(""),
    user: User = Depends(_require_user),
):
    """题干图 + 答案图识别 + 按题号配对。"""
    context = make_teaching_context(require_teacher_stage(user))
    with use_teaching_context(context):
        try:
            _validate_import_scope([{"chapter": chapter}])
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    if not question_files and not answer_files:
        raise HTTPException(400, detail="请上传题干图或答案图")

    import_id = uuid.uuid4().hex
    batch_dir = str(_import_metadata_path(import_id).parent)
    metadata = {"teacher_id": user.id, "teaching_context": context, "paired": [], "status": "recognizing"}
    _write_import_metadata(import_id, metadata)

    q_paths = [await _save_upload(f, batch_dir) for f in question_files]
    a_paths = [await _save_upload(f, batch_dir) for f in answer_files]

    # 题干识别（并发）
    vision_token = set_vision_teacher_id(str(user.id))
    try:
        q_tasks = [recognize_paper(p, teacher_id=str(user.id)) for p in q_paths]
        a_tasks = [recognize_answer_sheet(p) for p in a_paths]
        with use_teaching_context(context):
            q_results, a_results = await asyncio.gather(
                asyncio.gather(*q_tasks) if q_tasks else asyncio.sleep(0, result=[]),
                asyncio.gather(*a_tasks) if a_tasks else asyncio.sleep(0, result=[]),
            )
    finally:
        reset_vision_teacher_id(vision_token)

    # 题号 → 题目内容
    questions: dict[str, str] = {}
    question_types: dict[str, str] = {}
    for paper in q_results:
        for q in paper.get("questions", []):
            qn = str(q.get("question_number", "")).strip()
            text = (q.get("question_text") or "").strip()
            if qn and text and qn not in questions:
                questions[qn] = text
                question_types[qn] = str(
                    q.get("question_type") or q.get("type") or ""
                ).strip()

    # 题号 → 答案
    answers: dict[str, str] = {}
    for sheet in a_results:
        for item in sheet:
            qn = str(item.get("question_no", "")).strip()
            ans = (item.get("answer") or "").strip()
            if qn and qn not in answers:
                answers[qn] = ans

    all_qns = sorted(set(questions) | set(answers), key=lambda x: (len(x), x))
    paired = []
    for qn in all_qns:
        content = questions.get(qn, "")
        answer = answers.get(qn, "")
        if content and answer:
            status = "matched"
        elif content and not answer:
            status = "missing_answer"
        else:
            status = "missing_question"
        paired.append({
            "question_no": qn,
            "content": content,
            "answer": answer,
            "question_type": normalize_question_type(
                question_types.get(qn, ""), content, answer
            ) or "unknown",
            "status": status,
            "chapter": chapter,
        })

    metadata.update(paired=paired, status="review")
    _write_import_metadata(import_id, metadata)
    return {"paired": paired, "import_id": import_id, "teaching_context": context}


@router.get("/bank/photo-import/{import_id}")
def photo_import_restore(import_id: str, user: User = Depends(_require_user)):
    metadata = _owned_import_metadata(import_id, user)
    return {"import_id": import_id, "paired": metadata.get("paired", []), "status": metadata.get("status", "review"), "teaching_context": metadata["teaching_context"]}


class _SaveItem(BaseModel):
    question_no: str
    content: str
    answer: str
    question_type: Optional[str] = ""
    chapter: Optional[str] = ""
    topic: Optional[str] = ""
    subject: Literal["数学"] = "数学"
    education_stage: Optional[str] = None


class SaveReq(BaseModel):
    items: list[_SaveItem]
    import_id: str = ""


def _save_context(req: SaveReq, user: User) -> dict:
    context = (_owned_import_metadata(req.import_id, user)["teaching_context"] if req.import_id
               else make_teaching_context(require_teacher_stage(user)))
    with use_teaching_context(context):
        try:
            _validate_import_scope([item.model_dump(exclude_none=True) for item in req.items])
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    return context


@router.post("/bank/photo-import/save")
def photo_import_save(
    req: SaveReq,
    db: Session = Depends(get_db),
    user: User = Depends(_require_user),
):
    context = _save_context(req, user)
    with use_teaching_context(context):
        result = _photo_import_save(req, db, user)
    result["teaching_context"] = context
    return result


def _photo_import_save(req: SaveReq, db: Session, user: User):
    """老师确认后入题库，按 question 文本去重。

    普通老师导入进入本人私库；公库写入应走管理员专用接口。
    """
    if not req.items:
        raise HTTPException(400, detail="items 不能为空")

    inserted = 0
    skipped = 0
    seen_in_batch: set[str] = set()

    # 预加载已有题目的标准化文本（一次性加载，避免逐条查询）
    from services.bank_scope import visible_filter
    _existing_rows = db.query(QuestionBank.question).filter(visible_filter(user.id)).all()
    _existing_norm = {_normalize_question(r[0]) for r in _existing_rows}

    for item in req.items:
        content = (item.content or "").strip()
        answer = (item.answer or "").strip()
        if not content or not answer:
            skipped += 1
            continue
        if content in seen_in_batch:
            skipped += 1
            continue
        normalized = _normalize_question(content)
        if normalized in seen_in_batch or normalized in _existing_norm:
            skipped += 1
            continue
        seen_in_batch.add(normalized)

        # 精确匹配
        exists = db.query(QuestionBank.id).filter(
            visible_filter(user.id),
            QuestionBank.question == content,
        ).first()
        if exists:
            skipped += 1
            continue

        db.add(QuestionBank(
            **record_teaching_fields(chapter=(item.chapter or "").strip(), topic=(item.topic or "").strip()),
            teacher_id=user.id,
            is_public=False,
            question=content,
            answer=answer,
            question_type=normalize_question_type(
                item.question_type, content, answer
            ) or "unknown",
            chapter=(item.chapter or "").strip(),
            topic=(item.topic or "").strip(),
            source="photo_import",
        ))
        inserted += 1

    db.commit()
    return {
        "inserted": inserted,
        "skipped": skipped,
        "notice": "题目已进入你的私有题库。",
    }


@router.post("/admin/bank/public-import/save")
def public_import_save(
    req: SaveReq,
    db: Session = Depends(get_db),
    user: User = Depends(_require_admin),
):
    context = _save_context(req, user)
    with use_teaching_context(context):
        result = _public_import_save(req, db, user)
    result["teaching_context"] = context
    return result


def _public_import_save(req: SaveReq, db: Session, user: User):
    """管理员确认后入公共题库，普通老师不可调用。"""
    if not req.items:
        raise HTTPException(400, detail="items 不能为空")

    inserted = 0
    skipped = 0
    seen_in_batch: set[str] = set()

    from services.bank_scope import public_filter, teaching_filter

    for item in req.items:
        content = (item.content or "").strip()
        answer = (item.answer or "").strip()
        if not content or not answer:
            skipped += 1
            continue
        if content in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(content)

        exists = db.query(QuestionBank.id).filter(
            public_filter(),
            teaching_filter(),
            QuestionBank.question == content,
        ).first()
        if exists:
            skipped += 1
            continue

        db.add(QuestionBank(
            **record_teaching_fields(chapter=(item.chapter or "").strip(), topic=(item.topic or "").strip()),
            teacher_id=None,
            is_public=True,
            question=content,
            answer=answer,
            question_type=normalize_question_type(
                item.question_type, content, answer
            ) or "unknown",
            chapter=(item.chapter or "").strip(),
            topic=(item.topic or "").strip(),
            source="admin_public",
        ))
        inserted += 1

    db.commit()
    return {
        "inserted": inserted,
        "skipped": skipped,
        "notice": "题目已进入公共题库。",
    }
