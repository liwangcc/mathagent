"""Mixed numbers ("25 2/5") are parsed exactly; readable answers are decided, not parked for review.

Regression for the altitude/temperature application question: the student
wrote mixed numbers against a decimal reference (A=25.8, B=24, 山顶=22.8) and
the text model returned needs_review.  Routing contract:

* OCR tolerance applies (method + own final result correct, isolated misread
  number such as derived 25 4/5 transcribed 25 2/5) -> correct;
* a clear error outside the tolerance -> wrong (partial only when some
  independent results are right);
* only unreadable/doubtful OCR or content code cannot parse -> needs_review.
"""

import asyncio
from fractions import Fraction

import pytest

from services import grader
from services.grader import (
    NUMERIC_FINAL_ANSWER_METHOD,
    NUMERIC_OCR_TOLERANCE_METHOD,
    grade_paper_unified,
)
from services.matchers.math_matcher import MathMatcher
from services.numeric_answers import compare_final_numbers, format_value, parse_number
from services.quick_grader import _compare_one


REFERENCE = "A=25.8，B=24，山顶=22.8"
# The owner's screenshot: rate 4/5 vs 3/5, a false step (7×2/5≠4 4/5) and no
# derivation of 25.8 or 22.8 anywhere -> the tolerance cannot apply.
SCREENSHOT_TRANSCRIPTION = "2×4/5=1 3/5\n5×3/5=3\n7×2/5=4 4/5\nA=25 2/5，B=24，山顶=22 1/5"
CORRECT_TRANSCRIPTION = "3×3/5=1 4/5\n5×3/5=3\n2×3/5=1 1/5\nA=25 4/5，B=24，山顶=22 4/5"
# Correct method and derivation; only the copied conclusion 25 4/5 was
# transcribed as 25 2/5.
TOLERANCE_TRANSCRIPTION = (
    "3×3/5=1 4/5\n24+1 4/5=25 4/5\n2×3/5=1 1/5\n24-1 1/5=22 4/5\n"
    "A=25 2/5，B=24，山顶=22 4/5"
)
# Working in improper fractions: the student wrote 24+9/5=129/5 (=25.8) and
# the conclusion 25 4/5; OCR read 129/5 as 127/5 and 25 4/5 as 25 2/5, so the
# transcription never shows 25.8 anywhere.
IMPROPER_FRACTION_TRANSCRIPTION = "3×3/5=9/5\n24+9/5=127/5\nA=25 2/5"


class ImmediateQueue:
    async def submit(self, coro_factory, **_kwargs):
        return await coro_factory()


def _judged(result: str, explanation: str = "") -> dict:
    return {"result": result, "explanation": explanation, "hint": "", "model_answer": "", "standard_answer": ""}


@pytest.mark.parametrize("text", ["25 2/5", "25又2/5", "25⅖", r"25\frac{2}{5}", "25 2 / 5", "２５ ２/５"])
def test_mixed_number_forms_parse_to_exact_value(text):
    assert parse_number(text) == Fraction(127, 5) == Fraction("25.4")


def test_numeric_literals_are_exact_and_ambiguous_forms_are_undecided():
    assert parse_number("25 4/5") == parse_number("25.8") == parse_number("129/5")
    assert parse_number("252/5") == Fraction(252, 5)
    assert parse_number("-25 2/5") == Fraction(-127, 5)
    assert parse_number("40%") == Fraction(2, 5)
    for text in ("4 7/5", "1 + 2", "2×4/5", "25 2", "3/0", "", None):
        assert parse_number(text) is None
    assert format_value(Fraction(127, 5)) == "25.4"
    assert format_value(Fraction(1, 3)) == "1/3"


def test_equivalence_comparison_parses_mixed_numbers_before_removing_spaces():
    assert MathMatcher.is_equivalent("25 4/5", "25.8")
    assert not MathMatcher.is_equivalent("25 2/5", "25.8")
    # Whitespace collapse used to make 25 2/5 (25.4) equal to 252/5 (50.4).
    assert not MathMatcher.is_equivalent("25 2/5", "252/5")
    assert MathMatcher.is_equivalent("0.5", "2", variants=["1/2"])
    assert _compare_one("25 4/5", "25.8", question_type="fill") == "correct"
    assert _compare_one("25 2/5", "25.8", question_type="fill") == "wrong"
    assert _compare_one("25 2/5", "252/5", question_type="fill") == "wrong"


def test_final_numeric_answers_compare_against_decimal_reference():
    assert compare_final_numbers(CORRECT_TRANSCRIPTION, REFERENCE) == ("correct", [], False)
    assert compare_final_numbers("25 4/5", "25.8").result == "correct"
    assert compare_final_numbers("25 2/5", "25.8").result == "wrong"

    screenshot = compare_final_numbers(SCREENSHOT_TRANSCRIPTION, REFERENCE)
    assert screenshot.result == "partial"  # B=24 matches; A and 山顶 are not tolerated.
    assert screenshot.mismatches == ["25 2/5（=25.4），参考 25.8", "22 1/5（=22.2），参考 22.8"]


def test_ocr_tolerance_decides_correct_for_misread_conclusion_of_own_correct_result():
    assert compare_final_numbers(TOLERANCE_TRANSCRIPTION, REFERENCE) == ("correct", [], True)
    assert compare_final_numbers("24+1 4/5=25 4/5\nA=25 2/5", "A=25.8") == ("correct", [], True)
    # Derived as a decimal, concluded as a mixed number that OCR misread.
    assert compare_final_numbers("24+1.8=25.8\nA=25 2/5", "A=25.8") == ("correct", [], True)
    # Two isolated misreads are still within the tolerance.
    two = TOLERANCE_TRANSCRIPTION.replace("山顶=22 4/5", "山顶=22 1/5")
    assert compare_final_numbers(two, REFERENCE) == ("correct", [], True)


@pytest.mark.parametrize("student", [
    # 24+9/5 is 129/5 (=25.8); the transcribed 127/5 and 25 2/5 are two misreads.
    IMPROPER_FRACTION_TRANSCRIPTION,
    # The misread improper fraction is itself the conclusion.
    "3×3/5=9/5\nA=24+9/5=127/5",
    # A chain continued on the next line.
    "3×3/5=9/5\nA=24+9/5\n=127/5",
    # The working states the (misread) improper fraction without its computation.
    "A=127/5\n答：A=25 2/5",
])
def test_misread_improper_fraction_in_working_is_tolerated(student):
    assert compare_final_numbers(student, "A=25.8") == ("correct", [], True)


@pytest.mark.parametrize(("student", "reference", "expected"), [
    # The student's own work produces 25 2/5: a real error, graded as written.
    ("24+1 2/5=25 2/5\nA=25 2/5", "A=25.8", "wrong"),
    # Nothing in the work derives the reference value.
    ("A=25 2/5", "A=25.8", "wrong"),
    # Not a one-character misread of the reference.
    ("24+1 4/5=25 4/5\nA=23 1/5", "A=25.8", "wrong"),
    # Genuine arithmetic error in the working (24+9/5 is not 33/5).
    ("3×3/5=9/5\n24+9/5=33/5\nA=33/5", "A=25.8", "wrong"),
    # A genuine working error (3×3/5 is not 3/15) disables the tolerance.
    ("3×3/5=3/15\n24+9/5=129/5\nA=25 2/5", "A=25.8", "wrong"),
    # A misread value carried into later computation is the student's own result.
    ("3×3/5=7/5\n24+7/5=127/5\nA=25 2/5", "A=25.8", "wrong"),
    # Three misreads (7/5, 127/5, 25 2/5) exceed the tolerance.
    ("3×3/5=7/5\n24+9/5=127/5\nA=25 2/5", "A=25.8", "wrong"),
    ("24+1 4/5=25 4/5\n20+4=24\n24-1 1/5=22 4/5\nA=25 2/5，B=21，山顶=22 1/5", REFERENCE, "wrong"),
    # One tolerated misread cannot rescue a separate real error.
    ("24+1 4/5=25 4/5\nA=25 2/5，B=24，山顶=22 1/5", REFERENCE, "partial"),
])
def test_ocr_tolerance_does_not_cover_real_errors(student, reference, expected):
    outcome = compare_final_numbers(student, reference)
    assert outcome.result == expected
    assert outcome.ocr_tolerance is False


def test_labelled_answers_pair_by_label_and_conclusion_marker():
    student = "解：…\n答：山顶处22 4/5℃；A处气温为25 4/5℃；B处24℃"
    assert compare_final_numbers(student, "A处=25.8℃；B处=24℃；山顶=22.8℃").result == "correct"
    assert compare_final_numbers("A=24+1 4/5=25 4/5", "A=25.8").result == "correct"
    assert compare_final_numbers("x=1", "y=1") is None


def test_missing_unit_is_partial_by_contract():
    outcome = compare_final_numbers("25.8", "25.8℃")
    assert outcome.result == "partial"
    assert outcome.mismatches == ["25.8 缺少单位 °C"]


@pytest.mark.parametrize(("student", "reference"), [
    ("x≈25.8", "25.8"),              # approximation
    ("x=2或x=-1", "x=2或x=-1"),      # alternatives are not plain values
    ("25.8米", "25.8m"),             # two unit spellings code cannot reconcile
    ("25.8；24；22.8；3", REFERENCE),  # more results than the reference
    ("25.8", REFERENCE),             # fewer results than the reference
    ("x=√2", "√2"),                  # not a numeric literal
    ("A=25 2/5 B=24", "A=25.4；B=24"),  # unseparated results are not guessed apart
])
def test_non_plain_numeric_answers_stay_undecided(student, reference):
    assert compare_final_numbers(student, reference) is None


def test_variants_can_make_a_numeric_answer_correct():
    assert compare_final_numbers("0.5", "1/3", ["1/2"]).result == "correct"


def _grade(monkeypatch, judgment, *, student_answer, answer_state="visible",
           standard_answer=REFERENCE):
    calls = []

    async def fake_judge(questions, detail_level="detailed", *, teacher_material=None):
        calls.append(questions)
        return {"5": judgment}

    monkeypatch.setattr(grader, "judge_paper", fake_judge)
    monkeypatch.setattr(grader, "get_grading_queue", lambda: ImmediateQueue())
    result = asyncio.run(grade_paper_unified(
        paper={"image_path": "", "questions": [{
            "question_no": "5", "student_answer": student_answer, "answer_state": answer_state,
        }]},
        answer_map={"5": {
            "matched": True, "question_type": "solve",
            "question_text": "海拔每升高100米气温下降0.6℃，求A处、B处和山顶的气温。",
            "standard_answer": standard_answer, "subquestion_count": 1, "subquestion_scores": [10],
        }},
        student_name="学生", teacher_id=1, category="exam",
    ))
    return result, calls


def test_tolerance_eligible_misread_is_auto_correct_not_review(monkeypatch):
    result, calls = _grade(
        monkeypatch, _judged("needs_review", "结论数字与推导不一致，疑似OCR误读"),
        student_answer=TOLERANCE_TRANSCRIPTION,
    )

    detail = result["details"][0]
    assert len(calls) == 1
    assert result["needs_review_count"] == 0
    assert detail["result"] == "correct"
    assert detail["explanation"] == ""
    assert detail["match_method"] == NUMERIC_OCR_TOLERANCE_METHOD
    assert result["score"] == 10.0


def test_misread_improper_fraction_working_is_auto_correct_not_review(monkeypatch):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review", "过程与结论数字不一致"),
        student_answer=IMPROPER_FRACTION_TRANSCRIPTION, standard_answer="A=25.8",
    )

    detail = result["details"][0]
    assert result["needs_review_count"] == 0
    assert detail["result"] == "correct"
    assert detail["match_method"] == NUMERIC_OCR_TOLERANCE_METHOD
    assert result["score"] == 10.0


def test_correct_mixed_number_answer_is_auto_correct_after_model_abstains(monkeypatch):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review", "带分数与小数无法核对"),
        student_answer=CORRECT_TRANSCRIPTION,
    )

    detail = result["details"][0]
    assert result["needs_review_count"] == 0
    assert detail["result"] == "correct"
    assert detail["match_method"] == NUMERIC_FINAL_ANSWER_METHOD
    assert result["score"] == 10.0


def test_clear_wrong_answer_is_auto_wrong_not_review(monkeypatch):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review"),
        student_answer="A=25 2/5", standard_answer="A=25.8",
    )

    detail = result["details"][0]
    assert result["needs_review_count"] == 0
    assert detail["result"] == "wrong"
    assert "25 2/5（=25.4），参考 25.8" in detail["explanation"]
    assert detail["match_method"] == NUMERIC_FINAL_ANSWER_METHOD
    assert result["score"] == 0.0


def test_screenshot_transcription_without_tolerance_evidence_is_decided_not_review(monkeypatch):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review"),
        student_answer=SCREENSHOT_TRANSCRIPTION,
    )

    detail = result["details"][0]
    assert result["needs_review_count"] == 0
    assert detail["result"] == "partial"  # B=24 is an independent correct result.
    assert detail["standard_answer"] == REFERENCE
    assert result["score"] == 5.0


def test_genuinely_unreadable_ocr_routes_to_review_without_model_call(monkeypatch):
    result, calls = _grade(
        monkeypatch, _judged("correct"),
        student_answer="[无法识别]", answer_state="unreadable",
    )

    assert calls == []
    assert result["needs_review_count"] == 1
    assert result["details"][0]["error_type"] == "ocr_unreadable"
    assert result["score"] is None


@pytest.mark.parametrize("student_answer", [
    "A=25 ?/5，B=24，山顶=22 4/5",           # OCR marked a doubtful digit
    "A=25 [无法识别]，B=24，山顶=22 4/5",    # partially unreadable transcription
])
def test_doubtful_transcription_keeps_model_abstention(monkeypatch, student_answer):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review"), student_answer=student_answer,
    )

    assert result["needs_review_count"] == 1
    assert result["details"][0]["result"] == "needs_review"
    assert result["details"][0]["match_method"] == "llm"


def test_non_numeric_abstention_stays_with_teacher(monkeypatch):
    result, _calls = _grade(
        monkeypatch, _judged("needs_review"),
        student_answer="x=√2", standard_answer="x=√2",
    )

    assert result["details"][0]["result"] == "needs_review"


@pytest.mark.parametrize(("model_result", "student_answer"), [
    # OCR tolerance applied by the model itself stays correct.
    ("correct", TOLERANCE_TRANSCRIPTION),
    ("correct", "3×3/5=1 4/5\n5×3/5=8\n2×3/5=1 1/5\nA=25 4/5，B=24，山顶=22 4/5"),
    # A decided model verdict is never replaced by the numeric comparison.
    ("wrong", CORRECT_TRANSCRIPTION),
    ("partial", SCREENSHOT_TRANSCRIPTION),
])
def test_model_verdicts_are_not_overridden(monkeypatch, model_result, student_answer):
    result, _calls = _grade(
        monkeypatch, _judged(model_result, "" if model_result == "correct" else "模型说明"),
        student_answer=student_answer,
    )

    detail = result["details"][0]
    assert detail["result"] == model_result
    assert detail["match_method"] == "llm"


def test_drawing_mixed_text_side_is_not_resolved_by_numbers():
    unit = {
        "entry": {"answer_kind": "mixed"}, "student_answer": "25 4/5",
        "answer_state": "visible", "standard_answer": "25.8",
    }
    assert grader.resolve_numeric_review(unit, _judged("needs_review")) is None
