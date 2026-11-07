"""Taking the template out of a student page so only the student's ink can remain.

- Exemptions: ``_template_handwriting_region_mask`` rasterizes the frozen
  template-handwriting boxes; ``_merge_template_exemption_regions`` unions them
  with the answer boxes; ``_erase_template_handwriting`` blanks every pixel
  inside each approved exemption rectangle.  The
  ``TEMPLATE_HANDWRITING_MIN_AREA`` / ``TEMPLATE_HANDWRITING_RECOVERY_*``
  constants are kept with them; no code reads them.
- Residue: ``_suppress_registered_template_residue_v2`` drops differences that
  nearby registered template ink explains (``TEMPLATE_INK_*``,
  ``TEMPLATE_EDGE_DILATION_SIZE``, ``MIN_NEW_STROKE_SUPPORT_*``), and
  ``_restore_fill_line_overlap_strokes`` restores handwriting that crosses a
  printed fill-in line (``FILL_LINE_*``).
- ``_illumination_normalized_student_gray`` removes only broad exposure
  differences before subtraction (``TEMPLATE_ILLUMINATION_*``);
  ``_local_ink_mask`` finds strokes darker than the surrounding paper.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import math

from services.template_evidence_base import TemplateEvidenceError, cv2, np
from services.template_evidence_schema import _validated_template_handwriting_regions


# Printed template edges can produce a thin false difference after the student
# page is warped back into template coordinates.  These masks use local
# contrast, rather than an absolute white threshold, so photographed paper
# shadows do not become fake template ink.
TEMPLATE_INK_LOCAL_CONTRAST = 12
TEMPLATE_INK_MAX_GRAY = 235
TEMPLATE_EDGE_DILATION_SIZE = 5


# Photos of the same page can have a broad exposure gradient. Estimate that
# low-frequency difference before pixel subtraction, but keep the correction
# bounded so it cannot cancel a real dark student stroke.
TEMPLATE_ILLUMINATION_SIGMA = 31.0
TEMPLATE_ILLUMINATION_MAX_OFFSET = 45.0
MIN_NEW_STROKE_SUPPORT_PIXELS = 8
MIN_NEW_STROKE_SUPPORT_RATIO = 0.20
FILL_LINE_OPENING_WIDTH = 25
FILL_LINE_VERTICAL_DILATION = 5
FILL_LINE_SUPPORT_PAD_X = 10
FILL_LINE_SUPPORT_PAD_Y = 12
FILL_LINE_CROSSING_HORIZONTAL_PAD = 2
FILL_LINE_MIN_SIDE_SUPPORT_PIXELS = 2


# Printed glyphs are small and compact; regression fixtures use this bound for
# compact template ink that an exemption box must not treat as handwriting.
TEMPLATE_HANDWRITING_MIN_AREA = 200


# V2 can recover the faint edge of a student mark when the mark itself has
# enough registered-difference pixels to prove that it is new. Recovery stays
# inside the teacher-confirmed exemption neighborhood and rejects page rules.
TEMPLATE_HANDWRITING_RECOVERY_PAD = 4
TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_PIXELS = 3
TEMPLATE_HANDWRITING_RECOVERY_STRONG_SEED_RATIO = 0.65
TEMPLATE_HANDWRITING_RECOVERY_MIN_AREA = 50
TEMPLATE_HANDWRITING_RECOVERY_MIN_SEED_RATIO = 0.15
TEMPLATE_HANDWRITING_RECOVERY_MAX_EXTENT = 0.55
TEMPLATE_HANDWRITING_RECOVERY_MIN_ELONGATION = 3.0
TEMPLATE_HANDWRITING_RECOVERY_MAX_PAGE_SPAN_RATIO = 0.25


def _template_handwriting_region_mask(
    regions: list[dict],
    width: int,
    height: int,
    *,
    pad: int = 0,
) -> np.ndarray:
    """Rasterize frozen template-handwriting boxes in canonical page space."""
    mask = np.zeros((height, width), dtype=bool)
    for item in regions:
        for left, top, right, bottom in item["regions"]:
            x1 = max(0, int(math.floor(left * width / 1000)) - pad)
            y1 = max(0, int(math.floor(top * height / 1000)) - pad)
            x2 = min(width, int(math.ceil(right * width / 1000)) + pad)
            y2 = min(height, int(math.ceil(bottom * height / 1000)) + pad)
            if x2 > x1 and y2 > y1:
                mask[y1:y2, x1:x2] = True
    return mask


def _merge_template_exemption_regions(
    answer_regions: list[dict] | None,
    template_handwriting_regions: list[dict] | None,
) -> list[dict]:
    """Use the union of answer and handwriting boxes as the exemption mask.

    Artifacts produced by older template runs stored the outer answer box and
    the tighter handwriting exemption separately.  Passing only the latter to
    the difference renderer leaves the remainder of the approved answer area
    governed by template-residue filtering, which can erase a real stroke.
    Keep one canonical per-question collection while preserving all boxes.
    """
    # Callers have already validated these collections for their respective
    # artifact contracts.  Answer-region artifacts intentionally omit
    # ``question_type`` in some historical snapshots, so do not re-run the
    # stricter answer normalizer here.
    answer = list(answer_regions or [])
    handwriting = list(template_handwriting_regions or [])
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in [*answer, *handwriting]:
        if not (item.get("regions") or []):
            continue
        number = item["question_number"]
        if number not in merged:
            merged[number] = {"question_number": number, "regions": []}
            order.append(number)
        existing = {tuple(box) for box in merged[number]["regions"]}
        for box in item.get("regions") or []:
            key = tuple(box)
            if key not in existing:
                merged[number]["regions"].append(list(box))
                existing.add(key)
    return [merged[number] for number in order]


def _erase_template_handwriting(
    template_gray: np.ndarray,
    regions: list[dict],
) -> np.ndarray:
    """Blank every pixel inside each model-approved exemption rectangle.

    The model owns the exemption boundary. Once a rectangle is accepted, no
    connected-component, shape, or printed-ink heuristic may veto part of it:
    the registered template becomes a genuinely blank baseline in that box.
    The prompt asks for a tight box with a small margin, so this operation is
    deterministic, reusable, and cheap for every subsequent student match.
    """
    if (
        not isinstance(template_gray, np.ndarray)
        or template_gray.dtype != np.uint8
        or template_gray.ndim != 2
    ):
        raise TemplateEvidenceError("template handwriting image is invalid")
    # Explicitly low-confidence model boxes are candidates for teacher review,
    # never an input to the reusable whiteout baseline.  Legacy boxes without a
    # confidence field remain trusted for backward compatibility; teacher-saved
    # boxes carry ``human_confirmed`` and bypass the model threshold.
    normalized = _validated_template_handwriting_regions(regions)
    cleaned = template_gray.copy()
    height, width = cleaned.shape
    for item in normalized:
        for left, top, right, bottom in item["regions"]:
            x1 = max(0, min(width, int(math.floor(left * width / 1000))))
            y1 = max(0, min(height, int(math.floor(top * height / 1000))))
            x2 = max(0, min(width, int(math.ceil(right * width / 1000))))
            y2 = max(0, min(height, int(math.ceil(bottom * height / 1000))))
            if x2 > x1 and y2 > y1:
                cleaned[y1:y2, x1:x2] = 255
    return cleaned


def _suppress_registered_template_residue_v2(
    new_ink: np.ndarray,
    student_gray: np.ndarray,
    template_gray: np.ndarray,
    *,
    answer_regions: list[dict] | None = None,
) -> np.ndarray:
    """Drop differences that are explained by nearby registered template ink.

    A small geometric error makes the edges of printed text and answer lines
    appear as new dark pixels even when the student wrote nothing there.  A
    global threshold cannot distinguish that residue from handwriting.  Work
    component-by-component instead: a component with no meaningful dark
    support outside the dilated template ink is residue and goes entirely.

    A component with meaningful dark support away from registered template ink
    is a real student stroke. Keep that connected stroke whole: shaving only the
    pixels near template ink cuts visible bands through handwritten symbols.
    Components without that independent support are registration residue and
    are removed as a unit.
    """
    if (
        not isinstance(new_ink, np.ndarray)
        or new_ink.dtype != bool
        or not isinstance(student_gray, np.ndarray)
        or student_gray.dtype != np.uint8
        or not isinstance(template_gray, np.ndarray)
        or template_gray.dtype != np.uint8
        or student_gray.shape != template_gray.shape
        or new_ink.shape != template_gray.shape
    ):
        raise TemplateEvidenceError("template residue inputs are invalid")

    template_core = _local_ink_mask(
        template_gray,
        contrast=TEMPLATE_INK_LOCAL_CONTRAST,
        max_gray=TEMPLATE_INK_MAX_GRAY,
    ).astype(np.uint8)
    template_neighborhood = cv2.dilate(
        template_core,
        cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (
                TEMPLATE_EDGE_DILATION_SIZE,
                TEMPLATE_EDGE_DILATION_SIZE,
            ),
        ),
        iterations=1,
    ).astype(bool)
    candidate = new_ink.astype(np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        candidate,
        connectivity=8,
    )
    cleaned = new_ink.copy()
    for index in range(1, count):
        component_left = int(stats[index, cv2.CC_STAT_LEFT])
        component_top = int(stats[index, cv2.CC_STAT_TOP])
        component_width = int(stats[index, cv2.CC_STAT_WIDTH])
        component_height = int(stats[index, cv2.CC_STAT_HEIGHT])
        component_area = int(stats[index, cv2.CC_STAT_AREA])
        x2 = component_left + component_width
        y2 = component_top + component_height
        component = (
            labels[component_top:y2, component_left:x2] == index
        )
        neighborhood_slice = template_neighborhood[
            component_top:y2,
            component_left:x2,
        ]
        edge_pixels = component & neighborhood_slice
        if not np.any(edge_pixels):
            continue
        free_pixels = component & ~neighborhood_slice
        student_slice = student_gray[
            component_top:y2,
            component_left:x2,
        ]
        dark_free_pixels = free_pixels & (student_slice <= 190)
        free_support = int(np.count_nonzero(dark_free_pixels))
        required_support = max(
            MIN_NEW_STROKE_SUPPORT_PIXELS,
            int(math.ceil(component_area * MIN_NEW_STROKE_SUPPORT_RATIO)),
        )
        if free_support < required_support:
            cleaned_slice = cleaned[
                component_top:y2,
                component_left:x2,
            ]
            cleaned_slice[component] = False
    return cleaned


def _restore_fill_line_overlap_strokes(
    new_ink: np.ndarray,
    student_gray: np.ndarray,
    template_gray: np.ndarray,
    answer_regions: list[dict] | None = None,
) -> np.ndarray:
    """Restore handwriting pixels that cross a printed fill-in line.

    The template line is intentionally removed from the OCR evidence. A
    student stroke can share pixels with that line, though, and the residue
    filter cannot distinguish an exact overlap from printed ink. Restore only
    columns with retained new-ink support on both sides of the line. This is
    deliberately narrower than restoring a padded rectangle around an answer:
    an answer written above a blank line must not bring the line back.

    ``answer_regions`` only chooses where to look. The full-column contract has
    no answer boxes, and a student writes across a printed rule there just the
    same, so the whole page is scanned when they are absent. The per-column
    two-sided support test is what keeps a blank rule out of the evidence, and
    it does not depend on knowing the answer geometry.
    """
    if (
        not isinstance(new_ink, np.ndarray)
        or new_ink.dtype != bool
        or not isinstance(student_gray, np.ndarray)
        or student_gray.dtype != np.uint8
        or not isinstance(template_gray, np.ndarray)
        or template_gray.dtype != np.uint8
        or student_gray.shape != template_gray.shape
        or new_ink.shape != template_gray.shape
        or not isinstance(answer_regions, (list, type(None)))
    ):
        raise TemplateEvidenceError("fill line overlap inputs are invalid")

    height, width = student_gray.shape
    template_ink = _local_ink_mask(
        template_gray,
        contrast=TEMPLATE_INK_LOCAL_CONTRAST,
        max_gray=TEMPLATE_INK_MAX_GRAY,
    ).astype(np.uint8)
    horizontal = cv2.morphologyEx(
        template_ink,
        cv2.MORPH_OPEN,
        np.ones((1, min(FILL_LINE_OPENING_WIDTH, width)), dtype=np.uint8),
    )
    line_band = cv2.dilate(
        horizontal,
        cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (3, FILL_LINE_VERTICAL_DILATION),
        ),
        iterations=1,
    ).astype(bool)
    if not np.any(line_band):
        return new_ink

    if answer_regions is None:
        windows: list[tuple[int, int, int, int]] = [(0, 0, width, height)]
    else:
        windows = []
        for region in answer_regions:
            if region["question_type"] != "fill":
                continue
            left, top, right, bottom = region["answer_bbox"]
            windows.append((
                max(0, int(math.floor(left * width / 1000)) - FILL_LINE_SUPPORT_PAD_X),
                max(0, int(math.floor(top * height / 1000)) - FILL_LINE_SUPPORT_PAD_Y),
                min(width, int(math.ceil(right * width / 1000)) + FILL_LINE_SUPPORT_PAD_X),
                min(height, int(math.ceil(bottom * height / 1000)) + FILL_LINE_SUPPORT_PAD_Y),
            ))

    restored = new_ink.copy()
    for x1, y1, x2, y2 in windows:
        if x2 <= x1 or y2 <= y1:
            continue
        region_lines = line_band[y1:y2, x1:x2]
        if not np.any(region_lines):
            continue
        line_count, line_labels, line_stats, _line_centroids = (
            cv2.connectedComponentsWithStats(
                region_lines.astype(np.uint8),
                connectivity=8,
            )
        )
        for line_index in range(1, line_count):
            line_area = int(line_stats[line_index, cv2.CC_STAT_AREA])
            if line_area < FILL_LINE_OPENING_WIDTH:
                continue
            line_left = int(line_stats[line_index, cv2.CC_STAT_LEFT])
            line_top = int(line_stats[line_index, cv2.CC_STAT_TOP])
            line_width = int(line_stats[line_index, cv2.CC_STAT_WIDTH])
            line_height = int(line_stats[line_index, cv2.CC_STAT_HEIGHT])
            line_x1 = max(x1, x1 + line_left)
            line_x2 = min(x2, x1 + line_left + line_width)
            line_y1 = max(y1, y1 + line_top)
            line_y2 = min(y2, y1 + line_top + line_height)
            if line_x2 <= line_x1 or line_y2 <= line_y1:
                continue

            # A printed line is usually the baseline of the answer area. A
            # real stroke crossing it leaves new-ink support above and below;
            # a normal answer sitting above it leaves support on only one side.
            above_start = max(y1, line_y1 - FILL_LINE_SUPPORT_PAD_Y)
            above_end = max(above_start, line_y1 - 1)
            below_start = min(y2, line_y2 + 1)
            below_end = min(y2, line_y2 + FILL_LINE_SUPPORT_PAD_Y)
            if above_end <= above_start or below_end <= below_start:
                continue
            support_x1 = max(x1, line_x1 - FILL_LINE_SUPPORT_PAD_X)
            support_x2 = min(x2, line_x2 + FILL_LINE_SUPPORT_PAD_X)
            above = new_ink[above_start:above_end, support_x1:support_x2]
            below = new_ink[below_start:below_end, support_x1:support_x2]
            above_count = np.count_nonzero(above, axis=0)
            below_count = np.count_nonzero(below, axis=0)
            crossing = (
                (above_count >= FILL_LINE_MIN_SIDE_SUPPORT_PIXELS)
                & (below_count >= FILL_LINE_MIN_SIDE_SUPPORT_PIXELS)
            )
            if not np.any(crossing):
                continue
            crossing_mask = np.zeros((1, crossing.size), dtype=np.uint8)
            crossing_mask[0, crossing] = 1
            crossing_mask = cv2.dilate(
                crossing_mask,
                np.ones(
                    (1, 2 * FILL_LINE_CROSSING_HORIZONTAL_PAD + 1),
                    dtype=np.uint8,
                ),
            )[0].astype(bool)
            restore_x1 = max(0, line_x1)
            restore_x2 = min(width, line_x2)
            restore_y1 = max(0, line_y1)
            restore_y2 = min(height, line_y2)
            overlap = line_labels[
                line_top:line_top + line_height,
                line_left:line_left + line_width,
            ] == line_index
            crossing_slice = crossing_mask[
                restore_x1 - support_x1:
                restore_x2 - support_x1
            ]
            restored[restore_y1:restore_y2, restore_x1:restore_x2] |= (
                overlap
                & crossing_slice[np.newaxis, :]
                & (student_gray[restore_y1:restore_y2, restore_x1:restore_x2] < 235)
            )
    return restored


def _local_ink_mask(
    gray: np.ndarray,
    *,
    contrast: int,
    max_gray: int,
) -> np.ndarray:
    """Find dark strokes relative to the surrounding photographed paper."""
    background = cv2.GaussianBlur(gray, (0, 0), 9)
    return (
        (gray.astype(np.int16) + int(contrast) < background.astype(np.int16))
        & (gray < int(max_gray))
    )


def _illumination_normalized_student_gray(
    student_gray: np.ndarray,
    template_gray: np.ndarray,
) -> np.ndarray:
    """Remove only broad exposure differences before template subtraction."""
    if (
        not isinstance(student_gray, np.ndarray)
        or student_gray.dtype != np.uint8
        or not isinstance(template_gray, np.ndarray)
        or template_gray.dtype != np.uint8
        or student_gray.shape != template_gray.shape
    ):
        raise TemplateEvidenceError("illumination normalization inputs are invalid")

    student_float = student_gray.astype(np.float32)
    template_float = template_gray.astype(np.float32)
    low_frequency_offset = cv2.GaussianBlur(
        student_float - template_float,
        (0, 0),
        TEMPLATE_ILLUMINATION_SIGMA,
    )
    low_frequency_offset = np.clip(
        low_frequency_offset,
        -TEMPLATE_ILLUMINATION_MAX_OFFSET,
        TEMPLATE_ILLUMINATION_MAX_OFFSET,
    )
    return np.clip(
        student_float - low_frequency_offset,
        0,
        255,
    ).astype(np.uint8)
