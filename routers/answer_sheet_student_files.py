"""The files one student page's recognition writes, and whether a replacement is kept or undone.

A new recognition writes the page's aligned image and answer crops beside the
old ones (``_ALIGNED_EVIDENCE_RE``, ``_ANSWER_CROP_RE``; attempt directories
``_STUDENT_RECOGNITION_ATTEMPT_RE``).

- Success: ``_commit_student_asset_replacement`` keeps the new files and
  ``_cleanup_replaced_student_assets`` removes only the superseded ones.
- Failure: ``_discard_student_asset_replacement``, ``_cleanup_failed_aligned_asset``,
  ``_cleanup_student_recognition_attempt`` and ``_restore_student_page`` put the
  page back.  An accelerator result is left alone until the page checkpoint is
  saved; ``_finish_student_accelerator_replacement`` then consumes it.

Moved unchanged from routers/answer_sheet.py, which still exposes these names.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import shutil
from pathlib import Path

from services.accelerator_business import finish_business_preprocessing
from services.answer_sheet_session import AnswerSheetSession

# routers.answer_sheet's logger, so these records keep their logger name.
logger = logging.getLogger("routers.answer_sheet")


_ALIGNED_EVIDENCE_RE = re.compile(r"aligned-[0-9a-f]{32}\.png\Z")


_ANSWER_CROP_RE = re.compile(r"crop-[0-9a-f]{24}\.png\Z")


_STUDENT_RECOGNITION_ATTEMPT_RE = re.compile(
    r"\.student-recognition-[0-9a-f]{32}\Z"
)


def _cleanup_replaced_student_assets(
    session: AnswerSheetSession,
    page: dict,
    *,
    aligned_path: str,
    answers: list[dict],
) -> None:
    """Remove only generated assets superseded by a successful checkpoint."""
    evidence_root = (Path(session.session_dir) / "evidence").resolve()
    old_working = str(page.get("working_path") or "").strip()
    if old_working and os.path.realpath(old_working) != os.path.realpath(aligned_path):
        old_path = Path(old_working).resolve(strict=False)
        if (
            old_path.parent == evidence_root
            and _ALIGNED_EVIDENCE_RE.fullmatch(old_path.name)
        ):
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove superseded aligned answer sheet")

    retained_crops = {
        str(item.get("evidence_file") or "")
        for item in answers
        if isinstance(item, dict)
    }
    for old_answer in page.get("answers") or []:
        if not isinstance(old_answer, dict):
            continue
        name = str(old_answer.get("evidence_file") or "")
        if name in retained_crops or not _ANSWER_CROP_RE.fullmatch(name):
            continue
        try:
            (evidence_root / name).unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove superseded answer crop")


def _cleanup_failed_aligned_asset(
    session: AnswerSheetSession,
    aligned_path: str,
    *,
    crop_paths: list[str] | tuple[str, ...] = (),
) -> None:
    evidence_root = (Path(session.session_dir) / "evidence").resolve()
    for raw_path in crop_paths:
        candidate = Path(raw_path).resolve(strict=False)
        if (
            candidate.parent == evidence_root
            and _ANSWER_CROP_RE.fullmatch(candidate.name)
        ):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove incomplete answer crop")
    candidate = Path(aligned_path).resolve(strict=False)
    if (
        candidate.parent == evidence_root
        and _ALIGNED_EVIDENCE_RE.fullmatch(candidate.name)
    ):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove incomplete aligned answer sheet")


def _cleanup_student_recognition_attempt(
    evidence_root: Path,
    attempt_root: Path,
) -> None:
    root = evidence_root.resolve()
    candidate = attempt_root.resolve(strict=False)
    if (
        candidate.parent != root
        or not _STUDENT_RECOGNITION_ATTEMPT_RE.fullmatch(candidate.name)
    ):
        logger.error("refusing to remove invalid student recognition attempt path")
        return
    try:
        shutil.rmtree(candidate, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to remove student recognition attempt directory")


def _restore_student_page(page: dict, previous_page: dict) -> None:
    page.clear()
    page.update(copy.deepcopy(previous_page))


def _discard_student_asset_replacement(
    session: AnswerSheetSession,
    replacement: dict | None,
) -> None:
    if not replacement:
        return
    if replacement.get("accelerator_handle") is not None:
        # The remote artifact remains validated and retryable. It is consumed
        # only after the page checkpoint has been durably saved.
        return
    _cleanup_failed_aligned_asset(
        session,
        replacement["aligned_path"],
        crop_paths=replacement["crop_paths"],
    )
    _restore_student_page(replacement["page"], replacement["previous_page"])


def _commit_student_asset_replacement(
    session: AnswerSheetSession,
    replacement: dict | None,
) -> None:
    if not replacement:
        return
    if replacement.get("accelerator_handle") is not None:
        return
    _cleanup_replaced_student_assets(
        session,
        replacement["previous_page"],
        aligned_path=replacement["aligned_path"],
        answers=replacement["answers"],
    )


async def _finish_student_accelerator_replacement(
    replacement: dict | None,
) -> None:
    handle = replacement.get("accelerator_handle") if replacement else None
    if handle is None:
        return
    try:
        await asyncio.to_thread(
            finish_business_preprocessing,
            handle,
            fallback_succeeded=True,
        )
    except Exception:
        # The durable page is authoritative. A cleanup failure must not make a
        # teacher resubmit a page or cause a second image-processing job.
        logger.exception("failed to consume answer-sheet accelerator evidence")
