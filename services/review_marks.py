"""Render lightweight teacher review marks over normalized answer boxes."""

from __future__ import annotations

import hashlib
import json
import math
import copy
from pathlib import Path
from typing import Any



def _template_to_target_homography(template_path: str | Path, target_path: str | Path):
    """Estimate a page registration transform for template boxes."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        # OpenCV's default decoder may apply EXIF orientation depending on the
        # build.  The boxes are rendered on the display image, so load both
        # registration images through the same EXIF-normalized PIL path first.
        def load_gray(path):
            with Image.open(path) as source:
                return np.asarray(ImageOps.exif_transpose(source).convert("L"))

        template = load_gray(template_path)
        target = load_gray(target_path)
        if template is None or target is None or template.size == 0 or target.size == 0:
            return None

        # The old ORB matcher is very sensitive to repeated answer lines and
        # glyphs: it can find many apparently good matches that belong to a
        # different row/column, producing a plausible but wrong homography.
        # SIFT with a ratio test and reprojection/coverage checks is the same
        # family of registration used by template evidence generation and is
        # substantially more stable on ordinary worksheets.
        detector_factory = getattr(cv2, "SIFT_create", None)
        if detector_factory is None:
            return None
        detector = detector_factory(nfeatures=3000)
        key_template, desc_template = detector.detectAndCompute(template, None)
        key_target, desc_target = detector.detectAndCompute(target, None)
        if desc_template is None or desc_target is None:
            return None

        matches = cv2.BFMatcher(cv2.NORM_L2).knnMatch(
            desc_template, desc_target, k=2,
        )
        good = [
            first for pair in matches
            if len(pair) == 2
            for first, second in [pair]
            if first.distance < 0.75 * second.distance
        ]
        if len(good) < 12:
            return None
        source = np.float32([key_template[m.queryIdx].pt for m in good])
        destination = np.float32([key_target[m.trainIdx].pt for m in good])
        matrix, inliers = cv2.findHomography(source, destination, cv2.RANSAC, 5.0)
        if matrix is None or inliers is None or not np.isfinite(matrix).all():
            return None
        inlier_mask = inliers.ravel().astype(bool)
        inlier_count = int(np.count_nonzero(inlier_mask))
        if inlier_count < 8 or inlier_count / len(good) < 0.20:
            return None

        projected = cv2.perspectiveTransform(
            source[inlier_mask].reshape(-1, 1, 2), matrix,
        ).reshape(-1, 2)
        error = projected - destination[inlier_mask]
        reprojection_rmse = float(np.sqrt(np.mean(np.sum(error * error, axis=1))))
        if not np.isfinite(reprojection_rmse) or reprojection_rmse > 8.0:
            return None

        # Reject reflections and wildly extrapolated transforms.  A bad matrix
        # must fall back to normalized coordinates rather than moving marks to
        # a different question while still looking geometrically valid.
        height, width = template.shape[:2]
        target_height, target_width = target.shape[:2]
        corners = np.float32([[
            [0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1],
        ]])
        mapped_corners = cv2.perspectiveTransform(corners, matrix)[0]
        if not np.isfinite(mapped_corners).all():
            return None
        area = cv2.contourArea(mapped_corners.astype(np.float32))
        source_area = float(max(1, width * height))
        if area <= 0 or area / source_area < 0.20:
            return None
        if (
            mapped_corners[:, 0].max() < -0.25 * target_width
            or mapped_corners[:, 0].min() > 1.25 * target_width
            or mapped_corners[:, 1].max() < -0.25 * target_height
            or mapped_corners[:, 1].min() > 1.25 * target_height
        ):
            return None
        return matrix
    except Exception:
        return None


def align_template_review_details(
    details: list[dict[str, Any]], template_path: str | Path, target_path: str | Path
) -> list[dict[str, Any]]:
    """Map template-origin boxes into the displayed student image."""
    matrix = _template_to_target_homography(template_path, target_path)
    if matrix is None:
        return details
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
        with Image.open(template_path) as template_image, Image.open(target_path) as target_image:
            source_width, source_height = ImageOps.exif_transpose(template_image).size
            target_width, target_height = ImageOps.exif_transpose(target_image).size
        def map_boxes(value: Any) -> list[list[float]]:
            mapped = []
            for left, top, right, bottom in normalize_review_boxes(value):
                points = np.float32([[
                    [left * source_width / 1000, top * source_height / 1000],
                    [right * source_width / 1000, top * source_height / 1000],
                    [right * source_width / 1000, bottom * source_height / 1000],
                    [left * source_width / 1000, bottom * source_height / 1000],
                ]])
                transformed = cv2.perspectiveTransform(points, matrix)[0]
                xs = [float(point[0]) * 1000 / target_width for point in transformed]
                ys = [float(point[1]) * 1000 / target_height for point in transformed]
                candidate = [
                    max(0.0, min(1000.0, min(xs))),
                    max(0.0, min(1000.0, min(ys))),
                    max(0.0, min(1000.0, max(xs))),
                    max(0.0, min(1000.0, max(ys))),
                ]
                if candidate[0] < candidate[2] and candidate[1] < candidate[3]:
                    mapped.append(candidate)
            return mapped

        def map_item(item: dict[str, Any], inherited_source: str = "") -> dict[str, Any]:
            source = str(item.get("answer_box_source") or inherited_source).strip().lower()
            if source != "template":
                return item
            mapped = map_boxes(item.get("answer_bboxes") or item.get("answer_bbox"))
            item["answer_bboxes"] = mapped
            if mapped:
                item["answer_bbox"] = mapped[0]
            else:
                # Avoid falling back to a stale legacy singular box after a
                # transformed box was rejected as out of bounds.
                item.pop("answer_bbox", None)
            return item

        aligned = []
        for detail in details:
            item = copy.deepcopy(detail)
            if item.get("answer_box_source") != "template":
                aligned.append(item)
                continue
            map_item(item)
            # Folded fill questions retain per-blank boxes in grading_units.
            # They inherit the parent's template provenance, so they must use
            # the same transform as the parent detail.
            units = item.get("grading_units")
            if isinstance(units, list):
                item["grading_units"] = [
                    map_item(unit, "template") if isinstance(unit, dict) else unit
                    for unit in units
                ]
            aligned.append(item)
        return aligned
    except Exception:
        return details


def normalize_review_boxes(value: Any) -> list[list[float]]:
    """Return only finite normalized_1000_v1 boxes with positive area."""
    if isinstance(value, dict):
        for key in ("answer_bboxes", "answer_bbox", "student_answer_bbox", "bbox"):
            if key in value:
                return normalize_review_boxes(value[key])
        return []
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    ):
        try:
            box = [float(item) for item in value]
        except (TypeError, ValueError):
            return []
        if not all(math.isfinite(item) for item in box):
            return []
        left, top, right, bottom = box
        if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
            return []
        return [box]
    if isinstance(value, (list, tuple)):
        boxes: list[list[float]] = []
        for item in value:
            boxes.extend(normalize_review_boxes(item))
        return boxes
    return []


def detail_review_boxes(detail: dict[str, Any]) -> list[list[float]]:
    for key in ("answer_bboxes", "answer_bbox", "student_answer_bbox", "bbox"):
        boxes = normalize_review_boxes(detail.get(key))
        if boxes:
            return boxes
    return []


def _points_for_mark(result: str, cx: float, cy: float, size: float) -> list[list[tuple[float, float]]]:
    if result == "correct":
        return [[
            (cx - size * 0.55, cy + size * 0.02),
            (cx - size * 0.22, cy + size * 0.34),
            (cx + size * 0.58, cy - size * 0.42),
        ]]
    if result == "wrong":
        return [
            [
                (cx - size * 0.46, cy - size * 0.40),
                (cx - size * 0.10, cy - size * 0.08),
                (cx + size * 0.48, cy + size * 0.40),
            ],
            [
                (cx + size * 0.44, cy - size * 0.42),
                (cx + size * 0.08, cy - size * 0.02),
                (cx - size * 0.48, cy + size * 0.42),
            ],
        ]
    if result == "partial":
        return [[
            (cx - size * 0.48, cy + size * 0.30),
            (cx, cy - size * 0.46),
            (cx + size * 0.50, cy + size * 0.30),
            (cx - size * 0.48, cy + size * 0.30),
        ]]
    return []


def render_review_marks(image_path: str | Path, details: list[dict[str, Any]], output_path: str | Path) -> int:
    """Draw marks in-place on a new image and return the number of marked boxes."""
    from PIL import Image, ImageDraw
    with Image.open(image_path) as source:
        image = source.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    marked = 0
    for detail in details:
        # Multi-blank details are folded for persistence, but their marks must
        # still follow the individual grading units.  Fall back to the legacy
        # parent result for old records without this projection.
        unit_marks = detail.get("grading_units") or []
        if not isinstance(unit_marks, list) or not unit_marks:
            unit_marks = [detail]
        parent_boxes = detail_review_boxes(detail)
        for unit_index, mark in enumerate(unit_marks):
            result = str(mark.get("result") or "needs_review").strip().lower()
            if result not in {"correct", "partial", "wrong"}:
                continue
            boxes = detail_review_boxes(mark)
            # A few historical folded records persisted the per-unit verdicts
            # without copying their boxes.  Keep those marks tied to the
            # corresponding parent box instead of silently dropping them.
            if not boxes and len(unit_marks) == len(parent_boxes) and unit_index < len(parent_boxes):
                boxes = [parent_boxes[unit_index]]
            for left, top, right, bottom in boxes:
                cx = ((left + right) / 2) * width / 1000
                # Anchor the mark to the answer box's lower edge so it reads as a
                # teacher's check on the student's writing line.
                # The bottom edge is the teacher-confirmed writing-line
                # anchor.  Its length also gives a stable visual scale across
                # short fill blanks and tall solve regions; box height must not
                # make an otherwise identical mark jump or grow.
                bottom_edge_length = max(0.0, right - left) * width / 1000
                size = max(48.0, min(160.0, bottom_edge_length * 0.28))
                # A box touching a page edge must not clip the symbol. Keep the
                # mark inside the bitmap while preserving its lower-edge anchor.
                x_margin = min(size * 0.58, width / 2)
                y_margin = min(size * 0.50, height / 2)
                cx = max(x_margin, min(width - x_margin, cx))
                cy = bottom * height / 1000 - size * 0.42
                cy = max(y_margin, min(height - y_margin, cy))
                paths = _points_for_mark(result, cx, cy, size)
                color = (207, 35, 45, 235) if result != "partial" else (213, 126, 25, 235)
                stroke = max(1, int(size * 0.045))
                for points in paths:
                    draw.line(points, fill=(*color[:3], 70), width=stroke + 1, joint="curve")
                    draw.line(points, fill=color, width=stroke, joint="curve")
                marked += 1
    image.save(output_path, format="PNG", optimize=True)
    return marked


def review_cache_name(image_path: str | Path, details: list[dict[str, Any]]) -> str:
    source = Path(image_path)
    stat = source.stat()
    payload = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"thin-marks-v3:{source}:{stat.st_mtime_ns}:{stat.st_size}:{payload}".encode()).hexdigest()[:24]
    return f"review-{digest}.png"
