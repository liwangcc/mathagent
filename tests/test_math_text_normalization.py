from io import BytesIO

from docx import Document

from services.export_text import decode_export_text, normalize_export_text
from services.paper_generator import export_paper_to_word


def test_export_normalization_repairs_empty_root_degrees_and_nth_roots():
    source = r"$\sqrt [ ] {30}+\sqrt[3]{8}$"

    assert decode_export_text(source) == r"$\sqrt {30}+\sqrt[3]{8}$"
    assert normalize_export_text(source) == "√(30)+³√(8)"


def test_export_normalization_repairs_safe_json_control_escapes():
    damaged = "$\x0crac{1}{2}+\x08eta$"

    assert normalize_export_text(damaged) == "(1)/(2)+β"


def test_export_normalization_removes_zero_width_noise_and_fullwidth_slashes():
    damaged = "\u200b$﹨sqrt[]{9}$\ufeff"

    assert normalize_export_text(damaged) == "√(9)"


def test_legacy_bank_word_export_does_not_leak_latex_source():
    buffer = export_paper_to_word(
        {
            "title": "根式练习",
            "total": 1,
            "questions": [
                {
                    "question": r"计算 $\sqrt[]{9}+\sqrt[3]{8}$",
                    "answer": "5",
                }
            ],
        },
        with_answers=True,
    )
    document = Document(BytesIO(buffer.getvalue()))
    text = "\n".join(
        paragraph.text
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        for paragraph in cell.paragraphs
    )

    assert "√(9)+³√(8)" in text
    assert r"\sqrt" not in text
