"""chat 业务处理 — 6 个只读 handle 函数"""

import logging
import os

from sqlalchemy.orm import Session

from models import Student, StudentProfile, GradingResult, QuestionBank, ExamScore
from models_user import User
from services.analyzer import _call_llm
from services import class_analytics
from services.chat_utils import resolve_time_range, student_class_filter, student_class_label, find_student_by_name
from services.chat_intent import SECRETARY_PERSONA, SYSTEM_KNOWLEDGE
from services.grading_contract import VALID_GRADING_CATEGORIES
from services.scoring_compat import display_metric
from services.student_service import student_to_dict
from services.practice import filter_bank_stage, filter_teaching_stage
from services.teaching_context import current_teaching_context, stage_prompt
from config import FAILED_DIR

logger = logging.getLogger(__name__)


def _current_stage_records(records):
    stage = current_teaching_context().get("education_stage")
    if not stage:
        return records
    return [record for record in records if getattr(record, "education_stage", None) == stage]


def _current_stage_weaknesses(rows):
    stage = current_teaching_context().get("education_stage")
    return [row for row in rows if not stage or row.get("education_stage") == stage]


def _profile_metric_text(profile: StudentProfile) -> str:
    metric = display_metric(profile)
    if metric["kind"] == "points":
        value = metric["value"]
        maximum = metric["maximum"]
        if value is None:
            return "成绩待复核"
        return f"得分{value:g}/{maximum:g}分" if maximum else f"得分{value:g}分"
    rate = metric["value"]
    return f"正确率{rate:.0%}" if rate is not None else "正确率待复核"


# ══════════════════════════════════════════════════════════════════════════════
# 导航
# ══════════════════════════════════════════════════════════════════════════════

def handle_navigate(params: dict) -> dict:
    """导航到指定页面"""
    page = params.get("page", "").lower().replace("_", "-")

    page_map = {
        "home": {"path": "/", "name": "首页"},
        "upload": {"path": "/upload", "name": "批阅"},
        # 快速批阅已合并进批阅页（通过"简单模式"开关），统一导向 /upload
        "quick-grade": {"path": "/upload", "name": "批阅"},
        "students": {"path": "/students", "name": "学生管理"},
        "reports": {"path": "/reports", "name": "成绩报告"},
        "bank": {"path": "/bank", "name": "题库管理"},
        "exercise": {"path": "/exercise", "name": "出题练习"},
        "pending-answers": {"path": "/pending-answers", "name": "待入库"},
        "grade-records": {"path": "/grade-records", "name": "批改记录"},
        "stats": {"path": "/stats", "name": "统计分析"},
        "profile": {"path": "/profile", "name": "个人资料"},
    }

    if page in page_map:
        info = page_map[page]
        return {
            "intent": "navigate",
            "response": f"好的，正在为您打开「{info['name']}」页面",
            "data": {"navigate_to": info["path"]},
        }

    available = "、".join([f"「{v['name']}」" for v in page_map.values()])
    return {
        "intent": "navigate",
        "response": f"我不太确定您要去哪个页面。目前可以导航到：{available}",
        "data": {},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 帮助
# ══════════════════════════════════════════════════════════════════════════════

async def handle_help(
    message: str,
    history: list = None,
    *,
    teacher_id: str = "system",
) -> dict:
    """回答系统使用问题 — 基于系统知识库"""
    system_prompt = f"""{stage_prompt(SECRETARY_PERSONA)}

{SYSTEM_KNOWLEDGE}

老师在询问系统使用方法，请根据知识库回答。
回答要简洁实用，告诉老师具体在哪里操作。"""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        for h in history[-20:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    try:
        response = await _call_llm(
            messages,
            temperature=0.3,
            teacher_id=str(teacher_id or "system"),
        )
        return {"intent": "help", "response": response.strip(), "data": {}}
    except Exception as e:
        logger.warning("Help LLM failed: %s", e)
        return {
            "intent": "help",
            "response": "抱歉，我暂时无法回答。您可以查看左侧菜单，各功能都有对应的页面入口。",
            "data": {},
        }


# ══════════════════════════════════════════════════════════════════════════════
# 通用对话
# ══════════════════════════════════════════════════════════════════════════════

async def handle_chat(message: str, history: list, db: Session, user: User) -> dict:
    """智能对话 — 注入系统知识的秘书模式，支持上下文"""
    student_count = db.query(Student).filter(Student.teacher_id == user.id).count()
    recent_grades = filter_teaching_stage(db.query(StudentProfile), StudentProfile).join(Student).filter(
        Student.teacher_id == user.id,
        StudentProfile.analytics_included.is_(True),
    ).order_by(StudentProfile.created_at.desc()).limit(5).all()
    recent_grades = _current_stage_records(recent_grades)

    recent_summary = ""
    if recent_grades:
        total_q = sum(p.total_questions or 0 for p in recent_grades)
        total_c = sum(p.correct_count or 0 for p in recent_grades)
        rate = round(total_c / total_q * 100, 1) if total_q > 0 else 0
        recent_summary = f"最近5次批改：共{total_q}题，正确率{rate}%"

    from models_rate_limit import TeacherClass
    classes = db.query(TeacherClass).filter(
        TeacherClass.teacher_id == user.id,
        TeacherClass.deleted_at.is_(None)
    ).all()
    class_info = f"已创建{len(classes)}个班级" if classes else "暂未创建班级"

    user_context = f"""
当前老师：{user.name}
学生数量：{student_count}人
{class_info}
{recent_summary}
"""

    system_prompt = f"""{stage_prompt(SECRETARY_PERSONA)}

{SYSTEM_KNOWLEDGE}

## 当前用户数据概况
{user_context}

现在老师对你说话，请根据以上知识回应。记住之前的对话内容，保持连贯。"""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        for h in history[-20:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    try:
        response = await _call_llm(
            messages,
            temperature=0.7,
            teacher_id=str(user.id),
        )
        return {"intent": "chat", "response": response.strip(), "data": {}}
    except Exception as e:
        logger.warning("Chat LLM failed: %s", e)
        return {"intent": "chat", "response": "抱歉，我刚才走神了，能再说一遍吗？", "data": {}}


# ══════════════════════════════════════════════════════════════════════════════
# 学生查询
# ══════════════════════════════════════════════════════════════════════════════

async def handle_query_student(params: dict, db: Session, user: User) -> dict:
    student_name = params.get("student_name", "")
    if not student_name:
        return {"intent": "query_student", "response": "请告诉我学生姓名。", "data": {}}

    student = find_student_by_name(db, user.id, student_name)
    if not student:
        return {"intent": "query_student", "response": f"未找到学生「{student_name}」", "data": {}}

    profiles = (
        filter_teaching_stage(db.query(StudentProfile), StudentProfile)
        .filter(
            StudentProfile.student_id == student.id,
            StudentProfile.analytics_included.is_(True),
        )
        .order_by(StudentProfile.created_at.desc())
        .limit(5).all()
    )
    exams = (
        filter_teaching_stage(db.query(ExamScore), ExamScore)
        .filter(
            ExamScore.teacher_id == user.id,
            ExamScore.student_id == student.id,
            ExamScore.analytics_included.is_(True),
        )
        .order_by(ExamScore.exam_date.desc(), ExamScore.id.desc())
        .limit(5).all()
    )

    cls_label = student_class_label(db, student)
    if not profiles and not exams:
        return {
            "intent": "query_student",
            "response": f"{student.name}（{cls_label}）目前还没有批改记录或月考成绩。",
            "data": student_to_dict(db, student),
        }

    summary_parts = [f"{student.name}（{cls_label}）成绩概况："]
    if exams:
        summary_parts.append(f"最近{len(exams)}次月考：")
        for e in exams:
            date_str = e.exam_date.strftime('%m-%d') if e.exam_date else '-'
            summary_parts.append(f"• {date_str} {e.exam_name or '月考'}：{e.score:g}分")
    if profiles:
        summary_parts.append(f"最近{len(profiles)}次批改记录：")
        for p in profiles:
            date_str = p.created_at.strftime('%m-%d') if p.created_at else '-'
            exam_label = p.exam_name or '日常作业'
            summary_parts.append(
                f"• {date_str} {exam_label}：正确{p.correct_count} 部分{p.partial_count} 错误{p.wrong_count} "
                f"{_profile_metric_text(p)}"
            )

    pending_weak = [
        w for w in _current_stage_weaknesses(class_analytics.weakness_list(db, student.id))
        if w.get("status") == "pending"
    ][:3]
    if pending_weak:
        summary_parts.append("\n主要薄弱点：")
        for w in pending_weak:
            summary_parts.append(f"• {w['topic']}（{w['occurrences']}次）")

    return {
        "intent": "query_student",
        "response": "\n".join(summary_parts),
        "data": student_to_dict(db, student),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 统计总结
# ══════════════════════════════════════════════════════════════════════════════

async def handle_summary(params: dict, db: Session, user: User) -> dict:
    summary_type = str(params.get("type") or "daily").strip().lower()
    if summary_type not in VALID_GRADING_CATEGORIES:
        return {
            "intent": "summary",
            "response": "统计类型仅支持日常作业或考试，请重新指定。",
            "data": {},
        }
    class_name = params.get("class_name")
    start_date, end_date = resolve_time_range(params)

    q = filter_teaching_stage(db.query(StudentProfile), StudentProfile).filter(
        StudentProfile.category == summary_type,
        StudentProfile.analytics_included.is_(True),
    )
    if class_name:
        students = db.query(Student).filter(
            Student.teacher_id == user.id,
            student_class_filter(db, user.id, class_name),
        ).all()
        student_ids = [s.id for s in students]
        if not student_ids:
            return {"intent": "summary", "response": f"班级「{class_name}」没有学生数据。", "data": {}}
        q = q.filter(StudentProfile.student_id.in_(student_ids))
    else:
        my_ids = [r[0] for r in db.query(Student.id).filter(Student.teacher_id == user.id).all()]
        if not my_ids:
            return {"intent": "summary", "response": "你还没有学生批改数据。", "data": {}}
        q = q.filter(StudentProfile.student_id.in_(my_ids))
    q = q.filter(StudentProfile.created_at >= start_date)
    q = q.filter(StudentProfile.created_at <= end_date + "T23:59:59")

    profiles = _current_stage_records(q.all())
    if not profiles:
        type_label = "考试" if summary_type == "exam" else "作业"
        return {"intent": "summary", "response": f"{start_date}至{end_date}期间暂无{type_label}批改数据。", "data": {}}

    total_q = sum(p.total_questions or 0 for p in profiles)
    total_c = sum(p.correct_count or 0 for p in profiles)
    total_p = sum(p.partial_count or 0 for p in profiles)
    total_w = sum(p.wrong_count or 0 for p in profiles)
    total_t = total_q or 1

    error_details = []
    for p in profiles:
        for d in p.get_details():
            if d.get("result") in ("partial", "wrong"):
                error_details.append({
                    "question": d.get("question", ""),
                    "result": d.get("result"),
                    "explanation": d.get("explanation", ""),
                })

    prompt = stage_prompt("你是一位高中数学老师。") + f"""以下是{start_date}至{end_date}的{'考试' if summary_type == 'exam' else '作业'}批改数据：

总题数：{total_q}
正确率：{total_c / total_t:.1%}
部分正确率：{total_p / total_t:.1%}
错误率：{total_w / total_t:.1%}

高频错题：
{chr(10).join(f"- {e['question'][:80]} ({e['result']})" for e in error_details[:10])}

请生成一份教学总结报告，包含：
1. 整体情况概述
2. 高频错题及原因分析
3. 薄弱知识点
4. 改进建议
语言简洁，200-300字。"""

    try:
        report_text = await _call_llm(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            teacher_id=str(user.id),
        )
    except Exception:
        report_text = "LLM生成总结失败，请稍后重试"

    stats_line = (
        f"\n\n📊 统计：总题{total_q} | 正确率{total_c / total_t:.1%} | "
        f"部分正确率{total_p / total_t:.1%} | 错误率{total_w / total_t:.1%}"
    )

    return {
        "intent": "summary",
        "response": report_text + stats_line,
        "data": {
            "total_questions": total_q,
            "correct_rate": round(total_c / total_t, 3),
            "partial_rate": round(total_p / total_t, 3),
            "wrong_rate": round(total_w / total_t, 3),
            "profile_count": len(profiles),
            "start_date": start_date,
            "end_date": end_date,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# 待处理事项
# ══════════════════════════════════════════════════════════════════════════════

def handle_pending(db: Session, user: User) -> dict:
    judging_count = 0
    results = (
        filter_teaching_stage(db.query(GradingResult), GradingResult)
        .join(Student, GradingResult.student_id == Student.id)
        .filter(
            Student.teacher_id == user.id,
            GradingResult.deleted_at.is_(None),
        )
        .order_by(GradingResult.id.desc())
        .limit(200).all()
    )
    for r in results:
        for d in r.get_details():
            if d.get("result") in ("partial", "wrong", "needs_review") and not d.get("confirmed"):
                judging_count += 1

    from services.bank_scope import visible_filter
    auto_count = filter_bank_stage(db.query(QuestionBank)).filter(
        QuestionBank.source == "auto", visible_filter(user.id)
    ).count()

    failed_count = 0
    teacher_failed_dir = os.path.join(FAILED_DIR, str(int(user.id)))
    if os.path.exists(teacher_failed_dir):
        failed_count = len([f for f in os.listdir(teacher_failed_dir) if not f.endswith(".txt")])

    if judging_count == 0 and auto_count == 0 and failed_count == 0:
        return {"intent": "pending", "response": "目前没有待处理事项，一切正常！", "data": {}}

    parts = ["当前待处理事项：\n"]
    if judging_count > 0:
        parts.append(f"• 需确认的题目：{judging_count}道（可在「待处理」面板查看详情）")
    if auto_count > 0:
        parts.append(f"• 待确认的自动入库答案：{auto_count}条")
    if failed_count > 0:
        parts.append(f"• 导入失败的试卷：{failed_count}份（可在「待处理」面板重新导入）")

    parts.append("\n请前往「待处理」面板进行处理。")

    return {
        "intent": "pending",
        "response": "\n".join(parts),
        "data": {
            "judging_count": judging_count,
            "auto_answer_count": auto_count,
            "failed_import_count": failed_count,
        },
    }
