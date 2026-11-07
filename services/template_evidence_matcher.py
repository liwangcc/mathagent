"""Matching a student page to one verified template and building its answer evidence.

- ``TemplateEvidenceMatcher`` verifies the template artifacts once and caches
  each page type's SIFT features (``_TemplateFeatures``,
  ``_feature_gray_and_scales``, ``_keypoint_points``; bounded by
  ``FEATURE_MAX_EDGE`` and ``FEATURE_NFEATURES``).  ``match_and_build`` keeps the
  template whose homography qualifies and clearly wins (``MIN_GOOD_MATCHES``,
  ``MIN_INLIERS``, ``MIN_INLIER_RATIO``, ``MIN_PAGE_COVERAGE``,
  ``MAX_REPROJECTION_RMSE``, ``LOWE_RATIO``), aligns the page and builds its
  evidence; ``_candidate_public`` is what each candidate reports.
  ``_build_question_anchor_overlay`` is kept as a no-op for old callers.
- ``cleanup_evidence`` removes an evidence image ``match_and_build`` created
  (``_EVIDENCE_NAME_RE``), idempotently.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.template_evidence_alignment import (
    _coverage_for_homography,
    _refine_local_alignment,
    _refine_source_mask,
)
from services.template_evidence_artifacts import (
    _safe_artifact_path,
    verify_template_artifacts,
)
from services.template_evidence_base import (
    EvidenceResult,
    Image,
    MAX_IMAGE_EDGE,
    TemplateEvidenceError,
    cv2,
    np,
)
from services.template_evidence_canonical import _load_rgb_image, _sha256_file
from services.template_evidence_columns import (
    build_full_column_difference_evidence_from_aligned,
)


# Feature extraction is the memory-heavy part of local page registration.  Keep
# the stored template and the generated evidence at MAX_IMAGE_EDGE, but run
# SIFT on a smaller working image.  Coordinates are scaled back to the stored
# image space before estimating the homography.
FEATURE_MAX_EDGE = max(
    640,
    min(
        MAX_IMAGE_EDGE,
        int(os.environ.get("OCR_TEMPLATE_FEATURE_MAX_EDGE", "1280")),
    ),
)


FEATURE_NFEATURES = max(
    500,
    min(
        10000,
        int(os.environ.get("OCR_TEMPLATE_FEATURE_NFEATURES", "3000")),
    ),
)


MIN_GOOD_MATCHES = 25


MIN_INLIERS = 15


MIN_INLIER_RATIO = 0.20


MIN_PAGE_COVERAGE = 0.78


MAX_REPROJECTION_RMSE = 6.0


LOWE_RATIO = 0.75


_EVIDENCE_NAME_RE = re.compile(r"evidence-[0-9a-f]{32}\.png\Z")


@dataclass(frozen=True)
class _TemplateFeatures:
    descriptors: np.ndarray | None
    # Full-resolution coordinates corresponding to ``descriptors``.  SIFT is
    # run on a reduced image, so these points are explicitly mapped back to
    # the artifact coordinate space before RANSAC.
    points: np.ndarray
    shape: tuple[int, int]


def _feature_gray_and_scales(rgb: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Return a bounded SIFT input and its x/y scale relative to ``rgb``."""

    height, width = rgb.shape[:2]
    longest = max(width, height)
    if longest <= FEATURE_MAX_EDGE:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        return np.ascontiguousarray(gray), 1.0, 1.0

    scale = FEATURE_MAX_EDGE / float(longest)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    reduced = cv2.resize(
        rgb,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA,
    )
    gray = cv2.cvtColor(reduced, cv2.COLOR_RGB2GRAY)
    return (
        np.ascontiguousarray(gray),
        target_width / float(width),
        target_height / float(height),
    )


def _keypoint_points(
    keypoints: tuple[Any, ...],
    scale_x: float,
    scale_y: float,
) -> np.ndarray:
    if not keypoints:
        return np.empty((0, 2), dtype=np.float32)
    points = np.asarray([keypoint.pt for keypoint in keypoints], dtype=np.float32)
    points[:, 0] /= float(scale_x)
    points[:, 1] /= float(scale_y)
    return np.ascontiguousarray(points)


def _candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    reprojection_rmse = candidate.get("reprojection_rmse")
    return {
        "page_type_id": candidate["page_type_id"],
        "good_matches": candidate["good_matches"],
        "inliers": candidate["inliers"],
        "inlier_ratio": round(candidate["inlier_ratio"], 4),
        "reprojection_rmse": (
            round(reprojection_rmse, 3)
            if reprojection_rmse is not None
            else None
        ),
        "coverage": round(candidate["coverage"], 4),
        "qualified": candidate["qualified"],
    }


def _build_question_anchor_overlay(aligned: np.ndarray, anchors: list[dict]) -> np.ndarray:
    """Keep the aligned page unchanged; answer regions are the only overlays."""
    # Question-number geometry was removed from the active model contract.  The
    # blue answer-region overlays carry the question label instead.  Retain the
    # helper as a no-op for old callers and persisted artifacts.
    return aligned


class TemplateEvidenceMatcher:
    """Match a student page to one verified template and build answer evidence."""

    def __init__(self, artifacts, template_dir):
        cv2.setNumThreads(1)
        if not hasattr(cv2, "SIFT_create"):
            raise RuntimeError("OpenCV SIFT support is required")
        self.template_dir = os.path.realpath(os.fspath(template_dir))
        self.artifacts = verify_template_artifacts(template_dir, artifacts)
        # The key set is bounded by the verified artifact mapping and each SIFT
        # result is bounded by FEATURE_NFEATURES. Full RGB pages are deliberately
        # excluded so a many-page template cannot retain every decoded image.
        self._feature_cache: dict[str, _TemplateFeatures] = {}
        self._feature_lock = threading.RLock()

    def _load_verified_template_rgb(self, page_type_id: str) -> np.ndarray:
        artifact = self.artifacts[page_type_id]
        path, _relative = _safe_artifact_path(
            self.template_dir, artifact["template_image"]
        )
        if _sha256_file(path) != artifact["sha256"]:
            raise TemplateEvidenceError("template artifact changed after verification")
        image = _load_rgb_image(path)
        height, width = image.shape[:2]
        if (width, height) != (artifact["width"], artifact["height"]):
            raise TemplateEvidenceError("template artifact dimensions changed")
        return image

    def _load_verified_difference_template_rgb(self, page_type_id: str) -> np.ndarray:
        """Load the hash-bound baseline with approved boxes painted white."""
        artifact = self.artifacts[page_type_id]
        relative = artifact.get("difference_template_image")
        if not relative:
            # Blank templates and legacy artifacts have no separate whiteout
            # image; their original registration image is the correct baseline.
            return self._load_verified_template_rgb(page_type_id)
        path, _relative = _safe_artifact_path(self.template_dir, relative)
        expected_hash = artifact.get("difference_template_sha256")
        if not isinstance(expected_hash, str) or _sha256_file(path) != expected_hash:
            raise TemplateEvidenceError(
                "difference template changed after verification"
            )
        image = _load_rgb_image(path)
        if image.shape[:2] != (artifact["height"], artifact["width"]):
            raise TemplateEvidenceError("difference template dimensions changed")
        return image

    def _template_features(self, page_type_id: str) -> _TemplateFeatures:
        with self._feature_lock:
            cached = self._feature_cache.get(page_type_id)
            if cached is not None:
                return cached
            image = self._load_verified_template_rgb(page_type_id)
            height, width = image.shape[:2]
            gray, scale_x, scale_y = _feature_gray_and_scales(image)
            sift = cv2.SIFT_create(nfeatures=FEATURE_NFEATURES)
            keypoints, descriptors = sift.detectAndCompute(gray, None)
            keypoints = tuple(keypoints or ())
            cached = _TemplateFeatures(
                descriptors,
                _keypoint_points(keypoints, scale_x, scale_y),
                (height, width),
            )
            self._feature_cache[page_type_id] = cached
            return cached

    def _match_candidate(
        self,
        page_type_id: str,
        student_keypoints,
        student_descriptors,
        student_points: np.ndarray | None = None,
        student_shape: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        template = self._template_features(page_type_id)
        candidate: dict[str, Any] = {
            "page_type_id": page_type_id,
            "good_matches": 0,
            "inliers": 0,
            "inlier_ratio": 0.0,
            "reprojection_rmse": None,
            "coverage": 0.0,
            "qualified": False,
            "homography": None,
            "source_inliers": None,
            "target_inliers": None,
        }
        if (
            student_descriptors is None
            or template.descriptors is None
            or len(student_descriptors) < 2
            or len(template.descriptors) < 2
        ):
            return candidate

        matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
        try:
            pairs = matcher.knnMatch(student_descriptors, template.descriptors, k=2)
        except cv2.error:
            return candidate
        good = [
            pair[0] for pair in pairs
            if len(pair) == 2 and pair[0].distance < LOWE_RATIO * pair[1].distance
        ]
        candidate["good_matches"] = len(good)
        if len(good) < MIN_GOOD_MATCHES:
            return candidate

        if student_points is None:
            student_points = np.asarray(
                [keypoint.pt for keypoint in student_keypoints],
                dtype=np.float32,
            )
        source_points = np.float32(
            [student_points[match.queryIdx] for match in good]
        ).reshape(-1, 1, 2)
        target_points = np.float32(
            [template.points[match.trainIdx] for match in good]
        ).reshape(-1, 1, 2)
        try:
            homography, mask = cv2.findHomography(
                source_points, target_points, cv2.RANSAC, 5.0
            )
        except cv2.error:
            return candidate
        if homography is None or mask is None or not np.isfinite(homography).all():
            return candidate

        inlier_mask = mask.ravel().astype(bool)
        inliers = int(np.count_nonzero(inlier_mask))
        inlier_ratio = inliers / len(good)
        candidate["inliers"] = inliers
        candidate["inlier_ratio"] = inlier_ratio
        if inliers == 0:
            return candidate
        source_inliers = source_points.reshape(-1, 2)[inlier_mask]
        target_inliers = target_points.reshape(-1, 2)[inlier_mask]
        projected = cv2.perspectiveTransform(
            source_inliers.reshape(-1, 1, 2), homography
        ).reshape(-1, 2)
        reprojection_rmse = float(np.sqrt(np.mean(np.sum(
            (projected - target_inliers) ** 2,
            axis=1,
        ))))
        source_shape = student_shape or template.shape
        coverage = _coverage_for_homography(
            source_shape,
            template.shape,
            homography,
        )
        candidate.update({
            "inliers": inliers,
            "inlier_ratio": inlier_ratio,
            "reprojection_rmse": reprojection_rmse,
            "coverage": coverage,
            "qualified": (
                inliers >= MIN_INLIERS
                and inlier_ratio >= MIN_INLIER_RATIO
                and reprojection_rmse <= MAX_REPROJECTION_RMSE
                and coverage >= MIN_PAGE_COVERAGE
            ),
            "homography": homography,
            "source_inliers": source_inliers,
            "target_inliers": target_inliers,
        })
        return candidate

    @staticmethod
    def _has_clear_winner(top: dict[str, Any], second: dict[str, Any]) -> bool:
        inlier_gap = top["inliers"] - second["inliers"]
        required_gap = max(8, int(math.ceil(second["inliers"] * 0.20)))
        if inlier_gap >= required_gap:
            return True
        return inlier_gap >= 5 and (
            top["inlier_ratio"] - second["inlier_ratio"] >= 0.15
        )

    def match_and_build(
        self,
        student_path,
        output_dir,
        *,
        include_evidence: bool = True,
        include_source_mask: bool = False,
    ) -> EvidenceResult:
        if not isinstance(include_evidence, bool):
            raise ValueError("include_evidence must be a boolean")
        if not isinstance(include_source_mask, bool):
            raise ValueError("include_source_mask must be a boolean")
        student_rgb = _load_rgb_image(student_path)
        student_height, student_width = student_rgb.shape[:2]
        student_gray, scale_x, scale_y = _feature_gray_and_scales(student_rgb)
        sift = cv2.SIFT_create(nfeatures=FEATURE_NFEATURES)
        student_keypoints, student_descriptors = sift.detectAndCompute(
            student_gray, None
        )
        student_keypoints = tuple(student_keypoints or ())
        student_points = _keypoint_points(student_keypoints, scale_x, scale_y)

        candidates = [
            self._match_candidate(
                page_type_id,
                student_keypoints,
                student_descriptors,
                student_points,
                student_rgb.shape[:2],
            )
            for page_type_id in self.artifacts
        ]
        public_candidates = [_candidate_public(value) for value in candidates]
        diagnostics: dict[str, Any] = {
            "student_width": student_width,
            "student_height": student_height,
            "student_keypoints": len(student_keypoints),
            "feature_max_edge": FEATURE_MAX_EDGE,
            "feature_nfeatures": FEATURE_NFEATURES,
            "candidates": public_candidates,
        }
        qualified = [value for value in candidates if value["qualified"]]
        qualified.sort(
            key=lambda value: (
                value["inliers"],
                value["inlier_ratio"],
                value["coverage"],
                -value["reprojection_rmse"],
                value["good_matches"],
            ),
            reverse=True,
        )
        if not qualified:
            raise TemplateEvidenceError("student page did not match a template", diagnostics)
        winner = qualified[0]
        if len(qualified) > 1 and not self._has_clear_winner(winner, qualified[1]):
            diagnostics["ambiguous_page_types"] = [
                winner["page_type_id"], qualified[1]["page_type_id"]
            ]
            raise TemplateEvidenceError("student page matched multiple templates", diagnostics)

        page_type_id = winner["page_type_id"]
        artifact = self.artifacts[page_type_id]
        globally_aligned = cv2.warpPerspective(
            student_rgb,
            winner["homography"],
            (artifact["width"], artifact["height"]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )
        aligned, local_alignment, local_corrections = _refine_local_alignment(
            globally_aligned,
            winner["homography"],
            winner["source_inliers"],
            winner["target_inliers"],
            return_corrections=True,
        )
        source_mask = np.full(student_rgb.shape[:2], 255, dtype=np.uint8)
        globally_aligned_mask = cv2.warpPerspective(
            source_mask,
            winner["homography"],
            (artifact["width"], artifact["height"]),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        aligned_source_mask = _refine_source_mask(
            globally_aligned_mask,
            local_corrections,
        )
        question_crop_plan = artifact.get("question_crop_plan")
        question_anchors = artifact.get("question_anchors") or []
        question_numbers = list(
            artifact.get("question_numbers")
            or [item["question_number"] for item in artifact.get("question_schema") or []]
        )
        template_handwriting_regions = artifact.get(
            "template_handwriting_regions"
        ) or []
        template_answer_regions = artifact.get(
            "template_answer_regions"
        ) or []
        blank_detection_enabled = artifact["blank_detection_enabled"]
        anchor_overlay = _build_question_anchor_overlay(aligned, question_anchors)
        difference_template = self._load_verified_difference_template_rgb(
            page_type_id
        )
        evidence_result = (
            build_full_column_difference_evidence_from_aligned(
                aligned,
                aligned_source_mask,
                difference_template,
                {
                    "page_type_id": page_type_id,
                    "question_numbers": question_numbers,
                    **({"question_schema": artifact["question_schema"]}
                       if artifact.get("question_schema") else {}),
                    "_legacy_solve_label_fallback": "question_label_policy" not in artifact,
                    "question_label_policy": artifact.get("question_label_policy"),
                    "question_crop_plan": question_crop_plan,
                    "question_anchors": question_anchors,
                    "template_mode": artifact.get("template_mode"),
                    "template_answer_regions": template_answer_regions,
                    "template_handwriting_regions": template_handwriting_regions,
                    "template_baseline_prewhitened": bool(
                        artifact.get("difference_template_image")
                    ),
                },
                output_dir,
            )
            if include_evidence
            else None
        )
        evidence_paths = (
            tuple(evidence_result.evidence_paths)
            if evidence_result is not None
            else ()
        )

        output_root = Path(output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        aligned_path = output_root / f"aligned-{uuid.uuid4().hex}.png"
        anchor_overlay_path = (
            output_root / f"question-anchors-{uuid.uuid4().hex}.png"
            if question_anchors
            else None
        )
        try:
            Image.fromarray(aligned, mode="RGB").save(aligned_path, format="PNG")
            if anchor_overlay_path is not None:
                Image.fromarray(anchor_overlay, mode="RGB").save(
                    anchor_overlay_path,
                    format="PNG",
                )
        except Exception:
            for partial in (
                aligned_path,
                *(Path(path) for path in evidence_paths),
                anchor_overlay_path,
            ):
                if partial is None:
                    continue
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        diagnostics.update({
            "selected_page_type_id": page_type_id,
            "aligned_image_path": str(aligned_path.resolve()),
            "raw_pixel_to_template_pixel": winner["homography"].tolist(),
            "template_pixel_to_raw_pixel": np.linalg.inv(winner["homography"]).tolist(),
            "template_width": artifact["width"],
            "template_height": artifact["height"],
            "evidence_width": (
                int(evidence_result.diagnostics.get("evidence_width") or 0)
                if evidence_result is not None else 0
            ),
            "evidence_height": (
                int(evidence_result.diagnostics.get("evidence_height") or 0)
                if evidence_result is not None else 0
            ),
            "ownership": (
                evidence_result.diagnostics.get("ownership")
                if evidence_result is not None
                else "full_column_difference"
            ),
            "local_alignment": local_alignment,
            "blank_detection_enabled": blank_detection_enabled,
            "new_ink_pixels": {},
            "program_blank_count": 0,
            "answer_source_complete": True,
            "question_anchor_count": len(question_anchors),
            "question_anchor_overlay_path": (
                str(anchor_overlay_path.resolve())
                if anchor_overlay_path is not None
                else ""
            ),
        })
        if evidence_result is not None:
            # Keep the actual rendered label positions auditable at the
            # production matcher boundary, not just in direct helper tests.
            for key in ("evidence_columns", "qwen37_label_contract",
                        "blank_label_safety", "printed_solve_label_policy", "legacy_solve_label_policy",
                        "drawing_label_policy", "answer_position_routing", "answer_position_review"):
                if key in evidence_result.diagnostics:
                    diagnostics[key] = evidence_result.diagnostics[key]
        if question_crop_plan is not None:
            diagnostics["question_crop_plan"] = {
                "strategy": question_crop_plan["strategy"],
                "block_count": len(question_crop_plan["blocks"]),
                "anchor_count": len(question_crop_plan["anchors"]),
                "vertical_gaps": question_crop_plan["coverage"]["vertical_gaps"],
                "provider_bottom_ignored": question_crop_plan["provider_bottom_ignored"],
                "requires_question_number_gate": question_crop_plan[
                    "requires_question_number_gate"
                ],
                "missing_expected_question_numbers": question_crop_plan[
                    "hint_coverage"
                ]["missing_expected_question_numbers"],
            }
        return EvidenceResult(
            page_type_id=page_type_id,
            evidence_path=(
                evidence_paths[0] if evidence_paths else ""
            ),
            evidence_paths=evidence_paths,
            question_numbers=question_numbers,
            blank_question_numbers=[],
            diagnostics=diagnostics,
            aligned_path=str(aligned_path.resolve()),
            aligned_source_mask=aligned_source_mask if include_source_mask else None,
        )


def cleanup_evidence(path) -> None:
    """Remove an evidence image created by ``match_and_build`` (idempotently)."""

    if path is None or str(path).strip() == "":
        return
    evidence_path = Path(path)
    if not _EVIDENCE_NAME_RE.fullmatch(evidence_path.name):
        raise ValueError("refusing to remove a non-evidence path")
    try:
        evidence_path.unlink(missing_ok=True)
    except IsADirectoryError as exc:
        raise ValueError("evidence path is not a file") from exc
