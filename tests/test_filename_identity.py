"""Tests for services/filename_identity.py — 从上传文件名解析身份线索。

覆盖重点是"收集类小工具"的真实命名习惯：学生自己填姓名/学号，工具再拼上
自己的随机后缀。噪声后缀与真实学号在位置上无法区分，故解析器靠形状判断，
本文件把两类形状都钉住。
"""

from services.filename_identity import (
    FilenameIdentity,
    parse_filename_identity,
)


class TestCollectionToolNames:
    """收集工具的 "姓名学号_随机后缀" 命名。"""

    def test_random_suffix_is_not_mistaken_for_the_number(self):
        parsed = parse_filename_identity("马飞2021000008_1eax24.jpg")
        assert parsed == FilenameIdentity(
            name="马飞", student_no="2021000008", confidence="high",
        )

    def test_clean_name_without_suffix(self):
        parsed = parse_filename_identity("马飞2021000008.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "2021000008")

    def test_underscore_separated_number_is_not_stripped_as_noise(self):
        """回归：``张三_2021123456`` 的尾段就是学号本身，不能当后缀剥掉。"""
        parsed = parse_filename_identity("张三_2021123456.png")
        assert parsed.student_no == "2021123456"
        assert parsed.name == "张三"

    def test_hyphen_separator_with_trailing_noise(self):
        parsed = parse_filename_identity("李四-2020999888_abc123.jpg")
        assert (parsed.name, parsed.student_no) == ("李四", "2020999888")

    def test_number_before_name(self):
        parsed = parse_filename_identity("2021000008马飞.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "2021000008")

    def test_number_first_with_trailing_word(self):
        parsed = parse_filename_identity("2021000008_马飞_suffix.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "2021000008")

    def test_alphanumeric_student_number_is_digit_dominant(self):
        parsed = parse_filename_identity("王五2021A12345.jpg")
        assert (parsed.name, parsed.student_no) == ("王五", "2021A12345")

    def test_subject_words_are_not_taken_as_the_name(self):
        parsed = parse_filename_identity("数学作业_赵六2021591107_x9f2.jpg")
        assert (parsed.name, parsed.student_no) == ("赵六", "2021591107")


class TestRejectsNoise:
    """宁可不给，也不猜——错猜会把成绩记到别人名下。"""

    def test_camera_filename_yields_nothing(self):
        assert parse_filename_identity("IMG_0421.jpg") is None

    def test_pure_noise_yields_nothing(self):
        assert parse_filename_identity("random_image_abc123.jpg") is None

    def test_wechat_timestamp_is_not_a_student_number(self):
        """14 位时间戳与学号撞长度区间，必须靠日期前缀整体排除。"""
        assert parse_filename_identity("微信图片_20240315103022.jpg") is None

    def test_page_index_is_too_short_to_be_a_number(self):
        parsed = parse_filename_identity("2021级_马飞_08.jpg")
        assert parsed.student_no == ""
        assert parsed.name == "马飞"

    def test_blank_and_none_are_safe(self):
        assert parse_filename_identity("") is None
        assert parse_filename_identity(None) is None
        assert parse_filename_identity("   ") is None


class TestNameOnlyFilenames:
    """只填姓名不填学号——收集时很常见，必须能单靠姓名给出线索。

    学号缺失不影响姓名解析：``resolve_student_identity`` 在班级名单内按姓名
    唯一命中即可归属，同名多人时它会退回人工确认。
    """

    def test_bare_name(self):
        parsed = parse_filename_identity("马飞.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "")

    def test_name_with_collection_tool_suffix(self):
        parsed = parse_filename_identity("马飞_1eax24.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "")

    def test_compound_surname_is_kept_whole(self):
        """复姓 4 字仍在姓名长度上限内。"""
        parsed = parse_filename_identity("欧阳娜娜.jpg")
        assert parsed.name == "欧阳娜娜"

    def test_subject_prefix_before_a_bare_name(self):
        parsed = parse_filename_identity("数学作业_马飞.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "")

    def test_timestamp_suffix_does_not_become_a_number(self):
        parsed = parse_filename_identity("王芳芳_20240315103022.jpg")
        assert (parsed.name, parsed.student_no) == ("王芳芳", "")

    def test_multi_page_name_only_files_agree(self):
        """无学号时靠姓名归组，多页仍须解析出同一个姓名。"""
        first = parse_filename_identity("马飞_1.jpg")
        second = parse_filename_identity("马飞_2.jpg")
        assert first.name == second.name == "马飞"
        assert first.student_no == second.student_no == ""

    def test_function_words_are_not_a_name(self):
        """剔除学科词后剩下的语法碎片不是姓名。

        ``我的作业`` 去掉 ``作业`` 剩 ``我的``；若当成姓名，会与 OCR 正确读到的
        姓名撞成 conflict，反而多逼老师确认一次，比没有线索更糟。
        """
        assert parse_filename_identity("我的作业.jpg") is None
        assert parse_filename_identity("我们的作业.jpg") is None
        assert parse_filename_identity("这次月考.jpg") is None

    def test_pure_subject_words_yield_nothing(self):
        assert parse_filename_identity("作业.jpg") is None
        assert parse_filename_identity("未命名.jpg") is None
        assert parse_filename_identity("数学.jpg") is None


class TestFilenameShapes:
    def test_ocr_ready_wrapper_is_stripped_before_parsing(self):
        """前端预处理会改名为 ``<原名>.ocr-ready-v1.jpg``，原名须仍可解析。"""
        parsed = parse_filename_identity("马飞2021000008.ocr-ready-v1.jpg")
        assert parsed.student_no == "2021000008"

    def test_zip_relative_path_is_reduced_to_basename(self):
        parsed = parse_filename_identity("收集/初3班/马飞2021000008_1eax24.jpg")
        assert (parsed.name, parsed.student_no) == ("马飞", "2021000008")

    def test_windows_separator_is_handled(self):
        parsed = parse_filename_identity(r"收集\初3班\马飞2021000008.jpg")
        assert parsed.student_no == "2021000008"

    def test_multi_page_files_share_one_student_number(self):
        """同一学生的多页文件名只差页码，解析出的学号必须一致才能归到一起。"""
        first = parse_filename_identity("马飞2021000008_1.jpg")
        second = parse_filename_identity("马飞2021000008_2.jpg")
        assert first.student_no == second.student_no == "2021000008"
        assert first.name == second.name == "马飞"
