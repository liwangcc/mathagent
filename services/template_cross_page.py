"""A question the page break cut in two is solved and transcribed with its beginning in view.

A page can open with the rest of the previous page's last question
(``template_max_owned.CONTINUED_FROM_PREVIOUS_PAGE``). Its stem is on the other
page, so a request that sees only this page can leave the answer blank or make
the conditions up, and the transcribed question lacks its stem. Template pages
are recognized in order: when the import names the page before this one
(``after``), the one solver request that owns the question and the
transcription also get the tail of that page, from the start of its last
question down to the bottom, and are told what that image is (``bind``).

No request is added. Every other request, and every request of a page without
such a question, is sent exactly as before.
"""
from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from services import template_max_owned as max_owned
from services import template_one_three as one_three

logger = logging.getLogger(__name__)

# A stem is carried over onto a page students answer on, from a page that
# prints questions. An answer sheet, or the page after a cover, has none to find.
_ANSWERED_ON = "answer_page"
_PRINTS_QUESTIONS = frozenset({"answer_page", "question_only"})
# Page-normalized room kept left of, right of and above the tail.
_PAD = 30.0
# The tail never shrinks to a sliver of the page.
_MIN_HEIGHT = 100.0
_TAIL, _WHOLE_PAGE = "上一页的尾部", "上一页整页"
_STEM = ("本题接上页：题干在上一页，本页没有它的印刷题号。随本请求给出的最后一张图是{view}，不属于本页；"
         "本题的题干是那张图中的最后一道题（它在上一页没有印完）。")
# What replaces ``max_owned.CONTINUED_NOTE`` in the directory row, by request phase.
_NOTES = {
    one_three.SOLVE_PHASE: _STEM + (
        "解题时把那里的题干与本页上本题的图形、小问合成同一道题；只依据这两处可见内容，"
        "两处都没有的已知条件不得自行补设或凭记忆补入，依赖这些条件的作答位answer留空，并在uncertain写明缺少的条件。"
        "那张图中的其他题目不属于本请求。"),
    one_three.TRANSCRIBE_PHASE: _STEM + (
        "转录时先录那里的本题题干，再接本页上本题的正文，合成同一道题的question_text；"
        "只录印刷可见的文字，两处都没有的内容不补写。那张图中的其他题目不属于本请求，不转录。"),
}
# Its answer was derived from two pages: a teacher still checks it, and is told so.
BOUND_REVIEW_REASON = "本题接上页：题干在上一页，标准答案由上一页的题干与本页内容共同得出，请对照上一页核对"


@dataclass(frozen=True)
class PreviousPage:
    """The page uploaded just before: its role, its rectified image and, when it was recognized, its result."""

    role: str
    image_path: str
    result: dict | None = None


_previous_page: ContextVar[PreviousPage | None] = ContextVar("template_previous_page", default=None)


@contextmanager
def after(previous: PreviousPage | None, *, role: str):
    """Recognize a page of ``role`` knowing which page came before it."""
    usable = previous is not None and role == _ANSWERED_ON and previous.role in _PRINTS_QUESTIONS
    token = _previous_page.set(previous if usable else None)
    try:
        yield
    finally:
        _previous_page.reset(token)


def tail_box(previous: PreviousPage, *, width: int, height: int) -> list[int] | None:
    """Pixel box of the previous page's tail, or None when only the whole page is safe.

    The tail starts at the top of the last question's printed number, the one
    position the locators place well, and runs to the bottom of the page across
    that question's column. A stem that continues on the next page is the last
    thing printed on its page, so it lies inside. A last question outside the
    last column may wrap into the next one, and a page that was not recognized
    has no number box: both keep the whole page.
    """
    result = previous.result if isinstance(previous.result, dict) else {}
    anchors = result.get("question_anchors")
    last = anchors[-1] if isinstance(anchors, list) and anchors else None
    columns = (((result.get("layout_result") or {}).get("column_layout") or {}).get("columns")
               or [{"x1_1000": 0, "x2_1000": 1000}])
    try:
        x1, y1, x2, y2 = (float(value) for value in last["question_number_bbox"])
        left, right = max((float(column["x1_1000"]), float(column["x2_1000"])) for column in columns)
    except (KeyError, TypeError, ValueError):
        return None
    if (not all(math.isfinite(value) for value in (x1, y1, x2, y2, left, right))
            or not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000 and 0 <= left < right <= 1000)
            or not left <= (x1 + x2) / 2 <= right):
        return None
    top = max(0.0, min(y1 - _PAD, 1000.0 - _MIN_HEIGHT))
    return [max(0, math.floor((left - _PAD) * width / 1000)), math.floor(top * height / 1000),
            min(width, math.ceil((right + _PAD) * width / 1000)), height]


def _save_view(previous: PreviousPage, directory) -> dict:
    """What the request is shown of the previous page: ``view`` names it, ``path`` is the image."""
    # The import router loads this module at startup; the image runtime is loaded when a page needs it.
    from PIL import Image
    with Image.open(previous.image_path) as opened:
        box = tail_box(previous, width=opened.width, height=opened.height)
        if box is None:
            return {"view": _WHOLE_PAGE, "path": previous.image_path}
        path = Path(directory) / "previous-page-tail.png"
        opened.convert("RGB").crop(tuple(box)).save(path, format="PNG")
    return {"view": _TAIL, "path": str(path), "box_px": box}


def _reviewed_with_stem(validator, owner: str):
    """The solver's validator; the carried-over question's fixed review reason names both pages."""
    def validate(payload):
        result = validator(payload)
        for answer in result["answers"]:
            if answer["q_id"] == owner:
                answer["answer_review_reasons"] = [
                    BOUND_REVIEW_REASON if reason == max_owned.CONTINUED_REVIEW_REASON else reason
                    for reason in answer["answer_review_reasons"]]
        return result
    return validate


def bind(request, manifest, directory) -> tuple:
    """``(request, report)`` for this page's solver and transcription.

    The request is ``request`` itself, and the report empty, unless the page
    opens with a carried-over question and the import named the page before.
    Then a solver or transcription request that owns the question is sent
    with the previous page's tail as its last image and its directory row says
    where the stem is; the solver's fixed review reason says what the answer
    was derived from. Any other request passes through untouched. ``report``
    names the view and, once they start, the requests that carried it. A
    previous page that cannot be read is not worth failing this page for: the
    question is then handled as before.
    """
    previous = _previous_page.get()
    owner = next((row["q_id"] for row in manifest if max_owned.continues_previous_page(row)), None)
    if previous is None or owner is None:
        return request, {}
    try:
        shown = _save_view(previous, directory)
    except Exception as exc:
        logger.warning("上一页的图无法读取，接上页的题仍只按本页解答: %s", type(exc).__name__)
        return request, {}
    report = {key: value for key, value in shown.items() if key != "path"} | {"requests": []}

    async def bound(images, prompt, phase, validator, **options):
        note = _NOTES.get(phase)
        if (note is None or owner not in options.get("expected_ids", ())
                or prompt.count(max_owned.CONTINUED_NOTE) != 1):
            return await request(images, prompt, phase, validator, **options)
        # Solver and transcription start in an order that depends on the shard count.
        report["requests"] = sorted(report["requests"] + [options.get("stage_key") or phase])
        return await request(
            ([images] if isinstance(images, str) else list(images)) + [shown["path"]],
            prompt.replace(max_owned.CONTINUED_NOTE, note.format(view=shown["view"])), phase,
            _reviewed_with_stem(validator, owner) if phase == one_three.SOLVE_PHASE else validator, **options)

    return bound, report
