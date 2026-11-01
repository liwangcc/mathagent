from __future__ import annotations

import hashlib
import json
import math
import string
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


MAX_CONSENSUS_SOURCES = 24
REFERENCE_MAX_EDGE = 1800


class AlignmentError(RuntimeError):
    """Raised when a photographed page cannot be mapped to the template."""


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class LabConfig:
    max_edge: int = 1800
    feature_edge: int = 960
    detector: str = "sift"
    max_features: int = 3500
    ratio_test: float = 0.72
    ransac_threshold: float = 4.0
    min_good_matches: int = 24
    min_inliers: int = 16
    min_inlier_ratio: float = 0.28
    min_coverage: float = 0.78
    min_template_edge_recall: float = 0.72
    local_tiles_x: int = 2
    local_min_inliers: int = 8
    local_min_inlier_ratio: float = 0.55
    local_overlap_ratio: float = 0.06
    local_max_translation_ratio: float = 0.025
    local_max_scale_change: float = 0.04
    local_max_rotation_degrees: float = 3.0
    local_max_shear: float = 0.04
    consensus_percentile: int = 70
    print_vote_ratio: float = 0.70
    print_dark_threshold: int = 210
    print_intensity_percentile: int = 25
    print_mask_dilate: int = 2
    seed_dark_threshold: int = 188
    seed_difference: int = 24
    evidence_mode: str = "soft_difference"
    print_guard_radius: int = 2
    near_print_difference: int = 42
    near_print_min_template: int = 190
    blue_chroma_threshold: int = 18
    blue_chroma_delta: int = 12
    component_min_area: int = 4
    component_max_area_ratio: float = 0.025
    blank_pixel_limit: int = 5
    visible_pixel_limit: int = 24

    def validate(self) -> "LabConfig":
        if self.detector not in {"sift", "orb"}:
            raise ValueError("detector must be 'sift' or 'orb'")
        if self.max_edge < 640 or self.feature_edge < 320:
            raise ValueError("image bounds are too small")
        if not 0.5 <= self.ratio_test < 1.0:
            raise ValueError("ratio_test must be between 0.5 and 1.0")
        if not 0 < self.min_inlier_ratio <= 1:
            raise ValueError("min_inlier_ratio must be between 0 and 1")
        if not 0 < self.min_coverage <= 1:
            raise ValueError("min_coverage must be between 0 and 1")
        if not 0 < self.min_template_edge_recall <= 1:
            raise ValueError("min_template_edge_recall must be between 0 and 1")
        if not 1 <= self.local_tiles_x <= 4:
            raise ValueError("local_tiles_x must be between 1 and 4")
        if not 0 < self.local_min_inlier_ratio <= 1:
            raise ValueError("local_min_inlier_ratio must be between 0 and 1")
        if not 0 <= self.local_max_translation_ratio <= 0.10:
            raise ValueError(
                "local_max_translation_ratio must be between 0 and 0.10"
            )
        if not 0 <= self.local_max_scale_change <= 0.20:
            raise ValueError("local_max_scale_change must be between 0 and 0.20")
        if not 0 <= self.local_max_rotation_degrees <= 15:
            raise ValueError(
                "local_max_rotation_degrees must be between 0 and 15"
            )
        if not 0 <= self.local_max_shear <= 0.20:
            raise ValueError("local_max_shear must be between 0 and 0.20")
        if not 50 <= self.consensus_percentile <= 95:
            raise ValueError("consensus_percentile must be between 50 and 95")
        if not 0 < self.print_vote_ratio <= 1:
            raise ValueError("print_vote_ratio must be between 0 and 1")
        if not 5 <= self.print_intensity_percentile <= 50:
            raise ValueError("print_intensity_percentile must be between 5 and 50")
        if not 0 <= self.print_mask_dilate <= 4:
            raise ValueError("print_mask_dilate must be between 0 and 4")
        if self.evidence_mode not in {"strict_white", "soft_difference"}:
            raise ValueError(
                "evidence_mode must be 'strict_white' or 'soft_difference'"
            )
        if not 0 <= self.print_guard_radius <= 8:
            raise ValueError("print_guard_radius must be between 0 and 8")
        for name in (
            "print_dark_threshold",
            "seed_dark_threshold",
            "seed_difference",
            "near_print_difference",
            "near_print_min_template",
            "blue_chroma_threshold",
            "blue_chroma_delta",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise ValueError(f"{name} must be an integer between 0 and 255")
        if self.component_min_area < 1:
            raise ValueError("component_min_area must be positive")
        if not 0 < self.component_max_area_ratio <= 0.25:
            raise ValueError("component_max_area_ratio must be between 0 and 0.25")
        if self.blank_pixel_limit < 0 or self.visible_pixel_limit <= self.blank_pixel_limit:
            raise ValueError("ink state pixel limits are invalid")
        return self


@dataclass(frozen=True)
class QuestionRegion:
    question_number: str
    question_type: str
    bbox: tuple[int, int, int, int]

    @classmethod
    def from_mapping(cls, raw: dict) -> "QuestionRegion":
        if not isinstance(raw, dict):
            raise ValueError("each question region must be an object")
        number_fields = [
            field
            for field in ("question_number", "question_no")
            if raw.get(field) is not None
        ]
        if len(number_fields) != 1:
            raise ValueError("question region must contain exactly one number field")
        raw_number = raw[number_fields[0]]
        if not isinstance(raw_number, str):
            raise ValueError("question number must be text")
        number = raw_number.strip()
        if (
            not number
            or number != raw_number
            or len(number) > 64
            or any(ord(char) < 32 for char in number)
        ):
            raise ValueError("question region is missing question_number")
        raw_question_type = raw.get("question_type")
        if not isinstance(raw_question_type, str) or not raw_question_type.strip():
            raise ValueError(f"question {number} is missing question_type")
        question_type = raw_question_type.strip().lower()
        if question_type not in {"choice", "fill", "solve"}:
            raise ValueError(f"question {number} has invalid question_type")
        bbox_fields = [
            field
            for field in ("ownership_bbox", "answer_bbox", "bbox")
            if raw.get(field) is not None
        ]
        if len(bbox_fields) != 1:
            raise ValueError(
                f"question {number} must contain exactly one bbox field"
            )
        bbox = raw[bbox_fields[0]]
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError(f"question {number} has invalid bbox")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in bbox):
            raise ValueError(f"question {number} bbox coordinates must be integers")
        values = tuple(bbox)
        left, top, right, bottom = values
        if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
            raise ValueError(f"question {number} bbox must use 0..1000 coordinates")
        return cls(number, question_type, values)


@dataclass(frozen=True)
class RegionSet(Sequence[QuestionRegion]):
    artifact_registration_sha256: str
    questions: tuple[QuestionRegion, ...]

    def __len__(self) -> int:
        return len(self.questions)

    def __getitem__(self, index):
        return self.questions[index]

    def require_artifact(self, registration_sha256: object) -> None:
        if (
            not isinstance(registration_sha256, str)
            or registration_sha256.casefold()
            != self.artifact_registration_sha256.casefold()
        ):
            raise ValueError("question regions do not belong to this template artifact")

    @property
    def contract_sha256(self) -> str:
        payload = {
            "artifact_registration_sha256": self.artifact_registration_sha256,
            "questions": [
                {
                    "question_number": region.question_number,
                    "question_type": region.question_type,
                    "ownership_bbox": list(region.bbox),
                }
                for region in self.questions
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_sample_id(value: object) -> str:
    allowed = set(string.ascii_letters + string.digits + "_-")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or any(char not in allowed for char in value)
    ):
        raise ValueError(
            "sample_id must be 1..64 ASCII letters, digits, '_' or '-'"
        )
    return value


@dataclass
class AlignmentResult:
    rgb: np.ndarray
    stats: dict


@dataclass
class EvidenceResult:
    aligned_rgb: np.ndarray
    cleaned_gray: np.ndarray
    ink_mask: np.ndarray
    ambiguous_mask: np.ndarray
    evidence_rgb: np.ndarray
    stats: dict


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resize_long_edge(image: np.ndarray, max_edge: int) -> np.ndarray:
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return np.ascontiguousarray(image)
    scale = max_edge / float(longest)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def load_rgb(
    path: str | Path,
    *,
    max_edge: int | None = None,
    rotate_clockwise_quadrants: int = 0,
) -> np.ndarray:
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    with Image.open(source_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        rgb = np.asarray(image, dtype=np.uint8)
    turns = int(rotate_clockwise_quadrants) % 4
    if turns:
        rgb = np.rot90(rgb, k=-turns)
    rgb = np.ascontiguousarray(rgb)
    return _resize_long_edge(rgb, max_edge) if max_edge else rgb


def _gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _feature_gray(rgb: np.ndarray, max_edge: int) -> tuple[np.ndarray, float, float]:
    gray = _gray(rgb)
    reduced = _resize_long_edge(gray, max_edge)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    reduced = clahe.apply(reduced)
    return reduced, rgb.shape[1] / reduced.shape[1], rgb.shape[0] / reduced.shape[0]


def _detector(config: LabConfig):
    if config.detector == "orb":
        return cv2.ORB_create(nfeatures=config.max_features), cv2.NORM_HAMMING
    if not hasattr(cv2, "SIFT_create"):
        raise RuntimeError("OpenCV SIFT support is required")
    return cv2.SIFT_create(nfeatures=config.max_features), cv2.NORM_L2


def _extract_features(rgb: np.ndarray, config: LabConfig):
    reduced, scale_x, scale_y = _feature_gray(rgb, config.feature_edge)
    detector, norm = _detector(config)
    keypoints, descriptors = detector.detectAndCompute(reduced, None)
    keypoints = tuple(keypoints or ())
    points = np.asarray(
        [(point.pt[0] * scale_x, point.pt[1] * scale_y) for point in keypoints],
        dtype=np.float32,
    )
    return points, descriptors, norm, len(keypoints)


def _red_mask(rgb: np.ndarray) -> np.ndarray:
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    return (
        (red >= 105)
        & (red - green >= 38)
        & (red - blue >= 28)
        & (red >= (green * 6) // 5)
    )


def _normalize_illumination(gray: np.ndarray) -> np.ndarray:
    longest = max(gray.shape[:2])
    sigma = max(12.0, longest / 90.0)
    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    background = np.maximum(background, 1)
    normalized = cv2.divide(gray, background, scale=242)
    return np.clip(normalized, 0, 255).astype(np.uint8)


def _coverage_for_homography(
    source_shape: tuple[int, int],
    target_shape: tuple[int, int],
    homography: np.ndarray,
) -> float:
    source_height, source_width = source_shape
    target_height, target_width = target_shape
    mask = np.ones((source_height, source_width), dtype=np.uint8)
    warped = cv2.warpPerspective(
        mask,
        homography,
        (target_width, target_height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return float(np.count_nonzero(warped)) / float(target_height * target_width)


def _estimate_homography(
    moving: np.ndarray,
    reference: np.ndarray,
    config: LabConfig,
    reference_features=None,
):
    moving_points, moving_descriptors, moving_norm, moving_count = _extract_features(
        moving, config
    )
    if reference_features is None:
        reference_features = _extract_features(reference, config)
    reference_points, reference_descriptors, reference_norm, reference_count = (
        reference_features
    )
    if moving_descriptors is None or reference_descriptors is None:
        raise AlignmentError("not enough visual features")
    if moving_norm != reference_norm:
        raise AlignmentError("feature descriptor types do not match")

    matcher = cv2.BFMatcher(moving_norm, crossCheck=False)
    pairs = matcher.knnMatch(moving_descriptors, reference_descriptors, k=2)
    good = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < config.ratio_test * pair[1].distance
    ]
    if len(good) < config.min_good_matches:
        raise AlignmentError(f"only {len(good)} reliable feature matches")

    source = np.float32([moving_points[match.queryIdx] for match in good])
    target = np.float32([reference_points[match.trainIdx] for match in good])
    homography, inlier_mask = cv2.findHomography(
        source,
        target,
        cv2.RANSAC,
        config.ransac_threshold,
        maxIters=3000,
        confidence=0.995,
    )
    if homography is None or inlier_mask is None or not np.isfinite(homography).all():
        raise AlignmentError("homography estimation failed")
    inliers = inlier_mask.ravel().astype(bool)
    inlier_count = int(np.count_nonzero(inliers))
    inlier_ratio = inlier_count / len(good)
    if inlier_count < config.min_inliers or inlier_ratio < config.min_inlier_ratio:
        raise AlignmentError(
            f"weak homography: {inlier_count} inliers, ratio {inlier_ratio:.3f}"
        )

    source_inliers = source[inliers]
    target_inliers = target[inliers]
    projected = cv2.perspectiveTransform(
        source_inliers.reshape(-1, 1, 2), homography
    ).reshape(-1, 2)
    rmse = float(np.sqrt(np.mean(np.sum((projected - target_inliers) ** 2, axis=1))))
    coverage = _coverage_for_homography(
        moving.shape[:2], reference.shape[:2], homography
    )
    if coverage < config.min_coverage:
        raise AlignmentError(f"warped page coverage is only {coverage:.3f}")

    return homography, source_inliers, target_inliers, {
        "moving_keypoints": moving_count,
        "reference_keypoints": reference_count,
        "good_matches": len(good),
        "inliers": inlier_count,
        "inlier_ratio": round(inlier_ratio, 4),
        "reprojection_rmse": round(rmse, 3),
        "coverage": round(coverage, 4),
    }


def _tile_refine(
    globally_aligned: np.ndarray,
    homography: np.ndarray,
    source_inliers: np.ndarray,
    target_inliers: np.ndarray,
    config: LabConfig,
) -> tuple[np.ndarray, list[dict]]:
    tiles = config.local_tiles_x
    if tiles <= 1:
        return globally_aligned, []
    height, width = globally_aligned.shape[:2]
    warped_source = cv2.perspectiveTransform(
        source_inliers.reshape(-1, 1, 2), homography
    ).reshape(-1, 2)
    overlap = max(4, int(round(width * config.local_overlap_ratio / tiles)))
    accumulator = np.zeros_like(globally_aligned, dtype=np.float32)
    weights = np.zeros((height, width), dtype=np.float32)
    diagnostics: list[dict] = []

    for tile_index in range(tiles):
        core_left = round(tile_index * width / tiles)
        core_right = round((tile_index + 1) * width / tiles)
        select = (
            (target_inliers[:, 0] >= max(0, core_left - overlap))
            & (target_inliers[:, 0] < min(width, core_right + overlap))
        )
        selected_count = int(np.count_nonzero(select))
        correction = np.eye(2, 3, dtype=np.float32)
        correction_inliers = 0
        correction_applied = False
        rejection_reason = "not_enough_tile_matches"
        if selected_count >= config.local_min_inliers:
            estimated, mask = cv2.estimateAffine2D(
                warped_source[select],
                target_inliers[select],
                method=cv2.RANSAC,
                ransacReprojThreshold=max(1.5, config.ransac_threshold * 0.65),
                maxIters=2000,
                confidence=0.99,
                refineIters=10,
            )
            if estimated is not None and np.isfinite(estimated).all():
                correction_inliers = int(np.count_nonzero(mask)) if mask is not None else 0
                inlier_ratio = correction_inliers / selected_count
                trusted, rejection_reason = _trusted_local_affine(
                    estimated,
                    correction_inliers=correction_inliers,
                    inlier_ratio=inlier_ratio,
                    image_width=width,
                    image_height=height,
                    config=config,
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
        left = max(0, core_left - overlap)
        right = min(width, core_right + overlap)
        weight = np.zeros(width, dtype=np.float32)
        weight[left:right] = 1.0
        if tile_index > 0 and core_left > left:
            weight[left:core_left] = np.linspace(0.0, 1.0, core_left - left, endpoint=False)
        if tile_index + 1 < tiles and right > core_right:
            weight[core_right:right] = np.linspace(
                1.0, 0.0, right - core_right, endpoint=False
            )
        tile_weight = np.broadcast_to(weight, (height, width))
        accumulator += locally_aligned.astype(np.float32) * tile_weight[:, :, None]
        weights += tile_weight
        diagnostics.append({
            "tile": tile_index,
            "matches": selected_count,
            "inliers": correction_inliers,
            "applied": correction_applied,
            "rejection_reason": None if correction_applied else rejection_reason,
        })

    safe_weights = np.maximum(weights, 1e-6)
    refined = accumulator / safe_weights[:, :, None]
    uncovered = weights <= 1e-6
    refined[uncovered] = globally_aligned[uncovered]
    return np.clip(refined, 0, 255).astype(np.uint8), diagnostics


def _trusted_local_affine(
    estimated: np.ndarray,
    *,
    correction_inliers: int,
    inlier_ratio: float,
    image_width: int,
    image_height: int,
    config: LabConfig,
) -> tuple[bool, str]:
    if correction_inliers < config.local_min_inliers:
        return False, "too_few_affine_inliers"
    if inlier_ratio < config.local_min_inlier_ratio:
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
    scale_floor = 1.0 - config.local_max_scale_change
    scale_ceiling = 1.0 + config.local_max_scale_change
    if np.any(singular_values < scale_floor) or np.any(
        singular_values > scale_ceiling
    ):
        return False, "affine_scale_out_of_bounds"

    angle = abs(math.degrees(math.atan2(rotation[1, 0], rotation[0, 0])))
    if angle > config.local_max_rotation_degrees:
        return False, "affine_rotation_out_of_bounds"

    first_column = linear[:, 0]
    second_column = linear[:, 1]
    column_denominator = float(
        np.linalg.norm(first_column) * np.linalg.norm(second_column)
    )
    shear = (
        abs(float(np.dot(first_column, second_column))) / column_denominator
        if column_denominator > 0
        else math.inf
    )
    if shear > config.local_max_shear:
        return False, "affine_shear_out_of_bounds"

    if (
        abs(float(translation[0]))
        > image_width * config.local_max_translation_ratio
        or abs(float(translation[1]))
        > image_height * config.local_max_translation_ratio
    ):
        return False, "affine_translation_out_of_bounds"
    return True, ""


def align_rgb(
    moving: np.ndarray,
    reference: np.ndarray,
    config: LabConfig,
    *,
    try_rotations: Sequence[int] = (0,),
    reference_features=None,
) -> AlignmentResult:
    config.validate()
    failures: list[str] = []
    candidates: list[tuple[float, AlignmentResult]] = []
    for rotation in try_rotations:
        rotated = np.rot90(moving, k=-(int(rotation) % 4))
        rotated = np.ascontiguousarray(rotated)
        try:
            homography, source, target, stats = _estimate_homography(
                rotated,
                reference,
                config,
                reference_features=reference_features,
            )
            height, width = reference.shape[:2]
            globally_aligned = cv2.warpPerspective(
                rotated,
                homography,
                (width, height),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(255, 255, 255),
            )
            refined, local_stats = _tile_refine(
                globally_aligned, homography, source, target, config
            )
            stats["rotation_clockwise_quadrants"] = int(rotation) % 4
            stats["local_tiles"] = local_stats
            score = stats["inliers"] - stats["reprojection_rmse"] * 2.0
            candidates.append((score, AlignmentResult(refined, stats)))
        except (AlignmentError, cv2.error) as exc:
            failures.append(f"rotation {rotation}: {exc}")
    if not candidates:
        raise AlignmentError("; ".join(failures) or "no alignment candidate")
    return max(candidates, key=lambda item: item[0])[1]


def _orb_pair_score(left: np.ndarray, right: np.ndarray) -> int:
    detector = cv2.ORB_create(nfeatures=1600)
    left_gray = _feature_gray(left, 760)[0]
    right_gray = _feature_gray(right, 760)[0]
    left_points, left_desc = detector.detectAndCompute(left_gray, None)
    right_points, right_desc = detector.detectAndCompute(right_gray, None)
    if left_desc is None or right_desc is None:
        return 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    pairs = matcher.knnMatch(left_desc, right_desc, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance]
    if len(good) < 12:
        return 0
    source = np.float32([left_points[item.queryIdx].pt for item in good])
    target = np.float32([right_points[item.trainIdx].pt for item in good])
    _homography, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
    return int(np.count_nonzero(mask)) if mask is not None else 0


def _select_reference(images: Sequence[np.ndarray]) -> tuple[int, list[int]]:
    if len(images) == 1:
        return 0, [0]
    scores = [0 for _ in images]
    for left_index in range(len(images)):
        for right_index in range(left_index + 1, len(images)):
            score = _orb_pair_score(images[left_index], images[right_index])
            scores[left_index] += score
            scores[right_index] += score
    return int(np.argmax(scores)), scores


def _clean_gray_for_consensus(rgb: np.ndarray) -> np.ndarray:
    normalized = _normalize_illumination(_gray(rgb))
    normalized[_red_mask(rgb)] = 255
    return normalized


def _consensus_from_aligned(
    aligned_images: Sequence[np.ndarray],
    config: LabConfig,
) -> tuple[np.ndarray, np.ndarray, dict]:
    if len(aligned_images) < 2:
        raise ValueError("at least two aligned images are required")
    stack = np.stack(
        [_clean_gray_for_consensus(image) for image in aligned_images],
        axis=0,
    )
    dark_votes = np.count_nonzero(stack <= config.print_dark_threshold, axis=0)
    vote_limit = max(2, int(math.ceil(len(stack) * config.print_vote_ratio)))
    print_mask = (dark_votes >= vote_limit).astype(np.uint8) * 255
    print_mask = cv2.morphologyEx(
        print_mask,
        cv2.MORPH_CLOSE,
        np.ones((2, 2), dtype=np.uint8),
    )
    if config.print_mask_dilate:
        size = config.print_mask_dilate * 2 + 1
        print_mask = cv2.dilate(
            print_mask,
            np.ones((size, size), dtype=np.uint8),
        )

    intensity_position = int(round(
        (config.print_intensity_percentile / 100.0) * (len(stack) - 1)
    ))
    dark_consensus = np.partition(stack, intensity_position, axis=0)[
        intensity_position
    ]
    consensus = np.full(stack.shape[1:], 255, dtype=np.uint8)
    consensus[print_mask > 0] = dark_consensus[print_mask > 0]
    return consensus, print_mask, {
        "aligned_count": len(aligned_images),
        "print_vote_limit": vote_limit,
        "print_intensity_position": intensity_position,
        "print_pixel_ratio": round(float(np.count_nonzero(print_mask)) / print_mask.size, 5),
    }


def _template_edge_recall(template_gray: np.ndarray, aligned_rgb: np.ndarray) -> float:
    template_edges = cv2.Canny(template_gray, 70, 160)
    student_gray = _clean_gray_for_consensus(aligned_rgb)
    student_edges = cv2.Canny(student_gray, 70, 160)
    student_edges = cv2.dilate(student_edges, np.ones((5, 5), dtype=np.uint8))
    denominator = int(np.count_nonzero(template_edges))
    if denominator == 0:
        return 0.0
    overlap = np.count_nonzero((template_edges > 0) & (student_edges > 0))
    return float(overlap) / denominator


def _write_artifact_images(
    destination: Path,
    template_gray: np.ndarray,
    print_mask: np.ndarray,
    registration_rgb: np.ndarray,
) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    template_path = destination / "template.png"
    print_mask_path = destination / "print_mask.png"
    registration_path = destination / "registration.png"
    Image.fromarray(template_gray, mode="L").save(template_path, format="PNG")
    Image.fromarray(print_mask, mode="L").save(print_mask_path, format="PNG")
    Image.fromarray(registration_rgb, mode="RGB").save(
        registration_path,
        format="PNG",
    )
    return {
        "template_image": template_path.name,
        "template_sha256": _sha256(template_path),
        "print_mask": print_mask_path.name,
        "print_mask_sha256": _sha256(print_mask_path),
        "registration_image": registration_path.name,
        "registration_sha256": _sha256(registration_path),
    }


def _write_manifest(destination: Path, manifest: dict) -> dict:
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def compile_consensus_template(
    image_paths: Iterable[str | Path],
    output_dir: str | Path,
    *,
    config: LabConfig | None = None,
    rotate_clockwise_quadrants: int = 0,
) -> dict:
    config = (config or LabConfig()).validate()
    paths = sorted((Path(path) for path in image_paths), key=lambda value: value.name)
    if len(paths) < 3:
        raise ValueError("consensus compilation requires at least three images")
    if len(paths) > MAX_CONSENSUS_SOURCES:
        raise ValueError(
            "consensus compilation accepts at most "
            f"{MAX_CONSENSUS_SOURCES} research images"
        )
    images = [
        load_rgb(
            path,
            max_edge=config.max_edge,
            rotate_clockwise_quadrants=rotate_clockwise_quadrants,
        )
        for path in paths
    ]
    reference_index, medoid_scores = _select_reference(images)
    reference = images[reference_index]
    reference_features = _extract_features(reference, config)

    first_pass_count = 0
    first_stats: list[dict] = []
    coarse_config = replace(config, local_tiles_x=1)
    for sample_index, image in enumerate(images, start=1):
        if sample_index - 1 == reference_index:
            stats = {
                "sample_id": f"S{sample_index:02d}",
                "pass": True,
                "reference": True,
                "inliers": None,
            }
        else:
            try:
                result = align_rgb(
                    image,
                    reference,
                    coarse_config,
                    reference_features=reference_features,
                )
                stats = {"sample_id": f"S{sample_index:02d}", "pass": True, **result.stats}
            except AlignmentError as exc:
                stats = {"sample_id": f"S{sample_index:02d}", "pass": False, "error": str(exc)}
                first_stats.append(stats)
                continue
        first_pass_count += 1
        first_stats.append(stats)
    if first_pass_count < max(3, math.ceil(len(images) * 0.7)):
        raise AlignmentError("too few papers aligned during the first pass")

    # The consensus image intentionally removes most dark content.  That makes
    # it a good subtraction template but a weak feature-matching reference.
    # Re-run against the photographic medoid with local tile correction, then
    # build the final consensus from those refined alignments.
    second_pass: list[np.ndarray] = []
    second_stats: list[dict] = []
    for sample_index, image in enumerate(images, start=1):
        if sample_index - 1 == reference_index:
            aligned = cv2.resize(
                image,
                (reference.shape[1], reference.shape[0]),
                interpolation=cv2.INTER_AREA,
            )
            second_pass.append(aligned)
            second_stats.append({
                "sample_id": f"S{sample_index:02d}",
                "pass": True,
                "reference": True,
                "inliers": None,
            })
            continue
        try:
            result = align_rgb(
                image,
                reference,
                config,
                reference_features=reference_features,
            )
            second_pass.append(result.rgb)
            second_stats.append({"sample_id": f"S{sample_index:02d}", "pass": True, **result.stats})
        except AlignmentError as exc:
            second_stats.append({"sample_id": f"S{sample_index:02d}", "pass": False, "error": str(exc)})
    if len(second_pass) < max(3, math.ceil(len(images) * 0.8)):
        raise AlignmentError("too few papers aligned to the provisional template")

    template_gray, print_mask, consensus_stats = _consensus_from_aligned(
        second_pass, config
    )
    for stats, aligned in zip((item for item in second_stats if item["pass"]), second_pass):
        stats["template_edge_recall"] = round(
            _template_edge_recall(template_gray, aligned), 4
        )

    destination = Path(output_dir)
    artifact_files = _write_artifact_images(
        destination,
        template_gray,
        print_mask,
        reference,
    )

    pass_count = sum(bool(item.get("pass")) for item in second_stats)
    edge_recalls = [
        float(item["template_edge_recall"])
        for item in second_stats
        if item.get("pass") and item.get("template_edge_recall") is not None
    ]
    manifest = {
        "artifact_version": 2,
        "experimental": True,
        "source_mode": "student_consensus",
        "answer_preservation_safe": False,
        "source_count": len(paths),
        "input_rotation_clockwise_quadrants": int(rotate_clockwise_quadrants) % 4,
        "reference_sample_id": f"S{reference_index + 1:02d}",
        "reference_medoid_scores": medoid_scores,
        "canonical_size": [int(template_gray.shape[1]), int(template_gray.shape[0])],
        **artifact_files,
        "config": asdict(config),
        "first_pass": first_stats,
        "second_pass": second_stats,
        "quality": {
            "alignment_pass_count": pass_count,
            "alignment_pass_rate": round(pass_count / len(paths), 4),
            "mean_template_edge_recall": round(float(np.mean(edge_recalls)), 4)
            if edge_recalls
            else 0.0,
            **consensus_stats,
        },
    }
    return _write_manifest(destination, manifest)


def compile_blank_template(
    image_path: str | Path,
    output_dir: str | Path,
    *,
    config: LabConfig | None = None,
    rotate_clockwise_quadrants: int = 0,
) -> dict:
    """Compile an answer-preserving artifact from a genuinely blank paper.

    The input may be an electronic blank page or an unfilled photograph.  It
    must not contain any student's answers.  Unlike cross-student consensus,
    this source cannot absorb a commonly written answer into the print mask.
    """
    config = (config or LabConfig()).validate()
    source = load_rgb(
        image_path,
        max_edge=config.max_edge,
        rotate_clockwise_quadrants=rotate_clockwise_quadrants,
    )
    template_gray = _clean_gray_for_consensus(source)
    print_mask = (template_gray <= config.print_dark_threshold).astype(np.uint8) * 255
    print_mask = cv2.morphologyEx(
        print_mask,
        cv2.MORPH_CLOSE,
        np.ones((2, 2), dtype=np.uint8),
    )
    if config.print_mask_dilate:
        size = config.print_mask_dilate * 2 + 1
        print_mask = cv2.dilate(
            print_mask,
            np.ones((size, size), dtype=np.uint8),
        )
    subtraction = np.full(template_gray.shape, 255, dtype=np.uint8)
    subtraction[print_mask > 0] = template_gray[print_mask > 0]

    destination = Path(output_dir)
    artifact_files = _write_artifact_images(
        destination,
        subtraction,
        print_mask,
        source,
    )
    manifest = {
        "artifact_version": 2,
        "experimental": True,
        "source_mode": "verified_blank",
        "answer_preservation_safe": True,
        "source_count": 1,
        "input_rotation_clockwise_quadrants": int(rotate_clockwise_quadrants) % 4,
        "canonical_size": [int(template_gray.shape[1]), int(template_gray.shape[0])],
        **artifact_files,
        "config": asdict(config),
        "quality": {
            "print_pixel_ratio": round(
                float(np.count_nonzero(print_mask)) / print_mask.size,
                5,
            ),
        },
    }
    return _write_manifest(destination, manifest)


def _artifact_member(base: Path, raw_name: object, field: str) -> Path:
    if not isinstance(raw_name, str) or not raw_name:
        raise ValueError(f"{field} must be a relative PNG filename")
    windows = PureWindowsPath(raw_name)
    posix = PurePosixPath(raw_name.replace("\\", "/"))
    if (
        windows.is_absolute()
        or bool(windows.drive)
        or posix.is_absolute()
        or len(posix.parts) != 1
        or posix.name in {"", ".", ".."}
        or posix.suffix.lower() != ".png"
    ):
        raise ValueError(f"{field} must be a relative PNG filename")
    resolved = (base / posix.name).resolve()
    if resolved.parent != base.resolve():
        raise ValueError(f"{field} must stay inside the artifact directory")
    return resolved


def _read_artifact_png(path: Path, mode: str) -> np.ndarray:
    try:
        with Image.open(path) as source:
            if source.format != "PNG":
                raise ValueError("artifact image content must be PNG")
            source.load()
            return np.asarray(source.convert(mode), dtype=np.uint8)
    except OSError as exc:
        raise ValueError("artifact image is invalid") from exc


def _verified_artifact(manifest_path: str | Path):
    path = Path(manifest_path)
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("template manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("template manifest must be an object")
    if manifest.get("artifact_version") != 2 or not manifest.get("experimental"):
        raise ValueError("unsupported template artifact")
    source_mode = manifest.get("source_mode")
    preservation_safe = manifest.get("answer_preservation_safe")
    if source_mode not in {"verified_blank", "student_consensus"}:
        raise ValueError("template artifact has invalid source_mode")
    if preservation_safe is not (source_mode == "verified_blank"):
        raise ValueError("template artifact has invalid preservation safety flag")
    base = path.resolve().parent
    template_path = _artifact_member(base, manifest.get("template_image"), "template_image")
    mask_path = _artifact_member(base, manifest.get("print_mask"), "print_mask")
    registration_path = _artifact_member(
        base,
        manifest.get("registration_image"),
        "registration_image",
    )
    if (
        not template_path.is_file()
        or not mask_path.is_file()
        or not registration_path.is_file()
    ):
        raise ValueError("template artifact files are missing")
    if _sha256(template_path) != manifest.get("template_sha256"):
        raise ValueError("template image hash mismatch")
    if _sha256(mask_path) != manifest.get("print_mask_sha256"):
        raise ValueError("print mask hash mismatch")
    if _sha256(registration_path) != manifest.get("registration_sha256"):
        raise ValueError("registration image hash mismatch")
    template_gray = _read_artifact_png(template_path, "L")
    print_mask = _read_artifact_png(mask_path, "L")
    registration_rgb = _read_artifact_png(registration_path, "RGB")
    raw_size = manifest.get("canonical_size")
    if not isinstance(raw_size, list) or len(raw_size) != 2:
        raise ValueError("template artifact has invalid canonical_size")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_size):
        raise ValueError("template artifact has invalid canonical_size")
    expected_size = tuple(raw_size)
    if any(value <= 0 or value > 6000 for value in expected_size):
        raise ValueError("template artifact has invalid canonical_size")
    if (
        template_gray.shape[::-1] != expected_size
        or print_mask.shape != template_gray.shape
        or registration_rgb.shape[:2] != template_gray.shape
    ):
        raise ValueError("template artifact dimensions do not match")
    raw_config = manifest.get("config")
    if not isinstance(raw_config, dict):
        raise ValueError("template artifact has invalid config")
    expected_config_fields = {field.name for field in fields(LabConfig)}
    if set(raw_config) != expected_config_fields:
        raise ValueError("template artifact config fields do not match")
    try:
        config = LabConfig(**raw_config).validate()
    except (TypeError, ValueError) as exc:
        raise ValueError("template artifact has invalid config") from exc
    return manifest, template_gray, print_mask, registration_rgb, config


def load_verified_manifest(manifest_path: str | Path) -> dict:
    return _verified_artifact(manifest_path)[0]


def _filter_components(mask: np.ndarray, config: LabConfig) -> tuple[np.ndarray, list[dict]]:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    result = np.zeros_like(mask)
    components: list[dict] = []
    max_area = max(64, int(mask.size * config.component_max_area_ratio))
    area_scale = (max(mask.shape[:2]) / REFERENCE_MAX_EDGE) ** 2
    min_area = max(1, int(round(config.component_min_area * area_scale)))
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        width = int(stats[index, cv2.CC_STAT_WIDTH])
        height = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area < min_area or area > max_area:
            continue
        if width <= 1 and height <= 1:
            continue
        result[labels == index] = 255
        components.append({
            "component_id": f"E{len(components) + 1:04d}",
            "bbox": [
                int(stats[index, cv2.CC_STAT_LEFT]),
                int(stats[index, cv2.CC_STAT_TOP]),
                width,
                height,
            ],
            "centroid": [round(float(value), 2) for value in centroids[index]],
            "area": area,
        })
    return result, components


def _ink_evidence(
    aligned_rgb: np.ndarray,
    template_gray: np.ndarray,
    print_mask: np.ndarray,
    registration_rgb: np.ndarray,
    config: LabConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict], np.ndarray, dict]:
    if registration_rgb.shape != aligned_rgb.shape:
        raise ValueError("registration and aligned images must have equal dimensions")
    student_gray = _normalize_illumination(_gray(aligned_rgb))
    red = _red_mask(aligned_rgb)
    student_gray = student_gray.copy()
    student_gray[red] = 255

    background = (print_mask == 0) & (template_gray >= 225) & ~red
    if int(np.count_nonzero(background)) >= 100:
        brightness_shift = float(
            np.median(template_gray[background]) - np.median(student_gray[background])
        )
    else:
        brightness_shift = 0.0
    brightness_shift = max(-30.0, min(30.0, brightness_shift))
    adjusted = np.clip(
        student_gray.astype(np.float32) + brightness_shift,
        0,
        255,
    ).astype(np.uint8)
    adjusted[red] = 255

    difference = template_gray.astype(np.int16) - adjusted.astype(np.int16)
    dark_addition = (
        (adjusted <= config.seed_dark_threshold)
        & (difference >= config.seed_difference)
        & ~red
    )
    if config.evidence_mode == "strict_white":
        candidate = dark_addition & (print_mask == 0) & (template_gray >= 205)
    else:
        radius = config.print_guard_radius
        if radius:
            kernel_size = radius * 2 + 1
            near_print = cv2.dilate(
                print_mask,
                np.ones((kernel_size, kernel_size), dtype=np.uint8),
            ) > 0
        else:
            near_print = print_mask > 0
        confident_near_print = (
            dark_addition
            & near_print
            & (template_gray >= config.near_print_min_template)
            & (difference >= config.near_print_difference)
        )
        far_from_print = dark_addition & ~near_print

        red_channel = aligned_rgb[:, :, 0].astype(np.int16)
        green_channel = aligned_rgb[:, :, 1].astype(np.int16)
        blue_channel = aligned_rgb[:, :, 2].astype(np.int16)
        reference_red = registration_rgb[:, :, 0].astype(np.int16)
        reference_green = registration_rgb[:, :, 1].astype(np.int16)
        reference_blue = registration_rgb[:, :, 2].astype(np.int16)
        student_blue_excess = blue_channel - np.maximum(red_channel, green_channel)
        reference_blue_excess = reference_blue - np.maximum(
            reference_red,
            reference_green,
        )
        reference_blue_guard = np.clip(
            reference_blue_excess,
            0,
            255,
        ).astype(np.uint8)
        if config.print_guard_radius:
            guard_size = config.print_guard_radius * 2 + 1
            reference_blue_guard = cv2.dilate(
                reference_blue_guard,
                np.ones((guard_size, guard_size), dtype=np.uint8),
            )
        confident_blue_pen = (
            (student_blue_excess >= config.blue_chroma_threshold)
            & (
                student_blue_excess
                - reference_blue_guard.astype(np.int16)
                >= config.blue_chroma_delta
            )
            & (adjusted <= 210)
            & ~red
        )
        candidate = far_from_print | confident_near_print | confident_blue_pen

    seed = candidate.astype(np.uint8) * 255
    ambiguous_seed = np.zeros_like(seed)
    if config.evidence_mode == "soft_difference":
        candidate = far_from_print | confident_blue_pen
        ambiguous_candidate = confident_near_print & ~candidate
        seed = candidate.astype(np.uint8) * 255
        ambiguous_seed = ambiguous_candidate.astype(np.uint8) * 255
    filtered, components = _filter_components(seed, config)
    ambiguous_filtered, ambiguous_components = _filter_components(
        ambiguous_seed,
        config,
    )

    evidence = np.full((*student_gray.shape, 3), 255, dtype=np.uint8)
    ink_values = np.minimum(adjusted, 80)
    evidence[filtered > 0] = np.repeat(
        ink_values[:, :, None],
        3,
        axis=2,
    )[filtered > 0]
    metadata = {
        "evidence_mode": config.evidence_mode,
        "brightness_shift": round(brightness_shift, 2),
        "candidate_pixels": int(np.count_nonzero(seed)),
        "filtered_pixels": int(np.count_nonzero(filtered)),
        "ambiguous_candidate_pixels": int(np.count_nonzero(ambiguous_seed)),
        "ambiguous_pixels": int(np.count_nonzero(ambiguous_filtered)),
        "ambiguous_component_count": len(ambiguous_components),
        "ambiguous_components": ambiguous_components,
        "red_pixels_removed": int(np.count_nonzero(red)),
    }
    return (
        adjusted,
        filtered,
        ambiguous_filtered,
        components,
        evidence,
        metadata,
    )


class TemplateMatcher:
    """Verified batch matcher with one cached registration feature set."""

    def __init__(
        self,
        manifest_path: str | Path,
        *,
        allow_unsafe_consensus: bool = False,
    ):
        (
            self.manifest,
            self.template_gray,
            self.print_mask,
            self.registration_rgb,
            self.config,
        ) = _verified_artifact(manifest_path)
        if (
            self.manifest["answer_preservation_safe"] is not True
            and allow_unsafe_consensus is not True
        ):
            raise ValueError(
                "student_consensus artifacts require explicit research opt-in"
            )
        self.reference_features = _extract_features(
            self.registration_rgb,
            self.config,
        )

    def extract_evidence(
        self,
        image_path: str | Path,
        *,
        output_dir: str | Path | None = None,
        sample_id: str = "S01",
    ) -> EvidenceResult:
        safe_id = validate_sample_id(sample_id)
        moving = load_rgb(
            image_path,
            max_edge=self.config.max_edge,
            rotate_clockwise_quadrants=self.manifest[
                "input_rotation_clockwise_quadrants"
            ],
        )
        alignment = align_rgb(
            moving,
            self.registration_rgb,
            self.config,
            reference_features=self.reference_features,
        )
        edge_recall = _template_edge_recall(self.template_gray, alignment.rgb)
        if edge_recall < self.config.min_template_edge_recall:
            raise AlignmentError(
                "aligned template edge recall is only "
                f"{edge_recall:.4f}; minimum is "
                f"{self.config.min_template_edge_recall:.4f}"
            )
        (
            student_gray,
            ink_mask,
            ambiguous_mask,
            components,
            evidence,
            evidence_stats,
        ) = _ink_evidence(
            alignment.rgb,
            self.template_gray,
            self.print_mask,
            self.registration_rgb,
            self.config,
        )
        stats = {
            **alignment.stats,
            "source_mode": self.manifest["source_mode"],
            "answer_preservation_safe": self.manifest["answer_preservation_safe"],
            "artifact_registration_sha256": self.manifest["registration_sha256"],
            "sample_id": safe_id,
            "template_edge_recall": round(edge_recall, 4),
            "ink_pixels": int(np.count_nonzero(ink_mask)),
            "ink_pixel_ratio": round(
                float(np.count_nonzero(ink_mask)) / ink_mask.size,
                6,
            ),
            "component_count": len(components),
            "components": components,
            "config": asdict(self.config),
            **evidence_stats,
        }
        if output_dir is not None:
            destination = Path(output_dir)
            destination.mkdir(parents=True, exist_ok=True)
            Image.fromarray(alignment.rgb, mode="RGB").save(
                destination / f"{safe_id}-aligned.png"
            )
            Image.fromarray(ink_mask, mode="L").save(
                destination / f"{safe_id}-ink.png"
            )
            Image.fromarray(ambiguous_mask, mode="L").save(
                destination / f"{safe_id}-ambiguous.png"
            )
            Image.fromarray(evidence, mode="RGB").save(
                destination / f"{safe_id}-evidence.png"
            )
            (destination / f"{safe_id}-diagnostics.json").write_text(
                json.dumps(stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return EvidenceResult(
            alignment.rgb,
            student_gray,
            ink_mask,
            ambiguous_mask,
            evidence,
            stats,
        )


def extract_evidence(
    image_path: str | Path,
    manifest_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    sample_id: str = "S01",
    allow_unsafe_consensus: bool = False,
) -> EvidenceResult:
    return TemplateMatcher(
        manifest_path,
        allow_unsafe_consensus=allow_unsafe_consensus,
    ).extract_evidence(
        image_path,
        output_dir=output_dir,
        sample_id=sample_id,
    )


def _validated_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.casefold()


def load_regions(path: str | Path) -> RegionSet:
    data = json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if not isinstance(data, dict):
        raise ValueError("region file must be a JSON object")
    artifact_hash = _validated_sha256(
        data.get("artifact_registration_sha256"),
        "artifact_registration_sha256",
    )
    raw_regions = data.get("questions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("region file must contain a non-empty questions list")
    questions = _validated_regions(
        [QuestionRegion.from_mapping(raw) for raw in raw_regions],
    )
    return RegionSet(artifact_hash, tuple(questions))


def _validated_regions(
    regions: Sequence[QuestionRegion],
) -> list[QuestionRegion]:
    if not regions:
        raise ValueError("at least one question region is required")
    validated: list[QuestionRegion] = []
    for region in regions:
        if not isinstance(region, QuestionRegion):
            raise ValueError("question regions must be QuestionRegion values")
        validated.append(QuestionRegion.from_mapping({
            "question_number": region.question_number,
            "question_type": region.question_type,
            "ownership_bbox": list(region.bbox),
        }))

    regions = validated
    numbers = [region.question_number for region in regions]
    if len(set(numbers)) != len(numbers):
        raise ValueError("question regions contain duplicate numbers")
    for index, left_region in enumerate(regions):
        left_x1, left_y1, left_x2, left_y2 = left_region.bbox
        for right_region in regions[index + 1 :]:
            right_x1, right_y1, right_x2, right_y2 = right_region.bbox
            overlap_width = min(left_x2, right_x2) - max(left_x1, right_x1)
            overlap_height = min(left_y2, right_y2) - max(left_y1, right_y1)
            if overlap_width > 0 and overlap_height > 0:
                raise ValueError(
                    "question ownership regions overlap: "
                    f"{left_region.question_number} and {right_region.question_number}"
                )
    return regions


def _scaled_boundary(value: int, extent: int) -> int:
    return min(extent, max(0, (value * extent + 500) // 1000))


def _pixel_bbox(region: QuestionRegion, width: int, height: int, pad_ratio: float):
    left, top, right, bottom = region.bbox
    x1 = _scaled_boundary(left, width)
    y1 = _scaled_boundary(top, height)
    x2 = _scaled_boundary(right, width)
    y2 = _scaled_boundary(bottom, height)
    if x1 >= x2 or y1 >= y2:
        raise ValueError(
            f"question {region.question_number} bbox is empty at image resolution"
        )
    pad_x = int(round((x2 - x1) * pad_ratio))
    pad_y = int(round((y2 - y1) * pad_ratio))
    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(width, x2 + pad_x),
        min(height, y2 + pad_y),
    )


def _scaled_ink_limits(
    config: LabConfig,
    image_shape: tuple[int, int],
) -> tuple[int, int]:
    area_scale = (max(image_shape) / REFERENCE_MAX_EDGE) ** 2
    blank_limit = max(0, int(round(config.blank_pixel_limit * area_scale)))
    visible_limit = max(
        blank_limit + 1,
        int(round(config.visible_pixel_limit * area_scale)),
    )
    return blank_limit, visible_limit


def _region_state(
    pixel_count: int,
    config: LabConfig,
    image_shape: tuple[int, int],
) -> str:
    blank_limit, visible_limit = _scaled_ink_limits(config, image_shape)
    if pixel_count <= blank_limit:
        return "blank"
    if pixel_count < visible_limit:
        return "uncertain"
    return "visible"


def _evidence_config(evidence: EvidenceResult) -> LabConfig:
    raw_config = evidence.stats.get("config")
    if not isinstance(raw_config, dict):
        raise ValueError("evidence is missing its preprocessing config")
    expected_config_fields = {field.name for field in fields(LabConfig)}
    if set(raw_config) != expected_config_fields:
        raise ValueError("evidence preprocessing config fields do not match")
    try:
        return LabConfig(**raw_config).validate()
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence preprocessing config is invalid") from exc


def summarize_regions(
    evidence: EvidenceResult,
    regions: RegionSet,
    *,
    pad_ratio: float = 0.0,
) -> list[dict]:
    """Return local evidence measurements without asking an OCR model.

    ``edge_ink_pixels`` is deliberately only a diagnostic: an answer touching
    a region boundary may be written large rather than be wrong.  The caller
    can use it for a targeted second look instead of moving every answer to
    manual review.
    """
    if not 0.0 <= pad_ratio <= 0.5:
        raise ValueError("pad_ratio must be between 0 and 0.5")
    if pad_ratio != 0:
        raise ValueError("region summaries cannot expand ownership bounds")
    if not isinstance(regions, RegionSet):
        raise ValueError("region summaries require an artifact-bound RegionSet")
    regions.require_artifact(evidence.stats.get("artifact_registration_sha256"))
    regions = _validated_regions(regions)
    config = _evidence_config(evidence)
    height, width = evidence.ink_mask.shape
    image_shape = evidence.ink_mask.shape
    blank_limit, visible_limit = _scaled_ink_limits(config, image_shape)
    if evidence.ambiguous_mask.shape != evidence.ink_mask.shape:
        raise ValueError("evidence masks must have equal dimensions")
    summaries: list[dict] = []
    for region in regions:
        x1, y1, x2, y2 = _pixel_bbox(region, width, height, pad_ratio)
        crop = evidence.ink_mask[y1:y2, x1:x2]
        ambiguous_crop = evidence.ambiguous_mask[y1:y2, x1:x2]
        ink_pixels = int(np.count_nonzero(crop))
        ambiguous_pixels = int(np.count_nonzero(ambiguous_crop))
        edge_width = min(2, max(1, crop.shape[0] // 2), max(1, crop.shape[1] // 2))
        edge = np.zeros_like(crop, dtype=bool)
        edge[:edge_width, :] = True
        edge[-edge_width:, :] = True
        edge[:, :edge_width] = True
        edge[:, -edge_width:] = True
        edge_ink_pixels = int(np.count_nonzero((crop > 0) & edge))
        state = _region_state(ink_pixels, config, image_shape)
        if (
            state == "blank"
            and _region_state(ambiguous_pixels, config, image_shape) != "blank"
        ):
            state = "uncertain"
        summaries.append({
            "question_number": region.question_number,
            "question_type": region.question_type,
            "bbox_pixels": [x1, y1, x2, y2],
            "ink_pixels": ink_pixels,
            "ambiguous_pixels": ambiguous_pixels,
            "state": state,
            "blank_pixel_limit": blank_limit,
            "visible_pixel_limit": visible_limit,
            "edge_ink_pixels": edge_ink_pixels,
            "edge_ink_ratio": round(edge_ink_pixels / max(1, ink_pixels), 4),
        })
    return summaries


def _font(size: int):
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_region_sheets(
    evidence: EvidenceResult,
    regions: RegionSet,
    output_dir: str | Path,
    *,
    group_size: int = 10,
    pad_ratio: float = 0.0,
) -> list[Path]:
    if group_size < 1 or group_size > 20:
        raise ValueError("group_size must be between 1 and 20")
    if not 0.0 <= pad_ratio <= 0.5:
        raise ValueError("pad_ratio must be between 0 and 0.5")
    if not isinstance(regions, RegionSet):
        raise ValueError("region sheets require an artifact-bound RegionSet")
    regions.require_artifact(evidence.stats.get("artifact_registration_sha256"))
    regions = _validated_regions(regions)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    height, width = evidence.ink_mask.shape
    output_paths: list[Path] = []
    label_font = _font(28)

    for group_index in range(0, len(regions), group_size):
        group = regions[group_index : group_index + group_size]
        cell_width = 1200
        cell_height = 240
        label_width = 155
        canvas = Image.new("RGB", (cell_width, cell_height * len(group)), "white")
        draw = ImageDraw.Draw(canvas)
        for row, region in enumerate(group):
            own_x1, own_y1, own_x2, own_y2 = _pixel_bbox(
                region,
                width,
                height,
                0.0,
            )
            x1, y1, x2, y2 = _pixel_bbox(
                region,
                width,
                height,
                pad_ratio,
            )
            crop_mask = np.zeros((y2 - y1, x2 - x1), dtype=np.uint8)
            crop_mask[
                own_y1 - y1 : own_y2 - y1,
                own_x1 - x1 : own_x2 - x1,
            ] = evidence.ink_mask[own_y1:own_y2, own_x1:own_x2]
            crop_gray = evidence.cleaned_gray[y1:y2, x1:x2]

            row_top = row * cell_height
            draw.rectangle(
                (0, row_top, cell_width - 1, row_top + cell_height - 1),
                outline=(195, 205, 215),
                width=2,
            )
            draw.line(
                (label_width, row_top, label_width, row_top + cell_height),
                fill=(205, 215, 225),
                width=2,
            )
            draw.text((16, row_top + 20), f"Q{region.question_number}", fill=(20, 45, 70), font=label_font)

            masked = np.full((*crop_gray.shape, 3), 255, dtype=np.uint8)
            masked[crop_mask > 0] = (12, 28, 44)
            crop_image = Image.fromarray(masked, mode="RGB")
            available = (cell_width - label_width - 30, cell_height - 28)
            scale = min(available[0] / crop_image.width, available[1] / crop_image.height)
            target = (
                max(1, round(crop_image.width * scale)),
                max(1, round(crop_image.height * scale)),
            )
            crop_image = crop_image.resize(target, Image.Resampling.LANCZOS)
            paste_x = label_width + 15
            paste_y = row_top + (cell_height - crop_image.height) // 2
            canvas.paste(crop_image, (paste_x, paste_y))

        path = destination / f"questions-{group_index + 1:02d}-{group_index + len(group):02d}.png"
        canvas.save(path, format="PNG")
        output_paths.append(path)
    return output_paths


def benchmark_artifact(
    image_paths: Iterable[str | Path],
    manifest_path: str | Path,
) -> dict:
    matcher = TemplateMatcher(
        manifest_path,
        allow_unsafe_consensus=True,
    )
    manifest = matcher.manifest
    paths = sorted((Path(path) for path in image_paths), key=lambda value: value.name)
    samples: list[dict] = []
    for index, path in enumerate(paths, start=1):
        started = cv2.getTickCount()
        try:
            evidence = matcher.extract_evidence(path, sample_id=f"S{index:02d}")
            elapsed = (cv2.getTickCount() - started) / cv2.getTickFrequency()
            samples.append({
                "sample_id": f"S{index:02d}",
                "pass": True,
                "latency_seconds": round(float(elapsed), 3),
                "inliers": evidence.stats["inliers"],
                "inlier_ratio": evidence.stats["inlier_ratio"],
                "reprojection_rmse": evidence.stats["reprojection_rmse"],
                "coverage": evidence.stats["coverage"],
                "template_edge_recall": evidence.stats["template_edge_recall"],
                "ink_pixels": evidence.stats["ink_pixels"],
                "component_count": evidence.stats["component_count"],
            })
        except (AlignmentError, ValueError, OSError, cv2.error) as exc:
            elapsed = (cv2.getTickCount() - started) / cv2.getTickFrequency()
            samples.append({
                "sample_id": f"S{index:02d}",
                "pass": False,
                "latency_seconds": round(float(elapsed), 3),
                "error": str(exc),
            })
    passed = [sample for sample in samples if sample["pass"]]
    report = {
        "source_mode": manifest["source_mode"],
        "answer_preservation_safe": manifest["answer_preservation_safe"],
        "artifact_registration_sha256": manifest["registration_sha256"],
        "sample_count": len(samples),
        "alignment_pass_count": len(passed),
        "alignment_pass_rate": round(len(passed) / len(samples), 4) if samples else 0.0,
        "mean_latency_seconds": round(
            float(np.mean([sample["latency_seconds"] for sample in samples])), 3
        )
        if samples
        else 0.0,
        "mean_template_edge_recall": round(
            float(np.mean([sample["template_edge_recall"] for sample in passed])), 4
        )
        if passed
        else 0.0,
        "min_template_edge_recall": round(
            min(sample["template_edge_recall"] for sample in passed), 4
        )
        if passed
        else 0.0,
        "samples": samples,
    }
    report["alignment_gate_pass"] = bool(
        report["alignment_pass_rate"] >= 0.95
        and report["min_template_edge_recall"] >= 0.72
    )
    return report
