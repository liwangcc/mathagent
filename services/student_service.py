"""学生匹配 / 解析服务

从 routers/grade.py 迁入（Batch 2c）。处理上传批改时的学生归属逻辑，
避免在 router 层直接操作 DB。后续 chat.py 的学生查询逻辑可收拢到此。
"""

import re

from sqlalchemy.orm import Session

from models import Student
from models_rate_limit import TeacherClass


def student_class_name(db: Session, student: Student) -> str:
    """Return the canonical class name for a student.

    ``class_id`` is the authoritative relationship.  ``class_name`` remains
    only as a fallback for legacy rows that predate ``TeacherClass``.
    """
    if student.class_id:
        name = db.query(TeacherClass.class_name).filter(
            TeacherClass.id == student.class_id,
            TeacherClass.teacher_id == student.teacher_id,
        ).scalar()
        if name:
            return str(name)
    return str(student.class_name or "").strip()


def student_to_dict(db: Session, student: Student) -> dict:
    """Serialize a student with one shared, relationship-backed class label."""
    payload = student.to_dict()
    payload["class_name"] = student_class_name(db, student)
    return payload


def students_to_dict(db: Session, students: list[Student]) -> list[dict]:
    """Serialize a collection without issuing one class query per student."""
    class_ids = {student.class_id for student in students if student.class_id}
    class_names: dict[tuple[int, int], str] = {}
    if class_ids:
        class_names = {
            (teacher_id, class_id): class_name
            for class_id, class_name, teacher_id in db.query(
                TeacherClass.id, TeacherClass.class_name, TeacherClass.teacher_id,
            ).filter(TeacherClass.id.in_(class_ids)).all()
        }

    items = []
    for student in students:
        payload = student.to_dict()
        payload["class_name"] = (
            class_names.get((student.teacher_id, student.class_id))
            if student.class_id
            else None
        ) or str(student.class_name or "").strip()
        items.append(payload)
    return items


def _digits(raw: str | None) -> str:
    """仅保留数字字符。'  2021 ' → '2021'，'学号：8' → '8'，None → ''。"""
    if not raw:
        return ""
    return re.sub(r"\D", "", str(raw))


def _value(digits: str) -> str:
    """去前导零后的"值"，用于把 8/08/008 视为同一学号。'' 与全零归 '0'。"""
    v = digits.lstrip("0")
    return v or ("0" if digits else "")


def canonical_student_no(raw: str | None) -> str | None:
    """入库规范化学号：仅取数字；个位数补一个前导零（8→08），多位数原样。

    目的：让库里不存裸个位数，即便将来有代码做朴素后缀匹配，个位数 8 也不会
    撞上两位数 18（8 存成 08，"18".endswith("08") 为假）。空 → None（学号可空，
    避免空串撞 uq_students_teacher_student_no 唯一约束）。
    注意：本函数只做"个位补零"，不剥离多位数的前导零（045 保持 045），避免对
    位宽做多余假设——匹配侧 [_no_compatible] 已用去零同值 + 后缀兜底吸收差异。
    """
    d = _digits(raw)
    if not d:
        return None
    if len(d) == 1:
        return "0" + d
    return d


def student_no_sort_key(raw: str | None) -> tuple:
    """学号自然排序键：让 2 排在 11 前面。

    SQL 的 ``ORDER BY student_no`` 是按字符串比的，给出的顺序是 1 < 11 < 2，
    在班级表格里看着就是乱的。这里按"数字段按数值比、非数字段按文本比"来排。

    不能直接 ``int()``：学号可能带前导零（045）或字母（2021A12345），int 会
    丢掉这些标识符甚至抛错。故拆成数字/非数字交替的段序列逐段比较。

    学号是选填的，空学号一律排在最后——真实学号优先占据表格前部，空号不该
    顶掉它们。

    045 与 45 数值相同、段序列也相同，只靠段比较是平局，落到 sorted 的稳定性
    上就变成"顺序取决于查询返回的先后"。表格排序不该这样漂，故用原文做最后
    一道次键，让同值不同写法有确定顺序（045 在 45 前）。
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return (1, (), "")
    return (0, tuple(
        # 段内统一成三元组，避免 int 与 str 直接比较（Python 会 TypeError）。
        (0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
        for part in re.findall(r"\d+|\D+", text)
    ), text)


def _no_compatible(scanned: str, stored: str) -> bool:
    """判断 OCR 学号 scanned 是否与库中学号 stored 兼容（两者均为纯数字串）。

    鲁棒性规则（应对 OCR 漏读前导零 / 学生只写学号后几位）：
      1. 完全相同 → 兼容。
      2. 去前导零后同值（8≡08≡008，045≡45）→ 兼容。
      3. 短串只有 1 位（最易误配）：仅当长串以 "0"+该位 结尾才兼容。
         这样 8 能配 2021000008（末两位 08），但绝不配 18（末两位 18）。
      4. 短串 ≥2 位：短串是长串的后缀即兼容（45 配 12345）。
    注意：本函数只判"是否兼容"，最终是否采用还要过唯一性闸门（见 _pick_unique），
    多人兼容时一律不猜、退回姓名匹配。
    """
    if not scanned or not stored:
        return False
    if scanned == stored:
        return True
    sv = _value(scanned)
    if sv and sv == _value(stored):
        return True
    short, long = (scanned, stored) if len(scanned) <= len(stored) else (stored, scanned)
    if len(short) == 1:
        # 单个数字太易撞车：必须由长串里那位前面的 0 佐证（避免 8→18）
        return long.endswith("0" + short)
    return long.endswith(short)


def _pick_unique(
    candidates: list[Student],
    scanned: str,
) -> Student | None:
    """从兼容候选里挑唯一命中；歧义则返回 None（交由姓名匹配/人工确认）。

    优先级：唯一候选 > 去零同值的精确候选唯一。
    调用前应已按班级收窄候选池（见 match_student_for_upload）——同班内座号唯一，
    故收窄后基本只剩 0/1 个候选；此处的精确优先仅作多重兼容时的额外保险。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # 多人兼容：看"去前导零同值"的精确候选是否唯一
    sv = _value(scanned)
    exact = [c for c in candidates if _value(_digits(c.student_no)) == sv]
    if len(exact) == 1:
        return exact[0]
    return None


def _student_summary(student: Student) -> dict:
    """Return the minimal teacher-facing identity payload for a candidate."""
    return {
        "student_id": student.id,
        "name": student.name,
        "student_no": student.student_no or "",
        "class_id": student.class_id,
    }


def resolve_student_identity(
    db: Session,
    teacher_id: int,
    student_name: str | None,
    student_no: str | None,
    class_id: int | None,
    roster_candidate_names: list[str] | None = None,
    roster_match_status: str | None = None,
) -> dict:
    """Resolve one uploaded page without guessing or creating a Student.

    The selected class is a hard scope.  A unique student number or a unique
    exact name is sufficient to resolve the student.  The other field is an
    optional corroborating signal, not a hard requirement: OCR can misread or
    omit either field, and missing student numbers are valid in the roster.
    """
    raw_name = (student_name or "").strip()
    raw_no = (student_no or "").strip()
    scanned = _digits(raw_no)

    scoped = db.query(Student).filter(Student.teacher_id == teacher_id)
    if class_id is not None:
        scoped = scoped.filter(Student.class_id == class_id)
    class_students = scoped.order_by(Student.id.asc()).all()
    name_matches = [student for student in class_students if raw_name and student.name == raw_name]
    roster_status = (roster_match_status or "").strip()
    roster_candidates: list[Student] = []
    seen_roster_candidate_ids: set[int] = set()
    for candidate_name in roster_candidate_names or []:
        normalized_name = str(candidate_name or "").strip()
        if not normalized_name:
            continue
        for student in class_students:
            if student.name == normalized_name and student.id not in seen_roster_candidate_ids:
                roster_candidates.append(student)
                seen_roster_candidate_ids.add(student.id)

    def pending(reason: str, candidates: list[Student] | None = None) -> dict:
        unique: dict[int, Student] = {}
        for candidate in candidates or []:
            unique[candidate.id] = candidate
        return {
            "status": "needs_confirmation",
            "student_id": None,
            "reason": reason,
            "ocr_name": raw_name,
            "ocr_student_no": raw_no,
            "candidates": [_student_summary(student) for student in unique.values()],
        }

    def roster_candidates_first() -> list[Student]:
        """Keep AI suggestions visible first without hiding the rest of the class."""
        suggested_ids = {student.id for student in roster_candidates}
        return [*roster_candidates, *[
            student for student in class_students if student.id not in suggested_ids
        ]]

    def pending_after_roster_retry(
        default_reason: str,
        fallback_candidates: list[Student],
    ) -> dict:
        if roster_status == "ambiguous":
            return pending("roster_name_ambiguous", roster_candidates_first())
        if roster_status == "low_confidence":
            return pending("roster_name_low_confidence", roster_candidates_first())
        if roster_status == "no_match":
            return pending("roster_name_not_found", class_students)
        return pending(default_reason, fallback_candidates)

    if scanned:
        number_candidates = [
            student for student in class_students
            if _no_compatible(scanned, _digits(student.student_no))
        ]
        picked = _pick_unique(number_candidates, scanned)
        if picked is not None:
            return {
                "status": "matched",
                "student_id": picked.id,
                "reason": "unique_student_no",
                "ocr_name": raw_name,
                "ocr_student_no": raw_no,
                "candidates": [_student_summary(picked)],
            }
        if len(number_candidates) > 1:
            # A unique exact name is enough to recover from an ambiguous or
            # malformed number; never let an optional OCR field block grading.
            if len(name_matches) == 1:
                picked = name_matches[0]
                return {
                    "status": "matched",
                    "student_id": picked.id,
                    "reason": "unique_name",
                    "ocr_name": raw_name,
                    "ocr_student_no": raw_no,
                    "candidates": [_student_summary(picked)],
                }
            return pending("student_no_ambiguous", [*number_candidates, *name_matches])
        if len(name_matches) == 1:
            picked = name_matches[0]
            return {
                "status": "matched",
                "student_id": picked.id,
                "reason": "unique_name",
                "ocr_name": raw_name,
                "ocr_student_no": raw_no,
                "candidates": [_student_summary(picked)],
            }
        return pending_after_roster_retry("student_no_not_found", name_matches)

    if not raw_name:
        return pending_after_roster_retry("missing_identity", class_students)
    if len(name_matches) == 1:
        picked = name_matches[0]
        return {
            "status": "matched",
            "student_id": picked.id,
            "reason": "unique_name",
            "ocr_name": raw_name,
            "ocr_student_no": raw_no,
            "candidates": [_student_summary(picked)],
        }
    if len(name_matches) > 1:
        return pending("name_ambiguous", name_matches)
    return pending_after_roster_retry("name_not_found", class_students)


def match_student_for_upload(
    db: Session,
    teacher_id: int,
    student_name: str | None,
    student_no: str | None,
    class_id: int | None,
) -> tuple[Student | None, bool]:
    """按学号/姓名解析学生，不静默指派跨班同名学生。

    先按学号匹配（同班同名靠学号区分），学号兼容规则见 _no_compatible，
    并经唯一性闸门防误配；学号缺失或无法唯一命中时退回姓名匹配。

    学号即"班级+入学年前缀 + 班内座号(1–60)"：前缀在班内恒定、仅跨班不同，故一切匹配
    先按当前所选班级收窄——同班内座号唯一，命中即无歧义，也杜绝把甲班卷判给乙班同座号
    学生。未选班级时（class_id 为空）退化为按老师全量匹配 + 唯一性闸门。

    返回 (student, name_conflict):
      - (student, False): 唯一命中
      - (None, True): 同名多人且学号无法区分，需人工确认（冲突）
      - (None, False): 无匹配，调用方应新建
    """
    scanned = _digits(student_no)
    name = (student_name or "").strip()

    if scanned:
        # 候选池：优先按当前班级收窄（座号在班内唯一，前缀恒定，天然消歧且防跨班误配）；
        # 未选班级时才退回老师全量。兼容判断在 Python 侧做（去零/后缀），SQL 精确等值覆盖不了。
        q = db.query(Student).filter(
            Student.teacher_id == teacher_id,
            Student.student_no.isnot(None),
            Student.student_no != "",
        )
        if class_id:
            q = q.filter(Student.class_id == class_id)
        pool = q.all()
        candidates = [s for s in pool if _no_compatible(scanned, _digits(s.student_no))]
        picked = _pick_unique(candidates, scanned)
        if picked:
            return picked, False
        # 学号无法唯一命中 → 不猜，退回姓名匹配

    if not name:
        return None, False

    scoped = db.query(Student).filter(
        Student.teacher_id == teacher_id,
        Student.name == name,
    )
    if class_id:
        # 选了班级：姓名匹配也只在班内进行，绝不回退到全老师范围——否则会把本班的卷
        # 静默判给他班同名学生（正是学号前缀按班隔离要防的跨班误配）。
        class_matches = scoped.filter(Student.class_id == class_id).all()
        if len(class_matches) == 1:
            return class_matches[0], False
        if len(class_matches) > 1:
            return None, True
        return None, False

    # 未选班级：退化为老师全量姓名匹配（无班级可依，唯一即命中）。
    all_matches = scoped.all()
    if len(all_matches) == 1:
        return all_matches[0], False
    if len(all_matches) > 1:
        return None, True
    return None, False
