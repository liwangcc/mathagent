"""答案导入服务 — Excel/JSON导入 + 五级层级定位"""

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import QuestionBank
from services.teaching_context import current_teaching_context, record_teaching_fields
from services.exercise_bank_selector import normalize_question_type
from services.matchers.hash_matcher import HashMatcher

logger = logging.getLogger(__name__)

FUZZY_DEDUP_THRESHOLD = 0.98

# 题号前缀：阿拉伯数字 / 中文数字 / 罗马数字 + 点/顿号/括号/、/句号
_QNUM_PREFIX = re.compile(
    r'^\s*(?:'
    r'[（(]\s*\d+\s*[）)]'                    # (1) （1）
    r'|\d+\s*[\.\．、:：)）]'                # 19. / 20、 / 1)
    r'|[一二三四五六七八九十百零〇]+\s*[\.\．、:：)）]'  # 一. 二、
    r'|第\s*\d+\s*[题道]'                    # 第19题
    r'|题\s*\d*\s*[:：\.\．、]?'              # 题19： / 题:
    r')\s*'
)

# 答案/解答前缀
_ANS_PREFIX = re.compile(
    r'^\s*(?:'
    r'[（(]\s*\d+\s*[）)]'                # (1) (2)
    r'|\d+\s*[\.\．、:：)）]'              # 1. 2、
    r'|[①②③④⑤⑥⑦⑧⑨⑩]'                  # 圈数字
    r'|步骤\s*\d*\s*[:：\.]?'              # 步骤1：
    r'|第\s*\d+\s*步\s*[:：\.]?'           # 第1步：
    r'|答\s*[:：\.]'                       # 答：答.
    r'|解\s*[:：\.]'                       # 解：解.
    r'|证明\s*[:：\.]'                     # 证明：
    r'|证\s*[:：\.]'                       # 证：
    r'|因为\s*[:：]'                       # 因为：
    r'|所以\s*[:：]'                       # 所以：
    r')\s*'
)

# 标点（用于模糊比较时去除）
_PUNCT_RE = re.compile(
    r'[\s\.\,\;\:\!\?\'"“”‘’\(\)（）\[\]【】「」『』、，。；：！？·\-—_/\\\|]+'
)


def _validate_import_scope(items: list[dict]) -> None:
    """Validate the entire upload before writing any row, including duplicates."""
    context = current_teaching_context()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条题目必须为 JSON 对象")
        if not context:
            continue
        if (str(item.get("subject") or "数学").strip()) != "数学":
            raise ValueError(f"第 {index} 条题目不是数学，本次仅支持数学题库")
        if "education_stage" in item and str(item["education_stage"] or "").strip() != context["education_stage"]:
            raise ValueError(f"第 {index} 条题目的学段与当前个人资料不一致；历史未标学段题目不能自动重新归类")
        supplied_context = item.get("teaching_context")
        if supplied_context is not None and (
            not isinstance(supplied_context, dict)
            or supplied_context.get("education_stage") != context["education_stage"]
        ):
            raise ValueError(f"第 {index} 条题目的教学上下文与当前学段不一致")
        chapter = str(item.get("chapter") or "").strip()
        topic = str(item.get("topic") or "").strip()
        placeholders = {"", "未知", "未指定", "未标注", "未分类", "综合"}
        if chapter not in placeholders or topic not in placeholders:
            from services.math_curriculum import get_math_catalog, get_knowledge_point
            if chapter not in {entry["chapter"] for entry in get_math_catalog(context["education_stage"])}:
                raise ValueError(f"第 {index} 条题目的章节不属于当前学段目录")
            if topic not in placeholders and get_knowledge_point(context["education_stage"], chapter, topic) is None:
                raise ValueError(f"第 {index} 条题目的知识点不属于当前学段目录")


def import_from_json(db: Session, file_path: str | Path, teacher_id: int | None = None) -> dict:
    """Import questions from a JSON file. Returns {imported: int, skipped: int}.

    teacher_id: 导入归入该老师的私库（P0-4）。去重只在该老师可见范围内做。
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if "items" in data:
            _validate_import_scope([data])
            data = data["items"]
        else:
            data = [data]
    if not isinstance(data, list):
        return {"imported": 0, "skipped": 0, "error": "JSON格式错误：需要数组或对象"}
    _validate_import_scope(data)

    imported = 0
    skipped = 0

    question_index = _build_question_index(db, teacher_id)
    fuzzy_keys = _build_fuzzy_keys(db, teacher_id)

    for item in data:
        question_text = item.get("question", "").strip()
        if not question_text:
            skipped += 1
            continue

        if question_index.get(question_text) or _is_fuzzy_duplicate(question_text, fuzzy_keys):
            skipped += 1
            continue

        answer_text = item.get("answer", "")
        q = QuestionBank(
            **record_teaching_fields(chapter=item.get("chapter", ""), topic=item.get("topic", "")),
            teacher_id=teacher_id,
            is_public=False,
            subject="数学" if current_teaching_context() else item.get("subject", "数学"),
            chapter=item.get("chapter", ""),
            topic=item.get("topic", ""),
            difficulty=item.get("difficulty", 1),
            question=question_text,
            answer=answer_text,
            question_type=normalize_question_type(
                item.get("question_type") or item.get("type"),
                question_text,
                answer_text,
            ) or "unknown",
            grading_method=item.get("grading_method", "exact"),
        )
        variants = item.get("answer_variants", [])
        if isinstance(variants, list):
            q.set_variants(variants)
        else:
            q.set_variants([])
        steps = item.get("solution_steps", [])
        if not (isinstance(steps, list) and steps):
            steps = split_answer_into_steps(answer_text)
        if isinstance(steps, list):
            q.set_solution_steps(steps)
        else:
            q.set_solution_steps([])
        db.add(q)
        db.flush()
        question_index.put(question_text, q)
        fuzzy_keys.append(_fuzzy_key(question_text))
        imported += 1

    db.commit()
    logger.info("Imported %d questions from JSON, skipped %d duplicates", imported, skipped)
    return {"imported": imported, "skipped": skipped}


def import_from_excel(db: Session, file_path: str | Path, teacher_id: int | None = None) -> dict:
    """Import questions from an Excel file using the standard template format.

    teacher_id: 导入归入该老师的私库（P0-4）。
    """
    try:
        import openpyxl
    except ImportError:
        return {"imported": 0, "skipped": 0, "error": "openpyxl未安装"}

    wb = openpyxl.load_workbook(file_path, read_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()
    if len(rows) < 2:
        return {"imported": 0, "skipped": 0, "error": "Excel文件为空或只有表头"}

    headers = [str(h).strip() if h else "" for h in rows[0]]
    col_map = _map_columns(headers)
    items = []
    for row in rows[1:]:
        values = [str(c).strip() if c is not None else "" for c in row]
        items.append({field: values[idx] if idx < len(values) else "" for field, idx in col_map.items()})
    _validate_import_scope(items)

    imported = 0
    skipped = 0
    question_index = _build_question_index(db, teacher_id)
    fuzzy_keys = _build_fuzzy_keys(db, teacher_id)

    for q_data in items:
        question_text = q_data.get("question", "")
        if not question_text:
            skipped += 1
            continue

        if question_index.get(question_text) or _is_fuzzy_duplicate(question_text, fuzzy_keys):
            skipped += 1
            continue

        answer_text = q_data.get("answer", "")
        q = QuestionBank(
            **record_teaching_fields(chapter=q_data.get("chapter", ""), topic=q_data.get("topic", "")),
            teacher_id=teacher_id,
            is_public=False,
            subject=q_data.get("subject") or "数学",
            chapter=q_data.get("chapter", ""),
            topic=q_data.get("topic", ""),
            difficulty=int(q_data.get("difficulty", 1) or 1),
            question=question_text,
            answer=answer_text,
            question_type=normalize_question_type(
                q_data.get("question_type"), question_text, answer_text
            ) or "unknown",
            grading_method=q_data.get("grading_method", "exact"),
        )
        q.set_variants([])
        q.set_solution_steps(split_answer_into_steps(answer_text))
        db.add(q)
        db.flush()
        question_index.put(question_text, q)
        fuzzy_keys.append(_fuzzy_key(question_text))
        imported += 1

    db.commit()
    logger.info("Imported %d questions from Excel, skipped %d duplicates", imported, skipped)
    return {"imported": imported, "skipped": skipped}


def get_unclassified_questions(db: Session, teacher_id: int | None = None) -> list[dict]:
    """Get questions that need manual classification (scoped to teacher's visible bank)."""
    from services.bank_scope import visible_filter
    placeholders = ("", "未知", "未指定", "未标注", "未分类", "综合")
    q = db.query(QuestionBank).filter(
        or_(
            func.trim(func.coalesce(QuestionBank.chapter, "")).in_(placeholders),
            func.trim(func.coalesce(QuestionBank.topic, "")).in_(placeholders),
        )
    )
    if teacher_id is not None:
        q = q.filter(visible_filter(teacher_id))
    return [qq.to_dict() for qq in q.all()]


def _build_question_index(db: Session, teacher_id: int | None = None) -> HashMatcher:
    """Build a hash index of existing questions for deduplication (visible scope)."""
    from services.bank_scope import teaching_filter, visible_filter
    index = HashMatcher()
    q = db.query(QuestionBank).filter(teaching_filter())
    if teacher_id is not None:
        q = q.filter(visible_filter(teacher_id))
    for row in q.all():
        index.put(row.question, row)
    return index


def _build_fuzzy_keys(db: Session, teacher_id: int | None = None) -> list[str]:
    """Build a list of fuzzy keys from existing questions (visible scope)."""
    from services.bank_scope import teaching_filter, visible_filter
    q = db.query(QuestionBank.question).filter(teaching_filter())
    if teacher_id is not None:
        q = q.filter(visible_filter(teacher_id))
    return [_fuzzy_key(text) for (text,) in q.all()]


def _fuzzy_key(text: str) -> str:
    """Strip question-number prefix, punctuation, and whitespace for fuzzy comparison."""
    if not text:
        return ""
    s = text.strip()
    # Strip question-number prefix repeatedly (handles "19.（1）" etc.)
    for _ in range(3):
        new_s = _QNUM_PREFIX.sub("", s)
        if new_s == s:
            break
        s = new_s
    s = _PUNCT_RE.sub("", s)
    return s.lower()


def _is_fuzzy_duplicate(text: str, fuzzy_keys: list[str]) -> bool:
    """Return True if `text` is >FUZZY_DEDUP_THRESHOLD similar to any existing key."""
    key = _fuzzy_key(text)
    if not key:
        return False
    for existing in fuzzy_keys:
        if not existing:
            continue
        # Quick length filter — skip pairs with vastly different sizes
        if abs(len(key) - len(existing)) / max(len(key), len(existing)) > (1 - FUZZY_DEDUP_THRESHOLD):
            continue
        if SequenceMatcher(None, key, existing).ratio() >= FUZZY_DEDUP_THRESHOLD:
            return True
    return False


def find_fuzzy_match(
    db: Session, text: str, teacher_id: int, private_only: bool = True
) -> "QuestionBank | None":
    """Return the teacher's existing QuestionBank row whose question fuzzy-matches
    `text` (SequenceMatcher ratio ≥ FUZZY_DEDUP_THRESHOLD), else None.

    用于确认入库时去重：命中则原地更新，避免同一道题被反复批改反复插入、拖脏题库。
    private_only=True 只在该老师的私库里找——只有私有条目老师才有权改写；
    公共题库是管理员维护的，这里绝不改动它。
    """
    key = _fuzzy_key(text)
    if not key:
        return None
    from services.bank_scope import teaching_filter
    q = db.query(QuestionBank).filter(QuestionBank.teacher_id == teacher_id, teaching_filter())
    if private_only:
        q = q.filter(QuestionBank.is_public == False)  # noqa: E712
    best = None
    best_ratio = 0.0
    for row in q.all():
        existing = _fuzzy_key(row.question or "")
        if not existing:
            continue
        # Quick length filter — skip pairs with vastly different sizes
        if abs(len(key) - len(existing)) / max(len(key), len(existing)) > (1 - FUZZY_DEDUP_THRESHOLD):
            continue
        ratio = SequenceMatcher(None, key, existing).ratio()
        if ratio >= FUZZY_DEDUP_THRESHOLD and ratio > best_ratio:
            best, best_ratio = row, ratio
    return best


def split_answer_into_steps(answer: str) -> list[str]:
    """Split an answer string into discrete solution steps.

    Splits on newlines, semicolons, and Chinese/Latin periods. Strips common
    Chinese answer prefixes like "答："、"解："、"(1)" before returning.
    """
    if not answer:
        return []
    # Normalize line breaks, then split on newline / semicolon / period (Chinese & Latin).
    parts = re.split(r'[\n\r;；。]+', str(answer))
    steps: list[str] = []
    for part in parts:
        s = part.strip()
        if not s:
            continue
        # Strip leading prefix repeatedly so "(1)解：x" → "x"
        for _ in range(3):
            new_s = _ANS_PREFIX.sub("", s).strip()
            if new_s == s:
                break
            s = new_s
        if s:
            steps.append(s)
    return steps


def _map_columns(headers: list[str]) -> dict[str, int]:
    """Map Excel column headers to field names."""
    field_aliases = {
        "学段": "education_stage", "education_stage": "education_stage",
        "学科": "subject", "科目": "subject", "subject": "subject",
        "章节": "chapter", "章": "chapter",
        "知识点": "topic", "知识要点": "topic",
        "题目": "question", "题干": "question",
        "答案": "answer", "标准答案": "answer",
        "难度": "difficulty", "难度级别": "difficulty",
        "题型": "question_type", "题目类型": "question_type", "类型": "question_type",
        "批改方式": "grading_method", "批改方法": "grading_method", "评分方式": "grading_method",
    }
    col_map = {}
    for idx, h in enumerate(headers):
        if h in field_aliases:
            col_map[field_aliases[h]] = idx
    return col_map


