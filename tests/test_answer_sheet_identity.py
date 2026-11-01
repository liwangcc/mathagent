import cv2
import numpy as np

from services.answer_sheet_identity import read_choice_marks, read_exam_no_marks


def test_choice_omr_accepts_one_clear_mark_and_rejects_multiple():
    image = np.full((100, 400, 3), 255, dtype=np.uint8)
    cv2.circle(image, (150, 50), 25, (0, 0, 0), -1)
    result = read_choice_marks(image, ["A", "B", "C", "D"])
    assert result["status"] == "recognized"
    assert result["answer"] == "B"

    cv2.circle(image, (250, 50), 25, (0, 0, 0), -1)
    ambiguous = read_choice_marks(image, ["A", "B", "C", "D"])
    assert ambiguous["status"] == "needs_review"
    assert ambiguous["reason"] == "multiple_or_ambiguous_marks"


def test_exam_number_grid_preserves_leading_zero_and_rejects_weak_column():
    image = np.full((1000, 300, 3), 255, dtype=np.uint8)
    for column, digit in enumerate((0, 4, 2)):
        cv2.rectangle(
            image,
            (column * 100 + 20, digit * 100 + 20),
            (column * 100 + 80, digit * 100 + 80),
            (0, 0, 0),
            -1,
        )
    result = read_exam_no_marks(image, {"digits": 3})
    assert result["status"] == "recognized"
    assert result["value"] == "042"

    image[:, 100:200] = 255
    weak = read_exam_no_marks(image, {"digits": 3})
    assert weak["status"] == "needs_review"