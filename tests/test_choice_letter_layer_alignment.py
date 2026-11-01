"""Choice-letter acceptance must be identical across the transcription layer and
the grading layer.

Product ruling 2026-09-30: a written choice the transcription layer accepts must
never be re-parsed into an empty answer by grading.  The two layers previously
diverged (full-width letters passed transcription but were rejected at grading,
while 选/答案 prefixes were the reverse), so grading could lose a confirmed
answer and score it wrong.
"""

from services.answer_sheet_column_transcription import choice_letters
from services.grading_contract import normalize_choice_answer
from services.quick_grader import _compare_one

_OPTIONS = ["A", "B", "C", "D"]


def test_fullwidth_choice_letters_survive_grading_comparison():
    assert normalize_choice_answer("Ｂ") == "B"
    assert _compare_one("Ｂ", "B", question_type="choice", choice_mode="single") == "correct"
    assert _compare_one("Ａ、Ｃ", "AC", question_type="choice", choice_mode="multiple") == "correct"


def test_prefixed_choice_forms_align_across_layers():
    assert choice_letters("选B", _OPTIONS, "single") == "B"
    assert choice_letters("答案：A", _OPTIONS, "single") == "A"
    assert normalize_choice_answer("选B") == "B"
    assert normalize_choice_answer("答案：A") == "A"


def test_layers_agree_on_every_accepted_form():
    for raw in ["AC", "a c", "C、A", "选B", "答案：A", "Ｂ", "ac"]:
        transcribed = choice_letters(raw, _OPTIONS, "multiple")
        graded = normalize_choice_answer(raw)
        assert transcribed == graded, f"layers diverge on {raw!r}: {transcribed!r} vs {graded!r}"
    for raw in ["1", "13", "②", "ABAB"]:
        assert normalize_choice_answer(raw) == ""
        assert choice_letters(raw, _OPTIONS, "multiple") is None
