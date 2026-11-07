"""Pairing a batch's student pages and the identity a back page inherits.

- ``_apply_student_page_pairing`` pairs pages by upload folder, side-marked file
  name or roster name when every page fits one rule, otherwise by upload order.
- ``_apply_ordered_duplex_identity`` pairs adjacent capture pages
  (``_ORDERED_DUPLEX_PAIRING``) and lets a back page inherit its front's identity
  only after hard checks; a pair that fails one records why
  (``_ORDERED_PAIRING_REASONS``, ``_add_pairing_reason``, ``_clear_pairing_reasons``).
  ``_ordered_duplex_layout_ids`` lists the answer-sheet pages in order.

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

from models import Student
from services.answer_sheet_page_pairing import apply_path_pair_identity
from services.answer_sheet_session import AnswerSheetSession


_ORDERED_DUPLEX_PAIRING = "ordered_duplex"


_ORDERED_PAIRING_REASONS = {
    "ordered_pair_missing_back",
    "ordered_pair_template_unknown",
    "ordered_pair_page_type_mismatch",
    "ordered_pair_front_identity_missing",
    "ordered_pair_identity_conflict",
}


def _add_pairing_reason(page: dict, reason: str) -> None:
    reasons = list(page.get("review_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    page["review_reasons"] = reasons


def _clear_pairing_reasons(page: dict) -> None:
    page["review_reasons"] = [
        reason
        for reason in list(page.get("review_reasons") or [])
        if reason not in _ORDERED_PAIRING_REASONS
    ]


def _ordered_duplex_layout_ids(session: AnswerSheetSession) -> list[str]:
    page_ids = [
        str(page.get("page_id") or "").strip()
        for page in session.answer_sheet_pages
        if str(page.get("page_id") or "").strip()
    ]
    return page_ids


def _apply_student_page_pairing(
    session: AnswerSheetSession,
    roster_rows: list[Student],
) -> None:
    """Pair by upload folder, side-marked file name or roster name when every
    page fits one rule, otherwise by upload order."""
    roster = [
        {
            "student_id": row.id,
            "name": row.name,
            "student_no": row.student_no or "",
            "class_id": row.class_id,
        }
        for row in roster_rows
    ]
    if apply_path_pair_identity(
        session.student_pages,
        _ordered_duplex_layout_ids(session),
        roster,
    ):
        return
    _apply_ordered_duplex_identity(session)


def _apply_ordered_duplex_identity(session: AnswerSheetSession) -> None:
    """Pair adjacent capture pages and inherit identity only after hard checks."""
    upload = session.student_upload if isinstance(session.student_upload, dict) else {}
    if str(upload.get("pairing_mode") or _ORDERED_DUPLEX_PAIRING) != _ORDERED_DUPLEX_PAIRING:
        return

    pages = sorted(
        session.student_pages,
        key=lambda item: int(item.get("physical_order") or item.get("index", 0) + 1),
    )
    layout_ids = _ordered_duplex_layout_ids(session)
    template_page_count = len(layout_ids)
    if template_page_count == 1:
        for position, page in enumerate(pages, 1):
            page["physical_order"] = position
            page["physical_pair_index"] = None
            page["physical_side"] = "front"
            page["sheet_index"] = position
            page["sheet_page_index"] = 1
            _clear_pairing_reasons(page)
            page["pairing"] = {
                "mode": "single_page",
                "status": "not_applicable",
                "sheet_index": position,
                "side": "front",
            }
        return
    valid_template = template_page_count >= 2 and template_page_count % 2 == 0
    for position, page in enumerate(pages, 1):
        page["physical_order"] = position
        page["physical_pair_index"] = (position + 1) // 2
        page["physical_side"] = "front" if position % 2 else "back"
        page["sheet_index"] = (position - 1) // template_page_count + 1 if valid_template else None
        page["sheet_page_index"] = (position - 1) % template_page_count + 1 if valid_template else None
        _clear_pairing_reasons(page)
        page["pairing"] = {
            "mode": _ORDERED_DUPLEX_PAIRING,
            "pair_index": (position + 1) // 2,
            "side": "front" if position % 2 else "back",
            "status": "pending",
        }

    if not valid_template:
        for page in pages:
            page["pairing"]["status"] = "template_unknown"
            _add_pairing_reason(page, "ordered_pair_template_unknown")
        return

    if len(pages) % template_page_count:
        incomplete_start = len(pages) - (len(pages) % template_page_count)
        for page in pages[incomplete_start:]:
            page["pairing"]["status"] = "missing_back"
            _add_pairing_reason(page, "ordered_pair_missing_back")

    for sheet_start in range(0, len(pages) - (len(pages) % template_page_count), template_page_count):
        sheet_pages = pages[sheet_start:sheet_start + template_page_count]
        page_type_ok = all(
            page.get("page_id") == layout_ids[index]
            for index, page in enumerate(sheet_pages)
        )
        if not page_type_ok:
            for page in sheet_pages:
                page["pairing"]["status"] = "page_type_mismatch"
                _add_pairing_reason(page, "ordered_pair_page_type_mismatch")
            continue

        front_pages = sheet_pages[::2]
        identity_page = front_pages[0]
        identity = identity_page.get("identity") or {}
        front_id = identity.get("student_id")
        front_ready = identity.get("status") in {"recognized", "teacher_confirmed"} and front_id
        if not front_ready:
            for page in sheet_pages:
                page["pairing"]["status"] = "front_identity_missing"
                _add_pairing_reason(page, "ordered_pair_front_identity_missing")
            continue

        conflict = False
        for page in sheet_pages[1:]:
            page_identity = page.get("identity") or {}
            page_id = page_identity.get("student_id")
            if (
                page_identity.get("status") in {"recognized", "teacher_confirmed"}
                and page_id
                and str(page_id) != str(front_id)
            ):
                conflict = True
                break
        if conflict:
            for page in sheet_pages:
                page["pairing"]["status"] = "identity_conflict"
                _add_pairing_reason(page, "ordered_pair_identity_conflict")
            continue

        for page in sheet_pages:
            page_identity = page.get("identity") or {}
            if page is not identity_page and page_identity.get("status") not in {"recognized", "teacher_confirmed"}:
                page["identity"] = {
                    **page_identity,
                    "status": "ordered_pair_inherited",
                    "student_id": front_id,
                    "student_name": identity.get("student_name") or "",
                    "source": "physical_order_front",
                    "source_paper_id": identity_page.get("paper_id"),
                }
                page["review_reasons"] = [
                    reason
                    for reason in list(page.get("review_reasons") or [])
                    if not reason.startswith("identity_")
                ]
        for pair_offset in range(0, template_page_count, 2):
            front = sheet_pages[pair_offset]
            back = sheet_pages[pair_offset + 1]
            front["pairing"].update(status="paired", paired_paper_id=back.get("paper_id"))
            back["pairing"].update(status="identity_inherited", paired_paper_id=front.get("paper_id"))
