"""Exact parsing of numeric answer literals and short final-answer lists.

Handwritten mixed numbers reach grading as OCR text such as ``25 2/5``,
``25又2/5`` or ``25\\frac{2}{5}``.  Whitespace-insensitive normalization turns
the first form into ``252/5`` (50.4), so numbers must be parsed before any
whitespace is removed.  Values are exact ``Fraction`` objects: 25 4/5, 25.8
and 129/5 compare equal without a floating-point tolerance.

Anything that is not an unambiguous literal returns ``None``.  Callers must
treat ``None`` as "not decidable by code", never as a mismatch.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from fractions import Fraction
from typing import NamedTuple

_VULGAR_FRACTIONS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6",
    "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}
_LATEX_FRACTION_RE = re.compile(r"\\[dt]?frac\s*\{\s*(\d+)\s*\}\s*\{\s*(\d+)\s*\}")
_NUMBER_RE = re.compile(
    r"(?P<sign>[+-])?\s*(?:"
    r"(?P<whole>\d+)(?:\s+|\s*又\s*)(?P<num>\d+)\s*/\s*(?P<den>\d+)"
    r"|(?P<fnum>\d+)\s*/\s*(?P<fden>\d+)"
    r"|(?P<dec>\d+(?:\.\d+)?)"
    r")\s*(?P<percent>[%‰])?"
)
# Arithmetic in working lines: "24+9/5", "2×4/5", "24-1 1/5" (mixed number).
_EXPRESSION_TOKEN_RE = re.compile(
    r"\s*(?:(?P<mixed>\d+\s+\d+\s*/\s*\d+)(?![\d.])|(?P<number>\d+(?:\.\d+)?%?)|(?P<op>[-+*/()]))"
)
_MAX_EXPRESSION_LENGTH = 40

# Final-answer lists: "A=25 2/5，B=24，山顶=22 1/5" or "答：25.8℃；24℃".
_CONCLUSION_RE = re.compile(r"答\s*:?")
_SEGMENT_SPLIT_RE = re.compile(r"[;,、\n]+")
# "A=24+9/5\n=129/5" is one chain written over two lines.
_CONTINUATION_RE = re.compile(r"\s*\n\s*(?==)")
# Approximations, alternatives, sign words and OCR doubt are not plain values.
_UNDECIDABLE_MARKERS = ("无法识别", "?", "≈", "约", "左右", "或", "±", "~", "以上", "以下", "零下", "负")
_LABELLED_VALUE_RE = re.compile(r"(?P<label>.*?)(?P<value>[+-]?\s*\d[\d\s./又]*[%‰]?)\s*(?P<unit>[^\d]*)")
_VALUE_UNIT_RE = re.compile(r"(?P<value>[+-]?\s*\d[\d\s./又]*[%‰]?)\s*(?P<unit>[^\d]*)")
_ARITHMETIC_RE = re.compile(r"[\d\s+\-×÷*/^().%]+")
_LABEL_RE = re.compile(r"[A-Za-z\u0370-\u03ff\u4e00-\u9fff∠△][A-Za-z0-9_'′\u0370-\u03ff\u4e00-\u9fff∠△]*")
_LABEL_PREFIXES = ("∴", "∵", "所以", "因此", "故", "则", "即")
_LABEL_SUFFIXES = ("为", "是", ":")
_UNIT_RE = re.compile(r"[A-Za-z°\u4e00-\u9fff²³/·]*")

# The OCR tolerance covers at most two isolated one-character misreads.
_MAX_TOLERATED_MISREADS = 2
# Characters an OCR misread can substitute or add inside a number.
_MISREAD_CHARACTERS = "0123456789."


def _canonical(text: str) -> str:
    # Expand single-glyph fractions first: NFKC would render "25⅖" as the
    # ambiguous "252⁄5".
    for glyph, fraction in _VULGAR_FRACTIONS.items():
        text = text.replace(glyph, f" {fraction}")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("⁄", "/").replace("∕", "/").replace("−", "-").replace("$", "")
    return _LATEX_FRACTION_RE.sub(r" \1/\2", text).strip()


def parse_number(text: object) -> Fraction | None:
    """Return the exact value of one integer/decimal/fraction/mixed-number literal."""
    if not isinstance(text, str):
        return None
    match = _NUMBER_RE.fullmatch(_canonical(text))
    if match is None:
        return None
    if match["whole"] is not None:
        numerator, denominator = int(match["num"]), int(match["den"])
        # "4 7/5" is not a mixed number; it may be a product or a misread.
        if not 0 < numerator < denominator:
            return None
        value = int(match["whole"]) + Fraction(numerator, denominator)
    elif match["fnum"] is not None:
        denominator = int(match["fden"])
        if denominator == 0:
            return None
        value = Fraction(int(match["fnum"]), denominator)
    else:
        value = Fraction(match["dec"])
    if match["percent"]:
        value /= 100 if match["percent"] == "%" else 1000
    return -value if match["sign"] == "-" else value


def _evaluate(text: str) -> Fraction | None:
    """Exact value of a literal or of plain + - × ÷ arithmetic; None otherwise."""
    value = parse_number(text)
    if value is not None or len(text) > _MAX_EXPRESSION_LENGTH:
        return value
    source = text.replace("×", "*").replace("÷", "/").replace("·", "*").strip()
    tokens: list[object] = []
    position = 0
    while position < len(source):
        match = _EXPRESSION_TOKEN_RE.match(source, position)
        if match is None:
            return None
        position = match.end()
        if match["op"]:
            tokens.append(match["op"])
        else:
            number = parse_number(match["mixed"] or match["number"])
            if number is None:
                return None
            tokens.append(number)
    index = 0

    def peek():
        return tokens[index] if index < len(tokens) else None

    def factor():
        nonlocal index
        token = peek()
        if token in ("+", "-"):
            index += 1
            value = factor()
            return None if value is None else (value if token == "+" else -value)
        if token == "(":
            index += 1
            value = expression()
            if peek() != ")":
                return None
            index += 1
            return value
        if isinstance(token, Fraction):
            index += 1
            return token
        return None

    def term():
        nonlocal index
        value = factor()
        while value is not None and peek() in ("*", "/"):
            operator = tokens[index]
            index += 1
            right = factor()
            if right is None or (operator == "/" and right == 0):
                return None
            value = value * right if operator == "*" else value / right
        return value

    def expression():
        nonlocal index
        value = term()
        while value is not None and peek() in ("+", "-"):
            operator = tokens[index]
            index += 1
            right = term()
            if right is None:
                return None
            value = value + right if operator == "+" else value - right
        return value

    result = expression() if tokens else None
    return result if index == len(tokens) else None


def _one_character_misread(source: str, target: Fraction) -> bool:
    """One substituted, dropped or added character makes ``source`` equal ``target``.

    Works for any numeric form: 127/5 -> 129/5, 25 2/5 -> 25 4/5, 25.3 -> 25.8,
    or an operand inside an expression (24+7/5 -> 24+9/5).
    """
    text = re.sub(r"\s+", " ", source.strip())
    if not text or len(text) > _MAX_EXPRESSION_LENGTH:
        return False
    candidates = set()
    for index in range(len(text)):
        candidates.add(text[:index] + text[index + 1:])
        candidates.update(text[:index] + char + text[index + 1:] for char in _MISREAD_CHARACTERS)
    for index in range(len(text) + 1):
        candidates.update(text[:index] + char + text[index:] for char in _MISREAD_CHARACTERS + " ")
    candidates.discard(text)
    return any(_evaluate(candidate) == target for candidate in candidates)


def format_value(value: Fraction) -> str:
    """Render terminating values as decimals (25.4) and others as p/q."""
    denominator = value.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator != 1:
        return f"{value.numerator}/{value.denominator}"
    return format((Decimal(value.numerator) / Decimal(value.denominator)).normalize(), "f")


class _FinalValue(NamedTuple):
    label: str
    source: str
    value: Fraction
    unit: str
    # (segment index, "=" part index) of the literal in the whole answer.
    position: tuple[int, int] = (-1, -1)


def _clean_label(label: str) -> str | None:
    label = re.sub(r"\s+", "", label)
    for prefix in _LABEL_PREFIXES:
        if label.startswith(prefix):
            label = label[len(prefix):]
    for suffix in _LABEL_SUFFIXES:
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    if label and not _LABEL_RE.fullmatch(label):
        return None
    return label.casefold()


def _clean_unit(unit: str) -> str | None:
    unit = re.sub(r"[\s()\[\]]", "", unit)
    return unit if _UNIT_RE.fullmatch(unit) else None


def _parse_segment(segment: str) -> _FinalValue | None:
    text = segment.strip().rstrip("。.")
    if not text or any(marker in text for marker in _UNDECIDABLE_MARKERS):
        return None
    if "=" in text:
        parts = text.split("=")
        # "A=24+1 4/5=25 4/5": keep the last value; the middle must be arithmetic.
        if any(not _ARITHMETIC_RE.fullmatch(part.strip()) for part in parts[1:-1]):
            return None
        label, match = parts[0], _VALUE_UNIT_RE.fullmatch(parts[-1].strip())
    else:
        match = _LABELLED_VALUE_RE.fullmatch(text)
        label = match["label"] if match else ""
    if match is None:
        return None
    clean_label = _clean_label(label)
    value = parse_number(match["value"])
    unit = _clean_unit(match["unit"])
    if clean_label is None or value is None or unit is None:
        return None
    return _FinalValue(clean_label, match["value"].strip(), value, unit)


class _Answer(NamedTuple):
    finals: list[_FinalValue]
    # Segments in the conclusion region (after the last 答, or the whole text).
    conclusion_count: int
    # Every segment: the working first, the conclusion last.
    segments: list[str]


def _read_answer(text: str) -> _Answer:
    """Split an answer into its working and its conclusion.

    The conclusion is the trailing run of value segments after the last 答
    (or of the whole text); parsing stops at the first non-value segment.
    """
    canonical = _CONTINUATION_RE.sub("", _canonical(text))
    markers = list(_CONCLUSION_RE.finditer(canonical))
    head = canonical[:markers[-1].start()] if markers else ""
    region = canonical[markers[-1].end():] if markers else canonical
    work = [segment for segment in _SEGMENT_SPLIT_RE.split(head) if segment.strip()]
    conclusion = [segment for segment in _SEGMENT_SPLIT_RE.split(region) if segment.strip()]
    finals: list[_FinalValue] = []
    for segment in reversed(conclusion):
        parsed = _parse_segment(segment)
        if parsed is None:
            break
        finals.append(parsed)
    finals.reverse()
    segments = work + conclusion
    first_final = len(segments) - len(finals)
    finals = [
        final._replace(position=(first_final + offset, segments[first_final + offset].count("=")))
        for offset, final in enumerate(finals)
    ]
    return _Answer(finals, len(conclusion), segments)


class _Work(NamedTuple):
    # Values the working establishes once isolated misreads are restored.
    evidence: set[Fraction]
    # Literals that are one-character misreads of their chain's value.
    misreads: set[tuple[int, int]]
    # Literals that open a chain (nothing computed before them): a misread
    # there cannot be seen from the line itself, e.g. "A=127/5".
    openers: list[tuple[tuple[int, int], str]]
    # A chain step that is neither equal nor a one-character misread.
    genuine_error: bool


def _analyse_work(answer: _Answer) -> _Work:
    """Check every "=" chain: equal steps, isolated one-character misreads, or real errors.

    ``24+9/5=127/5``: the left side is 129/5, and 127/5 is one character away,
    so it is a misread and the chain keeps its true value 129/5.  A misread
    value is never carried forward, so a later step that uses it (24+7/5=127/5
    after 3×3/5=7/5) is the student's own computation, not new evidence.
    """
    final_positions = {final.position for final in answer.finals}
    evidence: set[Fraction] = set()
    misreads: set[tuple[int, int]] = set()
    openers: list[tuple[tuple[int, int], str]] = []
    genuine_error = False
    for segment_index, segment in enumerate(answer.segments):
        if any(marker in segment for marker in _UNDECIDABLE_MARKERS):
            continue
        trusted, trusted_unit = None, ""
        for part_index, part in enumerate(segment.split("=")):
            position = (segment_index, part_index)
            is_final = position in final_positions
            literal = _VALUE_UNIT_RE.fullmatch(part.strip())
            unit = _clean_unit(literal["unit"]) if literal else None
            if literal and unit is not None:
                source, value, is_literal = literal["value"].strip(), parse_number(literal["value"]), True
            else:
                source, value, unit, is_literal = part.strip(), _evaluate(part), "", False
            if value is None:
                trusted, trusted_unit = None, ""  # a label or symbol breaks the numeric chain
                continue
            if trusted is None or (unit and trusted_unit and unit.casefold() != trusted_unit.casefold()):
                # "100m=0.6℃" relates different quantities; start a new chain.
                if is_literal and not is_final:
                    openers.append((position, source))
            elif value == trusted:
                pass
            elif _one_character_misread(source, trusted):
                misreads.add(position)
                trusted_unit = unit or trusted_unit
                continue
            else:
                genuine_error = True
            trusted, trusted_unit = value, unit or trusted_unit
            if not is_final:
                evidence.add(value)
    return _Work(evidence, misreads, openers, genuine_error)


def _pair_values(
    expected: list[_FinalValue], actual: list[_FinalValue],
) -> list[tuple[_FinalValue, _FinalValue]] | None:
    if not all(item.label for item in expected) or not all(item.label for item in actual):
        return list(zip(expected, actual))
    # Both sides are labelled: pair "A处" with "A", never by position.
    pairs, used = [], set()
    for reference in expected:
        candidates = [index for index, item in enumerate(actual) if item.label == reference.label]
        if len(candidates) != 1:
            candidates = [
                index for index, item in enumerate(actual)
                if item.label.startswith(reference.label) or reference.label.startswith(item.label)
            ]
        if len(candidates) != 1 or candidates[0] in used:
            return None
        used.add(candidates[0])
        pairs.append((reference, actual[candidates[0]]))
    return pairs


class NumericComparison(NamedTuple):
    result: str
    mismatches: list[str]
    ocr_tolerance: bool = False


def _compare(answer: _Answer, work: _Work, reference: str) -> NumericComparison | None:
    expected = _read_answer(reference)
    # Every reference segment must be a plain value, and the student's final
    # run must have exactly as many: extra or missing results stay undecided.
    if (
        not expected.finals or len(expected.finals) != expected.conclusion_count
        or len(answer.finals) != len(expected.finals)
    ):
        return None
    pairs = _pair_values(expected.finals, answer.finals)
    if pairs is None:
        return None
    exact = [ref.value == student.value for ref, student in pairs]
    misreads = set(work.misreads)
    restored = []
    for (ref, student), matched in zip(pairs, exact):
        # A conclusion value is restorable when it is a one-character misread
        # of the reference AND the working reaches the reference: directly, or
        # through an opening literal that is itself a one-character misread.
        support = None
        if not matched and _one_character_misread(student.source, ref.value):
            if ref.value in work.evidence:
                support = ()
            else:
                support = next(
                    ((position,) for position, source in work.openers
                     if _one_character_misread(source, ref.value)),
                    None,
                )
        if support is not None:
            misreads.update(support)
            misreads.add(student.position)
        restored.append(support is not None)
    tolerated = any(restored) and not work.genuine_error and len(misreads) <= _MAX_TOLERATED_MISREADS
    accepted = [matched or (was_restored and tolerated) for matched, was_restored in zip(exact, restored)]
    if all(accepted):
        # Units are a scoring requirement: a missing unit is partial by
        # contract; two different unit spellings (米 / m) stay undecided.
        if any(
            ref.unit and student.unit and student.unit.casefold() != ref.unit.casefold()
            for ref, student in pairs
        ):
            return None
        missing_units = [
            f"{student.source} 缺少单位 {ref.unit}" for ref, student in pairs if ref.unit and not student.unit
        ]
        if missing_units:
            return NumericComparison("partial", missing_units)
        return NumericComparison("correct", [], ocr_tolerance=tolerated)
    mismatches = [
        f"{student.source}（={format_value(student.value)}），参考 {format_value(ref.value)}"
        for (ref, student), ok in zip(pairs, accepted) if not ok
    ]
    return NumericComparison("partial" if any(accepted) else "wrong", mismatches)


def compare_final_numbers(
    student_answer: str, standard_answer: str, answer_variants: list | None = None,
) -> NumericComparison | None:
    """Decide plain numeric final answers exactly; None when code cannot decide.

    The student's conclusion is the trailing run of ``label = value`` segments
    (after the last 答 when present); it must have exactly as many values as
    the reference.  Labelled answers pair by label, otherwise by position.

    OCR tolerance: at most two isolated one-character misreads anywhere in the
    transcription -- in the working (an improper fraction 129/5 read as 127/5)
    or in the conclusion (25 4/5 read as 25 2/5) -- are restored when the
    working then reaches the reference.  A working step that is neither equal
    nor a one-character misread is a genuine error and disables the tolerance.
    """
    if not isinstance(student_answer, str) or not isinstance(standard_answer, str):
        return None
    answer = _read_answer(student_answer)
    if not answer.finals:
        return None
    work = _analyse_work(answer)
    outcome = _compare(answer, work, standard_answer)
    if outcome is None or outcome.result == "correct":
        return outcome
    for variant in answer_variants or []:
        alternative = _compare(answer, work, variant) if isinstance(variant, str) else None
        if alternative is not None and alternative.result == "correct":
            return alternative
    return outcome
