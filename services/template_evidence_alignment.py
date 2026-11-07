"""Aligning a student photo to its template beyond the global homography, within tight limits.

- ``_coverage_for_homography`` measures how much of the canonical template the
  photo covers.
- ``_refine_local_alignment`` corrects small left/right page drift tile by tile
  (``LOCAL_TILES_X``) and accepts a local transform only when
  ``_trusted_local_affine`` finds it within the ``LOCAL_*`` limits on inliers,
  translation, scale, rotation and shear; large warps are refused.
- ``_refine_source_mask`` replays those local transforms with the same blend
  support, so the mask of real source pixels matches the aligned image.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math
from typing import Any

from services.template_evidence_base import cv2, np


LOCAL_TILES_X = 2
LOCAL_MIN_INLIERS = 8
LOCAL_MIN_INLIER_RATIO = 0.55
LOCAL_OVERLAP_RATIO = 0.06
LOCAL_MAX_TRANSLATION_RATIO = 0.025
LOCAL_MAX_SCALE_CHANGE = 0.04
LOCAL_MAX_ROTATION_DEGREES = 3.0
LOCAL_MAX_SHEAR = 0.04


def _coverage_for_homography(
    source_shape: tuple[int, int],
    target_shape: tuple[int, int],
    homography: np.ndarray,
) -> float:
    """Return how much of the canonical template is covered by the photo."""

    source_height, source_width = source_shape
    target_height, target_width = target_shape
    source_mask = np.ones((source_height, source_width), dtype=np.uint8)
    warped = cv2.warpPerspective(
        source_mask,
        homography,
        (target_width, target_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return float(np.count_nonzero(warped)) / float(target_height * target_width)


def _trusted_local_affine(
    estimated: np.ndarray,
    *,
    correction_inliers: int,
    inlier_ratio: float,
    image_width: int,
    image_height: int,
) -> tuple[bool, str]:
    if correction_inliers < LOCAL_MIN_INLIERS:
        return False, "too_few_affine_inliers"
    if inlier_ratio < LOCAL_MIN_INLIER_RATIO:
        return False, "weak_affine_inlier_ratio"

    linear = np.asarray(estimated[:, :2], dtype=np.float64)
    translation = np.asarray(estimated[:, 2], dtype=np.float64)
    determinant = float(np.linalg.det(linear))
    if determinant <= 0:
        return False, "affine_reflection_or_collapse"

    left, singular_values, right = np.linalg.svd(linear)
    rotation = left @ right
    if float(np.linalg.det(rotation)) <= 0:
        return False, "affine_reflection"
    if np.any(singular_values < 1.0 - LOCAL_MAX_SCALE_CHANGE) or np.any(
        singular_values > 1.0 + LOCAL_MAX_SCALE_CHANGE
    ):
        return False, "affine_scale_out_of_bounds"

    angle = abs(math.degrees(math.atan2(rotation[1, 0], rotation[0, 0])))
    if angle > LOCAL_MAX_ROTATION_DEGREES:
        return False, "affine_rotation_out_of_bounds"

    first_column = linear[:, 0]
    second_column = linear[:, 1]
    denominator = float(
        np.linalg.norm(first_column) * np.linalg.norm(second_column)
    )
    shear = (
        abs(float(np.dot(first_column, second_column))) / denominator
        if denominator > 0
        else math.inf
    )
    if shear > LOCAL_MAX_SHEAR:
        return False, "affine_shear_out_of_bounds"

    if (
        abs(float(translation[0])) > image_width * LOCAL_MAX_TRANSLATION_RATIO
        or abs(float(translation[1])) > image_height * LOCAL_MAX_TRANSLATION_RATIO
    ):
        return False, "affine_translation_out_of_bounds"
    return True, ""


def _refine_local_alignment(
    globally_aligned: np.ndarray,
    homography: np.ndarray,
    source_inliers: np.ndarray,
    target_inliers: np.ndarray,
    *,
    return_corrections: bool = False,
) -> tuple[np.ndarray, list[dict[str, Any]]] | tuple[
    np.ndarray, list[dict[str, Any]], list[np.ndarray]
]:
    """Correct small left/right page drift without permitting large warps.

    ``return_corrections`` lets the source-pixel gate replay the exact accepted
    local affine transforms on a binary source mask.  It is opt-in so existing
    callers retain the two-value return contract.
    """

    height, width = globally_aligned.shape[:2]
    warped_source = cv2.perspectiveTransform(
        source_inliers.reshape(-1, 1, 2), homography
    ).reshape(-1, 2)
    overlap = max(4, int(round(width * LOCAL_OVERLAP_RATIO / LOCAL_TILES_X)))
    accumulator = np.zeros_like(globally_aligned, dtype=np.float32)
    weights = np.zeros((height, width), dtype=np.float32)
    diagnostics: list[dict[str, Any]] = []
    corrections: list[np.ndarray] = []

    for tile_index in range(LOCAL_TILES_X):
        core_left = round(tile_index * width / LOCAL_TILES_X)
        core_right = round((tile_index + 1) * width / LOCAL_TILES_X)
        select = (
            (target_inliers[:, 0] >= max(0, core_left - overlap))
            & (target_inliers[:, 0] < min(width, core_right + overlap))
        )
        selected_count = int(np.count_nonzero(select))
        correction = np.eye(2, 3, dtype=np.float32)
        correction_inliers = 0
        correction_applied = False
        rejection_reason = "not_enough_tile_matches"

        if selected_count >= LOCAL_MIN_INLIERS:
            estimated, mask = cv2.estimateAffine2D(
                warped_source[select],
                target_inliers[select],
                method=cv2.RANSAC,
                ransacReprojThreshold=3.0,
                maxIters=2000,
                confidence=0.99,
                refineIters=10,
            )
            if estimated is not None and np.isfinite(estimated).all():
                correction_inliers = (
                    int(np.count_nonzero(mask)) if mask is not None else 0
                )
                trusted, rejection_reason = _trusted_local_affine(
                    estimated,
                    correction_inliers=correction_inliers,
                    inlier_ratio=correction_inliers / selected_count,
                    image_width=width,
                    image_height=height,
                )
                if trusted:
                    correction = estimated.astype(np.float32)
                    correction_applied = True
            else:
                rejection_reason = "affine_estimation_failed"

        locally_aligned = cv2.warpAffine(
            globally_aligned,
            correction,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(255, 255, 255),
        )
        left_edge = max(0, core_left - overlap)
        right_edge = min(width, core_right + overlap)
        horizontal_weight = np.zeros(width, dtype=np.float32)
        horizontal_weight[left_edge:right_edge] = 1.0
        if tile_index > 0 and core_left > left_edge:
            horizontal_weight[left_edge:core_left] = np.linspace(
                0.0, 1.0, core_left - left_edge, endpoint=False
            )
        if tile_index + 1 < LOCAL_TILES_X and right_edge > core_right:
            horizontal_weight[core_right:right_edge] = np.linspace(
                1.0, 0.0, right_edge - core_right, endpoint=False
            )
        tile_weight = np.broadcast_to(horizontal_weight, (height, width))
        accumulator += locally_aligned.astype(np.float32) * tile_weight[:, :, None]
        weights += tile_weight
        diagnostics.append({
            "tile": tile_index,
            "matches": selected_count,
            "inliers": correction_inliers,
            "applied": correction_applied,
            "rejection_reason": None if correction_applied else rejection_reason,
        })
        corrections.append(correction.copy())

    safe_weights = np.maximum(weights, 1e-6)
    refined = accumulator / safe_weights[:, :, None]
    uncovered = weights <= 1e-6
    refined[uncovered] = globally_aligned[uncovered]
    result = np.clip(np.rint(refined), 0, 255).astype(np.uint8)
    if return_corrections:
        return result, diagnostics, corrections
    return result, diagnostics


def _refine_source_mask(
    globally_aligned_mask: np.ndarray,
    corrections: list[np.ndarray],
) -> np.ndarray:
    """Replay local transforms with the exact positive-weight blend support.

    A final blended pixel is valid only when every tile contributing a positive
    weight maps to a real source pixel.  Taking a union is unsafe because one
    tile can contribute perspective-fill while another contributes real pixels.
    """

    height, width = globally_aligned_mask.shape[:2]
    overlap = max(4, int(round(width * LOCAL_OVERLAP_RATIO / LOCAL_TILES_X)))
    total_weight = np.zeros((height, width), dtype=np.float32)
    valid_weight = np.zeros((height, width), dtype=np.float32)
    for tile_index, correction in enumerate(corrections):
        core_left = round(tile_index * width / LOCAL_TILES_X)
        core_right = round((tile_index + 1) * width / LOCAL_TILES_X)
        left_edge = max(0, core_left - overlap)
        right_edge = min(width, core_right + overlap)
        locally_aligned = cv2.warpAffine(
            globally_aligned_mask,
            correction,
            (width, height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        horizontal_weight = np.zeros(width, dtype=np.float32)
        horizontal_weight[left_edge:right_edge] = 1.0
        if tile_index > 0 and core_left > left_edge:
            horizontal_weight[left_edge:core_left] = np.linspace(
                0.0, 1.0, core_left - left_edge, endpoint=False
            )
        if tile_index + 1 < LOCAL_TILES_X and right_edge > core_right:
            horizontal_weight[core_right:right_edge] = np.linspace(
                1.0, 0.0, right_edge - core_right, endpoint=False
            )
        tile_weight = np.broadcast_to(horizontal_weight, (height, width))
        total_weight += tile_weight
        valid_weight += tile_weight * (locally_aligned == 255)
    complete = (total_weight > 0.0) & np.isclose(
        valid_weight, total_weight, rtol=0.0, atol=1e-6
    )
    return np.where(complete, 255, 0).astype(np.uint8)
