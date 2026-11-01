"""Opt-in identity inheritance: blank pages follow the previous page's student."""

import copy

from services.identity_inheritance import (
    INHERITED_REASON,
    apply_identity_inheritance,
    inheritance_upload_order_key,
    page_has_no_identity,
)


def _matched(paper_id, page_type, student_id, name):
    return {
        "paper_id": paper_id,
        "filename": f"{paper_id}.jpg",
        "student_name": name,
        "student_no": f"0{student_id}",
        "identity": {
            "status": "matched", "student_id": student_id, "reason": "unique_name",
            "candidates": [{"student_id": student_id, "name": name}], "ocr_name": name,
        },
        "identity_roster_match_status": "unique_high_confidence",
        "identity_name_written": True,
        "ocr_page_type": page_type,
    }


def _blank(paper_id, page_type, **extra):
    return {
        "paper_id": paper_id,
        "filename": f"{paper_id}.jpg",
        "student_name": "",
        "student_no": "",
        "identity": {
            "status": "needs_confirmation", "student_id": None,
            "reason": "roster_name_not_found", "ocr_name": "", "ocr_student_no": "",
            "candidates": [], "name_sources": [],
        },
        "identity_roster_match_status": "no_match",
        "identity_name_written": False,
        "ocr_page_type": page_type,
        **extra,
    }


def _uploads(papers, *extra_ids):
    return [{"paper_id": paper["paper_id"]} for paper in papers] + [{"paper_id": pid} for pid in extra_ids]


def test_pages_two_to_four_inherit_page_one_identity():
    papers = [_matched("p1", "page_1", 7, "张三"), _blank("p2", "page_2"), _blank("p3", "page_3"), _blank("p4", "page_4")]

    newly = apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert [paper["paper_id"] for paper in newly] == ["p2", "p3", "p4"]
    for paper in papers[1:]:
        assert paper["identity"]["status"] == "matched"
        assert paper["identity"]["student_id"] == 7
        assert paper["identity"]["reason"] == INHERITED_REASON
        assert paper["identity"]["inherited_from"] == {"paper_id": "p1", "filename": "p1.jpg"}
        assert (paper["student_name"], paper["student_no"]) == ("张三", "07")
    snapshot = copy.deepcopy(papers)
    assert apply_identity_inheritance(_uploads(papers), papers, enabled=True) == []
    assert papers == snapshot


def test_disabled_batches_are_untouched():
    papers = [_matched("p1", "page_1", 7, "张三"), _blank("p2", "page_2")]
    snapshot = copy.deepcopy(papers)

    assert apply_identity_inheritance(_uploads(papers), papers, enabled=False) == []
    assert papers == snapshot


def test_written_unmatched_name_and_failed_identity_never_inherit():
    unknown_name = _blank("p2", "page_2", identity_name_written=True)
    failed_identity = _blank("p3", "page_2", identity_name_written=None, identity_roster_match_status="failed")
    papers = [_matched("p1", "page_1", 7, "张三"), unknown_name, failed_identity]

    apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert not page_has_no_identity(unknown_name)
    assert unknown_name["identity"]["status"] == "needs_confirmation"
    assert "inherited_from" not in unknown_name["identity"]
    # The unknown-name page starts its own group; the next blank-looking page
    # is not blank either (its identity OCR failed), so it stands alone too.
    assert "inherit_anchor" not in failed_identity["identity"]
    assert failed_identity["identity"]["status"] == "needs_confirmation"


def test_followers_of_a_pending_page_share_its_confirmation():
    pending_anchor = _blank("p1", "page_1", identity_name_written=True)
    papers = [pending_anchor, _blank("p2", "page_2"), _blank("p3", "page_3")]

    newly = apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert newly == []
    for paper in papers[1:]:
        assert paper["identity"]["status"] == "needs_confirmation"
        assert paper["identity"]["inherit_anchor"] == {"paper_id": "p1", "filename": "p1.jpg"}


def test_a_repeated_template_page_starts_a_new_student_for_teacher_review():
    papers = [
        _matched("p1", "page_1", 7, "张三"), _blank("p2", "page_2"),
        _blank("p3", "page_1"), _blank("p4", "page_2"),
    ]

    apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert papers[1]["identity"]["student_id"] == 7
    assert papers[2]["identity"]["status"] == "needs_confirmation"
    assert papers[2]["identity"]["inherit_blocked"] == "same_page_type"
    assert papers[3]["identity"]["inherit_anchor"]["paper_id"] == "p3"


def test_unknown_page_type_does_not_inherit():
    papers = [_matched("p1", "page_1", 7, "张三"), _blank("p2", "")]

    apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert papers[1]["identity"]["status"] == "needs_confirmation"
    assert "inherit_blocked" not in papers[1]["identity"]


def test_unrecognized_pages_break_the_chain_and_excluded_pages_are_skipped():
    papers = [_matched("p1", "page_1", 7, "张三"), _blank("p3", "page_3")]
    uploads = [{"paper_id": "p1"}, {"paper_id": "p2"}, {"paper_id": "p3"}]

    apply_identity_inheritance(uploads, papers, enabled=True)
    assert papers[1]["identity"]["status"] == "needs_confirmation"
    assert "inherit_anchor" not in papers[1]["identity"]

    newly = apply_identity_inheritance(uploads, papers, enabled=True, excluded_paper_ids=["p2"])
    assert [paper["paper_id"] for paper in newly] == ["p3"]
    assert papers[1]["identity"]["student_id"] == 7


def test_ocr_failure_page_is_not_blank_and_anchors_its_followers():
    failed = _blank("p2", "", ocr_failure=True, identity_name_written=None, identity_roster_match_status="")
    papers = [_matched("p1", "page_1", 7, "张三"), failed, _blank("p3", "page_3")]

    apply_identity_inheritance(_uploads(papers), papers, enabled=True)

    assert failed["identity"]["status"] == "needs_confirmation"
    assert papers[2]["identity"]["status"] == "needs_confirmation"
    assert papers[2]["identity"]["inherit_anchor"]["paper_id"] == "p2"


def test_own_identity_is_restored_when_the_anchor_disappears():
    papers = [_matched("p1", "page_1", 7, "张三"), _blank("p2", "page_2")]
    original = copy.deepcopy(papers[1])
    apply_identity_inheritance(_uploads(papers), papers, enabled=True)
    assert papers[1]["identity"]["status"] == "matched"

    # e.g. the anchor was removed for an orientation retry
    remaining = papers[1:]
    apply_identity_inheritance(_uploads(papers), remaining, enabled=True)

    assert remaining[0] == original


def test_upload_order_key_is_natural_and_keeps_folders_together():
    paths = ["扫描_10.jpg", "扫描_2.jpg", "扫描_1.jpg", "b/1.jpg", "a/10.jpg", "a/9.jpg", "x²/1.jpg"]

    ordered = sorted(paths, key=lambda path: inheritance_upload_order_key(path, ""))

    assert ordered[:3] == ["a/9.jpg", "a/10.jpg", "b/1.jpg"]
    assert ordered.index("扫描_1.jpg") < ordered.index("扫描_2.jpg") < ordered.index("扫描_10.jpg")
    assert inheritance_upload_order_key("", "scan_2.jpg") < inheritance_upload_order_key("", "scan_10.jpg")
