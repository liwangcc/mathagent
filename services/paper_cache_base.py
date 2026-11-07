"""What every part of answer-only recognition shares: question numbers and the evidence strategies.

- ``_normalize_question_number`` strips prefixes, suffixes, dots and spaces from
  a question number but keeps sub-question numbers; ``normalize_questions``
  applies it to a question list and ``_question_number_sort_key`` sorts numbers
  naturally ("2" before "10").
- ``question_number_signature`` is the question-number signature stored with
  each template page; it is part of the confirmed contract hash, so its value
  must stay byte-for-byte stable.
- ``_VISUAL_GRADING_EVIDENCE_STRATEGIES`` names the full-page and full-height
  evidence strategies, which are transcription-only for new requests.

Moved unchanged from services/paper_cache.py, which still exposes these names.
"""

import hashlib
import re


# Full-page/full-height evidence is transcription-only for new requests.  The
# separate grading provider consumes the frozen transcription contract.
_VISUAL_GRADING_EVIDENCE_STRATEGIES = {
    "full_height_columns",
    "full_height_column_plan",
    "full_column_difference",
    "client-evidence-multi-v2",
}


# ═══════════════════════════════════════════════════════
# 题号归一化
# ═══════════════════════════════════════════════════════

def _normalize_question_number(qn: str) -> str:
    """归一化题号：去除前缀/后缀词、点号、多余空白，保留子题号。"""
    import re
    qn = qn.strip().rstrip('.')
    qn = re.sub(r'^第\s*', '', qn)
    qn = re.sub(r'\s*题$', '', qn)
    qn = re.sub(r'\s+', '', qn)
    return qn


def _question_number_sort_key(qn: str):
    """题号自然排序键：按其中的数字序列排序，使 "2" 排在 "10" 前，"1(2)" 排在 "1(10)" 前。"""
    import re
    nums = re.findall(r'\d+', qn)
    return ([int(n) for n in nums], qn)


def normalize_questions(questions: list) -> list:
    """归一化questions列表中的question_number。"""
    for q in questions:
        if "question_number" in q:
            q["question_number"] = _normalize_question_number(q["question_number"])
    return questions


def question_number_signature(question_numbers) -> dict:
    """Question-number signature stored with each template page.

    Nothing reads it back, but it is part of a template page and so of the
    confirmed contract hash: its value must stay byte-for-byte stable.
    """
    numbers = sorted({
        number
        for number in (_normalize_question_number(str(raw)) for raw in question_numbers)
        if number
    })
    return {
        "qn_set": numbers,
        "qn_hash": hashlib.md5("|".join(numbers).encode()).hexdigest()[:12],
        "qn_count": len(numbers),
    }
