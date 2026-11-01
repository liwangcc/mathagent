"""Coarse geometric eligibility, not a correctness probability or a grader.

Inputs are the original registered RGB canvas, NEVER a reference overlay. The
teacher's standard is only the stored normalized geometry, re-rendered at
x*W/1000, y*H/1000 on the rectified page; no pixels are copied from the paper.
A blank-template difference isolates new ink. On a marked template the
registered template page only says what is already on the paper: ink dark on
both pages is not attributed, template-only marks read as blank paper, and
reference parts lying on such ink are not scored. Point-only standards keep
long-grid suppression. Complete alternatives are measured separately;
exceptions are unavailable, never zero coverage.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from services.drawing_reference import render_reference_mask, validate_confirmed_reference

VERSION = "drawing-pixel-coarse-v1"
THRESHOLD = 80
_GRAY_THRESHOLD = 160
_NEW_DARKENING = 40
# A standard stroke is scored only when at least half of it can be attributed;
# otherwise the result is unavailable and the model check decides alone.
_MIN_MEASURABLE = 0.5
# Below this fraction of print found again on the student page (within the
# registration slack) the rectification is not trusted for any attribution.
_MIN_PRINT_ALIGNMENT = 0.5


def _rgb(value):
    if (not isinstance(value, np.ndarray) or value.dtype != np.uint8
            or value.ndim != 3 or value.shape[2] != 3):
        raise ValueError("expected uint8 RGB canvas")
    height, width = value.shape[:2]
    if not 1 <= min(height, width) <= max(height, width) <= 16384 or height * width > 40_000_000:
        raise ValueError("invalid canvas dimensions")
    return height, width


def _box(raw, width, height):
    if (not isinstance(raw, list) or len(raw) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or not 0 <= v <= 1000 for v in raw)
            or raw[0] >= raw[2] or raw[1] >= raw[3]):
        raise ValueError("invalid drawing region")
    return (math.floor(raw[0] * width / 1000), math.floor(raw[1] * height / 1000),
            math.ceil(raw[2] * width / 1000), math.ceil(raw[3] * height / 1000))


def _visible(question, reference, width, height):
    regions = question.get("drawing_regions") if isinstance(question, dict) else None
    if not isinstance(regions, list) or not 1 <= len(regions) <= 64:
        raise ValueError("missing saved drawing regions")
    visible = np.zeros((height, width), dtype=bool)
    for region in regions:
        x1, y1, x2, y2 = _box(region, width, height)
        visible[y1:y2, x1:x2] = True
    if not visible.any():
        raise ValueError("drawing region has no pixels")
    for region in reference["context_regions"]:
        x1, y1, x2, y2 = _box(region, width, height)
        if not visible[y1:y2, x1:x2].all():
            raise ValueError("reference context is not preserved")
    return visible


def _thin(mask):
    """Zhang-Suen skeleton without an optional OpenCV/scikit-image dependency."""
    work = np.pad(mask.astype(np.uint8), 1)
    while True:
        removed = 0
        for step in (0, 1):
            p = work[1:-1, 1:-1]
            p2, p3, p4 = work[:-2, 1:-1], work[:-2, 2:], work[1:-1, 2:]
            p5, p6, p7 = work[2:, 2:], work[2:, 1:-1], work[2:, :-2]
            p8, p9 = work[1:-1, :-2], work[:-2, :-2]
            neighbours = (p2, p3, p4, p5, p6, p7, p8, p9)
            degree = sum(neighbours)
            transitions = sum((a == 0) & (b == 1) for a, b in zip(neighbours, neighbours[1:] + neighbours[:1]))
            constraint = ((p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0) if step == 0
                          else (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0))
            delete = (p == 1) & (degree >= 2) & (degree <= 6) & (transitions == 1) & constraint
            removed += int(delete.sum())
            p[delete] = 0
        if removed == 0:
            return work[1:-1, 1:-1].astype(bool)


def _skeleton_samples(skeleton):
    """Euclidean edge-midpoint weights; don't count redundant diagonal bridges."""
    height, width = skeleton.shape
    samples, weights = [], []
    connected = np.zeros_like(skeleton)
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        ya, yb = (0, height - dy), (dy, height)
        xa = (0, width - dx) if dx >= 0 else (-dx, width)
        xb = (dx, width) if dx >= 0 else (0, width + dx)
        edges = skeleton[ya[0]:ya[1], xa[0]:xa[1]] & skeleton[yb[0]:yb[1], xb[0]:xb[1]]
        if dy and dx:
            edges &= ~(skeleton[ya[0]:ya[1], xb[0]:xb[1]] | skeleton[yb[0]:yb[1], xa[0]:xa[1]])
        yy, xx = np.where(edges)
        xx, yy = xx + xa[0], yy + ya[0]
        if xx.size:
            samples.append(np.column_stack((xx + dx / 2, yy + dy / 2)))
            weights.append(np.full(xx.size, math.hypot(dx, dy)))
            connected[yy, xx] = True
            connected[yy + dy, xx + dx] = True
    yy, xx = np.where(skeleton & ~connected)
    if xx.size:
        samples.append(np.column_stack((xx, yy)))
        weights.append(np.ones(xx.size))
    return (np.concatenate(samples), np.concatenate(weights)) if samples else (np.empty((0, 2)), np.empty(0))


def _distance(mask):
    if not mask.any():
        return np.full(mask.shape, 1e6, dtype=np.float32)
    return cv2.distanceTransform((~mask).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)


def _sample_distance(distance, positions):
    # OpenCV remap limits each map dimension to SHRT_MAX, even though a valid
    # full-page diagonal may have more samples. Keep normal long segments valid.
    values = []
    for start in range(0, len(positions), 16000):
        chunk = positions[start:start + 16000]
        values.append(cv2.remap(distance, chunk[:, 0].astype(np.float32).reshape(1, -1),
                                chunk[:, 1].astype(np.float32).reshape(1, -1), cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=1e6).ravel())
    return np.concatenate(values)


def _ink(canvas, visible, blank_template, template_mode):
    gray = cv2.cvtColor(canvas, cv2.COLOR_RGB2GRAY)
    rgb = canvas.astype(np.int16)
    # Reject likely reference-layer contamination rather than counting colored
    # annotation as black ink, or treating a colored student pen as blank.
    chromatic = (rgb.max(axis=2) - rgb.min(axis=2) > 40) & (gray < 245)
    if np.any(chromatic & visible):
        raise ValueError("colored ink or reference overlay in original evidence")
    black = (gray < _GRAY_THRESHOLD) & visible
    if template_mode == "blank":
        if blank_template is None or _rgb(blank_template) != canvas.shape[:2]:
            raise ValueError("same-size blank template is required")
        background = cv2.cvtColor(blank_template, cv2.COLOR_RGB2GRAY).astype(np.int16)
        return black & (background - gray.astype(np.int16) >= _NEW_DARKENING), "blank_difference"
    if template_mode != "marked":
        raise ValueError("unknown template mode")
    kernel = max(3, round(49 * min(canvas.shape[:2]) / 1477))
    kernel += 1 - kernel % 2
    raw = black.astype(np.uint8)
    grid = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((1, kernel), np.uint8))
    grid |= cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((kernel, 1), np.uint8))
    return black & ~grid.astype(bool), "marked_point_grid_suppression"


def _registered_template_ink(canvas, template, visible):
    """Attribute student strokes on a rectified page against its marked template.

    The template is used only as "already on the paper", never as the answer.
    A dark pixel on both pages (print, or a template mark the student happens
    to cover) is unattributable; a template-only mark has no student pixel and
    reads as blank paper. A stroke pressed clearly darker than the nearby
    template ink, such as pencil over a light grid line, is still new ink.
    Registration slack follows the evidence pipeline, scaled to resolution.
    """
    from services.template_evidence import (
        MIN_NEW_STROKE_SUPPORT_PIXELS, MIN_NEW_STROKE_SUPPORT_RATIO, TEMPLATE_EDGE_DILATION_SIZE,
        TEMPLATE_INK_LOCAL_CONTRAST, TEMPLATE_INK_MAX_GRAY, TEMPLATE_OVERLAP_DARKER_MARGIN,
        _illumination_normalized_student_gray, _local_ink_mask,
    )
    if template is None or _rgb(template) != canvas.shape[:2]:
        raise ValueError("same-size registered template is required")
    # Slack scales with the whole page, never with the working window below.
    size = max(TEMPLATE_EDGE_DILATION_SIZE, 2 * round(3 * min(canvas.shape[:2]) / 1477) + 1)
    # Work on the drawing regions plus a margin wider than every blur kernel
    # (illumination sigma 31 -> radius < 128), so values inside the regions
    # equal a full-page computation. Results go back at the same page pixels.
    rows, columns = np.nonzero(visible)
    pad = 128
    window = (slice(max(0, int(rows.min()) - pad), min(canvas.shape[0], int(rows.max()) + 1 + pad)),
              slice(max(0, int(columns.min()) - pad), min(canvas.shape[1], int(columns.max()) + 1 + pad)))
    full_visible, visible = visible, visible[window]
    student = cv2.cvtColor(np.ascontiguousarray(canvas[window]), cv2.COLOR_RGB2GRAY)
    printed = cv2.cvtColor(np.ascontiguousarray(template[window]), cv2.COLOR_RGB2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    template_ink = _local_ink_mask(printed, contrast=TEMPLATE_INK_LOCAL_CONTRAST,
                                   max_gray=TEMPLATE_INK_MAX_GRAY) | (printed < _GRAY_THRESHOLD)
    near_template = cv2.dilate(template_ink.astype(np.uint8), kernel).astype(bool)
    student_ink = _local_ink_mask(student, contrast=TEMPLATE_INK_LOCAL_CONTRAST,
                                  max_gray=TEMPLATE_INK_MAX_GRAY) | (student < _GRAY_THRESHOLD)
    near_student = cv2.dilate(student_ink.astype(np.uint8), kernel).astype(bool)
    shared = template_ink & visible
    # Print must reappear on the student page within the slack. Template-only
    # marks lower this too, which can only route the item to the model check.
    alignment = float(np.mean(near_student[shared])) if shared.any() else 1.0
    if shared.sum() >= 50 and alignment < _MIN_PRINT_ALIGNMENT:
        raise ValueError("printed content is not registered within the slack")
    floor = cv2.erode(printed, kernel)  # darkest template value within the slack
    comparison = _illumination_normalized_student_gray(student, printed)
    dark = (student < _GRAY_THRESHOLD) & visible
    darker = comparison.astype(np.int16) + TEMPLATE_OVERLAP_DARKER_MARGIN < floor.astype(np.int16)
    new_ink = dark & (~near_template | darker)
    # Slightly larger misregistration leaves slivers beside dark print. As in
    # the evidence pipeline, a component without enough support away from
    # print is residue, not a student stroke; it becomes unattributable.
    band_size = 2 * (4 * (size // 2)) + 1
    band = cv2.dilate((printed < _GRAY_THRESHOLD).astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (band_size, band_size))).astype(bool)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(new_ink.astype(np.uint8), connectivity=8)
    if count > 1:
        free = np.bincount(labels[new_ink & ~band], minlength=count)
        needed = np.maximum(MIN_NEW_STROKE_SUPPORT_PIXELS,
                            np.ceil(stats[:, cv2.CC_STAT_AREA] * MIN_NEW_STROKE_SUPPORT_RATIO))
        residue = free < needed
        residue[0] = False
        new_ink &= ~residue[labels]
    page_ink = np.zeros(full_visible.shape, dtype=bool)
    page_unattributable = np.zeros(full_visible.shape, dtype=bool)
    page_ink[window] = new_ink
    page_unattributable[window] = dark & ~new_ink
    return page_ink, page_unattributable, {"registration_slack_px": size // 2,
                                           "print_alignment_percent": alignment * 100}


def _pixel(point, width, height):
    return np.minimum(np.asarray(point, dtype=float) * (width / 1000, height / 1000), (width - 1, height - 1))


def _on_mask(mask, positions):
    """Nearest-pixel lookup of local (x, y) sample positions; rows are y."""
    xs = np.clip(np.rint(positions[:, 0]).astype(int), 0, mask.shape[1] - 1)
    ys = np.clip(np.rint(positions[:, 1]).astype(int), 0, mask.shape[0] - 1)
    return mask[ys, xs]


def _measurable(region, unattributable, what):
    """Pixels of one required stroke that can be attributed at all."""
    if not region.any():
        raise ValueError(f"{what} has no visible pixels")
    kept = region & ~unattributable
    if kept.sum() < _MIN_MEASURABLE * region.sum():
        raise ValueError(f"{what} mostly overlaps ink already on the page")
    return kept


def _measure_variant(index, reference, visible, crop, ink, mode, width, height, unattributable=None):
    variant = reference["variants"][index]
    has_shapes = any(stroke["kind"] != "point" for stroke in variant["strokes"])
    if has_shapes and mode not in {"blank_difference", "registered_template_difference"}:
        raise ValueError("marked shapes need the registered template page")
    full_mask = render_reference_mask(reference, width, height, index)
    if not full_mask.any() or np.any((full_mask != 0) & ~visible):
        raise ValueError("complete reference must fit preserved drawing regions")
    local_ink = ink[crop]
    blocked = unattributable[crop] if unattributable is not None else np.zeros_like(local_ink)
    short_edge = min(width, height)
    point_tolerance = 3 * short_edge / 1477
    line_tolerance = 0.005 * short_edge
    # Students shade with spaced hatching, not solid fill. A pixel "fail" is an
    # automatic zero without a model check, so area coverage tolerates gaps of
    # a few millimetres; precision below still rejects ink in another region.
    area_tolerance = max(line_tolerance, 0.015 * short_edge)
    # Two freehand curves (or a compass circle against a dragged one) differ by
    # more than a ruled line does. Curves are scored in chunks of this length
    # with the area tolerance, so one local wobble is not an automatic zero.
    curve_tolerance, curve_chunk = area_tolerance, 0.04 * short_edge
    ink_distance = _distance(local_ink)
    point_coverages, point_geometries, lines, area_coverages = [], [], [], []
    area_mask = np.zeros_like(local_ink)
    # Local crop coordinates: x = page_x - crop.x0, y = page_y - crop.y0.
    origin = np.array([crop[1].start, crop[0].start], dtype=float)
    point_radius = max(1, round(5.5 * min(width / 1000, height / 820)))
    for stroke in variant["strokes"]:
        if stroke["kind"] == "point":
            single = {**reference, "variants": [{"strokes": [stroke]}]}
            point_mask = _measurable(render_reference_mask(single, width, height)[crop] != 0, blocked, "point reference")
            point_coverages.append(float(np.mean(ink_distance[point_mask] <= point_tolerance)))
            point_geometries.append(_pixel(stroke["point"], width, height) - origin)
        elif stroke["kind"] in {"polygon", "brush"} or (stroke["kind"] == "ellipse" and stroke["fill"]):
            single = {**reference, "variants": [{"strokes": [stroke]}]}
            painted = render_reference_mask(single, width, height)[crop] != 0
            measured = _measurable(painted, blocked, "filled reference")
            area_coverages.append(float(np.mean(ink_distance[measured] <= area_tolerance)))
            area_mask |= painted
        elif stroke["kind"] == "ellipse":
            center = _pixel(stroke["center"], width, height) - origin
            radii = np.asarray(stroke["radii"]) * (width / 1000, height / 1000)
            count = min(128, max(16, math.ceil(2 * math.pi * max(radii) / 4)))
            angles = np.linspace(0, 2 * math.pi, count + 1)
            lines.append((center + np.column_stack((np.cos(angles), np.sin(angles))) * radii, True))
        else:
            points = np.array([_pixel(point, width, height) - origin for point in stroke["points"]])
            steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
            # Curve, arc and spline tools store many short chords; ruled tools
            # store few long edges, each of which must be drawn on its own.
            lines.append((points, len(steps) >= 4 and float(steps.mean()) < 0.02 * short_edge))
    metrics = {"mode": mode, "new_ink_pixels": int(local_ink.sum()),
               "gray_threshold": _GRAY_THRESHOLD, "new_darkening_threshold": _NEW_DARKENING if mode == "blank_difference" else None,
               "point_tolerance_px": point_tolerance, "line_tolerance_px": line_tolerance,
               "area_tolerance_px": area_tolerance, "curve_tolerance_px": curve_tolerance,
               "unattributable_pixels": int(blocked.sum()),
               "point_coverage_percent": [value * 100 for value in point_coverages],
               "area_coverage_percent": [value * 100 for value in area_coverages],
               "segment_coverage_percent": [], "student_to_reference_percent": None}
    required = point_coverages + area_coverages
    recalls = []
    if lines:
        skeleton = _thin(local_ink)
        # Thinning a filled region collapses its outline into the interior.
        # Preserve observed ink inside the intended fill for line coverage;
        # lines outside that area still require their own student strokes.
        distance = _distance(skeleton | (local_ink & area_mask))
        student, weights = _skeleton_samples(skeleton)
        supported = np.zeros(len(student), dtype=bool)
        if area_mask.any():
            supported |= _sample_distance(_distance(area_mask), student) <= line_tolerance
        pieces = []
        for points, curved in lines:
            tolerance = curve_tolerance if curved else line_tolerance
            run = []
            for a, b in zip(points[:-1], points[1:]):
                delta = b - a
                length = float(np.linalg.norm(delta))
                if length < 1:
                    if curved:
                        continue
                    raise ValueError("subpixel reference segment cannot be measured reliably")
                count = max(1, math.ceil(length * 2))
                positions = a + (np.arange(count)[:, None] + 0.5) / count * delta
                proportion = np.clip(((student - a) @ delta) / (delta @ delta), 0, 1)
                supported |= np.linalg.norm(student - (a + proportion[:, None] * delta), axis=1) <= tolerance
                if curved:
                    run.append(positions)
                else:
                    pieces.append((positions, length, tolerance))
            if run:
                samples = np.concatenate(run)  # two samples per pixel of arc length
                for part in np.array_split(samples, max(1, round(len(samples) / (2 * curve_chunk)))):
                    pieces.append((part, len(part) / 2, tolerance))
        recalls, lengths, measured, total = [], [], 0.0, 0.0
        for positions, length, tolerance in pieces:
            keep = ~_on_mask(blocked, positions)
            total += length
            if keep.mean() < _MIN_MEASURABLE:
                continue  # lies on ink already on the page; the model judges it
            measured += length * float(keep.mean())
            recalls.append(float(np.mean(_sample_distance(distance, positions[keep]) <= tolerance)))
            lengths.append(length)
        if not recalls or measured < _MIN_MEASURABLE * total:
            raise ValueError("reference lines mostly overlap ink already on the page")
        # A point belonging to THIS complete variant is not an extra line.
        for point in point_geometries:
            supported |= np.linalg.norm(student - point, axis=1) <= point_radius + point_tolerance
        precision = float(weights[supported].sum() / weights.sum()) if weights.size else 0.0
        required.extend(recalls)
        required.append(precision)
        metrics.update(segment_coverage_percent=[value * 100 for value in recalls],
                       reference_to_student_percent=float(np.average(recalls, weights=lengths)) * 100,
                       measured_reference_line_percent=measured / total * 100,
                       student_to_reference_percent=precision * 100,
                       student_skeleton_length_px=float(weights.sum()))
    elif area_coverages:
        # Area fill must cover the intended region without rewarding ink in a
        # different region. The fixed template coordinates remain unchanged.
        reference_distance = _distance(full_mask[crop] != 0)
        precision = float(np.mean(reference_distance[local_ink] <= line_tolerance)) if local_ink.any() else 0.0
        required.append(precision)
        metrics["student_to_reference_percent"] = precision * 100
    if not required:
        raise ValueError("reference has no measurable required strokes")
    score = min(required) * 100
    if not math.isfinite(score):
        raise ValueError("non-finite coverage")
    metrics["minimum_required_coverage_percent"] = min(point_coverages + area_coverages + recalls) * 100
    if mode == "marked_point_grid_suppression":
        metrics["limitations"] = ["printed dots are not authenticated as handwriting", "grid suppression may remove coincident student ink",
                                  "point gate does not measure all extra ink"]
    elif mode == "registered_template_difference":
        metrics["limitations"] = ["ink dark on both pages is not attributed to the student",
                                  "template-only marks read as blank paper"]
    return {"variant_index": index, "status": "pass" if score >= THRESHOLD else "fail", "score": score,
            "reason": "粗覆盖达到门槛，仍须模型核对" if score >= THRESHOLD else "已成功计算，完整候选的粗覆盖低于门槛",
            "metrics": metrics}


def evaluate_pixel_gate(canvas, question, reference, *, blank_template=None, template_mode="marked",
                        marked_template=None) -> dict:
    """Measure each confirmed alternative independently; never infer a grade.

    Only all-successfully-computed ``fail`` alternatives permit the caller's
    coarse rejection policy. ``unavailable`` must not be treated as zero/fail.
    ``pass`` means eligible for a separate model check, not an automatic award.
    ``marked_template`` is the hash-bound canonical page of a marked template,
    in the same pixel frame as ``canvas``; it is only a printed-content mask.
    """
    result = {"version": VERSION, "threshold": THRESHOLD, "variants": []}
    raw_variants = reference.get("variants") if isinstance(reference, dict) else None
    count = len(raw_variants) if isinstance(raw_variants, list) and 1 <= len(raw_variants) <= 4 else 1

    def unavailable(index, exc):
        return {"variant_index": index, "status": "unavailable", "score": None,
                "reason": "像素证据无法可靠计算，不作为零覆盖：" + type(exc).__name__, "metrics": {"detail": str(exc)[:240]}}

    try:
        height, width = _rgb(canvas)
        reference = validate_confirmed_reference(reference, require_fixed=True)
        visible = _visible(question, reference, width, height)
        ink, mode = _ink(canvas, visible, blank_template, template_mode)
        yy, xx = np.where(visible)
        crop = (slice(int(yy.min()), int(yy.max()) + 1), slice(int(xx.min()), int(xx.max()) + 1))
    except Exception as exc:
        result["variants"] = [unavailable(index, exc) for index in range(count)]
        return result
    registered = None
    if mode == "marked_point_grid_suppression" and any(
            stroke["kind"] != "point" for variant in reference["variants"] for stroke in variant["strokes"]):
        try:
            registered = _registered_template_ink(canvas, marked_template, visible)
        except Exception as exc:
            registered = exc  # shapes become unavailable; point-only variants still measure
    for index in range(len(reference["variants"])):
        try:
            shapes = any(stroke["kind"] != "point" for stroke in reference["variants"][index]["strokes"])
            if shapes and isinstance(registered, Exception):
                raise registered
            if shapes and registered is not None:
                new_ink, unattributable, alignment = registered
                item = _measure_variant(index, reference, visible, crop, new_ink, "registered_template_difference",
                                        width, height, unattributable)
                item["metrics"].update(alignment)
            else:
                item = _measure_variant(index, reference, visible, crop, ink, mode, width, height)
            result["variants"].append(item)
        except Exception as exc:
            result["variants"].append(unavailable(index, exc))
    return result
