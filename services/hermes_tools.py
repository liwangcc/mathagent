"""MathAgent 工具注册 — 包装为 Hermes tool calling 契约

每个工具 = 一个 registry.register() 调用, handler 内部自己管理 DB session。

注册时机：app.py 启动时调 register_mathagent_tools()。
工具集：
- mathagent_teacher_read：教师本人可见业务数据的只读查询
- mathagent_teacher_search：固定公开搜索端点，只返回标题、链接和摘要
- mathagent：生成文件、导出等动作型能力（教师聊天入口不加载）
"""

import io
import json
import logging
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from services.export_text import normalize_export_text
from services.scoring_compat import display_metric, record_correct_rate

logger = logging.getLogger(__name__)

# ── 懒引用：避免模块 import 时触发 Hermes/DB 初始化 ──
_HERMES_AVAILABLE = False
_DOCUMENT_TOOL_MAX_BYTES = 20 * 1024 * 1024
_DOCUMENT_TOOL_PARSE_SLOT = threading.BoundedSemaphore(1)
_DOCUMENT_TOOL_ADMISSION_TIMEOUT_SECONDS = 1.0
_TEACHER_READ_TOOLSET = "mathagent_teacher_read"
_TEACHER_SEARCH_TOOLSET = "mathagent_teacher_search"
_MATHAGENT_ACTION_TOOLSET = "mathagent"
_PUBLIC_SEARCH_URL = "https://cn.bing.com/search"
_PUBLIC_SEARCH_TIMEOUT_SECONDS = 12.0
_PUBLIC_SEARCH_MAX_QUERY_CHARS = 200
_PRIVATE_SEARCH_MARKERS = (
    "学生姓名",
    "学号",
    "准考证号",
    "手机号",
    "电话号码",
    "家长电话",
    "身份证号",
    "成绩单",
    "个人成绩",
    "试卷照片",
    "原卷",
    "服务器路径",
    "环境变量",
    "密钥",
    "令牌",
)


def register_mathagent_tools() -> None:
    """向 Hermes 全局 registry 注册 MathAgent 工具。在 app.py 启动时调用。"""
    global _HERMES_AVAILABLE
    try:
        from tools.registry import registry  # Hermes 的工具注册表
        _HERMES_AVAILABLE = True
    except ImportError:
        logger.warning("Hermes tools.registry not available — mathagent tools not registered")
        return

    # ═══════════════════════════════════════════════════════════════
    # 1. query_student — 查学生信息/成绩/薄弱点
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_query_student",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_query_student",
            "description": "查询学生的基本信息、Excel导入的月考成绩、最近批改记录和薄弱知识点。不知道学生全名时可以模糊搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "student_name": {"type": "string", "description": "学生姓名（支持模糊匹配）"},
                },
                "required": ["student_name"],
            },
        },
        handler=_query_student,
        emoji="👤",
    )

    # ═══════════════════════════════════════════════════════════════
    # 2. query_summary — 统计汇总
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_query_summary",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_query_summary",
            "description": "生成一段时间内的批改统计汇总，包含正确率、高频错题和教学建议。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "汇总类型：all(全部) / daily(日常作业) / exam(考试)", "default": "all"},
                    "class_name": {"type": "string", "description": "班级名称（可选，不填则统计全部）"},
                    "time_range": {"type": "string", "description": "时间范围：this_week / last_week / this_month / last_month"},
                },
            },
        },
        handler=_query_summary,
        emoji="📊",
    )

    # ═══════════════════════════════════════════════════════════════
    # 3. generate_exercise — 出题
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_generate_exercise",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_generate_exercise",
            "description": "根据章节或知识点引导老师到「出题练习」页面从题库中选题。禁止自创题；题库不足时说明题库暂无足够题目，不能生成新题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "student_name": {"type": "string", "description": "学生姓名（可选，若指定则针对薄弱点出题）"},
                    "question_count": {"type": "integer", "description": "题目数量，默认5", "default": 5},
                    "chapters": {"type": "array", "items": {"type": "string"}, "description": "章节列表，如['函数','几何']"},
                    "topics": {"type": "array", "items": {"type": "string"}, "description": "知识点列表"},
                },
            },
        },
        handler=_generate_exercise,
        emoji="📝",
    )

    # ═══════════════════════════════════════════════════════════════
    # 4. generate_report — 学习报告
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_generate_report",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_generate_report",
            "description": "生成学生的学习报告，包含成绩趋势和薄弱点分析。",
            "parameters": {
                "type": "object",
                "properties": {
                    "student_name": {"type": "string", "description": "学生姓名"},
                    "format": {"type": "string", "description": "报告格式：summary(摘要文本) / detail(详细文本) / docx(Word文档+图表)", "default": "summary"},
                },
                "required": ["student_name"],
            },
        },
        handler=_generate_report,
        emoji="📋",
    )

    # ═══════════════════════════════════════════════════════════════
    # 5. export_error_sheet — 导出错题条
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_export_error_sheet",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_export_error_sheet",
            "description": "导出学生的错题条或按题目汇总的错题本（Word 文档），返回文件下载链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "exam_name": {"type": "string", "description": "考试名称（可选）"},
                    "class_name": {"type": "string", "description": "班级名称（可选）"},
                    "mode": {"type": "string", "description": "导出模式：student(按学生每人一张) / question(按题号汇总)", "default": "student"},
                },
            },
        },
        handler=_export_error_sheet,
        emoji="📄",
    )

    # ═══════════════════════════════════════════════════════════════
    # 6. query_pending — 待入库题目
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_query_pending",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_query_pending",
            "description": "查看待入库的题目列表（批改中遇到题库没有的新题目）。",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_query_pending,
        emoji="⏳",
    )

    # ═══════════════════════════════════════════════════════════════
    # 7. office_generate — 办公文件生成
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_office_generate",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_office_generate",
            "description": "根据文档内容生成 Word/Excel/PPT 文件。当老师要求生成文件、做通知、做表格、做PPT时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档标题"},
                    "format": {"type": "string", "description": "输出格式：word / excel / ppt"},
                    "content": {"type": "string", "description": "文档正文（纯文本或 Markdown，支持 #标题、**加粗**、|表格|、-列表）"},
                },
                "required": ["title", "format", "content"],
            },
        },
        handler=_office_generate,
        emoji="📁",
    )

    # ═══════════════════════════════════════════════════════════════
    # 8. list_classes — 列出班级
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_list_classes",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_list_classes",
            "description": "列出当前老师创建的所有班级及学生数量。",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_list_classes,
        emoji="🏫",
    )

    # ═══════════════════════════════════════════════════════════════
    # 9. list_students — 列出班级学生
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_list_students",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_list_students",
            "description": "列出指定班级的所有学生姓名和学号。当老师问「有哪些学生」「学生名单」「班里几个人」时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string", "description": "班级名称，如'24升学1班'"},
                },
                "required": ["class_name"],
            },
        },
        handler=_list_students,
        emoji="👥",
    )

    # ═══════════════════════════════════════════════════════════════
    # 10. query_question_bank — 查题库
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_query_question_bank",
        toolset=_TEACHER_READ_TOOLSET,
        schema={
            "name": "mathagent_query_question_bank",
            "description": "搜索题库中的题目，或查询当前题库总题数。按章节/知识点/关键词查询，返回匹配题目列表。不要凭记忆回答题库内容，每次都用此工具实时查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词（题目/章节/知识点，支持模糊匹配）"},
                    "chapter": {"type": "string", "description": "章节名称（可选）"},
                    "limit": {"type": "integer", "description": "返回数量上限，默认10", "default": 10},
                },
            },
        },
        handler=_query_question_bank,
        emoji="📚",
    )

    # ═══════════════════════════════════════════════════════════════
    # 11. public_web_search — 公开网页搜索（固定搜索端点，无正文抓取）
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_public_web_search",
        toolset=_TEACHER_SEARCH_TOOLSET,
        schema={
            "name": "mathagent_public_web_search",
            "description": (
                "搜索会随时间变化的公开信息，只返回结果标题、公开链接和摘要。"
                "不得搜索学生姓名、学号、成绩、试卷内容、教师私有数据、"
                "服务器信息或内部网址；MathAgent 使用方法应直接依据系统说明书。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "不含任何教学隐私的公开搜索词"},
                    "limit": {"type": "integer", "description": "返回条数，1-5，默认5", "default": 5},
                },
                "required": ["query"],
            },
        },
        handler=_public_web_search,
        emoji="🔎",
    )

    # ═══════════════════════════════════════════════════════════════
    # 12. export_scores_excel — 导出成绩表
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_export_scores_excel",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_export_scores_excel",
            "description": "导出考试成绩表为 Excel 文件（.xlsx），返回文件下载链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "exam_name": {"type": "string", "description": "考试名称（可选，不填则导出所有）"},
                    "class_name": {"type": "string", "description": "班级名称（可选）"},
                },
            },
        },
        handler=_export_scores_excel,
        emoji="📊",
    )

    # ═══════════════════════════════════════════════════════════════
    # 13. parse_document — 解析文档
    # ═══════════════════════════════════════════════════════════════
    registry.register(
        name="mathagent_parse_document",
        toolset=_MATHAGENT_ACTION_TOOLSET,
        schema={
            "name": "mathagent_parse_document",
            "description": "解析上传的 Word/Excel/PPT/PDF 文件内容，返回结构化文本，供生成文件时参考。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径（服务器 output/ 目录下）"},
                },
            },
        },
        handler=_parse_document_tool,
        emoji="📄",
    )

    logger.info(
        "MathAgent tools registered with Hermes: 6 teacher-read tools, "
        "1 public-search tool, 6 action tools"
    )


# ═══════════════════════════════════════════════════════════════════
# Tool handlers（每个独立管理 DB session + user context）
# ═══════════════════════════════════════════════════════════════════

# Hermes tool handler 签名: handler(params: dict) -> str（Hermes 传位置参数 args_dict）

import contextvars

# 请求级 teacher_id —— Hermes 工具在线程池中执行，不能依赖 FastAPI Depends。
# 用 ContextVar 而非 os.environ：环境变量是进程级全局，并发请求会互相覆盖导致
# 数据泄露（C2）。ContextVar 每个请求 Task 持有独立副本，且 asyncio.to_thread
# 会自动把当前 context 复制进工作线程，工具读取到的始终是本请求的 user_id。
_current_user_id: contextvars.ContextVar[int] = contextvars.ContextVar(
    "mathagent_current_user_id", default=0
)


def set_current_user_id(user_id: int) -> None:
    """在调 Hermes agent 前设置当前请求的 teacher_id。"""
    _current_user_id.set(int(user_id))


def _get_user_id() -> int:
    """获取当前请求的 teacher_id。chat.py / vision_office.py 在调 Hermes 前设置。"""
    return _current_user_id.get()


def _resolve_time_range(params: dict) -> tuple[str, str]:
    today = date.today()
    tr = ((params or {}).get("time_range") or "").strip()
    if tr in ("this_week", "本周", "这周"):
        monday = today - timedelta(days=today.weekday())
        return str(monday), str(today)
    if tr in ("last_week", "上周"):
        lm = today - timedelta(days=today.weekday() + 7)
        return str(lm), str(lm + timedelta(days=6))
    if tr in ("this_month", "本月"):
        return str(today.replace(day=1)), str(today)
    if tr in ("last_month", "上个月"):
        first = today.replace(day=1)
        last_day = first - timedelta(days=1)
        return str(last_day.replace(day=1)), str(last_day)
    return str(today - timedelta(days=7)), str(today)


def _get_db():
    from database import SessionLocal
    return SessionLocal()


def _student_class_filter(db, teacher_id: int, class_name: str):
    """Build a student class filter that supports current class_id and legacy class_name."""
    from models import Student
    from models_rate_limit import TeacherClass
    from sqlalchemy import or_

    cname = (class_name or "").strip()
    class_ids = [
        r[0]
        for r in db.query(TeacherClass.id).filter(
            TeacherClass.teacher_id == teacher_id,
            TeacherClass.class_name == cname,
            TeacherClass.deleted_at.is_(None),
        ).all()
    ]
    filters = [Student.class_name == cname]
    if class_ids:
        filters.insert(0, Student.class_id.in_(class_ids))
    return or_(*filters)


def _student_class_label(db, student) -> str:
    if getattr(student, "class_name", None):
        return student.class_name
    class_id = getattr(student, "class_id", None)
    if class_id:
        from models_rate_limit import TeacherClass
        name = db.query(TeacherClass.class_name).filter(TeacherClass.id == class_id).scalar()
        if name:
            return name
    return "未分班"


# ── Handler implementations ──

def _teaching_read_scope():
    from services.teaching_context import current_teaching_context
    context = current_teaching_context()
    stage = context.get("education_stage", "")
    version = context.get("curriculum_version", "")
    label = {"primary": "小学数学", "junior": "初中数学", "senior": "高中数学"}.get(stage, "历史未标学段资料")
    return stage, version, label


def _scope_teaching_query(query, model):
    stage, version, _ = _teaching_read_scope()
    return query.filter(model.education_stage == stage, model.catalog_version == version)

def _query_student(params: dict, **kwargs) -> str:
    student_name = (params or {}).get("student_name", "")
    if not student_name:
        return "请提供学生姓名。"
    db = _get_db()
    try:
        from models import Student, StudentProfile, ExamScore
        from services.security import escape_like
        from services import class_analytics
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份，请稍后重试。"
        safe = escape_like(student_name)
        s = db.query(Student).filter(
            Student.teacher_id == uid,
            Student.name.like(f"%{safe}%", escape='\\'),
        ).first()
        if not s:
            return f"未找到学生「{student_name}」。"
        profiles = _scope_teaching_query(db.query(StudentProfile).filter(
            StudentProfile.student_id == s.id,
            StudentProfile.analytics_included.is_(True),
        ), StudentProfile).order_by(StudentProfile.created_at.desc()).limit(10).all()
        exams = _scope_teaching_query(db.query(ExamScore).filter(
            ExamScore.teacher_id == uid,
            ExamScore.student_id == s.id,
            ExamScore.analytics_included.is_(True),
        ), ExamScore).order_by(ExamScore.exam_date.desc(), ExamScore.id.desc()).limit(10).all()
        class_label = _student_class_label(db, s)
        if not profiles and not exams:
            return f"{s.name}（{class_label}）目前还没有批改记录或月考成绩。"
        stage, version, scope_label = _teaching_read_scope()
        lines = [f"统计范围：{scope_label}。", f"{s.name}（{class_label}）成绩概况："]
        if exams:
            lines.append(f"最近{len(exams)}次月考：")
            for e in exams:
                ds = e.exam_date.strftime('%m-%d') if e.exam_date else '-'
                lines.append(f"• {ds} {e.exam_name or '月考'}：{e.score:g}分")
        if profiles:
            lines.append(f"最近{len(profiles)}次批改：")
            for p in profiles:
                ds = p.created_at.strftime('%m-%d') if p.created_at else '-'
                metric = display_metric(p)
                if metric["kind"] == "points" and metric["value"] is not None:
                    metric_text = (
                        f"得分{metric['value']:g}/{metric['maximum']:g}分"
                        if metric["maximum"] else f"得分{metric['value']:g}分"
                    )
                elif metric["value"] is not None:
                    metric_text = f"正确率{metric['value']:.0%}"
                else:
                    metric_text = "待复核"
                lines.append(f"• {ds} {p.exam_name or '日常作业'}：正确{p.correct_count} 部分{p.partial_count} 错误{p.wrong_count} {metric_text}")
        weak = [
            w for w in class_analytics.weakness_list(db, s.id, stage)
            if w["status"] == "pending" and w.get("catalog_version", "") == version
        ][:3]
        if weak:
            lines.append("\n薄弱点：")
            for w in weak:
                lines.append(f"• {w['topic']}（{w['occurrences']}次）")
        return "\n".join(lines)
    finally:
        db.close()


def _query_summary(params: dict, **kwargs) -> str:
    db = _get_db()
    try:
        from models import Student, StudentProfile, ExamScore
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        stype = (params or {}).get("type", "all")
        cname = (params or {}).get("class_name")
        sd, ed = _resolve_time_range(params)

        # 查学生 ID 列表
        sq = db.query(Student.id).filter(Student.teacher_id == uid)
        if cname:
            sq = sq.filter(_student_class_filter(db, uid, cname))
        sids = [r[0] for r in sq.all()]
        if not sids:
            return f"班级「{cname}」没有学生数据。" if cname else "你还没有学生数据。"

        parts = []

        # 1. 查 exam_scores 月考成绩（exam 或 all 时）
        if stype in ("all", "exam"):
            exam_q = db.query(ExamScore).filter(
                ExamScore.teacher_id == uid,
                ExamScore.student_id.in_(sids),
                ExamScore.exam_date >= sd,
                ExamScore.exam_date <= ed,
                ExamScore.analytics_included.is_(True),
            )
            exams = _scope_teaching_query(exam_q, ExamScore).all()
            if exams:
                scores = [e.score for e in exams if e.score is not None]
                avg = sum(scores) / len(scores) if scores else 0
                hi = max(scores) if scores else 0
                lo = min(scores) if scores else 0
                # 按学生汇总
                by_student = {}
                for e in exams:
                    sname = db.query(Student.name).filter(Student.id == e.student_id).scalar() or "?"
                    by_student.setdefault(sname, []).append(e.score or 0)
                top = sorted(by_student.items(), key=lambda x: max(x[1]), reverse=True)
                top_str = "、".join(f"{n}({max(s):.0f}分)" for n, s in top[:3])
                parts.append(
                    f"📊 月考成绩（{len(exams)}条记录）：\n"
                    f"平均{avg:.1f}分 | 最高{hi:.0f}分 | 最低{lo:.0f}分\n"
                    f"前三名：{top_str}"
                )

        # 2. 查 StudentProfile 批改数据（daily/exam/all 时）
        q = db.query(StudentProfile).filter(
            StudentProfile.student_id.in_(sids),
            StudentProfile.analytics_included.is_(True),
        )
        if stype and stype not in ("all",):
            q = q.filter(StudentProfile.category == stype)
        q = q.filter(StudentProfile.created_at >= sd, StudentProfile.created_at <= ed + "T23:59:59")
        profiles = _scope_teaching_query(q, StudentProfile).all()
        if profiles:
            tq = sum(p.total_questions or 0 for p in profiles)
            tc = sum(p.correct_count or 0 for p in profiles)
            tp = sum(p.partial_count or 0 for p in profiles)
            tw = sum(p.wrong_count or 0 for p in profiles)
            tt = tq or 1
            parts.append(
                f"📊 批改汇总（{len(profiles)}份试卷）：\n"
                f"共批改{tq}题 | 正确率{tc/tt:.1%} | "
                f"部分正确率{tp/tt:.1%} | 错误率{tw/tt:.1%}"
            )

        if not parts:
            return f"{sd}至{ed}期间暂无任何成绩或批改数据。"
        return f"统计范围：{_teaching_read_scope()[2]}。\n\n" + "\n\n".join(parts)
    finally:
        db.close()


def _generate_exercise(params: dict, **kwargs) -> str:
    # 简化版：返回指引（出题逻辑较复杂，避免重复实现）
    chapters = (params or {}).get("chapters", [])
    topics = (params or {}).get("topics", [])
    count = (params or {}).get("question_count", 5)
    student = (params or {}).get("student_name", "")
    target = student or ("、".join(chapters or topics or ["综合"]))
    return (
        f"出题指引：为「{target}」从题库选择{count}道练习题。\n"
        f"请老师前往「出题练习」页面（/exercise）操作；系统只从题库选题，题库不足时不会自动编题。"
    )



import io
import os
_CN_FONTS = ["SimHei", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Noto Sans CJK SC"]


def _matplotlib_runtime():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.font_manager as fm
        import matplotlib.pyplot as plt
    except ImportError:
        return None, None

    font_prop = fm.FontProperties()
    for family in _CN_FONTS:
        try:
            fm.findfont(
                fm.FontProperties(family=family),
                fallback_to_default=False,
            )
            font_prop = fm.FontProperties(family=family)
            break
        except ValueError:
            continue
    return plt, font_prop


def _chart_score_trend(profiles):
    plt, font_prop = _matplotlib_runtime()
    if plt is None:
        return None
    items = list(reversed(profiles[:12]))
    dates = [p.created_at.strftime("%m-%d") for p in items]
    scores = [(record_correct_rate(p) or 0) * 100 for p in items]
    fig, ax = plt.subplots(figsize=(7, 3))
    ax.plot(dates, scores, "o-", color="#4A90D9", linewidth=2, markersize=6)
    ax.fill_between(range(len(dates)), scores, alpha=0.15, color="#4A90D9")
    ax.set_ylim(0, 105)
    ax.set_ylabel("正确率(%)", fontproperties=font_prop)
    ax.set_title("成绩趋势", fontproperties=font_prop, fontsize=14)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=30, ha="right", fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    for i, v in enumerate(scores):
        ax.annotate(f"{v:.0f}%", (i, v), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _chart_weakness_bar(weak_list):
    if not weak_list:
        return None
    plt, font_prop = _matplotlib_runtime()
    if plt is None:
        return None
    topics = [w[0][:8] for w in weak_list]
    counts = [w[1] for w in weak_list]
    colors = ["#E74C3C" if c >= 3 else "#F39C12" if c >= 2 else "#3498DB" for c in counts]
    fig, ax = plt.subplots(figsize=(6, 3))
    bars = ax.barh(range(len(topics)), counts, color=colors, height=0.6)
    ax.set_yticks(range(len(topics)))
    ax.set_yticklabels(topics, fontproperties=font_prop, fontsize=10)
    ax.set_xlabel("出错次数", fontproperties=font_prop)
    ax.set_title("薄弱知识点", fontproperties=font_prop, fontsize=14)
    ax.invert_yaxis()
    for bar, c in zip(bars, counts):
        ax.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height() / 2,
                str(c), va="center", fontsize=10)
    ax.grid(axis="x", alpha=0.3)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_report(params: dict, **kwargs) -> str:
    student_name = (params or {}).get("student_name", "")
    fmt = (params or {}).get("format", "summary")
    if not student_name:
        return "请提供学生姓名。"
    db = _get_db()
    try:
        from models import Student, StudentProfile
        from services.security import escape_like
        from services import class_analytics
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        safe = escape_like(student_name)
        s = db.query(Student).filter(
            Student.teacher_id == uid, Student.name.like(f"%{safe}%", escape='\\')).first()
        if not s:
            return f"未找到学生「{student_name}」。"
        profiles = _scope_teaching_query(db.query(StudentProfile).filter(
            StudentProfile.student_id == s.id,
            StudentProfile.analytics_included.is_(True),
        ), StudentProfile).order_by(StudentProfile.created_at.desc()).all()
        if not profiles:
            return f"{s.name}暂无批改记录。"
        tq = sum(p.total_questions or 0 for p in profiles)
        tc = sum(p.correct_count or 0 for p in profiles)
        # (topic, occurrences) 元组，保持下游图表/文档代码兼容
        weak = [
            (w["topic"], w["occurrences"])
            for w in class_analytics.weakness_list(db, s.id, _teaching_read_scope()[0])
            if w["status"] == "pending" and w.get("catalog_version", "") == _teaching_read_scope()[1]
        ][:5]

        # ── 文本模式 ──
        if fmt != "docx":
            if tq:
                trends = [
                    f"{p.created_at.strftime('%m-%d')}: {(record_correct_rate(p) or 0):.0%}"
                    for p in profiles[:8]
                ]
                lines = [
                    f"统计范围：{_teaching_read_scope()[2]}。",
                    f"{s.name}（{_student_class_label(db, s)}）学习报告：",
                    f"总批改{tq}题，正确率{tc/tq:.1%}，共{len(profiles)}次",
                    "最近趋势：" + " → ".join(reversed(trends)),
                ]
            else:
                lines = [f"{s.name}暂无有效数据。"]
            if weak:
                lines.append("薄弱点TOP5：")
                for w, c in weak:
                    lines.append(f"• {w}（{c}次）")
            return "\n".join(lines)

        # ── Word 文档模式 ──
        if not tq:
            return f"{s.name}暂无有效数据，无法生成报告文档。"
        try:
            from docx import Document
            from docx.shared import Inches, Pt
            from docx.enum.text import WD_ALIGN_PARAGRAPH
        except ImportError:
            return "python-docx 未安装，无法生成 Word 文档。"

        trend_png = _chart_score_trend(profiles)
        weak_png = _chart_weakness_bar(weak) if weak else None

        doc = Document()
        doc.styles["Normal"].font.size = Pt(11)
        title = doc.add_heading(f"{s.name} 学习报告", level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cls_label = _student_class_label(db, s)
        doc.add_paragraph(
            f"范围：{_teaching_read_scope()[2]}    班级：{cls_label}    批改次数：{len(profiles)}次    "
            f"总题数：{tq}题    正确率：{tc/tq:.1%}")

        doc.add_heading("成绩趋势", level=2)
        if trend_png:
            doc.add_picture(io.BytesIO(trend_png), width=Inches(5.5))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_heading("历次成绩", level=2)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Shading Accent 1"
        hdr = table.rows[0].cells
        hdr[0].text = "日期"
        hdr[1].text = "正确率"
        hdr[2].text = "正确/总题"
        hdr[3].text = "需复核"
        for p in profiles[:15]:
            row = table.add_row().cells
            row[0].text = p.created_at.strftime("%Y-%m-%d")
            row[1].text = f"{(record_correct_rate(p) or 0):.0%}"
            row[2].text = f"{p.correct_count or 0}/{p.total_questions or 0}"
            row[3].text = str(p.needs_review_count or 0)

        if weak:
            doc.add_heading("薄弱知识点", level=2)
            if weak_png:
                doc.add_picture(io.BytesIO(weak_png), width=Inches(4.5))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            for w, c in weak:
                doc.add_paragraph(f"{w}（{c}次）", style="List Bullet")

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        from services.generated_files import register_generated_file, make_display_name
        display_name = make_display_name("学习报告", s.name, "docx")
        rec = register_generated_file(db, uid, buf.read(), display_name)
        db.commit()
        weak_summary = "、".join(w for w, _ in weak[:3]) if weak else "无"
        return (
            f"学习报告已生成：{display_name}。\n"
            f"正确率{tc/tq:.1%}，共{len(profiles)}次批改。\n"
            f"薄弱点：{weak_summary}\n"
            f"下载链接：/api/download/{rec.id}"
        )
    finally:
        db.close()

def _build_error_sheet_docx(results, exam_name: str, mode: str = "student") -> bytes:
    """Build the chat-tool DOCX with the same readable text contract as PDF exports."""
    # Resolve the optional PDF/plotting module only for an actual document
    # request; importing the chat router must remain lightweight.
    from services.error_sheet import _preferred_correct_answer, _preferred_solution_steps
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    doc = Document()
    section = doc.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(18)
    section.bottom_margin = Mm(18)
    section.left_margin = Mm(20)
    section.right_margin = Mm(20)

    style_specs = {
        "Normal": ("宋体", 10.5, False, "333333"),
        "Title": ("微软雅黑", 18, True, "2C3E50"),
        "Heading 1": ("微软雅黑", 14, True, "2980B9"),
        "Heading 2": ("微软雅黑", 11.5, True, "E67E22"),
    }
    for style_name, (font_name, size, bold, color) in style_specs.items():
        style = doc.styles[style_name]
        style.font.name = font_name
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = RGBColor.from_string(color)
        fonts = style._element.get_or_add_rPr().get_or_add_rFonts()
        fonts.set(qn("w:eastAsia"), font_name)
        fonts.set(qn("w:ascii"), font_name)
        fonts.set(qn("w:hAnsi"), font_name)

    entries = []
    for result in results:
        student = getattr(result, "student", None)
        errors = []
        for index, detail in enumerate(result.get_details() or [], 1):
            if not isinstance(detail, dict):
                continue
            status = str(detail.get("result") or "")
            if status not in ("wrong", "partial", "错误", "部分正确"):
                continue
            errors.append({
                "question_no": str(detail.get("question_no") or detail.get("question_number") or index),
                "question": detail.get("question") or detail.get("content") or "（未记录题目）",
                "student_answer": detail.get("student_answer") or detail.get("answer") or "未记录",
                "correct_answer": _preferred_correct_answer(detail) or "未记录",
                "solution_steps": _preferred_solution_steps(detail) or "暂无解题步骤",
                "status": "错误" if status in ("wrong", "错误") else "部分正确",
            })
        if errors:
            entries.append({
                "student_name": getattr(student, "name", None) or "未识别学生",
                "student_no": getattr(student, "student_no", None) or "",
                "class_name": getattr(student, "class_name", None) or "",
                "errors": errors,
            })

    title = f"{exam_name + ' - ' if exam_name else ''}{'错题汇总' if mode == 'question' else '错题条'}"
    title_paragraph = doc.add_heading(normalize_export_text(title), 0)
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle = doc.add_paragraph("按批改记录生成，请结合原卷复核。")
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(12)

    def add_labeled(label: str, value) -> None:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(4)
        label_run = paragraph.add_run(label)
        label_run.bold = True
        if isinstance(value, list):
            lines = [
                f"第{index}步：{normalize_export_text(item)}"
                for index, item in enumerate(value, 1)
                if str(item or "").strip()
            ]
            normalized = "\n".join(lines)
        else:
            normalized = normalize_export_text(value)
        paragraph.add_run(normalized or "（无内容）")

    if mode == "question":
        groups: dict[str, dict] = {}
        for entry in entries:
            for error in entry["errors"]:
                group = groups.setdefault(error["question_no"], {
                    "question": error["question"],
                    "correct_answer": "",
                    "solution_steps": "",
                    "errors": [],
                })
                if not group["question"] and error["question"]:
                    group["question"] = error["question"]
                if not group["correct_answer"] and error["correct_answer"]:
                    group["correct_answer"] = error["correct_answer"]
                if not group["solution_steps"] and error["solution_steps"]:
                    group["solution_steps"] = error["solution_steps"]
                group["errors"].append({
                    **error,
                    "student_name": entry["student_name"],
                    "student_no": entry["student_no"],
                })
        for question_no, group in groups.items():
            doc.add_heading(f"第{normalize_export_text(question_no)}题 - {len(group['errors'])}人出错", level=1)
            add_labeled("题目：", group["question"])
            add_labeled("参考答案：", group["correct_answer"])
            add_labeled("解题步骤：", group["solution_steps"])
            for error in group["errors"]:
                add_labeled(
                    f"{normalize_export_text(error['student_name'])}（{normalize_export_text(error['student_no']) or '无学号'}，{error['status']}）：",
                    error["student_answer"],
                )
    else:
        for entry_index, entry in enumerate(entries):
            if entry_index:
                doc.add_page_break()
            doc.add_heading(f"{normalize_export_text(entry['student_name'])} 的错题条", level=1)
            add_labeled(
                "学生信息：",
                f"学号 {entry['student_no'] or '未记录'} | 班级 {entry['class_name'] or '未记录'}",
            )
            for error in entry["errors"]:
                doc.add_heading(f"第{normalize_export_text(error['question_no'])}题 - {error['status']}", level=2)
                add_labeled("题目：", error["question"])
                add_labeled("学生答案：", error["student_answer"])
                add_labeled("参考答案：", error["correct_answer"])
                add_labeled("解题步骤：", error["solution_steps"])

    if not entries:
        doc.add_paragraph("没有可导出的错题记录。")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _export_error_sheet(params: dict, **kwargs) -> str:
    exam_name = (params or {}).get("exam_name", "")
    class_name = (params or {}).get("class_name", "")
    mode = (params or {}).get("mode", "student")
    db = _get_db()
    try:
        from models import Student, GradingResult
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        q = db.query(GradingResult).join(Student).filter(
            Student.teacher_id == uid,
            GradingResult.deleted_at.is_(None),
        )
        if exam_name:
            q = q.filter(GradingResult.exam_name == exam_name)
        if class_name:
            q = q.filter(_student_class_filter(db, uid, class_name))
        results = _scope_teaching_query(q, GradingResult).limit(50).all()
        if not results:
            return "没有找到符合条件的错题记录。"
        # 生成 Word 错题条
        try:
            import docx  # noqa: F401
        except ImportError:
            return "python-docx 未安装，无法导出错题条。"
        payload = _build_error_sheet_docx(results[:30], exam_name, mode)
        # 保存到 output 目录并登记归属（P0-1/P0-2）
        from services.generated_files import register_generated_file, make_display_name
        kind = "错题汇总" if mode == "question" else "错题条"
        display_name = make_display_name(kind, exam_name or "", "docx")
        rec = register_generated_file(db, uid, payload, display_name)
        db.commit()
        return f"错题条已生成：{display_name}（{len(results)}条记录）。下载链接：/api/download/{rec.id}"
    finally:
        db.close()


def _query_pending(params: dict, **kwargs) -> str:
    db = _get_db()
    try:
        from models import GradingResult, QuestionBank, Student
        from services.bank_scope import visible_filter
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        rows = _scope_teaching_query(db.query(GradingResult).join(Student).filter(
            Student.teacher_id == uid,
            GradingResult.deleted_at.is_(None),
            GradingResult.grading_details.isnot(None),
            GradingResult.grading_details != "[]",
        ), GradingResult).limit(300).all()
        seen = set()
        for result in rows:
            for item in result.get_details():
                if not isinstance(item, dict):
                    continue
                question = (item.get("question") or "").strip()
                model_answer = (item.get("model_answer") or "").strip()
                if not question or not model_answer or question in seen:
                    continue
                exists = db.query(QuestionBank.id).filter(
                    visible_filter(uid),
                    QuestionBank.question == question,
                ).first()
                if not exists:
                    seen.add(question)
        count = len(seen)
        if not count:
            return "目前没有待入库的题目。"
        return f"当前有 {count} 条待入库题目，请前往「待入库」页面（/pending-answers）确认。"
    finally:
        db.close()


def _office_generate(params: dict, **kwargs) -> str:
    title = (params or {}).get("title", "文档")
    fmt = (params or {}).get("format", "word").lower()
    content = (params or {}).get("content", "")
    if not content.strip():
        return "内容为空，无法生成文件。"

    structure = {
        "format": fmt,
        "title": title,
        "sections": [{"heading": "", "body": content}],
    }
    try:
        from services.office_gen import build_office
        from services.generated_files import register_generated_file, make_display_name
        buf, filename, _media = build_office(structure)
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份，文件未生成。"
        # 统一友好名：用文档标题作标识、原扩展名（P0-2）
        _stem, _ext = os.path.splitext(filename)
        display_name = make_display_name("办公文档", title or _stem, _ext or ".docx")
        db = _get_db()
        try:
            rec = register_generated_file(db, uid, buf.read(), display_name)
            db.commit()
            return f"文件已生成：{display_name}。下载链接：/api/download/{rec.id}"
        finally:
            db.close()
    except Exception as e:
        logger.error("office_generate tool failed: %s", e)
        return f"文件生成失败：{e}"


def _list_classes(params: dict, **kwargs) -> str:
    """列出当前老师的所有班级及学生数。"""
    db = _get_db()
    try:
        from models import Student
        from models_rate_limit import TeacherClass
        from sqlalchemy import func
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        classes = db.query(TeacherClass).filter(
            TeacherClass.teacher_id == uid, TeacherClass.deleted_at.is_(None)
        ).all()
        if not classes:
            return "你还没有创建班级。可以在顶部导航栏创建班级。"
        lines = ["你的班级："]
        for c in classes:
            cnt = db.query(func.count(Student.id)).filter(
                Student.teacher_id == uid, Student.class_id == c.id
            ).scalar() or 0
            lines.append(f"• {c.class_name} — {cnt}名学生")
        return "\n".join(lines)
    finally:
        db.close()


def _list_students(params: dict, **kwargs) -> str:
    """列出指定班级的所有学生。"""
    class_name = (params or {}).get("class_name", "").strip()
    if not class_name:
        return "请提供班级名称。"
    db = _get_db()
    try:
        from models import Student
        from models_rate_limit import TeacherClass
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        # 找班级
        cls = db.query(TeacherClass).filter(
            TeacherClass.teacher_id == uid,
            TeacherClass.class_name == class_name,
            TeacherClass.deleted_at.is_(None),
        ).first()
        if cls:
            students = db.query(Student).filter(
                Student.teacher_id == uid, Student.class_id == cls.id
            ).order_by(Student.student_no, Student.id).all()
        else:
            students = db.query(Student).filter(
                Student.teacher_id == uid, Student.class_name == class_name
            ).order_by(Student.student_no, Student.id).all()
        if not students:
            return f"班级「{class_name}」没有学生。"
        lines = [f"{class_name} 共{len(students)}名学生："]
        for i, s in enumerate(students, 1):
            no = s.student_no or "-"
            lines.append(f"{i}. {s.name}（学号: {no}）")
        return "\n".join(lines)
    finally:
        db.close()


def _query_question_bank(params: dict, **kwargs) -> str:
    """搜索题库。"""
    keyword = (params or {}).get("keyword", "")
    chapter = (params or {}).get("chapter", "")
    limit = (params or {}).get("limit", 10)
    db = _get_db()
    try:
        from models import QuestionBank
        from sqlalchemy import func, or_
        from services.bank_scope import visible_filter
        from services.security import escape_like
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        q = db.query(QuestionBank).filter(
            QuestionBank.answer.isnot(None),
            visible_filter(uid),
        )
        total = q.with_entities(func.count(QuestionBank.id)).scalar() or 0
        if not keyword and not chapter:
            return f"当前题库共 {total} 道题。"
        if keyword:
            like = f"%{escape_like(keyword)}%"
            q = q.filter(or_(
                QuestionBank.question.like(like, escape='\\'),
                QuestionBank.answer.like(like, escape='\\'),
                QuestionBank.topic.like(like, escape='\\'),
                QuestionBank.chapter.like(like, escape='\\'),
            ))
        if chapter:
            like_ch = f"%{escape_like(chapter)}%"
            q = q.filter(QuestionBank.chapter.like(like_ch, escape='\\'))
        items = q.limit(min(limit, 30)).all()
        if not items:
            return f"未找到匹配「{keyword or chapter}」的题库题目。当前题库共 {total} 道题。"
        lines = [f"找到 {len(items)} 道题（关键词：{keyword or '无'}）："]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. [{it.chapter or '-'}] {it.question[:100]}{'...' if len(it.question or '')>100 else ''}")
        lines.append(f"题库总数：{total} 道")
        return "\n".join(lines)
    finally:
        db.close()


def _public_search_query_is_private(query: str) -> bool:
    lowered = query.lower()
    if any(marker.lower() in lowered for marker in _PRIVATE_SEARCH_MARKERS):
        return True
    if re.search(r"\b1[3-9]\d{9}\b", query):
        return True
    if re.search(r"\b\d{6,}\b", query):
        return True
    if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", query):
        return True
    return any(marker in lowered for marker in (
        "localhost",
        "127.0.0.1",
        "/opt/",
        "c:\\",
        ".env",
        "internal api",
        "内部接口",
    ))


def _public_web_search(params: dict, **kwargs) -> str:
    """Search one fixed public endpoint and return metadata only.

    The handler accepts no URL, never fetches result pages, and rejects common
    private-data/internal-system query patterns before any outbound request.
    """
    query = " ".join(str((params or {}).get("query") or "").split())
    if not query:
        return json.dumps(
            {"success": False, "error": "请提供要查询的公开信息。"},
            ensure_ascii=False,
        )
    if len(query) > _PUBLIC_SEARCH_MAX_QUERY_CHARS:
        return json.dumps(
            {"success": False, "error": "搜索词过长，请只保留公开问题的关键词。"},
            ensure_ascii=False,
        )
    if _public_search_query_is_private(query):
        return json.dumps(
            {
                "success": False,
                "error": "搜索词可能包含教学隐私或内部信息，已阻止发送到公网。",
            },
            ensure_ascii=False,
        )

    try:
        limit = min(max(int((params or {}).get("limit", 5)), 1), 5)
    except (TypeError, ValueError):
        limit = 5

    try:
        import httpx
        from lxml import html

        response = httpx.get(
            _PUBLIC_SEARCH_URL,
            params={"q": query},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
            timeout=_PUBLIC_SEARCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
        document = html.fromstring(response.text)
        nodes = document.xpath(
            "//li[contains(concat(' ', normalize-space(@class), ' '), ' b_algo ')]"
        )
        results = []
        for node in nodes:
            links = node.xpath(".//h2/a[1]")
            if not links:
                continue
            link = links[0]
            url = str(link.get("href") or "").strip()
            if urlparse(url).scheme not in {"http", "https"}:
                continue
            title = " ".join("".join(link.itertext()).split())
            if not title:
                continue
            description_parts = node.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' b_caption ')]/p//text()"
            )
            if not description_parts:
                description_parts = node.xpath(".//p[1]//text()")
            description = " ".join(" ".join(description_parts).split())
            results.append({
                "title": title[:300],
                "url": url,
                "description": description[:800],
                "position": len(results) + 1,
            })
            if len(results) >= limit:
                break

        if not results:
            return json.dumps(
                {"success": False, "error": "公开搜索暂未返回可用结果。"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"success": True, "data": {"web": results}},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning("Teacher public web search failed: %s", type(exc).__name__)
        return json.dumps(
            {"success": False, "error": "公开搜索暂时不可用，请稍后再试。"},
            ensure_ascii=False,
        )


def _export_scores_excel(params: dict, **kwargs) -> str:
    """导出考试成绩为 Excel。"""
    exam_name = (params or {}).get("exam_name", "")
    class_name = (params or {}).get("class_name", "")
    db = _get_db()
    try:
        from models import ExamScore, Student
        from openpyxl import Workbook
        from openpyxl.styles import Font
        uid = _get_user_id()
        if not uid:
            return "无法确定当前老师身份。"
        q = db.query(ExamScore).join(Student, ExamScore.student_id == Student.id).filter(
            ExamScore.teacher_id == uid,
            Student.teacher_id == uid,
            ExamScore.analytics_included.is_(True),
        )
        if exam_name:
            q = q.filter(ExamScore.exam_name == exam_name)
        if class_name:
            q = q.filter(_student_class_filter(db, uid, class_name))
        scores = _scope_teaching_query(q, ExamScore).order_by(ExamScore.exam_date.desc(), ExamScore.score.desc()).all()
        if not scores:
            return "没有找到符合条件的月考成绩。"
        wb = Workbook()
        ws = wb.active
        ws.title = exam_name or "成绩表"
        headers = ["排名", "学号", "学生", "班级", "考试", "日期", "成绩"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.font = Font(bold=True)
        for rank, score in enumerate(scores, 1):
            row = rank + 1
            student = db.query(Student).filter(Student.id == score.student_id).first()
            ws.cell(row=row, column=1, value=rank)
            ws.cell(row=row, column=2, value=student.student_no if student else "")
            ws.cell(row=row, column=3, value=student.name if student else "-")
            ws.cell(row=row, column=4, value=_student_class_label(db, student) if student else "-")
            ws.cell(row=row, column=5, value=score.exam_name or "-")
            ws.cell(row=row, column=6, value=score.exam_date.strftime("%Y-%m-%d") if score.exam_date else "-")
            ws.cell(row=row, column=7, value=score.score)
        import io as _io
        from services.generated_files import register_generated_file, make_display_name
        _buf = _io.BytesIO()
        wb.save(_buf)
        display_name = make_display_name("成绩表", exam_name or "", "xlsx")
        rec = register_generated_file(db, uid, _buf.getvalue(), display_name)
        db.commit()
        return f"成绩表已生成：{display_name}（{len(scores)}条记录）。下载链接：/api/download/{rec.id}"
    finally:
        db.close()


def _parse_document_tool(params: dict, **kwargs) -> str:
    """解析已上传的文件内容。"""
    file_path = (params or {}).get("file_path", "")
    if not file_path:
        return "请提供要解析的文件路径。"
    base = (Path(__file__).resolve().parent.parent / "output").resolve()
    try:
        full = (base / str(file_path)).resolve()
        full.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return "非法文件路径。"
    if not full.is_file():
        return f"文件不存在：{file_path}"
    try:
        size = full.stat().st_size
    except OSError:
        return "文件状态不可用，无法解析。"
    if size <= 0 or size > _DOCUMENT_TOOL_MAX_BYTES:
        return "文件为空或超过 20MB 限制，无法解析。"
    if not _DOCUMENT_TOOL_PARSE_SLOT.acquire(
        timeout=_DOCUMENT_TOOL_ADMISSION_TIMEOUT_SECONDS
    ):
        return "文档解析资源正忙，请稍后重试。"
    try:
        with full.open("rb") as stream:
            raw = stream.read(_DOCUMENT_TOOL_MAX_BYTES + 1)
        if len(raw) > _DOCUMENT_TOOL_MAX_BYTES:
            return "文件超过 20MB 限制，无法解析。"
        from services.doc_parser import parse_document
        r = parse_document(
            raw,
            full.name,
            encode_images=False,
        )
        if r["kind"] == "text":
            return f"解析结果（{r.get('source_format','')}，{len(r['content'])}字符）：\n{r['content'][:3000]}"
        return f"文件解析为图片格式（{r.get('mime','')}），需走识图通道。"
    except Exception:
        logger.exception("Hermes document parsing failed")
        return "文档解析失败。"
    finally:
        _DOCUMENT_TOOL_PARSE_SLOT.release()
