import random
import sqlite3
from types import SimpleNamespace

import pytest

from services.exercise_bank_selector import (
    QuestionBankShortage,
    infer_question_type,
    parse_choice_question,
    select_exercises_from_bank,
    select_exercises_from_candidates,
)
from services.exercise_schema import ExerciseTypeCounts


def _row(
    row_id: int,
    question: str,
    answer: str,
    *,
    question_type: str = "",
    topic: str = "函数",
    chapter: str = "函数的概念与性质",
    difficulty: int = 2,
    usage_count: int = 0,
    steps=None,
):
    row = SimpleNamespace(
        id=row_id,
        question=question,
        answer=answer,
        question_type=question_type,
        topic=topic,
        chapter=chapter,
        difficulty=difficulty,
        usage_count=usage_count,
    )
    row.get_solution_steps = lambda: list(steps or [])
    return row


def test_parse_choice_question_handles_standard_and_ocr_separators():
    standard = "设集合 A={1,2}，则正确的是（ ）\nA. 选项一\nB. 选项二\nC. 选项三\nD. 选项四"
    stem, options = parse_choice_question(standard)
    assert stem == "设集合 A={1,2}，则正确的是（ ）"
    assert options == {"A": "选项一", "B": "选项二", "C": "选项三", "D": "选项四"}

    escaped_tabs = "函数定义域为 ( ) A {x|x≥0}\\tB. {x|x>0}\\tC. R\\tD. ∅"
    stem, options = parse_choice_question(escaped_tabs)
    assert stem == "函数定义域为 ( )"
    assert options["A"] == "{x|x≥0}"
    assert options["D"] == "∅"


def test_infer_question_type_is_strict_and_rejects_incomplete_choice():
    assert infer_question_type(
        "下列说法正确的是（ ） A. 甲 B. 乙 C. 丙 D. 丁", "BC"
    ) == "choice"
    assert infer_question_type("函数值为________________。", "3") == "fill"
    assert infer_question_type("【2020高考】18. (12分) (1) 求单调区间；(2) 证明不等式。", "解") == "solution"
    assert infer_question_type("求 x 的值。", "A") == ""


def test_selector_returns_exact_counts_and_valid_exercise_contract():
    rows = [
        _row(1, "函数 f(x)=x，则（ ）\nA. 1\nB. 2\nC. 3\nD. 4", "B", question_type="choice"),
        _row(2, "函数 f(x)=2x，则（ ）\nA. 1\nB. 2\nC. 3\nD. 4", "C", question_type="choice"),
        _row(3, "f(1)=________________。", "2", question_type="fill", steps=["代入 x=1", "得到 2"]),
        _row(4, "g(1)=________________。", "3", question_type="fill"),
        _row(5, "(1) 求函数单调区间；(2) 求最值。", "见解析", question_type="solution", steps=["求导", "讨论符号"]),
        _row(6, "题干缺少四个选项。", "A", question_type="choice"),
    ]

    selected = select_exercises_from_candidates(
        rows,
        ExerciseTypeCounts(choice=2, fill=1, solution=1),
        priority_terms=[("函数", 3)],
        rng=random.Random(7),
    )

    questions = [item.question for item in selected]
    assert [question.type for question in questions] == ["choice", "choice", "fill", "solution"]
    assert questions[0].options.keys() == {"A", "B", "C", "D"}
    assert questions[2].solution == "代入 x=1\n得到 2"
    assert len({item.row.id for item in selected}) == 4


def test_selector_reports_per_type_shortage_without_using_invalid_rows():
    rows = [
        _row(1, "题干缺少四个选项。", "A", question_type="choice"),
        _row(2, "f(1)=________________。", "2", question_type="fill"),
    ]

    with pytest.raises(QuestionBankShortage) as caught:
        select_exercises_from_candidates(
            rows,
            ExerciseTypeCounts(choice=1, fill=1, solution=1),
            rng=random.Random(1),
        )

    assert caught.value.available == {"choice": 0, "fill": 1, "solution": 0}
    assert "选择题缺 1 道" in str(caught.value)
    assert "解答题缺 1 道" in str(caught.value)


def test_question_bank_type_migration_backfills_only_strict_types_and_is_idempotent():
    from database import _migrate_question_bank_types

    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE question_bank ("
        "id INTEGER PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL)"
    )
    cur.executemany(
        "INSERT INTO question_bank(id, question, answer) VALUES (?, ?, ?)",
        [
            (1, "函数值为（ ） A. 1 B. 2 C. 3 D. 4", "B"),
            (2, "函数值为________________。", "3"),
            (3, "【2020高考】18. (12分) (1) 求导；(2) 证明。", "见解析"),
            (4, "题干缺少选项。", "A"),
        ],
    )

    assert _migrate_question_bank_types(cur) == {"choice": 1, "fill": 1, "solution": 1, "unknown": 1}
    cur.execute("SELECT id, question_type FROM question_bank ORDER BY id")
    assert cur.fetchall() == [(1, "choice"), (2, "fill"), (3, "solution"), (4, "unknown")]

    assert _migrate_question_bank_types(cur) == {"choice": 0, "fill": 0, "solution": 0, "unknown": 0}
    conn.close()


def test_database_selector_respects_scope_ownership_and_records_usage():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models_user  # noqa: F401 - register users table for the foreign key
    from models import QuestionBank

    engine = create_engine("sqlite:///:memory:")
    QuestionBank.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    own_rows = [
        QuestionBank(
            id=1,
            teacher_id=7,
            is_public=False,
            chapter="函数",
            topic="单调性",
            question_type="choice",
            question="函数单调递增的是（ ） A. 1 B. 2 C. 3 D. 4",
            answer="A",
            solution_steps='["逐项判断"]',
        ),
        QuestionBank(
            id=2,
            teacher_id=None,
            is_public=True,
            chapter="函数",
            topic="单调性",
            question_type="fill",
            question="f(1)=________________。",
            answer="2",
            solution_steps='["代入"]',
        ),
        QuestionBank(
            id=3,
            teacher_id=7,
            is_public=False,
            chapter="函数",
            topic="单调性",
            question_type="solution",
            question="(1) 求导；(2) 讨论单调性。",
            answer="见解析",
            solution_steps='["求导", "列表讨论"]',
        ),
    ]
    hidden = QuestionBank(
        id=4,
        teacher_id=8,
        is_public=False,
        chapter="函数",
        topic="单调性",
        question_type="choice",
        question="另一位老师的题（ ） A. 1 B. 2 C. 3 D. 4",
        answer="B",
    )
    outside_scope = QuestionBank(
        id=5,
        teacher_id=7,
        is_public=False,
        chapter="数列",
        topic="等差数列",
        question_type="choice",
        question="数列题（ ） A. 1 B. 2 C. 3 D. 4",
        answer="C",
    )
    session.add_all([*own_rows, hidden, outside_scope])
    session.commit()

    selected = select_exercises_from_bank(
        session,
        user_id=7,
        counts=ExerciseTypeCounts(choice=1, fill=1, solution=1),
        scope_terms=["函数"],
        priority_terms=[("单调性", 5)],
        rng=random.Random(3),
    )

    assert {item.row.id for item in selected} == {1, 2, 3}
    assert session.get(QuestionBank, 1).usage_count == 1
    assert session.get(QuestionBank, 4).usage_count == 0
    assert session.get(QuestionBank, 5).usage_count == 0
    session.close()


def test_database_selector_intersects_weakness_and_teacher_scope():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models_user  # noqa: F401
    from models import QuestionBank

    engine = create_engine("sqlite:///:memory:")
    QuestionBank.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        QuestionBank(
            id=1, teacher_id=7, is_public=False, chapter="函数", topic="单调性",
            question_type="fill", question="函数单调区间为________________。", answer="R",
        ),
        QuestionBank(
            id=2, teacher_id=7, is_public=False, chapter="函数", topic="奇偶性",
            question_type="fill", question="函数奇偶性为________________。", answer="奇函数",
        ),
        QuestionBank(
            id=3, teacher_id=7, is_public=False, chapter="数列", topic="等差数列",
            question_type="fill", question="公差为________________。", answer="2",
        ),
    ])
    session.commit()

    selected = select_exercises_from_bank(
        session,
        user_id=7,
        counts=ExerciseTypeCounts(choice=0, fill=1, solution=0),
        scope_term_groups=[["单调性", "等差数列"], ["函数"]],
        priority_terms=[("单调性", 8), ("等差数列", 5)],
        rng=random.Random(1),
    )

    assert [item.row.id for item in selected] == [1]
    assert session.get(QuestionBank, 2).usage_count == 0
    assert session.get(QuestionBank, 3).usage_count == 0
    session.close()


def test_database_selector_intersects_chapter_and_topic_groups():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models_user  # noqa: F401
    from models import QuestionBank

    engine = create_engine("sqlite:///:memory:")
    QuestionBank.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        QuestionBank(
            id=1, teacher_id=7, is_public=False, chapter="函数", topic="单调性",
            question_type="fill", question="函数单调区间为________________。", answer="R",
        ),
        QuestionBank(
            id=2, teacher_id=7, is_public=False, chapter="函数", topic="奇偶性",
            question_type="fill", question="函数奇偶性为________________。", answer="奇函数",
        ),
        QuestionBank(
            id=3, teacher_id=7, is_public=False, chapter="数列", topic="单调性",
            question_type="fill", question="数列单调性为________________。", answer="递增",
        ),
    ])
    session.commit()

    selected = select_exercises_from_bank(
        session,
        user_id=7,
        counts=ExerciseTypeCounts(choice=0, fill=1, solution=0),
        scope_term_groups=[["单调性"], ["函数"], ["单调性"]],
        priority_terms=[("单调性", 8)],
        rng=random.Random(1),
    )

    assert [item.row.id for item in selected] == [1]
    assert session.get(QuestionBank, 2).usage_count == 0
    assert session.get(QuestionBank, 3).usage_count == 0
    session.close()
