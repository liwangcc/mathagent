"""Keep a template page artifact consistent with the page's questions.

An artifact is keyed by question number throughout, so it has to follow every
edit of the questions:

- ``_question_numbers`` gives the numbers a page's artifact and crop plan
  expect.
- ``_synchronized_page_artifact`` rebuilds one artifact around an edited
  question list, keeping each renamed question's boxes.
- ``_refresh_template_difference_artifact`` re-renders the hash-bound
  difference image from the approved answer/handwriting boxes.
- ``_validate_template_drawing_geometry`` checks the drawing labels against
  the page.

Moved unchanged from routers/grade_template.py, which still exposes these names.
"""

import copy

from routers.grade_contract import _template_handwriting_regions_for_mode
from services.template_anchor_crops import validate_anchor_crop_plan
from services.template_evidence import (
    materialize_template_difference_artifact,
    normalize_question_anchors,
    normalize_template_handwriting_regions,
)
from services.template_session import normalize_template_mode


def _refresh_template_difference_artifact(
    template_dir: str,
    artifact: dict,
    *,
    template_mode: object = "marked",
    raw_regions: object = None,
) -> dict:
    """Keep the reusable whiteout baseline aligned with approved boxes.

    The original registration image remains intact for SIFT. Both template
    modes get a hash-bound difference image whenever their visual contract
    contains handwriting metadata, including the explicit empty-box baseline.
    Legacy blank artifacts without that field remain readable and are not
    retroactively given synthetic boxes.
    """
    updated = dict(artifact)
    expected = [
        str(value).strip()
        for value in (updated.get("question_numbers") or [])
        if str(value).strip()
    ]
    mode = normalize_template_mode(template_mode, default="marked")
    # The unified answer-box collection is the exemption contract for every
    # legacy mode label; never zero it for "blank". When the stored contract
    # is missing or empty, the confirmed answer boxes themselves carry it.
    has_region_contract = (
        raw_regions is not None
        or "template_handwriting_regions" in updated
        or "template_answer_regions" in updated
    )
    if mode == "blank" and not has_region_contract:
        # Old blank artifacts intentionally had no exemption contract. Keep
        # them compatible until a teacher reimports and confirms a new one.
        return updated
    stored_regions = updated.get("template_handwriting_regions")
    if raw_regions is not None:
        contract_regions = raw_regions
    elif stored_regions:
        contract_regions = stored_regions
    else:
        # A stored empty list cannot mean "erase the boxes": fall back to
        # the confirmed answer boxes so one collection serves both roles.
        contract_regions = updated.get("template_answer_regions")
    normalized = _template_handwriting_regions_for_mode(
        template_mode,
        contract_regions,
        expected,
    )
    updated["template_handwriting_regions"] = normalized
    updated.pop("difference_template_image", None)
    updated.pop("difference_template_sha256", None)
    return materialize_template_difference_artifact(template_dir, updated)


def _synchronized_page_artifact(
    artifact: dict,
    page: dict,
    renames: dict[str, str],
    *,
    added_number: str = "",
    before_number: str = "",
) -> dict:
    """Rebuild one page artifact around an edited question list.

    Every collection in an artifact is keyed by question number and asserted to
    cover the page's questions exactly, so a question edit has to move all of
    them together or the next validation rejects the template.  A renamed
    question keeps its boxes: the ink they mark did not move, only its label.
    """
    numbers = [
        str(
            question.get("question_no")
            or question.get("question_number")
            or ""
        ).strip()
        for question in (page.get("questions") or [])
    ]
    numbers = [number for number in numbers if number]
    updated = dict(artifact)
    updated["question_numbers"] = list(numbers)
    from services.template_evidence import normalize_question_schema
    updated["question_schema"] = normalize_question_schema(page.get("questions") or [])
    if "question_anchors" in artifact:
        existing = normalize_question_anchors(artifact.get("question_anchors") or [])
        renamed = {
            renames.get(anchor["question_number"], anchor["question_number"]): anchor
            for anchor in existing
        }
        anchors: list[dict] = []
        for number in numbers:
            found = renamed.get(number)
            if found is not None:
                # Relabelling a question must not discard the confidence and
                # provenance attached to its server-owned geometry.
                preserved = dict(found)
                preserved["question_number"] = number
                if "question_number_bbox" in found:
                    # Preserve legacy geometry while loading an old artifact;
                    # new model responses do not create this field.
                    preserved["question_number_bbox"] = list(
                        found["question_number_bbox"]
                    )
                anchors.append(preserved)
            elif number == added_number:
                # This conspicuous top-of-page handle is an editable draft,
                # NOT a located printed number. A review gate below prevents
                # freezing until the teacher saves the corrected location.
                new_anchor = {"question_number": number}
                if artifact.get("question_label_policy"):
                    new_anchor["question_number_bbox"] = [80, 20, 120, 45]
                anchors.append(new_anchor)
                updated["geometry_review_pending"] = True
        updated["question_anchors"] = normalize_question_anchors(anchors)
    if "template_handwriting_regions" in artifact:
        existing_regions = normalize_template_handwriting_regions(
            artifact.get("template_handwriting_regions") or []
        )
        renamed_regions = {
            renames.get(item["question_number"], item["question_number"]): item
            for item in existing_regions
        }
        synchronized_regions = []
        for number in numbers:
            found = renamed_regions.get(number)
            if found is None:
                synchronized_regions.append({
                    "question_number": number,
                    "regions": [],
                })
                continue
            # Keep confidence/source/teacher-confirmed markers alongside the
            # moved rectangles; only the question label changes.
            preserved = dict(found)
            preserved["question_number"] = number
            preserved["regions"] = [list(box) for box in (found.get("regions") or [])]
            synchronized_regions.append(preserved)
        updated["template_handwriting_regions"] = normalize_template_handwriting_regions(
            synchronized_regions
        )
    if "template_answer_regions" in artifact:
        answer_by_number = {
            renames.get(item["question_number"], item["question_number"]): item
            for item in normalize_template_handwriting_regions(artifact["template_answer_regions"])
        }
        updated["template_answer_regions"] = [
            {**copy.deepcopy(answer_by_number.get(number) or {"regions": []}), "question_number": number}
            for number in numbers
        ]
    updated.pop("difference_template_image", None)
    updated.pop("difference_template_sha256", None)
    raw_plan = artifact.get("question_crop_plan")
    if isinstance(raw_plan, dict):
        synchronized_plan = dict(raw_plan)
        synchronized_plan["expected_question_numbers"] = list(numbers)
        updated["question_crop_plan"] = validate_anchor_crop_plan(
            synchronized_plan,
            artifact_sha256=str(artifact.get("sha256") or ""),
        )
    return updated


def _validate_template_drawing_geometry(artifact: dict, template_mode: str) -> None:
    from services.template_evidence import validate_drawing_label_geometry
    validate_drawing_label_geometry(
        artifact.get("question_schema") or [], artifact.get("question_anchors") or [],
        artifact.get("template_answer_regions") or [], artifact.get("template_handwriting_regions") or [],
        page_width=artifact["width"], page_height=artifact["height"],
        question_crop_plan=artifact.get("question_crop_plan"), template_mode=template_mode,
    )


def _question_numbers(questions: list[dict]) -> list[str]:
    """Each question's number (question_no, else question_number); blanks dropped."""
    numbers = (
        str(question.get("question_no") or question.get("question_number") or "").strip()
        for question in questions
    )
    return [number for number in numbers if number]
