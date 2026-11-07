"""The student's new ink in each answer region, once the template is taken out.

- ``_answer_only_pixels_v2`` removes registered template ink while keeping new
  student strokes (``TEMPLATE_OVERLAP_DARKER_MARGIN``).
- ``_choice_answer_pixels`` keeps the dominant handwritten choice mark and drops
  OCR-text fragments.
- ``_confident_blank_questions`` names the questions whose answer boxes hold no
  confident new ink, counted by ``_new_ink_pixel_count`` on template-white pixels.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

from services.template_evidence_base import TemplateEvidenceError, cv2, np
from services.template_evidence_crops import _answer_crop_boxes, _pixel_regions
from services.template_evidence_residue import (
    TEMPLATE_INK_LOCAL_CONTRAST,
    TEMPLATE_INK_MAX_GRAY,
    _erase_template_handwriting,
    _illumination_normalized_student_gray,
    _local_ink_mask,
    _restore_fill_line_overlap_strokes,
    _suppress_registered_template_residue_v2,
    _template_handwriting_region_mask,
)
from services.template_evidence_schema import (
    _validated_template_handwriting_regions,
    normalize_answer_regions,
)


# A student stroke pressed over printed template ink only becomes a difference
# candidate when it reads materially darker than the registered template stroke.
# The same margin decides which overlap pixels survive the residue cleanup, so
# the two tests must not drift apart.
TEMPLATE_OVERLAP_DARKER_MARGIN = 28


def _answer_only_pixels_v2(
    aligned_rgb: np.ndarray,
    template_rgb: np.ndarray,
    source_mask: np.ndarray,
    answer_regions: list[dict] | None = None,
    template_handwriting_regions: list[dict] | None = None,
) -> np.ndarray:
    """Remove registered template ink while retaining new student strokes.

    The visual model must not be allowed to solve from printed question text.
    Both images are already in the same canonical coordinate system, so a
    conservative pixel difference is enough: dark pixels on a template-white
    location, or pixels materially darker/different from the template, are
    treated as student ink.  Invalid projected source pixels stay white.

    ``template_handwriting_regions`` names boxes approved by the vision model.
    Every pixel in each box is blanked in the registered baseline, and the
    corresponding student pixels are allowed through as-is. There is no second
    pixel/shape decision inside an approved exemption box.
    """
    if (
        not isinstance(aligned_rgb, np.ndarray)
        or not isinstance(template_rgb, np.ndarray)
        or aligned_rgb.dtype != np.uint8
        or template_rgb.dtype != np.uint8
        or aligned_rgb.ndim != 3
        or template_rgb.shape != aligned_rgb.shape
        or aligned_rgb.shape[2] != 3
        or not isinstance(source_mask, np.ndarray)
        or source_mask.dtype != np.uint8
        or source_mask.shape != aligned_rgb.shape[:2]
    ):
        raise TemplateEvidenceError("answer-only image inputs are invalid")

    student_gray = cv2.cvtColor(aligned_rgb, cv2.COLOR_RGB2GRAY)
    template_gray = cv2.cvtColor(template_rgb, cv2.COLOR_RGB2GRAY)
    normalized_handwriting = (
        _validated_template_handwriting_regions(template_handwriting_regions)
        if template_handwriting_regions is not None
        else []
    )
    # Frozen template handwriting is erased from the baseline before anything
    # else reads it. The model-approved rectangle is the complete authority;
    # there is no pixel-level veto inside it.
    if template_handwriting_regions is not None:
        template_gray = _erase_template_handwriting(
            template_gray,
            normalized_handwriting,
        )
    height, width = student_gray.shape
    normalized_answers = (
        normalize_answer_regions(answer_regions)
        if answer_regions is not None
        else None
    )
    choice_answer_mask = None
    if normalized_answers is not None:
        choice_answer_mask = np.zeros((height, width), dtype=bool)
        for region in _pixel_regions(normalized_answers, width, height):
            if region["question_type"] == "choice":
                choice_answer_mask[
                    region["y1"]:region["y2"],
                    region["x1"]:region["x2"],
                ] = True
    # Compare exposure-normalized gray values, while retaining the original
    # student pixels for the evidence image below.
    comparison_student_gray = _illumination_normalized_student_gray(
        student_gray,
        template_gray,
    )
    difference = cv2.absdiff(comparison_student_gray, template_gray)
    # A little tolerance absorbs camera brightness and interpolation noise,
    # while the dark-pixel gate avoids copying the page background.
    student_dark = _local_ink_mask(
        comparison_student_gray,
        contrast=16,
        max_gray=235,
    ) | (comparison_student_gray <= 160)
    template_ink = _local_ink_mask(
        template_gray,
        contrast=TEMPLATE_INK_LOCAL_CONTRAST,
        max_gray=TEMPLATE_INK_MAX_GRAY,
    )
    template_white = ~template_ink
    # Interpolation and a small local registration error can make printed
    # template strokes differ by more than 24 gray levels.  Treating every
    # such difference as student ink leaks question text back into the OCR
    # image.  A template-white pixel may be new handwriting; over an existing
    # dark template stroke, retain it only when the student stroke is clearly
    # darker than the registered template stroke.
    substantially_darker = (
        comparison_student_gray.astype(np.int16) + TEMPLATE_OVERLAP_DARKER_MARGIN
        < template_gray.astype(np.int16)
    )
    new_ink = student_dark & (
        (template_white & (difference >= 12))
        | ((~template_white) & substantially_darker)
    )
    candidate_before_residue_filter = new_ink.copy()
    new_ink = _suppress_registered_template_residue_v2(
        new_ink,
        comparison_student_gray,
        template_gray,
        answer_regions=normalized_answers,
    )
    if choice_answer_mask is not None:
        # Choice marks are compact and sit beside printed brackets. Keep the
        # candidate pixels in the teacher-confirmed choice box, while fill and
        # solve answers remain subject to residue filtering across the column.
        new_ink[choice_answer_mask] = candidate_before_residue_filter[
            choice_answer_mask
        ]
    # Runs with or without answer boxes.  The full-column contract carries no
    # answer regions, and a student crossing a printed rule there needs the same
    # recovery: the crossing test below is a property of the stroke, not of any
    # declared answer ownership.
    new_ink = _restore_fill_line_overlap_strokes(
        new_ink,
        comparison_student_gray,
        template_gray,
        normalized_answers,
    )
    if normalized_handwriting:
        # An approved exemption is intentionally destructive: after all
        # generic residue, choice, and fill-line filters have run, every valid
        # student-side pixel in the model box remains authoritative. A second
        # darkness threshold here would erase faint pencil inside approved boxes.
        exemption_mask = _template_handwriting_region_mask(
            normalized_handwriting,
            width,
            height,
        )
        new_ink[exemption_mask] = True
    new_ink &= source_mask == 255
    answer_only = np.full_like(aligned_rgb, 255)
    answer_only[new_ink] = aligned_rgb[new_ink]
    return np.ascontiguousarray(answer_only)


def _choice_answer_pixels(crop: np.ndarray) -> np.ndarray:
    """Keep the dominant handwritten choice mark, dropping OCR-text fragments."""
    if crop.size == 0:
        return crop
    ink = np.any(crop < 245, axis=2).astype(np.uint8)
    detection = cv2.morphologyEx(
        ink,
        cv2.MORPH_CLOSE,
        np.ones((3, 3), dtype=np.uint8),
        iterations=1,
    )
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        detection,
        connectivity=8,
    )
    candidates = [
        index for index in range(1, count)
        if int(stats[index, cv2.CC_STAT_AREA]) >= 12
    ]
    if not candidates:
        return np.full_like(crop, 255)
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    darkness = {
        index: int(np.sum((255 - gray)[(labels == index) & (ink == 1)]))
        for index in candidates
    }
    dominant = max(
        candidates,
        key=lambda index: (
            int(stats[index, cv2.CC_STAT_HEIGHT]),
            darkness[index],
        ),
    )
    dominant_left = int(stats[dominant, cv2.CC_STAT_LEFT])
    dominant_top = int(stats[dominant, cv2.CC_STAT_TOP])
    dominant_width = int(stats[dominant, cv2.CC_STAT_WIDTH])
    dominant_height = int(stats[dominant, cv2.CC_STAT_HEIGHT])
    horizontal_margin = max(8, int(round(dominant_height * 0.25)))
    vertical_margin = 6
    nearby_left = dominant_left - horizontal_margin
    nearby_right = dominant_left + dominant_width + horizontal_margin
    nearby_top = dominant_top - vertical_margin
    nearby_bottom = dominant_top + dominant_height + vertical_margin
    keep_components = {dominant}
    for index in candidates:
        center_y = float(_centroids[index][1])
        component_left = int(stats[index, cv2.CC_STAT_LEFT])
        component_right = component_left + int(
            stats[index, cv2.CC_STAT_WIDTH]
        )
        if (
            component_right >= nearby_left
            and component_left <= nearby_right
            and nearby_top <= center_y <= nearby_bottom
        ):
            keep_components.add(index)
    keep = np.isin(labels, list(keep_components))
    filtered = np.full_like(crop, 255)
    original_ink = ink.astype(bool)
    filtered[keep & original_ink] = crop[keep & original_ink]
    return np.ascontiguousarray(filtered)


def _new_ink_pixel_count(student_crop: np.ndarray, template_crop: np.ndarray) -> int:
    """Count only confident new dark connected components on template-white pixels."""
    student_gray = cv2.cvtColor(student_crop, cv2.COLOR_RGB2GRAY)
    template_gray = cv2.cvtColor(template_crop, cv2.COLOR_RGB2GRAY)
    template_white = template_gray >= 225
    if int(np.count_nonzero(template_white)) < 25:
        return 1

    student_values = student_gray[template_white]
    template_values = template_gray[template_white]
    brightness_shift = float(np.median(template_values) - np.median(student_values))
    brightness_shift = max(-35.0, min(35.0, brightness_shift))
    adjusted = np.clip(
        student_gray.astype(np.float32) + brightness_shift,
        0,
        255,
    ).astype(np.uint8)
    new_dark = (
        template_white
        & (adjusted <= 190)
        & ((template_gray.astype(np.int16) - adjusted.astype(np.int16)) >= 30)
    ).astype(np.uint8)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        new_dark,
        connectivity=8,
    )
    pixels = 0
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        component_width = int(stats[index, cv2.CC_STAT_WIDTH])
        component_height = int(stats[index, cv2.CC_STAT_HEIGHT])
        if area >= 3 and (component_width >= 2 or component_height >= 2):
            pixels += area
    return pixels


def _confident_blank_questions(
    aligned_rgb: np.ndarray,
    template_rgb: np.ndarray,
    regions: list[dict],
    *,
    answer_only_rgb: np.ndarray | None = None,
) -> tuple[list[str], dict[str, int]]:
    if answer_only_rgb is not None and (
        not isinstance(answer_only_rgb, np.ndarray)
        or answer_only_rgb.dtype != np.uint8
        or answer_only_rgb.shape != aligned_rgb.shape
    ):
        raise TemplateEvidenceError("answer-only blank detection input is invalid")
    height, width = aligned_rgb.shape[:2]
    boxes = _answer_crop_boxes(regions, width, height)
    counts: dict[str, int] = {}
    blanks: list[str] = []
    for region, box in zip(regions, boxes):
        if answer_only_rgb is None:
            student_crop = np.ascontiguousarray(
                aligned_rgb[box["y1"]:box["y2"], box["x1"]:box["x2"]]
            )
            template_crop = np.ascontiguousarray(
                template_rgb[box["y1"]:box["y2"], box["x1"]:box["x2"]]
            )
        else:
            student_crop = np.ascontiguousarray(
                answer_only_rgb[
                    box["y1"]:box["y2"],
                    box["x1"]:box["x2"],
                ]
            )
            template_crop = np.full_like(student_crop, 255)
        pixels = _new_ink_pixel_count(student_crop, template_crop)
        del student_crop, template_crop
        number = region["question_number"]
        counts[number] = pixels
        if pixels == 0:
            blanks.append(number)
    return blanks, counts
