import io
import zipfile

import pytest

pytest.importorskip("reportlab")

from services import error_sheet
from services.batch_review_export import (
    ReviewExportError,
    export_all_review_error_sheets_zip,
    export_review_question_summary_pdf,
    export_single_review_error_sheet_pdf,
)


def _review(paper_id, name, student_no, *, disposition="active", mode="precise", question_no="1"):
    return {
        "review_id": paper_id,
        "paper_id": paper_id,
        "student_name": name,
        "student_no": student_no,
        "grading_mode": mode,
        "disposition": disposition,
        "result": {
            "score": 0.0,
            "total": 2,
            "correct_count": 0,
            "wrong_count": 1,
            "details": [{
                "question_no": question_no,
                "question": f"第 {question_no} 题题干",
                "student_answer": "错误答案",
                "correct_answer": "正确答案",
                "solution_steps": ["第一步", "第二步"],
                "result": "wrong",
            }],
        },
    }


def test_single_review_error_sheet_is_named_pdf():
    payload = export_single_review_error_sheet_pdf(_review("p1", "张三", "001"), "一班")

    assert payload[:4] == b"%PDF"
    assert len(payload) > 1000


def test_all_review_error_sheets_zip_skips_voided_and_contains_student_name():
    payload = export_all_review_error_sheets_zip([
        _review("p1", "张三", "001"),
        _review("p2", "李四", "002", disposition="voided"),
    ], "一班")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert "张三" in names[0]
        assert names[0].endswith(".pdf")
        assert archive.read(names[0])[:4] == b"%PDF"


def test_review_question_summary_pdf_groups_errors_by_question_number():
    payload = export_review_question_summary_pdf([
        _review("p1", "张三", "001", question_no="2"),
        _review("p2", "李四", "002", question_no="2"),
        _review("p3", "王五", "003", disposition="voided", question_no="2"),
    ], "一班")

    assert payload[:4] == b"%PDF"
    assert len(payload) > 1000


def test_simple_mode_review_rejects_detailed_error_sheet_export():
    with pytest.raises(ReviewExportError, match="详细批改"):
        export_single_review_error_sheet_pdf(_review("p1", "张三", "001", mode="simple"), "一班")


def test_legacy_auto_mode_keeps_detailed_error_sheet_export_compatibility():
    payload = export_single_review_error_sheet_pdf(_review("p1", "张三", "001", mode="auto"), "一班")
    assert payload[:4] == b"%PDF"


def test_summary_rendering_keeps_chinese_and_interprets_escaped_math_text():
    assert error_sheet._CN_FONT not in {"Helvetica", "Helvetica-Bold"}
    assert error_sheet._normalize_math_text(r"\sqrt{x} &gt; 0") == "√(x) > 0"
    styles = error_sheet._make_styles()
    inline = error_sheet._text_with_latex("定义：$\\sqrt{x}$", styles["Body"])
    assert len(inline) == 1
    elements = error_sheet._build_question_summary(
        "1",
        "a&gt;0",
        [{
            "student_name": "张三",
            "student_no": "001",
            "student_answer": "x&gt;3",
            "status": "wrong",
            "correct_answer": "x≥3",
            "solution_steps": ["列出条件 a&gt;0"],
        }],
        styles,
    )
    table = next(item for item in elements if isinstance(item, error_sheet.Table))
    assert all(
        isinstance(cell, error_sheet.Paragraph)
        for row in table._cellvalues
        for cell in row
    )


def test_provisional_export_prefers_model_answer_over_answer_key_steps():
    from services.batch_review_export import _student_error_entry

    review = _review("p1", "张三", "001")
    detail = review["result"]["details"][0]
    detail["solution_steps"] = ["教师评分依据"]
    detail["model_answer"] = "完整正确答案与推导"

    entry = _student_error_entry(review, "一班")
    assert entry["errors"][0]["solution_steps"] == detail["model_answer"]
