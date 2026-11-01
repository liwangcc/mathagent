"""班级学情聚合引擎 — knowledge_events 之上的成长指标计算（坑2）。

知识点事件负责历史统计，WrongQuestionCycle 负责当前是否仍待纠正；
weaknesses 表仅供旧页面兼容。
供班级表格 / 学生详情 / 报告调用。无副作用，纯查询。

口径（设计契约 六）:
- 纠错率 = 已纠正原题身份数 / 曾经错过的原题身份数
- 错题率 = 最近 N 次批改窗口内 wrong/partial/blank 事件 / 窗口内总事件
- 薄弱点数 = 当前未纠正的知识点个数
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from models import (
    KnowledgeEvent,
    QuestionBank,
    ExamScore,
    Student,
    StudentProfile,
    WeaknessOverride,
    GradingResult,
)
from services.scoring_compat import record_correct_rate
from services.student_service import student_no_sort_key
from models_rate_limit import TeacherClass
from models_correction import QuestionAttemptEvent, StudentObservationPeriod, WrongQuestionCycle
from services.correction_tracking import (
    correction_rate as question_correction_rate,
    question_knowledge,
    wrong_question_summary,
)

logger = logging.getLogger(__name__)


def report_scope_label(education_stage=None) -> str:
    return {None: "全部历史（含未标学段资料）", "": "历史未标学段资料", "legacy": "历史未标学段资料",
            "primary": "小学数学", "junior": "初中数学", "senior": "高中数学"}[education_stage]


def filter_education_stage(query, model, education_stage=None):
    """None selects all history; empty/legacy explicitly selects unlabeled rows."""
    from services.teaching_context import normalize_education_stage
    if education_stage is None:
        return query
    stage = "" if education_stage == "legacy" else normalize_education_stage(education_stage, allow_legacy=True)
    return query.filter(model.education_stage == stage)


def row_stage_metadata(row) -> dict:
    read = row.get if isinstance(row, dict) else lambda name, default=None: getattr(row, name, default)
    return {"education_stage": read("education_stage", "") or "",
            "catalog_version": read("catalog_version", "") or ""}


def _stage_identity(row) -> tuple[str, str]:
    meta = row_stage_metadata(row)
    return meta["education_stage"], meta["catalog_version"]


def _scoped_rows(rows, education_stage=None):
    from services.teaching_context import normalize_education_stage
    if education_stage is None:
        return rows
    stage = "" if education_stage == "legacy" else normalize_education_stage(education_stage, allow_legacy=True)
    return [row for row in rows if row_stage_metadata(row)["education_stage"] == stage]

# 兼容没有原题周期的旧事件；一次做对即可结束当前薄弱周期。
MASTERY_STREAK = 1
# 错题率滑动窗口：最近 N 次批改
WRONG_RATE_WINDOW = 3

def _pct_str(v: float) -> str:
    return f"{v * 100:.0f}%"


def _compute_risk(
    wrong_rate: float,
    correction_rate: float,
    weakness_count: int,
    *,
    has_records: bool = True,
    has_mistakes: bool = True,
) -> tuple[str, str]:
    """学生风险等级与原因（替代前端临时规则）。

    规则与前端 ClassTable.riskMeta 一致，口径后端化：
    - no_data：尚无已归档批阅记录 → 暂无数据，不参与风险判断
    - attention：错题率≥35% 或薄弱点≥3 → 需重点关注
    - watch：有薄弱点 或 曾出错且纠错率<40% → 需持续跟进
    - stable：其余 → 表现良好

    纠错率只对曾经出错的学生有意义；从未出错的学生不会因"纠错率0%"被标记。
    """
    if not has_records:
        return "no_data", "暂无已归档的批阅记录"
    if wrong_rate >= 0.35 or weakness_count >= 3:
        reasons = []
        if wrong_rate >= 0.35:
            reasons.append(f"错题率{_pct_str(wrong_rate)}偏高")
        if weakness_count >= 3:
            reasons.append(f"未纠正薄弱点{weakness_count}个")
        return "attention", "；".join(reasons)
    low_correction = has_mistakes and correction_rate < 0.4
    if weakness_count > 0 or low_correction:
        reasons = []
        if weakness_count > 0:
            reasons.append(f"存在{weakness_count}个薄弱点")
        if low_correction:
            reasons.append(f"纠错率{_pct_str(correction_rate)}偏低")
        return "watch", "；".join(reasons) if reasons else "需持续关注"
    return "stable", "各项指标正常"


def _exam_rank_delta(db: Session, student_id: int, class_id: int | None, education_stage=None) -> int | None:
    """名次变化（最近一次月考 vs 前一次）。正数=进步（名次上升），null=无足够数据。

    同分并列（dense rank）：排名=1+严格高于自己的人数。
    """
    if not class_id:
        return None
    tl = exam_timeline(db, student_id, education_stage)
    if len(tl) < 2:
        return None
    if _stage_identity(tl[-1]) != _stage_identity(tl[-2]):
        return None
    latest_name = tl[-1]["exam_name"]
    prev_name = tl[-2]["exam_name"]
    if not latest_name or not prev_name or latest_name == prev_name:
        return None
    all_scores = (
        db.query(ExamScore)
        .filter(ExamScore.class_id == class_id,
                ExamScore.exam_name.in_([latest_name, prev_name]),
                ExamScore.analytics_included.is_(True))
        .all()
    )
    all_scores = [row for row in all_scores if _stage_identity(row) == _stage_identity(tl[-1])]
    latest_scores = {s.student_id: s.score for s in all_scores if s.exam_name == latest_name}
    prev_scores = {s.student_id: s.score for s in all_scores if s.exam_name == prev_name}
    if student_id not in latest_scores or student_id not in prev_scores:
        return None
    my_latest = latest_scores[student_id]
    my_prev = prev_scores[student_id]
    latest_rank = 1 + sum(1 for v in latest_scores.values() if v > my_latest)
    prev_rank = 1 + sum(1 for v in prev_scores.values() if v > my_prev)
    return prev_rank - latest_rank  # 正=进步


def _teaching_advice(chapter: str, wrong_students: int, total_students: int) -> tuple[str, str]:
    """讲评备课建议 + 优先级。

    - teaching_suggestion：仅按章名和薄弱人数生成知识点讲评建议。
    - priority：high > medium > low，按薄弱人数占比分段。
    """
    pct = wrong_students / max(total_students, 1)
    if pct >= 0.4:
        priority = "high"
    elif pct >= 0.15:
        priority = "medium"
    else:
        priority = "low"
    priority_map = {"high": "优先讲评", "medium": "适时讲评", "low": "个别辅导"}
    suggestion = (
        f"{priority_map[priority]}：{chapter}，{wrong_students}人薄弱，"
        "建议按知识点集中讲解并安排同类巩固"
    )
    return suggestion, priority


def _topic_key(
    ev: KnowledgeEvent,
    question_bank_knowledge: dict[int, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """知识点身份 = (chapter, topic)。topic 为空归 '未知'。"""
    if question_bank_knowledge is not None:
        return _event_knowledge(ev, question_bank_knowledge)
    return (ev.chapter or "", ev.topic or "未知")


def _events_for_student(db: Session, student_id: int, education_stage=None) -> list[KnowledgeEvent]:
    """取某学生全部事件，按时间升序（掌握判定依赖顺序）。"""
    query = (
        db.query(KnowledgeEvent)
        .filter(
            KnowledgeEvent.student_id == student_id,
            KnowledgeEvent.analytics_included.is_(True),
        )
    )
    return filter_education_stage(query, KnowledgeEvent, education_stage).order_by(
        KnowledgeEvent.created_at.asc(), KnowledgeEvent.id.asc()).all()


def _topic_mastery(
    events: list[KnowledgeEvent],
    question_bank_knowledge: dict[int, tuple[str, str]] | None = None,
) -> dict:
    """按知识点折叠学生事件，判定每个知识点的掌握状态。

    返回 {(chapter,topic): {chapter, topic, ever_weak, corrected, occurrences,
                            first_seen, last_seen}}
    兼容判定：曾出现 wrong/partial/blank，且其后做对一次 → corrected。
    """
    by_topic: dict[tuple, list[KnowledgeEvent]] = defaultdict(list)
    for ev in events:
        by_topic[(*_topic_key(ev, question_bank_knowledge), *_stage_identity(ev))].append(ev)

    out = {}
    for key, evs in by_topic.items():
        ever_weak = False
        streak = 0          # 末尾连续做对计数
        corrected = False
        for ev in evs:      # 已按时间升序
            if ev.result in ("wrong", "partial", "blank"):
                ever_weak = True
                streak = 0
                corrected = False
            elif ev.result == "correct":
                streak += 1
                if ever_weak and streak >= MASTERY_STREAK:
                    corrected = True
            # needs_review 不影响掌握判定
        out[key] = {
            "chapter": key[0],
            "topic": key[1],
            "education_stage": key[2],
            "catalog_version": key[3],
            "ever_weak": ever_weak,
            "corrected": corrected,
            "occurrences": sum(1 for e in evs if e.result in ("wrong", "partial", "blank")),
            "first_seen": evs[0].created_at,
            "last_seen": evs[-1].created_at,
        }
    return out


def _topic_key_str(chapter: str, topic: str, education_stage: str = "") -> str:
    """知识点 override 的字符串键。"""
    key = f"{chapter or ''}||{topic or '未知'}"
    return f"{education_stage}||{key}" if education_stage else key


def _overrides(db: Session, student_id: int) -> dict:
    """该学生的手动修正 {topic_key: status}。"""
    return {
        o.topic_key: o.status
        for o in db.query(WeaknessOverride).filter(WeaknessOverride.student_id == student_id).all()
    }


def _cycle_topic_statuses(db: Session, student_id: int, education_stage=None) -> dict[tuple, list[str]]:
    """Map original-question cycles back to the knowledge point they represent."""
    query = db.query(WrongQuestionCycle, QuestionAttemptEvent, GradingResult).join(
        QuestionAttemptEvent,
        QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
    ).outerjoin(GradingResult, GradingResult.id == QuestionAttemptEvent.grading_result_id).filter(
        WrongQuestionCycle.student_id == student_id,
        WrongQuestionCycle.status != "voided",
    )
    rows = filter_education_stage(query, GradingResult, education_stage).all()
    statuses: dict[tuple[str, str], list[str]] = defaultdict(list)
    for cycle, attempt, grading in rows:
        knowledge = question_knowledge(db, attempt)
        topic = str(knowledge.get("topic") or "").strip()
        if not topic or topic in {"未知", "未标注", "综合"}:
            continue
        statuses[(str(knowledge.get("chapter") or ""), topic, *_stage_identity(grading))].append(cycle.status)
    return statuses


def correction_rate(db: Session, student_id: int, education_stage=None) -> float:
    """不同原题纠正率；与知识点掌握率严格分开。"""
    if education_stage is not None:
        cycles = db.query(WrongQuestionCycle).join(
            QuestionAttemptEvent, QuestionAttemptEvent.id == WrongQuestionCycle.opened_attempt_id,
        ).join(GradingResult, GradingResult.id == QuestionAttemptEvent.grading_result_id).filter(
            WrongQuestionCycle.student_id == student_id,
            WrongQuestionCycle.status != "voided",
            QuestionAttemptEvent.analytics_included.is_(True),
        )
        scoped_cycles = filter_education_stage(cycles, GradingResult, education_stage).all()
        if scoped_cycles:
            identities = {cycle.question_identity for cycle in scoped_cycles}
            still_open = {cycle.question_identity for cycle in scoped_cycles if cycle.status == "pending"}
            return round(len(identities - still_open) / len(identities), 4)
    rate = question_correction_rate(db, student_id, teaching_context={}) if education_stage is None else 0.0
    has_question_history = db.query(WrongQuestionCycle.id).filter(
        WrongQuestionCycle.student_id == student_id,
    ).first()
    if has_question_history and education_stage is None:
        return rate
    # Compatibility for untouched legacy/test rows. New archived results always
    # create question cycles, so production metrics use the question contract.
    events = _events_for_student(db, student_id, education_stage)
    mastery = _topic_mastery(events, _question_bank_knowledge(db, events))
    overrides = _overrides(db, student_id)
    weak = [item for item in mastery.values() if item["ever_weak"]]
    if not weak:
        return 0.0
    corrected = sum(
        1 for item in weak
        if (
            overrides.get(_topic_key_str(item["chapter"], item["topic"], item["education_stage"])) == "corrected"
            if overrides.get(_topic_key_str(item["chapter"], item["topic"], item["education_stage"]))
            else item["corrected"]
        )
    )
    return round(corrected / len(weak), 4)


def wrong_rate(db: Session, student_id: int, window: int = WRONG_RATE_WINDOW, *, education_stage=None) -> float:
    """错题率 = 最近 window 次批改的错误/部分正确/空白事件占比。"""
    events = _events_for_student(db, student_id, education_stage)
    if not events:
        return 0.0
    # 用 grading_result_id 划分批次；None 各自成批（按事件 id 区分）
    batches, seen = [], set()
    for ev in reversed(events):  # 倒序取最近
        bkey = ("g", ev.grading_result_id) if ev.grading_result_id else ("q", ev.id)
        if bkey not in seen:
            seen.add(bkey)
            batches.append(bkey)
        if len(batches) >= window:
            break
    batch_set = set(batches)
    windowed = [
        ev for ev in events
        if (("g", ev.grading_result_id) if ev.grading_result_id else ("q", ev.id)) in batch_set
    ]
    if not windowed:
        return 0.0
    wrong = sum(1 for ev in windowed if ev.result in ("wrong", "partial", "blank"))
    return round(wrong / len(windowed), 4)


def weakness_list(db: Session, student_id: int, education_stage=None) -> list[dict]:
    """学生薄弱点（曾薄弱），未纠正在前、按出现次数降序。手动修正优先。"""
    events = _events_for_student(db, student_id, education_stage)
    mastery = _topic_mastery(events, _question_bank_knowledge(db, events))
    ovr = _overrides(db, student_id)
    cycle_statuses = _cycle_topic_statuses(db, student_id, education_stage)
    items = []
    for m in mastery.values():
        if not m["ever_weak"]:
            continue
        st = ovr.get(_topic_key_str(m["chapter"], m["topic"], m["education_stage"]))
        cycle_states = cycle_statuses.get((m["chapter"], m["topic"], m["education_stage"], m["catalog_version"]))
        automatically_corrected = (
            not any(state == "pending" for state in cycle_states)
            if cycle_states
            else m["corrected"]
        )
        is_corr = (st == "corrected") if st else automatically_corrected
        items.append({
            "chapter": m["chapter"],
            "topic": m["topic"],
            "topic_key": _topic_key_str(m["chapter"], m["topic"], m["education_stage"]),
            "education_stage": m["education_stage"],
            "catalog_version": m["catalog_version"],
            "suggested_action": "同类题巩固",
            "status": "corrected" if is_corr else "pending",
            "manual": bool(st),     # 是否被老师手动修正过
            "auto_status": "corrected" if automatically_corrected else "pending",
            "occurrences": m["occurrences"],
            "first_seen": str(m["first_seen"]) if m["first_seen"] else None,
            "last_seen": str(m["last_seen"]) if m["last_seen"] else None,
        })
    items.sort(key=lambda x: (x["status"] == "corrected", -x["occurrences"]))
    return items


def weakness_count(db: Session, student_id: int, education_stage=None) -> int:
    """当前未纠正知识点个数。"""
    return sum(1 for w in weakness_list(db, student_id, education_stage) if w["status"] == "pending")


_UNKNOWN_KNOWLEDGE = {"", "未知", "未指定", "未标注", "未分类", "综合"}


def _known_knowledge(value: object) -> str:
    value = str(value or "").strip()
    return "" if value in _UNKNOWN_KNOWLEDGE else value


def _row_value(row: object, name: str, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def _question_bank_knowledge(db: Session, events: list[object]) -> dict[int, tuple[str, str]]:
    """Load confirmed/suggested bank metadata for legacy unknown events.

    While the durable backfill is still pending, an event may point to a bank
    row that has already been classified by a teacher or the YS worker. Use
    that authoritative metadata immediately so the page need not wait for the
    projection write-back.
    """
    ids = {
        int(_row_value(event, "question_bank_id"))
        for event in events
        if _row_value(event, "question_bank_id") is not None
    }
    if not ids:
        return {}
    rows = db.query(QuestionBank).filter(QuestionBank.id.in_(ids)).all()
    result: dict[int, tuple[str, str]] = {}
    for row in rows:
        chapter = _known_knowledge(row.chapter)
        topic = _known_knowledge(row.topic)
        if chapter and topic:
            result[(int(row.id), *_stage_identity(row))] = (chapter, topic)
    return result


def _event_knowledge(
    event: object,
    question_bank_knowledge: dict[int, tuple[str, str]],
) -> tuple[str, str]:
    chapter = _known_knowledge(_row_value(event, "chapter"))
    topic = _known_knowledge(_row_value(event, "topic"))
    question_bank_id = _row_value(event, "question_bank_id")
    if not (chapter and topic) and question_bank_id:
        chapter, topic = question_bank_knowledge.get((int(question_bank_id), *_stage_identity(event)), ("", ""))
    return (chapter, topic) if chapter and topic else ("未分类", "未分类")


def _knowledge_distribution_for_events(
    events: list[object],
    question_bank_knowledge: dict[int, tuple[str, str]],
) -> list[dict]:
    """Aggregate wrong events by confirmed knowledge point only."""
    buckets: dict[tuple[str, str], dict] = {}
    for event in events:
        if _row_value(event, "result") not in ("wrong", "partial", "blank"):
            continue
        chapter, topic = _event_knowledge(event, question_bank_knowledge)
        key = (chapter, topic, *_stage_identity(event))
        bucket = buckets.setdefault(key, {"count": 0, "students": set()})
        bucket["count"] += 1
        student_id = _row_value(event, "student_id")
        if student_id is not None:
            bucket["students"].add(int(student_id))

    total = sum(int(bucket["count"]) for bucket in buckets.values())
    output = []
    for (chapter, topic, stage, version), bucket in buckets.items():
        count = int(bucket["count"])
        output.append({
            "chapter": chapter,
            "topic": topic,
            "education_stage": stage,
            "catalog_version": version,
            "label": topic,
            "count": count,
            "student_count": len(bucket["students"]),
            "pct": round(count / total, 4) if total else 0.0,
        })
    output.sort(key=lambda item: (-item["count"], item["chapter"], item["topic"]))
    return output


def knowledge_distribution(db: Session, student_id: int, education_stage=None) -> list[dict]:
    """Student wrong-question distribution: knowledge point first, error second."""
    events = _events_for_student(db, student_id, education_stage)
    return _knowledge_distribution_for_events(events, _question_bank_knowledge(db, events))


def knowledge_distribution_for_rows(db: Session, rows: list[object], education_stage=None) -> list[dict]:
    """Aggregate event-like rows, including legacy profile-detail dictionaries."""
    rows = _scoped_rows(rows, education_stage)
    return _knowledge_distribution_for_events(rows, _question_bank_knowledge(db, rows))


def class_knowledge_distribution(
    db: Session,
    student_ids: list[int],
    *,
    grading_result_ids: list[int] | None = None,
    education_stage=None,
) -> list[dict]:
    """Class-scoped knowledge distribution with optional report-period filter."""
    if not student_ids:
        return []
    query = db.query(KnowledgeEvent).filter(
        KnowledgeEvent.student_id.in_(student_ids),
        KnowledgeEvent.analytics_included.is_(True),
    )
    if grading_result_ids is not None:
        if not grading_result_ids:
            return []
        query = query.filter(KnowledgeEvent.grading_result_id.in_(grading_result_ids))
    events = filter_education_stage(query, KnowledgeEvent, education_stage).order_by(KnowledgeEvent.created_at.asc(), KnowledgeEvent.id.asc()).all()
    return _knowledge_distribution_for_events(events, _question_bank_knowledge(db, events))


def error_distribution(db: Session, student_id: int, education_stage=None) -> list[dict]:
    """Compatibility endpoint: semantic error-cause classification is retired."""
    return []


def dashboard_overview(db: Session, teacher_id: int, education_stage=None) -> dict:
    """仪表盘「本周概览」——供首页 4 张统计卡。纯查询，按 teacher 隔离。

    口径：
    - 本周 = 最近 7 天滚动窗口；上周 = 前 7 天，用于环比。
    - 已批改「份数」以 StudentProfile 计（每份卷一行，带 score+created_at）。
    - 平均分 = 本周各份卷得分均值。
    - 薄弱知识点 = 近 30 天 wrong/partial 事件去重后的 (chapter,topic) 个数。
    数据源：StudentProfile（份数/均分）+ KnowledgeEvent（薄弱点）+ TeacherClass（班级）。
    """
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)
    month_ago = now - timedelta(days=30)

    student_ids = [
        s.id for s in db.query(Student.id).filter(Student.teacher_id == teacher_id).all()
    ]

    def _window(profiles, start, end):
        return [p for p in profiles if start <= p.created_at < end] if profiles else []

    profiles = []
    if student_ids:
        profiles = (
            db.query(StudentProfile)
            .filter(
                StudentProfile.student_id.in_(student_ids),
                StudentProfile.created_at >= two_weeks_ago,
                StudentProfile.analytics_included.is_(True),
            )
            .all()
        )
    profiles = _scoped_rows(profiles, education_stage)
    this_week = _window(profiles, week_ago, now)
    prev_week = _window(profiles, two_weeks_ago, week_ago)

    graded = len(this_week)
    graded_prev = len(prev_week)
    graded_delta_pct = round((graded - graded_prev) / graded_prev, 4) if graded_prev else None

    this_rates = [rate for p in this_week if (rate := record_correct_rate(p)) is not None]
    prev_rates = [rate for p in prev_week if (rate := record_correct_rate(p)) is not None]
    avg = round(sum(this_rates) / len(this_rates), 4) if this_rates else 0.0
    avg_prev = round(sum(prev_rates) / len(prev_rates), 4) if prev_rates else None
    avg_delta = round(avg - avg_prev, 4) if avg_prev is not None else None

    class_count = (
        db.query(TeacherClass)
        .filter(TeacherClass.teacher_id == teacher_id, TeacherClass.deleted_at.is_(None))
        .count()
    )
    student_count = len(student_ids)

    weak_topics = set()
    if student_ids:
        rows = (
            db.query(KnowledgeEvent)
            .filter(
                KnowledgeEvent.teacher_id == teacher_id,
                KnowledgeEvent.created_at >= month_ago,
                KnowledgeEvent.result.in_(("wrong", "partial", "blank")),
                KnowledgeEvent.analytics_included.is_(True),
            )
            .all()
        )
        rows = _scoped_rows(rows, education_stage)
        weak_topics = {(row.chapter or "", row.topic or "未知", *_stage_identity(row)) for row in rows}

    return {
        "graded_count": graded,
        "education_stage": education_stage,
        "education_stages": sorted({row_stage_metadata(row)["education_stage"] for row in profiles}),
        "graded_delta_pct": graded_delta_pct,
        "avg_score": avg,
        "avg_score_delta": avg_delta,
        "class_count": class_count,
        "student_count": student_count,
        "weak_topic_count": len(weak_topics),
    }


def exam_timeline(db: Session, student_id: int, education_stage=None) -> list[dict]:
    """月考成绩时间序列（升序），供折线图。"""
    rows = (
        db.query(ExamScore)
        .filter(
            ExamScore.student_id == student_id,
            ExamScore.analytics_included.is_(True),
        )
        .order_by(ExamScore.exam_date.asc(), ExamScore.id.asc())
        .all()
    )
    rows = _scoped_rows(rows, education_stage)
    return [{"exam_name": r.exam_name, "score": r.score, **row_stage_metadata(r),
             "date": str(r.exam_date) if r.exam_date else None} for r in rows]


def latest_exam(db: Session, student_id: int, education_stage=None) -> dict | None:
    """最近一次月考，供班级表格列。无则 None。"""
    tl = exam_timeline(db, student_id, education_stage)
    return tl[-1] if tl else None


def class_table(db: Session, class_id: int, education_stage=None) -> list[dict]:
    """班级表格视图（主界面，规划 3.3）。每个学生一行，含可排序指标。"""
    class_name = db.query(TeacherClass.class_name).filter(
        TeacherClass.id == class_id,
    ).scalar() or ""
    # SQL 的 ORDER BY student_no 是字符串序（1 < 11 < 2），班级表格里看着是乱的，
    # 故在 Python 里按学号自然序重排；id 作为稳定次键。
    students = sorted(
        db.query(Student).filter(Student.class_id == class_id).all(),
        key=lambda student: (student_no_sort_key(student.student_no), student.id),
    )
    rows = []
    active_observation = {
        row.student_id: row
        for row in db.query(StudentObservationPeriod).filter(
            StudentObservationPeriod.class_id == class_id,
            StudentObservationPeriod.ended_at.is_(None),
        ).all()
    }
    recorded_ids: set[int] = set()
    if students:
        recorded_query = db.query(KnowledgeEvent.student_id).filter(
            KnowledgeEvent.student_id.in_([student.id for student in students]),
            KnowledgeEvent.analytics_included.is_(True),
        )
        recorded_ids = {
            row[0]
            for row in filter_education_stage(recorded_query, KnowledgeEvent, education_stage).distinct().all()
        }
    for s in students:
        observed = s.id in active_observation
        if observed:
            rows.append({
                "id": s.id,
                "student_no": s.student_no,
                "exam_no": s.exam_no,
                "name": s.name,
                "phone": s.phone,
                "class_id": class_id,
                "class_name": class_name,
                "wrong_rate": None,
                "correction_rate": None,
                "weakness_count": None,
                "latest_exam_score": None,
                "latest_exam_name": None,
                "risk_level": "observation",
                "risk_reason": "观察模式期间不参与正式风险判断",
                "observation_mode": True,
                "observation_reason": active_observation[s.id].reason,
            })
            continue
        le = latest_exam(db, s.id, education_stage)
        wr = wrong_rate(db, s.id, education_stage=education_stage)
        cr = correction_rate(db, s.id, education_stage)
        weaknesses = weakness_list(db, s.id, education_stage)
        wc = sum(1 for w in weaknesses if w["status"] == "pending")
        has_records = s.id in recorded_ids
        has_mistakes = bool(weaknesses) or cr > 0
        risk_level, risk_reason = _compute_risk(
            wr, cr, wc, has_records=has_records, has_mistakes=has_mistakes,
        )
        rows.append({
            "id": s.id,
            "student_no": s.student_no,
            "exam_no": s.exam_no,
            "name": s.name,
            "phone": s.phone,
            "class_id": class_id,
            "class_name": class_name,
            "wrong_rate": wr,
            "correction_rate": cr,
            "weakness_count": wc,
            "latest_exam_score": le["score"] if le else None,
            "latest_exam_name": le["exam_name"] if le else None,
            "risk_level": risk_level,
            "risk_reason": risk_reason,
            "has_records": has_records,
            "has_mistakes": has_mistakes,
            "observation_mode": s.id in active_observation,
            "observation_reason": active_observation[s.id].reason if s.id in active_observation else "",
        })
    for row in rows:
        row["education_stage"] = education_stage
    return rows


def class_heatmap(db: Session, class_id: int, education_stage=None) -> list[dict]:
    """班级知识点热力图（章 → 知识点 → 学生名）。

    只统计当前"未纠正"的薄弱点，避免已掌握的旧错干扰讲评决策。
    每章返回 teaching_suggestion（讲评备课建议）和 priority（high/medium/low），
    供前端"讲评备课面板"使用。
    """
    students = db.query(Student).filter(Student.class_id == class_id).all()
    observed_ids = {
        row.student_id
        for row in db.query(StudentObservationPeriod).filter(
            StudentObservationPeriod.class_id == class_id,
            StudentObservationPeriod.ended_at.is_(None),
        ).all()
    }
    effective_students = [student for student in students if student.id not in observed_ids]
    total = len(effective_students)
    id_to_name = {s.id: s.name for s in effective_students}

    # chapter -> topic -> set(student_name)
    tree: dict = defaultdict(lambda: defaultdict(set))
    # chapter -> set(student) ; chapter/topic -> set(student) 用于计数
    chapter_students: dict = defaultdict(set)
    topic_students: dict = defaultdict(set)
    for sid, name in id_to_name.items():
        for w in weakness_list(db, sid, education_stage):
            if w["status"] != "pending":
                continue
            ch, tp = (w["chapter"] or "未分类", *_stage_identity(w)), w["topic"]
            tree[ch][tp].add(name)
            chapter_students[ch].add(name)
            topic_students[(ch, tp)].add(name)

    out = []
    for ch, topics in tree.items():
        topic_nodes = []
        for tp, names in topics.items():
            topic_nodes.append({
                "topic": tp,
                "education_stage": ch[1],
                "catalog_version": ch[2],
                "wrong_students": len(topic_students[(ch, tp)]),
                "students": sorted(names),
            })
        topic_nodes.sort(key=lambda x: -x["wrong_students"])
        suggestion, priority = _teaching_advice(
            ch[0], len(chapter_students[ch]), total)
        out.append({
            "chapter": ch[0],
            "education_stage": ch[1],
            "catalog_version": ch[2],
            "wrong_students": len(chapter_students[ch]),
            "topics": topic_nodes,
            "teaching_suggestion": suggestion,
            "priority": priority,
        })
    out.sort(key=lambda x: -x["wrong_students"])
    return out


def student_detail(db: Session, student_id: int, education_stage=None) -> dict:
    """学生详情页聚合（规划 3.6 / 契约 7.3）。

    新增 latest_exam_delta（较上次分数变化）和 latest_exam_rank_delta（较上次名次变化，
    正=进步），替代前端临时计算口径。
    """
    exams = exam_timeline(db, student_id, education_stage)
    delta = None
    if len(exams) >= 2 and _stage_identity(exams[-1]) == _stage_identity(exams[-2]):
        delta = round(exams[-1]["score"] - exams[-2]["score"], 1)

    s = db.query(Student).filter(Student.id == student_id).first()
    class_id = s.class_id if s else None
    rank_delta = _exam_rank_delta(db, student_id, class_id, education_stage)
    wrong_questions = wrong_question_summary(db, student_id, teaching_context={})
    grading_ids = {item.get("grading_result_id") for item in wrong_questions if item.get("grading_result_id")}
    grading_meta = {row.id: row_stage_metadata(row) for row in db.query(GradingResult).filter(GradingResult.id.in_(grading_ids)).all()} if grading_ids else {}
    wrong_questions = [{**item, **grading_meta.get(item.get("grading_result_id"), row_stage_metadata(item))} for item in wrong_questions]
    events = _events_for_student(db, student_id, education_stage)
    graded_count = len({
        ("g", ev.grading_result_id) if ev.grading_result_id else ("q", ev.id)
        for ev in events
    })

    return {
        "education_stage": education_stage,
        "scope_label": report_scope_label(education_stage),
        "weaknesses": weakness_list(db, student_id, education_stage),
        "exam_scores": exams,
        "correction_rate": correction_rate(db, student_id, education_stage),
        "wrong_rate": wrong_rate(db, student_id, education_stage=education_stage),
        "latest_exam_delta": delta,
        "latest_exam_rank_delta": rank_delta,
        "has_records": graded_count > 0,
        "graded_count": graded_count,
        "wrong_questions": [
            item for item in _scoped_rows(wrong_questions, education_stage)
            if item.get("analytics_included", True)
        ],
    }
