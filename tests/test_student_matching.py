"""学号匹配鲁棒性测试 —— services.student_service.match_student_for_upload。

覆盖用户诉求：
  - 学号缺省时按姓名匹配（保持旧行为）。
  - 同班同名靠学号区分，不再误建占位生。
  - OCR 漏前导零 / 学生只写学号后几位仍能命中（8≡08、8→…08、45→12345）。
  - 关键安全边界：单个数字 8 绝不误配到 18；多人兼容时不猜、退回姓名冲突。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401  注册全部 ORM 表
import models_user  # noqa: F401
import models_rate_limit  # noqa: F401
from database import Base
from models import Student
from models_user import User
from models_rate_limit import TeacherClass
from services.student_service import (
    match_student_for_upload,
    resolve_student_identity,
    student_no_sort_key,
    _no_compatible,
    _digits,
    _value,
    canonical_student_no,
)

T1 = 1


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    s.add(User(id=T1, name="师一", role="teacher"))
    s.add(TeacherClass(id=1, teacher_id=T1, class_name="一班"))
    s.add(TeacherClass(id=2, teacher_id=T1, class_name="二班"))
    s.commit()
    yield s
    s.close()


def _add(db, name, no=None, class_id=1):
    st = Student(teacher_id=T1, class_id=class_id, name=name, student_no=no)
    db.add(st)
    db.commit()
    return st


# ─────────────────────── 纯函数：兼容规则 ───────────────────────

def test_value_strips_leading_zeros():
    assert _value("08") == "8"
    assert _value("008") == "8"
    assert _value("045") == "45"
    assert _value("0") == "0"
    assert _value("") == ""


def test_digits_extracts_only_numbers():
    assert _digits("学号：2021") == "2021"
    assert _digits("  08 ") == "08"
    assert _digits(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("8", "08"),           # 个位补零（核心：杜绝裸个位入库）
    ("0", "00"),
    ("08", "08"),          # 两位原样
    ("18", "18"),
    ("2021000008", "2021000008"),
    ("045", "045"),        # 多位不剥前导零
    ("学号：7", "07"),     # 先抽数字再补零
    ("", None),            # 空 → None（避免空串撞唯一约束）
    (None, None),
    ("abc", None),         # 无数字 → None
])
def test_canonical_student_no(raw, expected):
    assert canonical_student_no(raw) == expected


@pytest.mark.parametrize("scanned,stored,ok", [
    ("8", "8", True),            # 完全相同
    ("8", "08", True),           # 去零同值
    ("08", "8", True),           # 去零同值（反向）
    ("008", "8", True),
    ("045", "45", True),
    ("8", "2021000008", True),   # 单数字 → 末两位 08 佐证
    ("2021000008", "8", True),   # 反向：库短卷长
    ("45", "12345", True),       # 多位后缀
    ("12345", "45", True),
    # —— 安全边界：必须为 False ——
    ("8", "18", False),          # 单数字绝不配 18（末两位 18 无 0 佐证）
    ("8", "38", False),
    ("8", "2021591118", False),  # 末两位 18，非 08
    ("7", "08", False),          # 值不同
    ("46", "12345", False),      # 非后缀
    ("", "8", False),            # 空不匹配
    ("8", "", False),
])
def test_no_compatible(scanned, stored, ok):
    assert _no_compatible(scanned, stored) is ok


# ─────────────────────── 匹配：无学号退回姓名 ───────────────────────

def test_no_number_unique_name(db):
    a = _add(db, "张三")
    st, conflict = match_student_for_upload(db, T1, "张三", None, 1)
    assert st is not None and st.id == a.id and conflict is False


def test_no_number_same_name_conflict(db):
    _add(db, "李华")
    _add(db, "李华")
    st, conflict = match_student_for_upload(db, T1, "李华", None, 1)
    assert st is None and conflict is True


def test_no_match_returns_none_false(db):
    _add(db, "张三")
    st, conflict = match_student_for_upload(db, T1, "王五", None, 1)
    assert st is None and conflict is False


# ─────────────────────── 匹配：学号区分同名（用户核心诉求）───────────────────────

def test_same_name_disambiguated_by_exact_no(db):
    _add(db, "李华", "20230101")
    b = _add(db, "李华", "20230102")
    st, conflict = match_student_for_upload(db, T1, "李华", "20230102", 1)
    assert st is not None and st.id == b.id and conflict is False


def test_leading_zero_equivalence(db):
    # 库里存 08，学生只写 8 → 命中，且不碰 18
    target = _add(db, "李华", "08")
    _add(db, "李华", "18")
    st, conflict = match_student_for_upload(db, T1, "李华", "8", 1)
    assert st is not None and st.id == target.id and conflict is False


def test_single_digit_never_matches_18(db):
    # 只有 18 这个候选、没有 08：单数字 8 不该配 18 → 无学号命中，退回姓名（唯一）
    only18 = _add(db, "李华", "18")
    st, conflict = match_student_for_upload(db, T1, "李华", "8", 1)
    # 学号不兼容，退回姓名匹配：班内李华唯一 → 命中该生（但不是靠学号）
    assert st is not None and st.id == only18.id and conflict is False


def test_single_digit_18_vs_08_picks_08(db):
    # 同名两人 08 / 18，OCR 读到 8 → 只兼容 08，唯一命中 08
    s08 = _add(db, "李华", "08")
    _add(db, "李华", "18")
    st, conflict = match_student_for_upload(db, T1, "李华", "8", 1)
    assert st is not None and st.id == s08.id and conflict is False


def test_long_number_scanned_short_stored(db):
    # 库里存 8，学生完整写 2021000008 → 末两位 08 佐证，命中
    target = _add(db, "李华", "8")
    st, conflict = match_student_for_upload(db, T1, "李华", "2021000008", 1)
    assert st is not None and st.id == target.id and conflict is False


def test_ambiguous_value_defers_to_name_conflict(db):
    # 两人学号 08 / 008（异串但同值 8，唯一约束允许并存）→ scanned 8 对两者都"去零同值"，
    # 精确候选 2 人、同班无法收窄 → 学号不猜。退回姓名：两人同名 → (None, True) 需人工确认。
    _add(db, "李华", "08")
    _add(db, "李华", "008")
    st, conflict = match_student_for_upload(db, T1, "李华", "8", 1)
    assert st is None and conflict is True


def test_single_digit_uniquely_matches_zero_padded(db):
    # scanned 8：008 去零同值命中，108（值108）不兼容 → 唯一命中 008，不歧义。
    target = _add(db, "甲", "008")
    _add(db, "乙", "108")
    st, conflict = match_student_for_upload(db, T1, "甲", "8", 1)
    assert st is not None and st.id == target.id and conflict is False


def test_multi_digit_suffix_unique(db):
    target = _add(db, "赵六", "12345")
    st, conflict = match_student_for_upload(db, T1, "赵六", "45", 1)
    assert st is not None and st.id == target.id


def test_exact_wins_over_suffix_when_both_present(db):
    # 45（精确去零同值）和 12345（后缀）都兼容 scanned=45 → 精确候选唯一，取 45
    exact = _add(db, "钱七", "45")
    _add(db, "钱七", "12345")
    st, conflict = match_student_for_upload(db, T1, "钱七", "45", 1)
    assert st is not None and st.id == exact.id and conflict is False


def test_class_scope_by_value(db):
    # 一班 08、二班 008：选一班 → 候选池先按班级收窄，二班 008 根本不进候选，
    # 一班内唯一命中 08。杜绝跨班误配（也不会把二班同座号误当歧义）。
    one = _add(db, "孙八", "08", class_id=1)
    _add(db, "孙八", "008", class_id=2)
    st, conflict = match_student_for_upload(db, T1, "孙八", "8", 1)
    assert st is not None and st.id == one.id and conflict is False


def test_full_number_does_not_leak_across_class(db):
    # 座号前缀跨班不同，但若两班座号后缀相同、学生写全号：选一班时绝不能判给二班学生。
    # 二班学生 2022000108，当前批改一班且一班没有该号 → 学号不命中，退回姓名（一班无此人）→ 新建。
    _add(db, "周九", "2022000108", class_id=2)
    st, conflict = match_student_for_upload(db, T1, "周九", "2022000108", 1)
    assert st is None and conflict is False  # 不跨班命中，交调用方在一班新建


def test_number_matches_within_selected_class(db):
    # 一班和二班各有一个座号 08 的不同学生；选一班、扫到 8 → 命中一班那个，不受二班干扰。
    a = _add(db, "吴一", "08", class_id=1)
    _add(db, "郑二", "008", class_id=2)
    st, conflict = match_student_for_upload(db, T1, "吴一", "8", 1)
    assert st is not None and st.id == a.id and conflict is False


def test_name_match_stays_in_class_no_cross_class_fallback(db):
    # 选了一班，但同名学生只在二班 → 不回退全老师范围，交调用方在一班新建（防跨班误判）。
    _add(db, "陈十", None, class_id=2)
    st, conflict = match_student_for_upload(db, T1, "陈十", None, 1)
    assert st is None and conflict is False


def test_name_match_teacher_wide_when_no_class_selected(db):
    # 未选班级（class_id=None）：退化为老师全量姓名匹配，唯一即命中（保留旧行为）。
    target = _add(db, "钱十一", None, class_id=2)
    st, conflict = match_student_for_upload(db, T1, "钱十一", None, None)
    assert st is not None and st.id == target.id and conflict is False


# ─────────────────────── 安全身份解析契约 ───────────────────────


def test_identity_unique_number_and_consistent_name_auto_matches(db):
    target = _add(db, "张三", "20230108")

    resolution = resolve_student_identity(db, T1, "张三", "20230108", 1)

    assert resolution["status"] == "matched"
    assert resolution["student_id"] == target.id
    assert resolution["reason"] == "unique_student_no"


def test_identity_unique_number_wins_when_name_is_wrong(db):
    target = _add(db, "张三", "20230108")

    resolution = resolve_student_identity(db, T1, "李四", "20230108", 1)

    assert resolution["status"] == "matched"
    assert resolution["student_id"] == target.id
    assert resolution["reason"] == "unique_student_no"


def test_identity_unique_name_fallback_survives_unknown_number(db):
    target = _add(db, "王五", "20230109")

    resolution = resolve_student_identity(db, T1, "王五", "999999", 1)

    assert resolution["status"] == "matched"
    assert resolution["student_id"] == target.id
    assert resolution["reason"] == "unique_name"


def test_identity_blank_fields_require_confirmation_without_candidates(db):
    _add(db, "赵六", "20230110")
    resolution = resolve_student_identity(db, T1, "", "", 1)

    assert resolution["status"] == "needs_confirmation"
    assert resolution["student_id"] is None
    assert resolution["reason"] == "missing_identity"
    assert resolution["ocr_name"] == ""
    assert resolution["ocr_student_no"] == ""
    assert [item["name"] for item in resolution["candidates"]] == ["赵六"]


def test_identity_unique_name_without_number_auto_matches(db):
    target = _add(db, "赵六", "20230110")

    resolution = resolve_student_identity(db, T1, "赵六", "", 1)

    assert resolution["status"] == "matched"
    assert resolution["student_id"] == target.id
    assert resolution["reason"] == "unique_name"


def test_identity_candidates_never_leak_from_other_class(db):
    _add(db, "孙七", "001", class_id=2)

    resolution = resolve_student_identity(db, T1, "孙七", "001", 1)

    assert resolution["status"] == "needs_confirmation"
    assert resolution["reason"] == "student_no_not_found"
    assert resolution["candidates"] == []


def test_identity_roster_ambiguous_candidates_require_teacher_confirmation(db):
    li_shuo = _add(db, "周涛", "01")
    wang_yihan = _add(db, "吴静", "02")
    _add(db, "赵六", "03")

    resolution = resolve_student_identity(
        db,
        T1,
        "李",
        "",
        1,
        roster_candidate_names=["周涛", "吴静"],
        roster_match_status="ambiguous",
    )

    assert resolution["status"] == "needs_confirmation"
    assert resolution["reason"] == "roster_name_ambiguous"
    assert [item["student_id"] for item in resolution["candidates"][:2]] == [
        li_shuo.id,
        wang_yihan.id,
    ]


def test_identity_roster_low_confidence_candidate_requires_teacher_confirmation(db):
    li_shuo = _add(db, "周涛", "01")
    _add(db, "吴静", "02")

    resolution = resolve_student_identity(
        db,
        T1,
        "李",
        "",
        1,
        roster_candidate_names=["周涛"],
        roster_match_status="low_confidence",
    )

    assert resolution["status"] == "needs_confirmation"
    assert resolution["reason"] == "roster_name_low_confidence"
    assert resolution["candidates"][0]["student_id"] == li_shuo.id


def test_identity_roster_no_match_exposes_only_current_class_for_teacher_fallback(db):
    one = _add(db, "赵六", "01", class_id=1)
    _add(db, "孙七", "02", class_id=2)

    resolution = resolve_student_identity(
        db,
        T1,
        "陈强",
        "",
        1,
        roster_candidate_names=[],
        roster_match_status="no_match",
    )

    assert resolution["status"] == "needs_confirmation"
    assert resolution["reason"] == "roster_name_not_found"
    assert [item["student_id"] for item in resolution["candidates"]] == [one.id]


# ─────────────────────── 学号自然排序 ───────────────────────


class TestStudentNoSortKey:
    """班级表格的学号列必须按人看的顺序排：2 在 11 前面。

    回归背景：原先是 SQL ``ORDER BY student_no``（字符串序），班级表格首屏
    给出 1 → 11 → 2，老师看着就是乱的。
    """

    @staticmethod
    def _sorted(values):
        return sorted(values, key=student_no_sort_key)

    def test_plain_numbers_sort_by_value_not_text(self):
        assert self._sorted(["11", "2", "1", "10", "3"]) == ["1", "2", "3", "10", "11"]

    def test_leading_zero_is_preserved_and_grouped_with_its_value(self):
        """045 与 45 同值，必须相邻，且都不能被 int() 抹掉前导零。"""
        assert self._sorted(["45", "045", "7"]) == ["7", "045", "45"]

    def test_letters_do_not_crash_and_sort_naturally(self):
        assert self._sorted(["2021A12345", "2021A2"]) == ["2021A2", "2021A12345"]

    def test_blank_numbers_sort_last(self):
        """学号选填，空号不该顶掉真实学号占据表格前部。"""
        assert self._sorted(["5", "", None, "1"]) == ["1", "5", "", None]

    def test_key_is_totally_ordered_across_mixed_shapes(self):
        """混排不得抛 TypeError（int 与 str 不可直接比较）。"""
        mixed = ["10", "2021A1", "", "045", None, "2"]
        assert self._sorted(mixed) == ["2", "10", "045", "2021A1", "", None]
