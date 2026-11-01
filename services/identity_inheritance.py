"""Opt-in identity inheritance for ordinary (identified) grading batches.

Many students write their name only on the first page of a multi-page paper.
When the teacher enables inheritance for a batch, a page that carries no
identity clue at all takes the identity of the previous page in upload order,
which is the natural order of the uploaded file paths.

Only a truly blank page inherits: the identity crop OCR succeeded and saw no
handwritten name or number, and no roster name came from the file or folder
path. A written but unmatched name, a low-confidence or conflicting identity, or
a failed OCR stands on its own and stays with the teacher. A page never joins
a student who already has a page of the same template page type; that
usually means a new student forgot to write a name.

Followers of an auto-matched page are matched to the same student. Followers
of a page that still needs confirmation keep their own pending identity and
carry ``inherit_anchor``, so one teacher decision covers the whole group.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

INHERITED_REASON = "inherited_previous_page"
SAME_PAGE_TYPE_BLOCK = "same_page_type"
_ASCII_DIGITS_RE = re.compile(r"([0-9]+)")


def inheritance_upload_order_key(source_path: str, filename: str) -> tuple:
    """Natural path order: ``扫描_2`` before ``扫描_10``, folders kept together."""
    path = str(source_path or filename or "").replace("\\", "/")
    return tuple(
        tuple(
            (0, int(token), len(token)) if _ASCII_DIGITS_RE.fullmatch(token) else (1, token.casefold(), 0)
            for token in _ASCII_DIGITS_RE.split(segment)
            if token
        )
        for segment in path.split("/")
    )


def page_has_no_identity(paper: dict) -> bool:
    """True only when the page itself offers nothing that names a student."""
    identity = paper.get("identity") or {}
    return (
        not paper.get("ocr_failure")
        and paper.get("identity_name_written") is False
        and str(paper.get("identity_roster_match_status") or "") == "no_match"
        and identity.get("status") != "matched"
        and not str(identity.get("ocr_name") or "").strip()
        and not str(identity.get("ocr_student_no") or "").strip()
        and not identity.get("name_sources")
    )


def _restore_own_identity(paper: dict) -> None:
    own = paper.pop("own_identity", None)
    if isinstance(own, dict):
        paper["identity"] = dict(own.get("identity") or {})
        paper["student_name"] = str(own.get("student_name") or "")
        paper["student_no"] = str(own.get("student_no") or "")


def _remember_own_identity(paper: dict) -> dict:
    own = dict(paper.get("identity") or {})
    paper["own_identity"] = {
        "identity": own,
        "student_name": str(paper.get("student_name") or ""),
        "student_no": str(paper.get("student_no") or ""),
    }
    return own


def _inherit(paper: dict, anchor: dict) -> None:
    own = _remember_own_identity(paper)
    anchor_identity = anchor.get("identity") or {}
    source = {
        "paper_id": str(anchor.get("paper_id") or ""),
        "filename": str(anchor.get("filename") or ""),
    }
    if anchor_identity.get("status") == "matched" and anchor_identity.get("student_id"):
        paper["identity"] = {
            **own,
            "status": "matched",
            "student_id": anchor_identity["student_id"],
            "reason": INHERITED_REASON,
            "candidates": list(anchor_identity.get("candidates") or []),
            "resolved_name": str(anchor.get("student_name") or ""),
            "resolved_student_no": str(anchor.get("student_no") or ""),
            "inherited_from": source,
        }
        paper["student_name"] = str(anchor.get("student_name") or "")
        paper["student_no"] = str(anchor.get("student_no") or "")
    else:
        paper["identity"] = {**own, "inherit_anchor": source}


def apply_identity_inheritance(
    uploads: list[dict],
    papers: list[dict],
    *,
    enabled: bool,
    excluded_paper_ids: Iterable[str] = (),
) -> list[dict]:
    """Recompute inherited identities in upload order, in place and idempotently.

    Returns the pages that became auto-matched by this call so the caller can
    start grading them. A page that is not recognized yet breaks the chain until
    it is; teacher-excluded pages are skipped.
    """
    by_id = {str(paper.get("paper_id") or ""): paper for paper in papers}
    excluded = {str(paper_id) for paper_id in excluded_paper_ids}
    newly_matched: list[dict] = []
    group: dict | None = None
    for upload in uploads:
        paper_id = str(upload.get("paper_id") or "")
        if paper_id in excluded:
            continue
        paper = by_id.get(paper_id)
        if paper is None:
            group = None
            continue
        was_matched = (paper.get("identity") or {}).get("status") == "matched"
        _restore_own_identity(paper)
        page_type = str(paper.get("ocr_page_type") or "")
        blocked = False
        if enabled and group is not None and page_has_no_identity(paper):
            if page_type and page_type not in group["page_types"]:
                group["page_types"].add(page_type)
                _inherit(paper, group["anchor"])
                if not was_matched and paper["identity"].get("status") == "matched":
                    newly_matched.append(paper)
                continue
            blocked = bool(page_type)
        if blocked:
            own = _remember_own_identity(paper)
            paper["identity"] = {**own, "inherit_blocked": SAME_PAGE_TYPE_BLOCK}
        group = {"anchor": paper, "page_types": {page_type} if page_type else set()}
    return newly_matched
