import routers.grade as grade_router


def _alignment_fixture():
    options = ["A", "B", "C", "D"]
    answer_map = {
        str(index): {
            "matched": True,
            "question_type": "choice",
            "question_text": f"第 {index} 题",
            "standard_answer": options[(index - 1) % len(options)],
        }
        for index in range(1, 21)
    }
    questions = [
        {
            "question_no": str(index),
            "question_type": "choice",
            "student_answer": (
                answer_map[str(index + 1)]["standard_answer"]
                if index < 20 else ""
            ),
            "answer_state": "visible" if index < 20 else "blank",
        }
        for index in range(1, 21)
    ]
    return {
        "paper_id": "paper-1",
        "image_path": "paper.png",
        "ocr_answers_only": True,
        "questions": questions,
    }, answer_map


def test_alignment_retry_does_not_replace_a_standard_answer_shift():
    paper, _answer_map = _alignment_fixture()

    retried = grade_router._mark_unverified_answer_alignment(paper)

    assert retried is paper
    assert "ocr_alignment_verified" not in retried


def test_unreadable_answer_is_kept_for_manual_review_without_retry():
    paper = {
        "paper_id": "paper-unreadable",
        "image_path": "paper.png",
        "ocr_answers_only": True,
        "questions": [{
            "question_no": "1",
            "question_type": "choice",
            "student_answer": "[无法识别]",
            "answer_state": "unreadable",
        }],
    }
    retried = grade_router._mark_unverified_answer_alignment(paper)

    assert retried is not paper
    assert retried["ocr_alignment_retry"] == "skipped_no_spatial_evidence"
    assert retried["questions"] == paper["questions"]
    assert retried.get("ocr_alignment_verified") is not True


def test_local_recovery_matching_answer_key_returns_to_manual_review():
    paper = {
        "questions": [{
            "question_no": "10",
            "question_type": "choice",
            "student_answer": "A",
            "answer_state": "visible",
            "_ocr_local_recovery": True,
        }],
    }

    guarded = grade_router._reject_answer_key_matching_local_recoveries(
        paper,
        {"10": {"standard_answer": "A"}},
    )

    assert guarded["questions"] == [{
        "question_no": "10",
        "question_type": "choice",
        "student_answer": "[无法识别]",
        "answer_state": "unreadable",
    }]
    assert guarded["ocr_local_recovery"] == "answer_key_match_rejected"


def test_local_recovery_preserves_visible_wrong_answer_and_strips_marker():
    paper = {
        "questions": [{
            "question_no": "10",
            "question_type": "choice",
            "student_answer": "A",
            "answer_state": "visible",
            "_ocr_local_recovery": True,
        }],
    }

    guarded = grade_router._reject_answer_key_matching_local_recoveries(
        paper,
        {"10": {"standard_answer": "C"}},
    )

    assert guarded["questions"] == [{
        "question_no": "10",
        "question_type": "choice",
        "student_answer": "A",
        "answer_state": "visible",
    }]
    assert "ocr_local_recovery" not in guarded
