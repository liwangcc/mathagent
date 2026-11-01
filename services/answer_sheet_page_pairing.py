"""Front/back pairing of student answer-sheet pages by upload path.

Teachers import student answer sheets as a folder, a ZIP/RAR or loose files,
and each page keeps the relative path it was imported under.  A batch is
paired by path with the first rule that every page satisfies:

1. folder: every page sits in a folder and every folder holds exactly two pages;
2. file name: every file name ends in an explicit side word (正面/正/front,
   反面/反/背面/背/back) and every base name has exactly one front and one back;
3. roster name: every page's path resolves to one roster student and every
   student has exactly two pages.

Anything less leaves the batch on the upload-order pairing in
``routers/answer_sheet.py``; the rules never mix within a batch.  Front and
back follow the template page type, and a side word must agree with it.

The path is also an identity clue, resolved only through the roster-scoped
``resolve_collection_identity`` that detailed grading uses (file name and the
folders below the selected top folder; unique roster name or complete student
number, never a guessed fragment).  A pair carries one identity: its path, a
page recognized from its own identity regions, or a teacher confirmation.
Disagreement between any of them, a page type that does not fit the two-page
template, or no identity at all is left for the teacher.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from services.collection_identity import resolve_collection_identity

FOLDER_PAIRING_MODE = "folder_pairs"
FILENAME_PAIRING_MODE = "filename_side_pairs"
ROSTER_NAME_PAIRING_MODE = "roster_name_pairs"
PATH_PAIRED_STATUS = "path_paired"
PATH_PAIRING_REASONS = frozenset({
    "path_pair_page_type_mismatch",
    "path_pair_identity_conflict",
    "path_pair_identity_missing",
})
_PAIRING_REASON_PREFIXES = ("ordered_pair_", "path_pair_")
_DERIVED_STATUSES = frozenset({"ordered_pair_inherited", PATH_PAIRED_STATUS})
_OWN_STATUSES = frozenset({"recognized", "teacher_confirmed"})
_PATH_CONFLICT_REASONS = frozenset({"identity_name_conflict", "identity_number_conflict"})

_OCR_READY_RE = re.compile(r"\.ocr-ready-v1\.jpg\Z", re.I)
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,8}\Z")
_SEPARATORS = r"\s_\-.·()\[\]【】"
# English side words need a non-letter before them ("feedback" is no side).
_SIDE_RE = re.compile(
    rf"(?P<base>.*?)[{_SEPARATORS}]*"
    r"(?P<side>正面|反面|背面|正|反|背|(?<![a-z])front|(?<![a-z])back)"
    rf"[{_SEPARATORS}]*\Z"
)
_SIDES = {
    "正面": "front", "正": "front", "front": "front",
    "反面": "back", "反": "back", "背面": "back", "背": "back", "back": "back",
}

Pair = tuple[dict, dict]


def parse_side_filename(filename: object) -> tuple[str, str] | None:
    """Return ``(base_key, "front"|"back")`` for an explicit side suffix."""
    name = str(filename or "").replace("\\", "/").split("/")[-1]
    stem = _EXTENSION_RE.sub("", _OCR_READY_RE.sub("", name))
    stem = unicodedata.normalize("NFKC", stem).casefold()
    match = _SIDE_RE.fullmatch(stem)
    if not match:
        return None
    base = re.sub(r"\s+", "", match.group("base"))
    base = re.sub(rf"\A[{_SEPARATORS}]+|[{_SEPARATORS}]+\Z", "", base)
    if not base:
        return None
    return base, _SIDES[match.group("side")]


def _first_order(group: Sequence[Mapping[str, Any]]) -> int:
    return min(int(page.get("physical_order") or 0) for page in group)


def _two_page_groups(keyed: Sequence[tuple[str | int, dict]]) -> list[Pair] | None:
    groups: dict[str | int, list[dict]] = {}
    for key, page in keyed:
        groups.setdefault(key, []).append(page)
    if any(len(group) != 2 for group in groups.values()):
        return None
    return [(group[0], group[1]) for group in sorted(groups.values(), key=_first_order)]


def source_directory(page: Mapping[str, Any]) -> str | None:
    parts = str(page.get("source_relative_path") or "").split("/")
    return "/".join(parts[:-1]) if len(parts) >= 2 else None


def folder_pairs(pages: Sequence[dict]) -> list[Pair] | None:
    """Pair pages folder by folder, or ``None`` unless every folder has two."""
    if not pages:
        return None
    keyed = []
    for page in pages:
        directory = source_directory(page)
        if directory is None:
            return None
        keyed.append((directory, page))
    return _two_page_groups(keyed)


def filename_side_pairs(pages: Sequence[dict]) -> list[Pair] | None:
    """Pair every page by side-marked file name as ``(front, back)``."""
    if not pages or len(pages) % 2:
        return None
    groups: dict[str, dict[str, dict]] = {}
    for page in pages:
        parsed = parse_side_filename(page.get("filename"))
        if parsed is None:
            return None
        base, side = parsed
        slot = groups.setdefault(base, {})
        if side in slot:
            return None
        slot[side] = page
    if any(len(slot) != 2 for slot in groups.values()):
        return None
    return [
        (slot["front"], slot["back"])
        for slot in sorted(groups.values(), key=lambda slot: _first_order(list(slot.values())))
    ]


def path_identity(page: Mapping[str, Any], roster: list[dict]) -> dict[str, Any]:
    """Roster student named by the page's upload path, if exactly one."""
    result = resolve_collection_identity(
        roster,
        filename=str(page.get("filename") or ""),
        source_relative_path=str(page.get("source_relative_path") or ""),
    )
    student = None
    if result.get("status") == "matched":
        student = next(
            (row for row in roster if row["student_id"] == result.get("student_id")),
            None,
        )
    return {
        "student": student,
        "conflict": result.get("reason") in _PATH_CONFLICT_REASONS,
        "clue_source": str(result.get("clue_source") or ""),
    }


def roster_name_pairs(pages: Sequence[dict], identities: Mapping[int, dict]) -> list[Pair] | None:
    """Pair pages by the roster student their paths name, two per student."""
    if not pages:
        return None
    keyed = []
    for page in pages:
        student = identities[id(page)]["student"]
        if student is None:
            return None
        keyed.append((int(student["student_id"]), page))
    return _two_page_groups(keyed)


def _reset_page(page: dict) -> None:
    identity = dict(page.get("identity") or {})
    if identity.get("status") in _DERIVED_STATUSES:
        for key in ("source", "source_paper_id", "clue_source"):
            identity.pop(key, None)
        identity.update(status="needs_review", student_id=None, student_name="")
        page["identity"] = identity
    page["review_reasons"] = [
        reason for reason in list(page.get("review_reasons") or [])
        if not str(reason).startswith(_PAIRING_REASON_PREFIXES)
    ]


def _flag(pages: Sequence[dict], status: str, reason: str) -> None:
    for page in pages:
        page["pairing"]["status"] = status
        reasons = list(page.get("review_reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        page["review_reasons"] = reasons


def _sides(pair: Pair, mode: str, layout_page_ids: Sequence[str]) -> Pair | None:
    """``(front, back)`` by template page type; side words must agree."""
    first, second = pair
    if mode == FILENAME_PAIRING_MODE:
        ordered = (first, second)
    elif second.get("page_id") == layout_page_ids[0]:
        ordered = (second, first)
    else:
        ordered = (first, second)
    front, back = ordered
    if front.get("page_id") != layout_page_ids[0] or back.get("page_id") != layout_page_ids[1]:
        return None
    for page, side in ((front, "front"), (back, "back")):
        parsed = parse_side_filename(page.get("filename"))
        if parsed is not None and parsed[1] != side:
            return None
    return ordered


def _apply_pair(
    pair_index: int,
    pair: Pair,
    mode: str,
    layout_page_ids: Sequence[str],
    identities: Mapping[int, dict],
) -> None:
    sides = _sides(pair, mode, layout_page_ids)
    for position, (page, partner) in enumerate((pair, pair[::-1])):
        _reset_page(page)
        side = None if sides is None else ("front" if page is sides[0] else "back")
        page["physical_pair_index"] = pair_index
        page["physical_side"] = side
        page["sheet_index"] = pair_index
        page["sheet_page_index"] = None if side is None else (1 if side == "front" else 2)
        page["pairing"] = {
            "mode": mode,
            "pair_index": pair_index,
            "side": side,
            "status": "pending",
            "paired_paper_id": partner.get("paper_id"),
        }
    if sides is None:
        _flag(pair, "page_type_mismatch", "path_pair_page_type_mismatch")
        return

    page_identities = [dict(page.get("identity") or {}) for page in pair]
    teacher_settled = all(
        identity.get("status") == "teacher_confirmed" and identity.get("student_id")
        for identity in page_identities
    )
    candidates: dict[int, str] = {}
    for identity in page_identities:
        if identity.get("status") not in _OWN_STATUSES or not identity.get("student_id"):
            continue
        if teacher_settled and identity.get("status") != "teacher_confirmed":
            continue
        candidates.setdefault(int(identity["student_id"]), str(identity.get("student_name") or ""))
    path_clues = [identities[id(page)] for page in pair]
    if not teacher_settled:
        for clue in path_clues:
            if clue["student"] is not None:
                candidates.setdefault(int(clue["student"]["student_id"]), str(clue["student"]["name"]))

    if len(candidates) > 1 or (not teacher_settled and any(clue["conflict"] for clue in path_clues)):
        _flag(pair, "identity_conflict", "path_pair_identity_conflict")
        return
    if not candidates:
        _flag(pair, "identity_missing", "path_pair_identity_missing")
        return

    student_id, student_name = next(iter(candidates.items()))
    named = [
        clue for clue in path_clues
        if clue["student"] is not None and int(clue["student"]["student_id"]) == student_id
    ]
    for page, partner, clue in ((pair[0], pair[1], path_clues[0]), (pair[1], pair[0], path_clues[1])):
        identity = dict(page.get("identity") or {})
        if identity.get("status") not in _OWN_STATUSES:
            paired = {
                **identity,
                "status": PATH_PAIRED_STATUS,
                "student_id": student_id,
                "student_name": student_name,
            }
            if named:
                own = clue if clue in named else named[0]
                paired.update(source="path", clue_source=own["clue_source"])
            else:
                paired.update(source="paired_page", source_paper_id=partner.get("paper_id"))
            page["identity"] = paired
            page["review_reasons"] = [
                reason for reason in list(page.get("review_reasons") or [])
                if not str(reason).startswith("identity_")
            ]
        page["pairing"]["status"] = "paired"


def apply_path_pair_identity(
    pages: Sequence[dict],
    layout_page_ids: Sequence[str],
    roster: list[dict],
) -> bool:
    """Pair and identify pages by upload path; ``False`` leaves them untouched.

    ``roster`` rows are ``{"student_id", "name", "student_no", "class_id"}``
    for the batch's own teacher and class.
    """
    if len(layout_page_ids) != 2 or not pages:
        return False
    identities = {id(page): path_identity(page, roster) for page in pages}
    for mode, pairs in (
        (FOLDER_PAIRING_MODE, folder_pairs(pages)),
        (FILENAME_PAIRING_MODE, filename_side_pairs(pages)),
        (ROSTER_NAME_PAIRING_MODE, roster_name_pairs(pages, identities)),
    ):
        if pairs is not None:
            for pair_index, pair in enumerate(pairs, 1):
                _apply_pair(pair_index, pair, mode, layout_page_ids, identities)
            return True
    return False
