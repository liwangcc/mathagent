from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.shared import Cm, Mm
from lxml import etree

from services.exercise_document import (
    _COLUMN_GAP_TWIPS,
    _COLUMN_WIDTH_MM,
    _blank_line_count,
    build_exercise_documents,
    _extract_question_parts,
)
from services.exercise_schema import (
    ExerciseQuestion,
    ExerciseTypeCounts,
)


def _questions(raw: list[dict], expected: ExerciseTypeCounts) -> list[ExerciseQuestion]:
    """Parse fixture rows as ExercisePaper does, in choice/fill/solution order."""
    order = {"choice": 0, "fill": 1, "solution": 2}
    questions = sorted((ExerciseQuestion.model_validate(item) for item in raw), key=lambda q: order[q.type])
    assert {kind: sum(q.type == kind for q in questions) for kind in order} == expected.as_dict()
    return questions


_V_NS = "urn:schemas-microsoft-com:vml"
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _sample_questions() -> list[dict]:
    return [
        {
            "type": "choice",
            "question": "函数 f(x)=x² 在 x=2 时的函数值为（　）",
            "options": {"A": "2", "B": "4", "C": "6", "D": "8"},
            "answer": "B",
            "solution": "把 x=2 代入 f(x)=x²，得到 f(2)=4。",
            "knowledge_point": "函数求值",
            "difficulty": 1,
        },
        {
            "type": "fill",
            "question": "若 x+8=50，则 x=________",
            "options": {},
            "answer": "42",
            "solution": "等式两边同时减去 8，得到 x=42。",
            "knowledge_point": "一元一次方程",
            "difficulty": 1,
        },
        {
            "type": "solution",
            "question": "已知数列 aₙ=2n+1，求它的前 n 项和。",
            "options": {},
            "answer": "Sₙ=n²+2n",
            "solution": "Sₙ=Σ(2k+1)=n(n+1)+n=n²+2n。",
            "knowledge_point": "数列求和",
            "difficulty": 2,
        },
    ]


def _all_text(document: Document) -> str:
    values = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                values.extend(paragraph.text for paragraph in cell.paragraphs)
    return "\n".join(values)


def _iter_paragraphs(container):
    yield from container.paragraphs
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell)


def _shape_text(shape) -> str:
    return "".join(
        node.text or ""
        for node in shape.iter(f"{{{_W_NS}}}t")
    )


def _binding_header(section, side: str):
    assert section._sectPr.find(qn("w:pgBorders")) is None
    document_grid = section._sectPr.find(qn("w:docGrid"))
    assert document_grid.get(qn("w:type")) == "linesAndChars"
    assert document_grid.get(qn("w:linePitch")) == "312"
    assert document_grid.get(qn("w:charSpace")) == "-3149"
    assert not section.header.tables
    shapes = list(section.header._element.iter(f"{{{_V_NS}}}shape"))
    if side == "left":
        assert len(shapes) == 2
        line, identity = shapes
        assert "margin-left:-88.1pt" in line.get("style")
        assert "height:838.15pt" in line.get("style")
        line_text = _shape_text(line)
        assert all(label in line_text for label in ("装", "订", "线"))
        line_textbox = line.find(f"{{{_V_NS}}}textbox")
        assert line_textbox.get("style") == "layout-flow:vertical-ideographic;"

        assert "margin-left:-58.5pt" in identity.get("style")
        assert "height:594.7pt" in identity.get("style")
        identity_text = _shape_text(identity)
        for field in ("姓名：", "班级：", "学号："):
            assert field in identity_text
        for removed_field in ("学校：", "学科：", "考号："):
            assert removed_field not in identity_text
        identity_textbox = identity.find(f"{{{_V_NS}}}textbox")
        assert identity_textbox.get("style") == (
            "layout-flow:vertical;mso-layout-flow-alt:bottom-to-top;"
        )
        underlined_blanks = identity.xpath(
            './/w:r[w:rPr/w:u[@w:val="single"]]/w:t',
            namespaces={"w": _W_NS},
        )
        assert len(underlined_blanks) == 3
        assert all(not node.text.strip() for node in underlined_blanks)
        return shapes

    assert side == "right"
    assert len(shapes) == 1
    warning = shapes[0]
    assert "margin-left:1023.75pt" in warning.get("style")
    assert "height:839.9pt" in warning.get("style")
    warning_text = _shape_text(warning).replace("　", "")
    assert "装订线内不要答题" in warning_text
    assert not any(field in warning_text for field in ("姓名", "班级", "学号"))
    textbox = warning.find(f"{{{_V_NS}}}textbox")
    assert textbox.get("style") == "layout-flow:vertical-ideographic;"
    return shapes


def _pagination_sample() -> list[dict]:
    choice, fill, solution = _sample_questions()
    questions = []
    for index in range(12):
        item = deepcopy(choice)
        item["question"] = f"{item['question']}（选择变式 {index + 1}）"
        questions.append(item)
    for index in range(4):
        item = deepcopy(fill)
        item["question"] = f"{item['question']}（填空变式 {index + 1}）"
        questions.append(item)
    for index in range(6):
        item = deepcopy(solution)
        item["question"] = f"{item['question']}（解答变式 {index + 1}）"
        item["solution"] = f"{item['solution']} 写出第 {index + 1} 题的完整推导过程。"
        questions.append(item)
    return questions


def _compact_pagination_sample() -> list[dict]:
    questions = _pagination_sample()
    return [
        *[item for item in questions if item["type"] == "choice"][:11],
        *[item for item in questions if item["type"] == "fill"][:3],
        *[item for item in questions if item["type"] == "solution"][:5],
    ]


def _long_frequency_table_question() -> dict:
    question = deepcopy(_sample_questions()[2])
    question["question"] = r"""**19.&#x20;**【2017高考（新课标Ⅲ）】18.（12 分）某超市计划按月订购一种酸奶，每天进货量相同。为了确定六月份的订购计划，统计了前三年六月份各天的最高气温（单位：$^\circ C$），得到下面的频数分布表：
\[
\begin{array}{|c|c|c|c|c|c|c|}
\hline 最高气温 & [10,15) & [15,20) & [20,25) & [25,30) & [30,35) & [35,40] \\
\hline 天数 & 2 & 16 & 36 & 25 & 7 & 4 \\
\hline
\end{array}
\]"""
    return question


def _flattened_machine_quality_question() -> dict:
    """The table shape returned by the reported exercise-generation failure."""
    cells = [
        "**—**", "**一级品**", "**二级品**", "**合计**",
        "**甲机床**", "150", "50", "200",
        "**乙机床**", "120", "80", "200",
        "**合计**", "270", "130", "400",
    ]
    flattened = "\n\n".join(
        f"|   |\n| - |\n{cell}" for cell in cells
    )
    question = deepcopy(_sample_questions()[2])
    question["question"] = (
        "**17.&#x20;**【2021高考（全国甲卷）】甲、乙两台机床生产同种产品，"
        "产品按质量分为一级品和二级品，统计如下表：\n\n"
        f"{flattened}\n\n"
        "(1) 甲机床、乙机床生产的产品中一级品的频率分别是多少？\n\n"
        "附：\n\n"
        "(|l|l|l|l|)\n"
        "hline & 0.050 & 0.010 & 0.001 \\\\\n"
        "hline\n\n"
        "(|l|l|l|l|)\n"
        "hline & 3.841 & 6.635 & 10.828 \\\\\n"
        "hline"
    )
    return question


def test_count_only_clients_receive_stable_distribution():
    assert ExerciseTypeCounts.from_total(1).as_dict() == {
        "choice": 1,
        "fill": 0,
        "solution": 0,
    }
    assert ExerciseTypeCounts.from_total(10).as_dict() == {
        "choice": 4,
        "fill": 4,
        "solution": 2,
    }
    assert ExerciseTypeCounts.from_total(99).total == 22

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="1 到 22"):
        ExerciseTypeCounts(choice=12, fill=5, solution=6)

    with pytest.raises(ValidationError):
        ExerciseTypeCounts(choice=1, fill=1, solution=7)


def test_exercise_generation_accepts_full_curriculum_chapter_selection():
    from pydantic import ValidationError
    from routers.exercise import ExerciseGenerateRequest

    selected_chapters = [
        "必修第一册 第一章 集合与常用逻辑用语",
        "必修第一册 第二章 一元二次函数、方程与不等式",
        "必修第一册 第三章 函数的概念与性质",
        "必修第一册 第四章 指数函数与对数函数",
        "必修第一册 第五章 三角函数",
        "必修第二册 第六章 平面向量及其应用",
        "必修第二册 第七章 复数",
        "必修第二册 第十章 概率",
        "选修第一册 第一章 空间向量与立体几何",
        "选修第一册 第二章 直线和圆的方程",
        "选修第一册 第三章 圆锥曲线与方程",
        "选修第二册 第四章 数列",
    ]
    request = ExerciseGenerateRequest(chapters=selected_chapters)
    assert request.chapters == selected_chapters

    from services.math_curriculum import get_math_catalog

    for stage in ("junior", "senior"):
        all_chapters = [chapter["chapter"] for chapter in get_math_catalog(stage)]
        assert ExerciseGenerateRequest(chapters=all_chapters).chapters == all_chapters
    with pytest.raises(ValidationError, match="at most 64 items"):
        ExerciseGenerateRequest(chapters=[f"章节 {index}" for index in range(65)])


def test_correction_paper_uses_the_shared_export_question_contract():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base
    from models import GradingResult, Student
    from models_correction import QuestionAttemptEvent, WrongQuestionCycle
    from routers.exercise import _correction_paper
    from services.exercise_schema import ExercisePaper

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    student = Student(id=1, teacher_id=7, name="学生", student_no="001")
    grading = GradingResult(
        id=1, student_id=1, category="daily", exam_name="", image_content_hash="f" * 64,
        total_questions=1, correct_count=0, partial_count=0, wrong_count=1, needs_review_count=0,
    )
    attempt = QuestionAttemptEvent(
        id=1, student_id=1, teacher_id=7, grading_result_id=1, question_no="1",
        question_identity="fingerprint:test", identity_method="fingerprint", question_fingerprint="a" * 64,
        question_type="solve", question_snapshot_json='{"question_text":"求 x+1=0 的解","question_type":"solve","standard_answer":"x=-1","solution_steps":["移项"]}',
        student_answer_snapshot_json='{}', final_result="wrong", analytics_included=True,
    )
    cycle = WrongQuestionCycle(
        student_id=1, teacher_id=7, question_identity="fingerprint:test", question_fingerprint="a" * 64,
        cycle_no=1, status="pending", opened_attempt_id=1, wrong_count=1,
    )
    db.add_all([student, grading, attempt, cycle])
    db.commit()

    title, questions, available = _correction_paper(
        db,
        7,
        scope="personal",
        question_count=10,
        student_id=1,
    )
    paper = ExercisePaper.model_validate({"title": title, "questions": questions})
    assert available == 1
    assert paper.questions[0].question == "求 x+1=0 的解"
    assert paper.questions[0].answer == "x=-1"
    assert paper.questions[0].solution == "移项"
    assert paper.questions[0].source_wrong_cycle_id == cycle.id
    assert paper.questions[0].source_question_identity == "fingerprint:test"


def test_class_correction_paper_ranks_shared_questions_and_preserves_cycle_map():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base
    from models import GradingResult, Student
    from models_correction import QuestionAttemptEvent, WrongQuestionCycle
    from routers.exercise import _correction_paper
    from services.exercise_schema import ExercisePaper

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([
        Student(id=1, teacher_id=7, name="甲", student_no="001", class_name="高一1班"),
        Student(id=2, teacher_id=7, name="乙", student_no="002", class_name="高一1班"),
    ])
    now = datetime.now()
    for index, (student_id, identity, text, wrong_count) in enumerate([
        (1, "shared", "全班共性题", 2),
        (2, "shared", "全班共性题", 1),
        (1, "single", "个人错题", 9),
    ], start=1):
        db.add(GradingResult(
            id=index, student_id=student_id, category="daily", exam_name="",
            image_content_hash=f"{index:064d}", total_questions=1, correct_count=0,
            partial_count=0, wrong_count=1, needs_review_count=0,
        ))
        db.add(QuestionAttemptEvent(
            id=index, student_id=student_id, teacher_id=7, grading_result_id=index,
            question_no=str(index), question_identity=identity, identity_method="fingerprint",
            question_fingerprint=f"{index:064d}", question_type="solve",
            question_snapshot_json=json.dumps({
                "question_text": text, "question_type": "solve",
                "standard_answer": "答案", "solution_steps": ["步骤"],
            }, ensure_ascii=False),
            student_answer_snapshot_json="{}", final_result="wrong", analytics_included=True,
            occurred_at=now + timedelta(minutes=index),
        ))
        db.flush()
        db.add(WrongQuestionCycle(
            student_id=student_id, teacher_id=7, question_identity=identity,
            question_fingerprint=f"{index:064d}", cycle_no=1, status="pending",
            opened_attempt_id=index, wrong_count=wrong_count,
            opened_at=now + timedelta(minutes=index),
        ))
    db.commit()

    title, questions, available = _correction_paper(
        db, 7, scope="class", question_count=1, class_name="高一1班"
    )
    paper = ExercisePaper.model_validate({"title": title, "questions": questions})

    assert title == "高一1班高频原题订正卷"
    assert available == 2
    assert paper.questions[0].question == "全班共性题"
    assert paper.questions[0].wrong_student_count == 2
    assert paper.questions[0].wrong_event_count == 3
    assert paper.questions[0].source_wrong_cycles == [
        {"student_id": 1, "cycle_id": 1},
        {"student_id": 2, "cycle_id": 2},
    ]


def test_correction_generation_honors_count_and_reports_shortage(monkeypatch):
    from routers import exercise as exercise_router

    question = {
        "type": "solution", "question": "原题", "answer": "答案", "solution": "步骤",
        "options": {}, "knowledge_point": "原题订正", "difficulty": 2,
    }
    monkeypatch.setattr(
        exercise_router,
        "_correction_paper",
        lambda *_args, **kwargs: ("个人原题订正卷", [question], 1),
    )
    response = asyncio.run(exercise_router.generate_exercises(
        exercise_router.ExerciseGenerateRequest(
            mode="correction", correction_scope="personal", student_id=3, question_count=5,
        ),
        db=object(),
        user=SimpleNamespace(id=7, education_stage="senior"),
    ))

    assert response["requested_count"] == 5
    assert response["available_count"] == 1
    assert response["selected_count"] == 1
    assert response["shortage_message"] == "请求 5 道，当前共有 1 道待订正原题，已生成 1 道"


def test_all_exercise_modes_dispatch_to_code_only_bank_selector(monkeypatch):
    from services.exercise_bank_selector import SelectedExercise
    from services.exercise_schema import ExerciseQuestion
    from routers import exercise as exercise_router

    sample = ExerciseQuestion.model_validate(_sample_questions()[0])
    calls = []

    monkeypatch.setattr(
        exercise_router,
        "_bank_selection_scope",
        lambda _chapters, _topics: ("题库练习", ["函数"], [("函数", 1)]),
    )
    monkeypatch.setattr(
        exercise_router,
        "_personal_selection_scope",
        lambda _db, _user_id, _student_id, _chapters, _topics: (
            "个人练习", [["函数"]], [("函数", 5)]
        ),
    )
    monkeypatch.setattr(
        exercise_router,
        "_class_selection_scope",
        lambda _db, _user_id, _class_id, _class_name, _chapters, _topics: (
            "班级练习", [["函数"]], [("函数", 9)]
        ),
    )

    def fake_select(db, **kwargs):
        calls.append((db, kwargs))
        return [SelectedExercise(row=SimpleNamespace(id=101), question=sample)]

    monkeypatch.setattr(exercise_router, "select_exercises_from_bank", fake_select)
    fake_db = object()
    user = SimpleNamespace(id=7, education_stage="senior")
    requests = [
        exercise_router.ExerciseGenerateRequest(mode="bank", chapters=["函数"], question_count=1),
        exercise_router.ExerciseGenerateRequest(mode="personal", student_id=3, question_count=1),
        exercise_router.ExerciseGenerateRequest(mode="class", class_name="高一1班", question_count=1),
    ]

    responses = [
        asyncio.run(exercise_router.generate_exercises(request, db=fake_db, user=user))
        for request in requests
    ]

    assert [response["title"] for response in responses] == ["题库练习", "个人练习", "班级练习"]
    assert all(response["source"] == "question_bank" for response in responses)
    assert all(response["exercises"][0]["bank_question_id"] == 101 for response in responses)
    assert [call[1]["user_id"] for call in calls] == [7, 7, 7]
    assert not hasattr(exercise_router, "_call_llm")
    assert not hasattr(exercise_router, "check_llm_rate")


def test_exercise_generation_reports_per_type_bank_shortage(monkeypatch):
    from fastapi import HTTPException
    from services.exercise_bank_selector import QuestionBankShortage
    from routers import exercise as exercise_router

    monkeypatch.setattr(
        exercise_router,
        "_bank_selection_scope",
        lambda _chapters, _topics: ("测试练习", ["函数"], [("函数", 1)]),
    )
    monkeypatch.setattr(
        exercise_router,
        "select_exercises_from_bank",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            QuestionBankShortage(
                {"choice": 2, "fill": 1, "solution": 1},
                {"choice": 1, "fill": 1, "solution": 0},
            )
        ),
    )

    request = exercise_router.ExerciseGenerateRequest(
        chapters=["函数"],
        type_counts=ExerciseTypeCounts(choice=2, fill=1, solution=1),
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            exercise_router.generate_exercises(
                request,
                db=object(),
                user=SimpleNamespace(id=1, education_stage="senior"),
            )
        )

    assert caught.value.status_code == 409
    assert "选择题缺 1 道" in caught.value.detail
    assert "解答题缺 1 道" in caught.value.detail


def test_personal_and_class_modes_select_tagged_questions_end_to_end(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base
    from models import QuestionBank, Student, StudentProfile, Weakness
    from routers import exercise as exercise_router

    engine = create_engine(f"sqlite:///{tmp_path / 'exercise-modes.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    student = Student(id=1, teacher_id=7, name="测试学生", class_name="高一1班")
    profile = StudentProfile(student_id=1, education_stage="senior")
    profile.set_details(
        [
            {"result": "wrong", "knowledge_point": "单调性"},
            {"result": "partial", "knowledge_point": "单调性"},
            {"result": "correct", "knowledge_point": "数列"},
        ]
    )
    bank_rows = [
        QuestionBank(
            id=11,
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
            id=12,
            teacher_id=7,
            is_public=False,
            chapter="函数",
            topic="单调性",
            question_type="fill",
            question="单调区间为________________。",
            answer="(0,+∞)",
            solution_steps='["求导判断"]',
        ),
        QuestionBank(
            id=13,
            teacher_id=7,
            is_public=False,
            chapter="函数",
            topic="单调性",
            question_type="solution",
            question="(1) 求导；(2) 讨论函数单调性。",
            answer="见解析",
            solution_steps='["求导", "列表讨论"]',
        ),
        QuestionBank(
            id=14,
            teacher_id=8,
            is_public=False,
            chapter="函数",
            topic="单调性",
            question_type="choice",
            question="其他老师私题（ ） A. 1 B. 2 C. 3 D. 4",
            answer="B",
        ),
    ]
    for bank_row in bank_rows:
        from services.math_curriculum import catalog_version

        bank_row.education_stage = "senior"
        bank_row.catalog_version = catalog_version("senior")
        bank_row.chapter = "函数的概念与性质"
        bank_row.topic = "函数的单调性"
    profile.set_details([
        {"result": "wrong", "knowledge_point": "函数的单调性"},
        {"result": "partial", "knowledge_point": "函数的单调性"},
        {"result": "correct", "knowledge_point": "数列"},
    ])
    db.add_all(
        [
            student,
            profile,
            Weakness(student_id=1, topic="函数的单调性", occurrences=5, education_stage="senior"),
            *bank_rows,
        ]
    )
    db.commit()
    counts = ExerciseTypeCounts(choice=1, fill=1, solution=1)
    user = SimpleNamespace(id=7, education_stage="senior")

    personal = asyncio.run(
        exercise_router.generate_exercises(
            exercise_router.ExerciseGenerateRequest(
                mode="personal", student_id=1, type_counts=counts
            ),
            db=db,
            user=user,
        )
    )
    class_paper = asyncio.run(
        exercise_router.generate_exercises(
            exercise_router.ExerciseGenerateRequest(
                mode="class", class_name="高一1班", type_counts=counts
            ),
            db=db,
            user=user,
        )
    )

    assert personal["source"] == class_paper["source"] == "question_bank"
    assert {item["bank_question_id"] for item in personal["exercises"]} == {11, 12, 13}
    assert {item["bank_question_id"] for item in class_paper["exercises"]} == {11, 12, 13}
    assert all(item["knowledge_point"] == "函数的单调性" for item in personal["exercises"])
    assert db.get(QuestionBank, 14).usage_count == 0
    db.close()


def test_class_scope_prioritizes_number_of_students_before_repeat_count(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base
    from models import Student, Weakness
    from routers.exercise import _class_selection_scope

    engine = create_engine(f"sqlite:///{tmp_path / 'class-priority.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([
        Student(id=1, teacher_id=7, name="甲", class_name="高一1班"),
        Student(id=2, teacher_id=7, name="乙", class_name="高一1班"),
        Weakness(student_id=1, topic="单人重复", occurrences=50),
        Weakness(student_id=1, topic="班级共性", occurrences=1),
        Weakness(student_id=2, topic="班级共性", occurrences=1),
    ])
    db.commit()

    title, groups, priorities = _class_selection_scope(
        db, 7, None, "高一1班", [], []
    )

    assert title == "高一1班班级共性强化"
    assert groups[0][:2] == ["班级共性", "单人重复"]
    assert priorities[0][1] > priorities[1][1]
    db.close()


def test_personal_scope_uses_pending_cycle_knowledge_and_does_not_revive_closed_history(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models  # noqa: F401
    import models_rate_limit  # noqa: F401
    import models_user  # noqa: F401
    from database import Base
    from fastapi import HTTPException
    from models import Student
    from models_user import User
    from routers.exercise import _personal_selection_scope
    from services.grading_persistence import persist_grading_result

    engine = create_engine(f"sqlite:///{tmp_path / 'personal-cycle.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    teacher = User(id=7, name="教师", role="teacher")
    student = Student(id=1, teacher_id=7, name="甲", student_no="001")
    db.add_all([teacher, student])
    db.commit()
    base_detail = {
        "question_no": "1",
        "question": "判断函数单调性",
        "question_type": "solve",
        "standard_answer": "递增",
        "student_answer": "递减",
        "result": "wrong",
    }

    def persist(image_hash: str, result: str):
        detail = {**base_detail, "result": result}
        persist_grading_result(
            db,
            student=student,
            teacher_id=teacher.id,
            category="daily",
            exam_name="",
            image_path="",
            image_hash=image_hash,
            total_questions=1,
            correct_count=int(result == "correct"),
            partial_count=0,
            wrong_count=int(result == "wrong"),
            needs_review_count=0,
            score=None,
            details=[detail],
            knowledge_items=[{"chapter": "函数", "topic": "单调性"}],
        )

    persist("e1" * 32, "wrong")
    _title, groups, priorities = _personal_selection_scope(db, 7, 1, [], [])
    assert groups[0] == ["单调性"]
    assert priorities == [("单调性", 1)]

    persist("e2" * 32, "correct")
    with pytest.raises(HTTPException, match="当前没有待纠正错题"):
        _personal_selection_scope(db, 7, 1, [], [])
    db.close()


def test_docx_is_a3_landscape_two_column_editable_and_separates_answers():
    questions = _questions(
        _sample_questions(),
        ExerciseTypeCounts(choice=1, fill=1, solution=1),
    )
    paper_bytes, answer_bytes = build_exercise_documents("函数与数列阶段练习", questions)

    assert paper_bytes.startswith(b"PK")
    assert answer_bytes.startswith(b"PK")

    paper = Document(BytesIO(paper_bytes))
    answer = Document(BytesIO(answer_bytes))
    assert len(paper.sections) == 2
    assert paper.sections[-1].orientation == WD_ORIENT.LANDSCAPE
    assert round(paper.sections[-1].page_width.mm) == 420
    assert round(paper.sections[-1].page_height.mm) == 297
    assert round(paper.sections[-1].left_margin.mm) == 35
    assert round(paper.sections[-1].right_margin.mm) == 20

    columns = paper.sections[-1]._sectPr.find(qn("w:cols"))
    assert columns is not None
    assert columns.get(qn("w:num")) == "2"
    assert columns.get(qn("w:sep")) == "1"
    assert paper._element.xpath('.//w:br[@w:type="column"]')

    for section in paper.sections:
        _binding_header(section, "left")
        assert not section.even_page_header.tables
    settings = paper.settings._element
    assert settings.find(qn("w:mirrorMargins")) is None
    assert settings.find(qn("w:evenAndOddHeaders")) is None

    paper_text = _all_text(paper)
    answer_text = _all_text(answer)
    for field in ("姓名：", "学号：", "班级：", "日期：", "考试时间：", "得分："):
        assert field not in paper_text
    assert "装订线内不要答题" not in paper_text
    assert "科目：高中数学" in answer_text
    assert "日期：____年__月__日" in answer_text
    for heading in ("一、选择题", "二、填空题", "三、解答题"):
        assert heading in paper_text
    for stem in ("函数 f(x)=x²", "若 x+8=50", "已知数列 aₙ=2n+1"):
        assert stem in paper_text
    assert "答案：" not in paper_text
    assert "等式两边同时减去 8" not in paper_text
    assert "答案：" in answer_text
    assert "等式两边同时减去 8" in answer_text
    assert len(paper.inline_shapes) == 0

    title = next(paragraph for paragraph in paper.paragraphs if "函数与数列阶段练习" in paragraph.text)
    title.add_run("（教师修改版）")
    edited_stream = BytesIO()
    paper.save(edited_stream)
    edited = Document(BytesIO(edited_stream.getvalue()))
    assert "教师修改版" in _all_text(edited)


def test_docx_translates_latex_into_editable_word_math():
    questions = deepcopy(_sample_questions())
    questions[0]["question"] = r"已知 $f(x)=\frac{x^2}{\sqrt[]{3}}$，则 $f(3)=$（　）"
    questions[0]["options"] = {
        "A": r"$\sqrt{3}$",
        "B": r"$3\sqrt{3}$",
        "C": r"$\frac{1}{3}$",
        "D": r"$\sqrt[3]{8}$",
    }
    paper_bytes, _ = build_exercise_documents("公式转译测试", questions)
    with ZipFile(BytesIO(paper_bytes)) as archive:
        document_xml = archive.read("word/document.xml")

    assert document_xml.count(b"<m:oMath>") >= 5
    assert b"<m:f>" in document_xml
    assert b"<m:rad>" in document_xml
    assert b"\\frac" not in document_xml
    assert b"\\sqrt" not in document_xml
    assert b"$" not in document_xml

    root = etree.fromstring(document_xml)
    radicals = root.xpath(".//m:rad", namespaces={"m": _M_NS})
    square_roots = [
        radical
        for radical in radicals
        if not "".join(radical.xpath("./m:deg//m:t/text()", namespaces={"m": _M_NS})).strip()
    ]
    nth_roots = [radical for radical in radicals if radical not in square_roots]
    assert square_roots
    assert nth_roots
    assert all(
        radical.xpath(
            './m:radPr/m:degHide[@m:val="1"]',
            namespaces={"m": _M_NS},
        )
        for radical in square_roots
    )
    assert all(
        not radical.xpath("./m:radPr/m:degHide", namespaces={"m": _M_NS})
        for radical in nth_roots
    )


def test_docx_cleans_source_markup_and_converts_latex_frequency_table():
    questions = _questions(
        [_long_frequency_table_question()],
        ExerciseTypeCounts(choice=0, fill=0, solution=1),
    )
    paper_bytes, answer_bytes = build_exercise_documents("频数表排版测试", questions)

    for payload in (paper_bytes, answer_bytes):
        document = Document(BytesIO(payload))
        text = _all_text(document)
        assert "&#x20;" not in text
        assert "**" not in text
        assert "\\hline" not in text
        assert "|c|c|" not in text
        assert "1. 19." not in text
        assert "】18." not in text
        assert "】18．" not in text
        assert "最高气温（单位：℃）" in text
        table = next(
            table
            for table in document.tables
            if "最高气温" in table.rows[0].cells[0].text
        )
        assert len(table.rows) == 2
        assert len(table.columns) == 7
        assert "[20,25)" in _all_text(document)
        assert "36" in table.rows[1].cells[3].text


def test_flattened_markdown_and_damaged_latex_tables_are_recovered_in_order():
    question = _flattened_machine_quality_question()
    parts = _extract_question_parts(question["question"])
    tables = [content for kind, content in parts if kind == "table"]
    assert len(tables) == 3
    assert tables[0] == [
        ["—", "一级品", "二级品", "合计"],
        ["甲机床", "150", "50", "200"],
        ["乙机床", "120", "80", "200"],
        ["合计", "270", "130", "400"],
    ]
    assert tables[1] == [["—", "0.050", "0.010", "0.001"]]
    assert tables[2] == [["—", "3.841", "6.635", "10.828"]]

    paper_bytes, answer_bytes = build_exercise_documents(
        "损坏表格回归", [question]
    )
    for payload in (paper_bytes, answer_bytes):
        document = Document(BytesIO(payload))
        text = _all_text(document)
        for raw_marker in ("&#x20;", "| - |", "(|l|l|l|l|)", "hline"):
            assert raw_marker not in text
        data_tables = [
            table for table in document.tables
            if any("甲机床" in cell.text for row in table.rows for cell in row.cells)
        ]
        assert len(data_tables) == 1
        assert len(data_tables[0].rows) == 4
        assert data_tables[0].rows[1].cells[1].text == "150"
        assert any(
            "0.050" in cell.text
            for table in document.tables
            for row in table.rows
            for cell in row.cells
        )


def test_long_student_papers_do_not_force_a_redundant_page_orphan():
    base = _sample_questions()
    questions = [deepcopy(base[index % 3]) for index in range(50)]
    paper_bytes, _ = build_exercise_documents("长卷分页回归", questions)
    paper = Document(BytesIO(paper_bytes))
    numbered = {
        int(match.group(1)): paragraph
        for paragraph in _iter_paragraphs(paper)
        if (match := re.match(r"^(\d{1,2})\. ", paragraph.text))
    }
    # The second solution question used to receive an unconditional page
    # break after Word had already moved the preceding content to a new page.
    assert numbered[36].paragraph_format.page_break_before is not True
    assert numbered[50].paragraph_format.page_break_before is not True


def test_standard_11_3_5_paper_follows_teacher_layout_and_aligns_choice_slots():
    questions = _questions(
        _compact_pagination_sample(),
        ExerciseTypeCounts(choice=11, fill=3, solution=5),
    )
    paper_bytes, _ = build_exercise_documents("A3 双栏教师排版测试", questions)
    paper = Document(BytesIO(paper_bytes))
    text = _all_text(paper)
    with ZipFile(BytesIO(paper_bytes)) as archive:
        document_xml = archive.read("word/document.xml")
        word_xml = b"".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        )

    assert "一、选择题（共 11 题）" in text
    assert "二、填空题（共 3 题）" in text
    assert "三、解答题（共 5 题）" in text
    assert "（续，余" not in text
    assert document_xml.count(b'w:type="column"') >= 1
    assert word_xml.count(b"<v:shape") == 3
    assert b"w:tblpPr" not in word_xml
    assert b"w:pgBorders" not in word_xml
    assert len(paper.sections) == 3
    _binding_header(paper.sections[0], "left")
    assert not paper.sections[2].header.tables
    _binding_header(paper.sections[1], "left")
    _binding_header(paper.sections[2], "right")
    assert round(paper.sections[2].left_margin.mm) == 20
    assert round(paper.sections[2].right_margin.mm) == 35
    second_page_columns = paper.sections[2]._sectPr.find(qn("w:cols"))
    assert second_page_columns.get(qn("w:num")) == "1"
    second_page_layout = next(
        table
        for table in paper.tables
        if len(table.rows) == 1
        and len(table.rows[0].cells) == 3
        and "12. " in table.rows[0].cells[0].text
    )
    second_page_cells = second_page_layout.rows[0].cells
    assert "17. " in second_page_cells[0].text
    assert second_page_cells[1].text == ""
    for number in (18, 19):
        assert f"{number}. " in second_page_cells[2].text
    assert [column.width.twips for column in second_page_layout.columns] == [
        Mm(_COLUMN_WIDTH_MM).twips,
        _COLUMN_GAP_TWIPS,
        Mm(_COLUMN_WIDTH_MM).twips,
    ]
    for cell in (second_page_cells[0], second_page_cells[2]):
        margins = cell._tc.tcPr.first_child_found_in("w:tcMar")
        assert margins.find(qn("w:start")).get(qn("w:w")) == "0"
        assert margins.find(qn("w:end")).get(qn("w:w")) == "0"

    numbered = {}
    for paragraph in _iter_paragraphs(paper):
        match = re.match(r"^(\d{1,2})\. ", paragraph.text)
        if match:
            numbered[int(match.group(1))] = paragraph
    assert set(range(1, 20)).issubset(numbered)

    for number in range(1, 12):
        paragraph = numbered[number]
        assert paragraph.text.endswith("\t（　）")
        assert len(re.findall(r"[（(][\s\u3000]*[）)]", paragraph.text)) == 1
        right_tabs = paragraph._p.xpath('./w:pPr/w:tabs/w:tab[@w:val="right"]')
        assert len(right_tabs) == 1
        column_gap_mm = 840 * 25.4 / 1440
        expected_column_width = (420 - 35 - 20 - column_gap_mm) / 2
        assert right_tabs[0].get(qn("w:pos")) == str(Mm(expected_column_width).twips)

    previous = numbered[9]._p.getprevious()
    assert previous is not None
    assert previous.xpath('.//w:br[@w:type="column"]')
    assert not numbered[18]._p.getprevious().xpath('.//w:br[@w:type="column"]')
    assert not numbered[16]._p.xpath('./w:pPr/w:pageBreakBefore')

    assert _blank_line_count(questions[14].model_dump(), solution_count=5) >= 8
    for question in questions[14:]:
        assert _blank_line_count(question.model_dump(), solution_count=5) == 11


def test_student_spacing_and_two_page_flow_do_not_change_answer_layout():
    questions = _questions(
        _pagination_sample()[:2]
        + _pagination_sample()[12:14]
        + _pagination_sample()[16:19],
        ExerciseTypeCounts(choice=2, fill=2, solution=3),
    )
    paper_bytes, answer_bytes = build_exercise_documents("学生卷排版约束测试", questions)
    paper = Document(BytesIO(paper_bytes))
    answer = Document(BytesIO(answer_bytes))

    paper_numbered = {}
    for paragraph in _iter_paragraphs(paper):
        match = re.match(r"^(\d{1,2})\. ", paragraph.text)
        if match:
            paper_numbered[int(match.group(1))] = paragraph
    assert paper_numbered[1].paragraph_format.line_spacing == 1.5
    assert paper_numbered[3].paragraph_format.line_spacing == 1.5
    assert paper_numbered[5].paragraph_format.page_break_before is not True
    assert paper_numbered[6].paragraph_format.page_break_before is True
    assert paper_numbered[7].paragraph_format.page_break_before is not True
    paper_options = [
        paragraph
        for paragraph in _iter_paragraphs(paper)
        if paragraph.text.startswith("A. ")
    ]
    assert paper_options
    assert all(paragraph.paragraph_format.line_spacing == 1.5 for paragraph in paper_options)
    assert _blank_line_count(questions[4].model_dump(), solution_count=3) >= 12

    answer_numbered = {}
    for paragraph in _iter_paragraphs(answer):
        match = re.match(r"^(\d{1,2})\. ", paragraph.text)
        if match:
            answer_numbered[int(match.group(1))] = paragraph
    assert answer_numbered[1].paragraph_format.line_spacing != 1.5
    assert answer_numbered[3].paragraph_format.line_spacing != 1.5
    assert answer_numbered[6].paragraph_format.page_break_before is not True
    answer_options = [
        paragraph
        for paragraph in _iter_paragraphs(answer)
        if paragraph.text.startswith("A. ")
    ]
    assert answer_options
    assert all(paragraph.paragraph_format.line_spacing != 1.5 for paragraph in answer_options)
    assert "科目：高中数学" in _all_text(answer)


def test_22_question_a3_paper_keeps_numbering_and_wps_safe_binding_line():
    questions = _questions(
        _pagination_sample(),
        ExerciseTypeCounts(choice=12, fill=4, solution=6),
    )
    paper_bytes, _ = build_exercise_documents("A3 双栏两页装订测试", questions)
    paper = Document(BytesIO(paper_bytes))
    text = _all_text(paper)
    for number in (1, 12, 13, 16, 17, 22):
        assert f"{number}. " in text
    numbered = {
        int(match.group(1)): paragraph
        for paragraph in _iter_paragraphs(paper)
        if (match := re.match(r"^(\d{1,2})\. ", paragraph.text))
    }
    assert numbered[17].paragraph_format.page_break_before is not True
    assert numbered[18].paragraph_format.page_break_before is True
    assert all(
        numbered[number].paragraph_format.page_break_before is not True
        for number in range(19, 23)
    )
    for section in paper.sections:
        _binding_header(section, "left")
    assert not paper._element.xpath('.//w:tblpPr')
