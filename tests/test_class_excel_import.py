"""Tests for the Excel roster/score import helpers in routers/class_mgmt.py.

老师的名单表几乎不可能是干净的：学号被 Excel 存成数字、中间夹空行、某几行
只填了前两列。这些都不该让整份导入失败，也不该让报错行号对不上原表。
"""

import io

import openpyxl
import pytest

from routers.class_mgmt import _cell_text, _find_cols, _read_xlsx_rows, _row_value


def _xlsx(rows: list[list]) -> bytes:
    """把二维数据写成 xlsx 字节流，模拟老师上传的文件。"""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestCellText:
    """单元格规范化：学号必须原样落地，不能被浮点/科学计数法改写。"""

    def test_blank_cell_becomes_empty_string(self):
        assert _cell_text(None) == ""

    def test_integer_student_number_keeps_all_digits(self):
        assert _cell_text(2021000008) == "2021000008"

    def test_integral_float_does_not_gain_a_decimal_tail(self):
        """Excel 常把学号存成 float；2021000008.0 入库就是错的学号。"""
        assert _cell_text(2021000008.0) == "2021000008"

    def test_long_integral_float_does_not_become_scientific_notation(self):
        assert _cell_text(123456789012345.0) == "123456789012345"

    def test_genuine_decimal_is_preserved(self):
        """成绩列可能是 95.5，不能被截成 95。"""
        assert _cell_text(95.5) == "95.5"

    def test_surrounding_whitespace_is_trimmed(self):
        assert _cell_text("  马飞  ") == "马飞"

    def test_boolean_is_not_treated_as_an_integer(self):
        """bool 是 int 的子类，若不先拦会变成 "1"/"0"。"""
        assert _cell_text(True) == "True"


class TestReadXlsxRows:
    def test_trailing_blank_rows_are_dropped(self):
        """Excel 常带一片空尾行，会让"表格为空"判断失效。"""
        rows = _read_xlsx_rows(_xlsx([
            ["学号", "姓名"],
            ["01", "马飞"],
            [None, None],
            [None, None],
        ]))
        assert rows == [["学号", "姓名"], ["01", "马飞"]]

    def test_interior_blank_row_is_kept_so_row_numbers_stay_aligned(self):
        """核心回归：中间空行**必须保留**。

        调用方用 ``enumerate(rows[1:], start=2)`` 给老师报"第N行学号重复"。
        一旦删掉中间任何一行，后面所有行号都会错位，老师照着去改会改错人。
        """
        rows = _read_xlsx_rows(_xlsx([
            ["学号", "姓名"],
            ["01", "马飞"],
            [None, None],
            ["03", "赵六"],
        ]))
        assert len(rows) == 4
        # 赵六在原表第 4 行，解析后仍须落在下标 3。
        assert rows[3] == ["03", "赵六"]

    def test_numeric_student_numbers_survive_the_round_trip(self):
        rows = _read_xlsx_rows(_xlsx([
            ["学号", "姓名"],
            [2021000008, "马飞"],
        ]))
        assert rows[1][0] == "2021000008"

    def test_a_non_xlsx_payload_is_rejected_with_a_clear_message(self):
        with pytest.raises(Exception) as excinfo:
            _read_xlsx_rows(b"this is not a workbook")
        assert "xlsx" in str(excinfo.value)


class TestRowValue:
    """短行不得打断整份导入——一行脏数据废掉整张表是最不该有的失败方式。"""

    def test_missing_logical_column_yields_empty_string(self):
        assert _row_value(["01", "马飞"], {"name": 1}, "phone") == ""

    def test_short_row_does_not_raise_index_error(self):
        cols = {"no": 0, "name": 1, "phone": 2}
        assert _row_value(["01", "马飞"], cols, "phone") == ""

    def test_value_is_read_through_cell_normalisation(self):
        assert _row_value([2021000008, "马飞"], {"no": 0}, "no") == "2021000008"


class TestFindCols:
    def test_header_synonyms_are_matched(self):
        cols = _find_cols(
            ["编号", "考号", "名字", "家长手机"],
            {
                "no": ["学号", "编号"], "exam_no": ["准考证号", "考号"],
                "name": ["姓名", "名字"], "phone": ["手机", "电话", "家长"],
            },
        )
        assert cols == {"no": 0, "exam_no": 1, "name": 2, "phone": 3}

    def test_absent_optional_columns_are_simply_missing(self):
        cols = _find_cols(["姓名"], {"no": ["学号"], "name": ["姓名"]})
        assert cols == {"name": 0}
