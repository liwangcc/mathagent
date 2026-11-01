"""Server-side registration of student answer-sheet pages (answer-sheet B5).

A template-code batch registers every student page to the frozen answer-sheet
template frame on the server when the upload is processed (and again when
the teacher corrects a page's orientation).  The existing SIFT/RANSAC
``TemplateEvidenceMatcher`` does the matching; the aligned page is stored
write-once in the batch's existing evidence directory
(``<batch>/evidence/aligned-<hex>.png``, removed by the raw-image retention
cleanup like every other batch image) next to a record of what was
registered: template fingerprint, source image hash, matched page type, image
hash and size, and which answer regions are backed by real source pixels.

Recognition (pixel bubbles and column transcription) reads only this image,
and only after ``load_registered_page`` has re-verified the record against
the batch.  A missing, failed, stale or altered registration fails closed;
there is no fallback to the accelerator evidence / ``stage_prompt`` path.
Records hold no absolute path and no student identity.
"""

from __future__ import annotations

import copy
import hashlib
import io
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from services.answer_sheet_layout_contract import normalized_bbox_to_pixels

REGISTRATION_CONTRACT_VERSION = "answer-sheet-page-registration-v1"
REGISTRATION_REGISTERED = "registered"
REGISTRATION_FAILED = "failed"
REGISTRATION_DIRNAME = "evidence"
REGISTERED_IMAGE_RE = re.compile(r"aligned-[0-9a-f]{32}\.png\Z")
_ATTEMPT_PREFIX = ".registration-"
# Failures that repeat for the same template and source image; anything else
# (I/O, a stalled worker) is retried when the upload processing resumes.
_DETERMINISTIC_FAILURES = frozenset({
    "page_not_matched", "page_ambiguous", "template_not_published", "template_artifacts_invalid",
})
_PUBLIC_CANDIDATE_KEYS = (
    "page_type_id", "good_matches", "inliers", "inlier_ratio", "reprojection_rmse", "coverage", "qualified",
)


class PageRegistrationError(ValueError):
    """The page has no usable registration; nothing may be read from it."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _public_diagnostics(diagnostics: object) -> dict[str, Any]:
    """Numeric match quality only: no paths, no homography, no pixels."""
    if not isinstance(diagnostics, Mapping):
        return {}
    result: dict[str, Any] = {
        "candidates": [
            {key: item.get(key) for key in _PUBLIC_CANDIDATE_KEYS}
            for item in diagnostics.get("candidates") or []
            if isinstance(item, Mapping)
        ],
    }
    for key in ("student_width", "student_height", "selected_page_type_id", "ambiguous_page_types"):
        if key in diagnostics:
            result[key] = copy.deepcopy(diagnostics[key])
    local = diagnostics.get("local_alignment")
    if isinstance(local, list):
        result["local_alignment"] = [
            {key: item.get(key) for key in ("tile", "matches", "inliers", "applied", "rejection_reason")}
            for item in local if isinstance(item, Mapping)
        ]
    return result


def _failed(reason_code: str, message: str, *, template_fingerprint: str, source_sha256: str,
            diagnostics: object = None, error_type: str = "") -> dict[str, Any]:
    record: dict[str, Any] = {
        "contract_version": REGISTRATION_CONTRACT_VERSION,
        "status": REGISTRATION_FAILED,
        "reason_code": reason_code,
        "message": message,
        "template_fingerprint": template_fingerprint,
        "source_sha256": source_sha256,
        "diagnostics": _public_diagnostics(diagnostics),
        "registered_at": _now(),
    }
    if error_type:
        record["error_type"] = error_type
    return record


def failed_registration(reason_code: str, message: str, *, template_fingerprint: str,
                        source_sha256: str) -> dict[str, Any]:
    """Record a registration that could not even be attempted (fail closed)."""
    return _failed(reason_code, message, template_fingerprint=template_fingerprint,
                   source_sha256=source_sha256)


def _covered(mask: np.ndarray, bbox: object) -> bool:
    height, width = mask.shape[:2]
    try:
        x1, y1, x2, y2 = normalized_bbox_to_pixels(list(bbox), width, height)
    except (TypeError, ValueError):
        return False
    crop = mask[y1:y2, x1:x2]
    return crop.size > 0 and bool((crop == 255).all())


def _incomplete(mask: np.ndarray, regions: Sequence[Mapping[str, Any]], page_id: str) -> list[str]:
    return [
        str(region.get("region_id") or "")
        for region in regions
        if str(region.get("page_id") or "") == page_id and not _covered(mask, region.get("bbox"))
    ]


def register_student_page(
    matcher: Any,
    source_path: str,
    session_dir: str | os.PathLike[str],
    *,
    answer_regions: Sequence[Mapping[str, Any]],
    identity_regions: Sequence[Mapping[str, Any]],
    template_fingerprint: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Register one oriented student page to the template frame (CPU, blocking).

    Returns the registration record; a page that does not match the template
    (or matches more than one page type) yields a ``failed`` record rather
    than an exception, so the upload keeps every page and recognition fails
    closed on it later.
    """
    from services.template_evidence import TemplateEvidenceError

    evidence_root = Path(session_dir).resolve() / REGISTRATION_DIRNAME
    evidence_root.mkdir(parents=True, exist_ok=True)
    attempt = evidence_root / f"{_ATTEMPT_PREFIX}{uuid.uuid4().hex}"
    attempt.mkdir(exist_ok=False)
    common = {"template_fingerprint": template_fingerprint, "source_sha256": source_sha256}
    try:
        try:
            result = matcher.match_and_build(
                source_path, str(attempt), include_evidence=False, include_source_mask=True,
            )
        except TemplateEvidenceError as exc:
            ambiguous = "ambiguous_page_types" in (exc.diagnostics or {})
            return _failed(
                "page_ambiguous" if ambiguous else "page_not_matched",
                "学生页面同时匹配多个答题卡页型" if ambiguous else "学生页面未能与答题卡模板配准",
                diagnostics=exc.diagnostics, **common,
            )
        except Exception as exc:  # recorded, never silently skipped
            return _failed("registration_error", "学生页面配准过程出错", error_type=type(exc).__name__, **common)

        diagnostics = result.diagnostics if isinstance(result.diagnostics, Mapping) else {}
        page_id = str(result.page_type_id or "")
        staged = Path(str(result.aligned_path or "")).resolve(strict=False)
        width, height = diagnostics.get("template_width"), diagnostics.get("template_height")
        mask = result.aligned_source_mask
        if (
            not page_id
            or staged.parent != attempt.resolve()
            or not REGISTERED_IMAGE_RE.fullmatch(staged.name)
            or not staged.is_file()
            or isinstance(width, bool) or not isinstance(width, int)
            or isinstance(height, bool) or not isinstance(height, int)
            or not isinstance(mask, np.ndarray)
            or mask.shape[:2] != (height, width)
        ):
            return _failed("registration_error", "学生页面配准结果不完整", diagnostics=diagnostics, **common)

        incomplete_answers = _incomplete(mask, answer_regions, page_id)
        incomplete_identity = _incomplete(mask, identity_regions, page_id)
        final = evidence_root / staged.name
        os.replace(staged, final)
        data = final.read_bytes()
        return {
            "contract_version": REGISTRATION_CONTRACT_VERSION,
            "status": REGISTRATION_REGISTERED,
            "file": final.name,
            "sha256": _sha256(data),
            "bytes": len(data),
            "width": width,
            "height": height,
            "page_id": page_id,
            "template_fingerprint": template_fingerprint,
            "source_sha256": source_sha256,
            "answer_source_complete": not incomplete_answers,
            "incomplete_answer_regions": incomplete_answers,
            "incomplete_identity_regions": incomplete_identity,
            "diagnostics": _public_diagnostics(diagnostics),
            "registered_at": _now(),
        }
    finally:
        shutil.rmtree(attempt, ignore_errors=True)


def registration_is_current(record: object, *, template_fingerprint: str, source_sha256: str) -> bool:
    """Whether ``record`` already answers registration for these exact inputs."""
    return (
        isinstance(record, Mapping)
        and record.get("contract_version") == REGISTRATION_CONTRACT_VERSION
        and record.get("template_fingerprint") == template_fingerprint
        and record.get("source_sha256") == source_sha256
        and (
            record.get("status") == REGISTRATION_REGISTERED
            or (record.get("status") == REGISTRATION_FAILED
                and record.get("reason_code") in _DETERMINISTIC_FAILURES)
        )
    )


def registered_image_path(session_dir: str | os.PathLike[str], record: object) -> Path:
    """Path of a registered image, confined to the batch evidence directory."""
    name = record.get("file") if isinstance(record, Mapping) else None
    if not isinstance(name, str) or not REGISTERED_IMAGE_RE.fullmatch(name):
        raise PageRegistrationError("registration_invalid", "页面配准记录无效")
    root = (Path(session_dir).resolve() / REGISTRATION_DIRNAME).resolve()
    path = (root / name).resolve(strict=False)
    if path.parent != root:
        raise PageRegistrationError("registration_invalid", "页面配准记录无效")
    return path


def load_registered_page(
    session_dir: str | os.PathLike[str],
    record: object,
    *,
    template_fingerprint: str,
    source_sha256: str,
    page_sizes: Mapping[str, Sequence[int]],
) -> tuple[np.ndarray, dict[str, Any]]:
    """The verified registered image (RGB uint8) and its record.

    Fails closed unless the record is a successful registration made for this
    batch's template fingerprint and the page's current source image, the
    stored bytes still match, and the image is exactly the template page size.
    """
    if not isinstance(record, Mapping) or not record:
        raise PageRegistrationError("registration_missing", "页面尚未完成服务端配准，请重新上传该页或确认方向")
    if record.get("contract_version") != REGISTRATION_CONTRACT_VERSION:
        raise PageRegistrationError("registration_invalid", "页面配准记录无效")
    if record.get("status") == REGISTRATION_FAILED:
        raise PageRegistrationError(
            "registration_failed",
            str(record.get("message") or "学生页面未能与答题卡模板配准"),
        )
    if record.get("status") != REGISTRATION_REGISTERED:
        raise PageRegistrationError("registration_invalid", "页面配准记录无效")
    if record.get("template_fingerprint") != template_fingerprint:
        raise PageRegistrationError("registration_stale", "页面配准所用模板与批次模板不一致，需要重新配准")
    if record.get("source_sha256") != source_sha256:
        raise PageRegistrationError("registration_stale", "页面图像或方向已变化，需要重新配准")
    path = registered_image_path(session_dir, record)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise PageRegistrationError("registration_file_missing", "配准图像已按保留策略清理或缺失") from exc
    if _sha256(data) != record.get("sha256"):
        raise PageRegistrationError("registration_integrity_failed", "配准图像完整性校验失败")
    page_id = str(record.get("page_id") or "")
    expected = page_sizes.get(page_id)
    if (
        expected is None
        or list(expected) != [record.get("width"), record.get("height")]
    ):
        raise PageRegistrationError("registration_size_mismatch", "配准图像页型或尺寸与答题卡模板不一致")
    from PIL import Image

    with Image.open(io.BytesIO(data)) as decoded:
        image = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
    if image.shape[:2] != (record.get("height"), record.get("width")):
        raise PageRegistrationError("registration_size_mismatch", "配准图像页型或尺寸与答题卡模板不一致")
    return np.ascontiguousarray(image), dict(record)
