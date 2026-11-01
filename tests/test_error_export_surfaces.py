import io
from types import SimpleNamespace

import pytest
from docx import Document

pytest.importorskip("reportlab")

from services import error_sheet, exporter, hermes_tools
from services.batch_review_export import (
    export_all_review_error_sheets_zip,
    export_review_question_summary_pdf,
    export_single_review_error_sheet_pdf,
)
from services.export_text import escape_export_html, normalize_export_text


def _student_errors():
    return [{
        "name": "张三",
        "student_no": "001",
        "class_name": "一班",
        "score": 0,
        "metric_text": "得分：0/10分",
        "total": 1,
        "correct": 0,
        "wrong_count": 1,
        "errors": [{
            "q_index": "2",
            "question": r"若 $\frac{1}{2}$ &amp;lt; x，求 $\sqrt{x^2}$",
            "student_answer": r"x &gt; \frac{3}{4}",
            "correct_answer": r"x \geq \frac{1}{2}",
            "solution_steps": [r"先化简 \sqrt{x^2}", "再比较 &gt; 关系"],
            "result": "wrong",
            "status": "wrong",
        }],
    }]


def _review():
    return {
        "review_id": "p1",
        "paper_id": "p1",
        "student_name": "张三",
        "student_no": "001",
        "grading_mode": "precise",
        "disposition": "active",
        "result": {
            "score": 0,
            "total": 1,
            "correct_count": 0,
            "wrong_count": 1,
            "details": _student_errors()[0]["errors"],
        },
    }


def test_export_text_decodes_entities_and_common_latex_without_evaluation():
    value = r"\frac{1}{2} &amp;lt; x \leq \sqrt{9}, x^2 + a_{10}"

    assert normalize_export_text(value) == "(1)/(2) < x ≤ √(9), x² + a₁₀"
    assert escape_export_html("<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_pdf_zip_and_summary_share_readable_math_contract():
    review = _review()

    student_pdf = export_single_review_error_sheet_pdf(review, "一班")
    summary_pdf = export_review_question_summary_pdf([review], "一班")
    archive = export_all_review_error_sheets_zip([review], "一班")

    assert student_pdf[:4] == b"%PDF"
    assert summary_pdf[:4] == b"%PDF"
    assert archive[:2] == b"PK"
    assert len(student_pdf) > 1000
    assert len(summary_pdf) > 1000


def test_html_student_and_question_exports_are_offline_readable():
    student_html = error_sheet._render_error_sheet_html(_student_errors(), "期中考试", "student")
    question_html = error_sheet._render_error_sheet_html(_student_errors(), "期中考试", "question")

    for payload in (student_html, question_html):
        assert "cdn.jsdelivr.net" not in payload
        assert r"\frac" not in payload
        assert "&amp;lt;" not in payload
        assert "(1)/(2) &lt; x" in payload
        assert "√(x²)" in payload
    assert "张三 的错题条" in student_html
    assert "第2题 - 共1人出错" in question_html
    assert "<table>" in question_html


def test_report_page_html_escapes_steps_and_normalizes_math():
    assert exporter._escape(r"x &amp;gt; \sqrt{4}") == "x &gt; √(4)"
    assert exporter._solution_html(["a&gt;0", r"\frac{1}{2}"]) == (
        "第1步：a&gt;0<br>第2步：(1)/(2)"
    )


def test_archived_exam_export_keeps_real_question_number():
    result = SimpleNamespace(
        student=SimpleNamespace(id=1, name="张三", student_no="001", class_name="一班"),
        get_details=lambda: [{
            "question_no": "12(2)",
            "question": "题目",
            "result": "wrong",
        }],
        wrong_count=1,
        score=0,
        total_questions=1,
        correct_count=0,
        category="daily",
    )

    collected = error_sheet._collect_errors([result])

    assert collected[0]["errors"][0]["q_index"] == "12(2)"


def test_chat_tool_docx_contains_complete_readable_error_details():
    details = _student_errors()[0]["errors"]
    result = SimpleNamespace(
        student=SimpleNamespace(name="张三", student_no="001", class_name="一班"),
        get_details=lambda: details,
    )

    payload = hermes_tools._build_error_sheet_docx([result], "期中考试", "student")
    document = Document(io.BytesIO(payload))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert "张三 的错题条" in text
    assert "学生答案：x > (3)/(4)" in text
    assert "参考答案：x ≥ (1)/(2)" in text
    assert "解题步骤：第1步：先化简 √(x²)" in text
    assert r"\frac" not in text
    assert "&gt;" not in text


def test_error_exports_prefer_model_answer_for_complete_solution():
    detail = {
        "question_no": "3",
        "question": "求 x",
        "student_answer": "x=0",
        "correct_answer": "x=1",
        "standard_answer": "x=1",
        "solution_steps": ["教师评分依据"],
        "model_answer": "设方程成立，移项得 x=1。",
        "explanation": "学生移项时符号错误",
        "result": "wrong",
    }
    result = SimpleNamespace(
        student=SimpleNamespace(id=1, name="张三", student_no="001", class_name="一班"),
        get_details=lambda: [detail],
        wrong_count=1,
        score=0,
        total_questions=1,
        correct_count=0,
        category="daily",
    )

    collected = error_sheet._collect_errors([result])
    assert collected[0]["errors"][0]["solution_steps"] == detail["model_answer"]

    html = error_sheet._render_error_sheet_html(collected, "期中考试", "student")
    assert detail["model_answer"] in html
    assert detail["explanation"] not in html

    payload = hermes_tools._build_error_sheet_docx([result], "期中考试", "student")
    document = Document(io.BytesIO(payload))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert detail["model_answer"] in text


def test_error_export_uses_hint_when_legacy_record_has_no_solution_fields():
    detail = {
        "question_no": "4",
        "question": "求 y",
        "student_answer": "y=0",
        "correct_answer": "y=2",
        "hint": "先整理等式两边，再代入已知条件。",
        "explanation": "移项时漏掉常数项。",
        "result": "wrong",
    }
    result = SimpleNamespace(
        student=SimpleNamespace(id=1, name="李四", student_no="002", class_name="一班"),
        get_details=lambda: [detail],
        wrong_count=1,
        score=0,
        total_questions=1,
        correct_count=0,
        category="daily",
    )

    collected = error_sheet._collect_errors([result])
    assert collected[0]["errors"][0]["solution_steps"] == detail["hint"]


def test_question_html_summary_keeps_later_complete_solution_after_sparse_record():
    entries = [
        {
            "name": "张三",
            "student_no": "001",
            "class_name": "一班",
            "wrong_count": 1,
            "errors": [{
                "q_index": "5",
                "question": "",
                "student_answer": "未识别",
                "correct_answer": "",
                "solution_steps": "",
                "status": "wrong",
            }],
        },
        {
            "name": "李四",
            "student_no": "002",
            "class_name": "一班",
            "wrong_count": 1,
            "errors": [{
                "q_index": "5",
                "question": "求 x",
                "student_answer": "x=0",
                "correct_answer": "x=1",
                "solution_steps": "移项得 x=1。",
                "status": "wrong",
            }],
        },
    ]

    html = error_sheet._render_error_sheet_html(entries, "期中考试", "question")
    assert "求 x" in html
    assert "参考答案：x=1" in html
    assert "移项得 x=1。" in html
