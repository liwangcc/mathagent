from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT

from services.answer_sheet_document import build_answer_sheet_document
from services.exercise_schema import ExercisePaper


def _question(kind: str, number: int, difficulty: int = 2) -> dict:
    item = {
        "type": kind,
        "question": f"第{number}题题干 SECRET-{number}",
        "options": {},
        "answer": f"SECRET-{number}",
        "solution": f"解析 SECRET-SOLUTION-{number}",
        "knowledge_point": "综合",
        "difficulty": difficulty,
    }
    if kind == "choice":
        item["options"] = {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}
        item["answer"] = "B"
    return item


def _paper(choice=11, fill=3, solution=5):
    questions = []
    number = 1
    for kind, count in (("choice", choice), ("fill", fill), ("solution", solution)):
        for _ in range(count):
            questions.append(_question(kind, number))
            number += 1
    return ExercisePaper(title="高中数学综合测试答题卡", questions=questions)


def _text(document):
    values = [p.text for p in document.paragraphs]
    def collect_table(table):
        for row in table.rows:
            for cell in row.cells:
                values.extend(p.text for p in cell.paragraphs)
                for nested in cell.tables:
                    collect_table(nested)
    for table in document.tables:
        collect_table(table)
    return "\n".join(values)


def _cell_text(cell):
    values = [paragraph.text for paragraph in cell.paragraphs]

    def collect_table(table):
        for row in table.rows:
            for nested_cell in row.cells:
                values.extend(paragraph.text for paragraph in nested_cell.paragraphs)
                for nested_table in nested_cell.tables:
                    collect_table(nested_table)

    for table in cell.tables:
        collect_table(table)
    return "\n".join(values)


def _table_paragraphs(table):
    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs
            for nested_table in cell.tables:
                yield from _table_paragraphs(nested_table)


def _all_tables(document):
    def collect(table):
        yield table
        for row in table.rows:
            for cell in row.cells:
                for nested_table in cell.tables:
                    yield from collect(nested_table)

    for table in document.tables:
        yield from collect(table)


def _answer_block_height(document, number):
    label = f"{number}. 解答："
    for table in _all_tables(document):
        if len(table.rows) != 1 or len(table.columns) != 1:
            continue
        direct_text = "\n".join(paragraph.text for paragraph in table.cell(0, 0).paragraphs)
        if label in direct_text:
            return table.rows[0].height.mm
    raise AssertionError(f"未找到第 {number} 题答题框")


def _page_break_count(data):
    with ZipFile(BytesIO(data)) as archive:
        xml = archive.read("word/document.xml")
    return xml.count(b'w:type="page"')


def test_answer_sheet_is_editable_a3_landscape_duplex_three_panel():
    data = build_answer_sheet_document(_paper().title, _paper().questions)
    assert data.startswith(b"PK")
    assert _page_break_count(data) == 1
    document = Document(BytesIO(data))
    section = document.sections[0]
    assert section.orientation == WD_ORIENT.LANDSCAPE
    assert round(section.page_width.mm) == 420
    assert round(section.page_height.mm) == 297
    assert len(document.tables) >= 6
    assert any(len(table.columns) == 3 for table in document.tables)


def test_answer_sheet_contains_identity_grouping_and_reference_structure_without_answer_leakage():
    data = build_answer_sheet_document(_paper().title, _paper().questions)
    text = _text(Document(BytesIO(data)))
    for field in ("班级：", "姓名：", "学号：", "准考证号/考生号：", "贴条形码区", "模板码：", "注意事项"):
        assert field in text
    for section in ("选择题", "填空题", "续答区", "15. 解答：", "16. 解答：", "17. 解答：", "18. 解答：", "19. 解答："):
        assert section in text
    assert "·  正面  ·" in text and "·  背面  ·" in text
    assert "第1页（共6页）" in text and "第6页（共6页）" in text
    assert text.count("AS-") >= 8
    assert "正反面组码" not in text
    assert "答题卡编号" not in text
    assert "SECRET-" not in text
    assert "解析" not in text
    assert "题干" not in text


def test_answer_sheet_uses_actual_numbers_and_preserves_standard_six_solution_layout_without_figures():
    paper = _paper(choice=12, fill=4, solution=6)
    data = build_answer_sheet_document(paper.title, paper.questions)
    document = Document(BytesIO(data))
    text = _text(document)
    for number in range(1, 13):
        assert f"{number}. [A]" in text
    for number in range(13, 17):
        assert any(
            cell.text.startswith(f"{number}.\n_")
            for table in document.tables
            for row in table.rows
            for cell in row.cells
            for nested in cell.tables
            for nested_row in nested.rows
            for nested_cell in nested_row.cells
            for fill_table in nested_cell.tables
            for fill_row in fill_table.rows
            for cell in fill_row.cells
        )
    fill_lines = [
        paragraph
        for table in document.tables
        for paragraph in _table_paragraphs(table)
        if paragraph.text.startswith("___")
    ]
    assert len(fill_lines) == 4
    assert all(paragraph.paragraph_format.space_before.mm >= 9 for paragraph in fill_lines)
    for number in range(17, 23):
        assert f"{number}. 解答：" in text
    assert "本卷共 22 题" in text
    front_panels = [table for table in document.tables if len(table.columns) == 3][0].rows[0].cells
    back_panels = [table for table in document.tables if len(table.columns) == 3][1].rows[0].cells
    assert "17. 解答：" in _cell_text(front_panels[0])
    assert "17题续答区" in _cell_text(front_panels[1]) and "18. 解答：" in _cell_text(front_panels[1])
    assert "19. 解答：" in _cell_text(front_panels[2])
    assert "20. 解答：" in _cell_text(back_panels[0])
    assert "21. 解答：" in _cell_text(back_panels[1])
    assert "22. 解答：" in _cell_text(back_panels[2])
    assert _answer_block_height(document, 17) == pytest.approx(55.0, abs=0.2)
    with ZipFile(BytesIO(data)) as archive:
        assert not any(name.startswith("word/media/") for name in archive.namelist())


def test_answer_sheet_reclaims_missing_objective_rows_and_omits_phantom_question_regions():
    paper = _paper(choice=10, fill=3, solution=4)
    document = Document(BytesIO(build_answer_sheet_document(paper.title, paper.questions)))
    text = _text(document)
    front_panels = [table for table in document.tables if len(table.columns) == 3][0].rows[0].cells
    back_panels = [table for table in document.tables if len(table.columns) == 3][1].rows[0].cells

    assert _answer_block_height(document, 14) > 70
    assert "14. 解答：" in _cell_text(front_panels[0])
    assert "14题续答区" in _cell_text(front_panels[1]) and "15. 解答：" in _cell_text(front_panels[1])
    assert "16. 解答：" in _cell_text(front_panels[2])
    assert "17. 解答：" in _cell_text(back_panels[0])
    assert "解答题备用区" in _cell_text(back_panels[1])
    assert "解答题备用区" in _cell_text(back_panels[2])
    assert "18. 解答：" not in text


def test_answer_sheet_can_use_freed_objective_space_for_six_renumbered_solutions():
    paper = _paper(choice=8, fill=0, solution=6)
    document = Document(BytesIO(build_answer_sheet_document(paper.title, paper.questions)))
    text = _text(document)
    front_panels = [table for table in document.tables if len(table.columns) == 3][0].rows[0].cells
    back_panels = [table for table in document.tables if len(table.columns) == 3][1].rows[0].cells

    assert _answer_block_height(document, 9) > 130
    assert "9. 解答：" in _cell_text(front_panels[0])
    assert "9题续答区" in _cell_text(front_panels[1]) and "10. 解答：" in _cell_text(front_panels[1])
    assert "11. 解答：" in _cell_text(front_panels[2])
    assert "12. 解答：" in _cell_text(back_panels[0])
    assert "13. 解答：" in _cell_text(back_panels[1])
    assert "14. 解答：" in _cell_text(back_panels[2])
    assert "15. 解答：" not in text


def test_answer_sheet_moves_first_solution_to_next_panel_when_objective_section_is_tall():
    paper = _paper(choice=1, fill=20, solution=1)
    document = Document(BytesIO(build_answer_sheet_document(paper.title, paper.questions)))
    front_panels = [table for table in document.tables if len(table.columns) == 3][0].rows[0].cells

    assert "22. 解答：" not in _cell_text(front_panels[0])
    assert "解答题备用区" in _cell_text(front_panels[0])
    assert "22. 解答：" in _cell_text(front_panels[1])
    assert "解答题备用区" in _cell_text(front_panels[2])


def test_answer_sheet_and_generation_contract_reject_more_than_22_questions():
    from pydantic import ValidationError
    from routers.exercise import ExerciseGenerateRequest

    questions = [_question("solution", number) for number in range(1, 24)]
    with pytest.raises(ValueError, match="最多支持 22 道题"):
        build_answer_sheet_document("超量答题卡", questions)
    with pytest.raises(ValidationError):
        ExerciseGenerateRequest(question_count=23)
    with pytest.raises(ValidationError):
        ExercisePaper(title="超量练习", questions=questions)

    with pytest.raises(ValueError, match="最多支持 6 道解答题"):
        build_answer_sheet_document("解答题过量", questions[:7])


def test_export_registers_generated_file(monkeypatch):
    from routers import exercise as exercise_router
    from services import generated_files

    calls = []
    monkeypatch.setattr(generated_files, "register_generated_file", lambda db, owner_id, data, display_name: calls.append((db, owner_id, data, display_name)) or SimpleNamespace(id=321))
    monkeypatch.setattr(generated_files, "make_display_name", lambda kind, title, ext: f"{kind}_{title}.{ext}")
    db = SimpleNamespace(commit=lambda: calls.append("commit"))
    response = asyncio.run(exercise_router.exercise_export_answer_sheet_docx(_paper(), db=db, user=SimpleNamespace(id=7, education_stage="senior")))
    assert response["download_url"] == "/api/download/321"
    assert response["layout"] == "a3_landscape_duplex_three_panel"
    assert calls[-1] == "commit"
    assert calls[0][2].startswith(b"PK")
