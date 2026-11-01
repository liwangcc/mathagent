"""Front/back pairing of student answer-sheet pages by upload path.

A batch is paired by folder, side-marked file name or roster name only when
every page fits that rule; anything else stays on the upload-order pairing.
Identity from the path goes through the roster-scoped collection resolver and
never overrides a disagreeing page.
"""
import io
from types import SimpleNamespace

import pytest

from routers import answer_sheet as router_module
from services.answer_sheet_page_pairing import (
    PATH_PAIRED_STATUS,
    apply_path_pair_identity,
    filename_side_pairs,
    folder_pairs,
    parse_side_filename,
)
from services.answer_sheet_session import AnswerSheetSession
from tests.test_answer_sheet_session import _png_bytes, _student_upload_session, client_env  # noqa: F401

LAYOUT = ["page_1", "page_2"]
ROSTER = [
    {"student_id": 1, "name": "张三", "student_no": "2021001", "class_id": 7},
    {"student_id": 2, "name": "李四", "student_no": "2021002", "class_id": 7},
    {"student_id": 3, "name": "王五", "student_no": "2021003", "class_id": 7},
]


def _page(order, path, page_id, identity=None, reasons=None):
    return {
        "paper_id": f"student_{order}",
        "physical_order": order,
        "filename": path.rsplit("/", 1)[-1],
        "source_relative_path": path,
        "page_id": page_id,
        "identity": identity or {"status": "needs_review", "student_id": None, "candidates": []},
        "review_reasons": list(reasons if reasons is not None else ["identity_requires_teacher_confirmation"]),
    }


def _recognized(student_id, name):
    return {"status": "recognized", "student_id": student_id, "student_name": name, "candidates": []}


def _ids(pages):
    return {page["paper_id"]: page["identity"]["student_id"] for page in pages}


@pytest.mark.parametrize(("filename", "expected"), [
    ("张三_正面.jpg", ("张三", "front")),
    ("张三-反面.png", ("张三", "back")),
    ("张三背面.ocr-ready-v1.jpg", ("张三", "back")),
    ("张三 正.jpg", ("张三", "front")),
    ("张三（反）.jpg", ("张三", "back")),
    ("【张三】背.jpg", ("张三", "back")),
    ("2021001_Front.JPG", ("2021001", "front")),
    ("张三back.jpg", ("张三", "back")),
    ("王正正面.jpg", ("王正", "front")),
    ("dir/sub\\李四_反.jpg", ("李四", "back")),
])
def test_explicit_side_suffixes_parse(filename, expected):
    assert parse_side_filename(filename) == expected


@pytest.mark.parametrize("filename", [
    "张三.jpg", "张三_1.jpg", "张三_2.jpg", "feedback.jpg", "scan_front_001.jpg",
    "张三_正面(1).jpg", "正面.jpg", "_反.jpg", "", None,
])
def test_names_without_an_explicit_trailing_side_do_not_parse(filename):
    assert parse_side_filename(filename) is None


def test_side_named_pairs_ordered_by_first_upload_position():
    pages = [
        _page(1, "李四_反面.jpg", "page_2"),
        _page(2, "张三_正面.jpg", "page_1"),
        _page(3, "李四_正面.jpg", "page_1"),
        _page(4, "张三_反面.jpg", "page_2"),
    ]
    assert [(front["paper_id"], back["paper_id"]) for front, back in filename_side_pairs(pages)] == [
        ("student_3", "student_1"),
        ("student_2", "student_4"),
    ]


@pytest.mark.parametrize("paths", [
    ["scan_001.jpg", "scan_002.jpg", "scan_003.jpg", "scan_004.jpg"],
    ["扫描/001_正面.jpg", "扫描/001_反面.jpg", "扫描/002.jpg", "扫描/002_反面.jpg"],
    ["收集.zip/张三/a.jpg", "收集.zip/张三/b.jpg", "收集.zip/张三/c.jpg", "收集.zip/李四/a.jpg"],
    ["收集.zip/张三_1.jpg", "收集.zip/张三_2.jpg", "收集.zip/张山_1.jpg", "收集.zip/张山_2.jpg"],
])
def test_batch_that_no_path_rule_fully_covers_stays_on_upload_order(paths):
    pages = [_page(index, path, LAYOUT[(index - 1) % 2]) for index, path in enumerate(paths, 1)]
    snapshot = [dict(page) for page in pages]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is False
    assert pages == snapshot


def test_student_folders_pair_by_folder_and_side_follows_page_type():
    pages = [
        _page(1, "收集.zip/张三/IMG_1.jpg", "page_2"),
        _page(2, "收集.zip/张三/IMG_2.jpg", "page_1"),
        _page(3, "收集.zip/李四/a.jpg", "page_1"),
        _page(4, "收集.zip/李四/b.jpg", "page_2"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert _ids(pages) == {"student_1": 1, "student_2": 1, "student_3": 2, "student_4": 2}
    assert [page["pairing"]["side"] for page in pages] == ["back", "front", "front", "back"]
    for page in pages:
        assert page["pairing"]["mode"] == "folder_pairs"
        assert page["pairing"]["status"] == "paired"
        assert page["identity"]["status"] == PATH_PAIRED_STATUS
        assert page["identity"]["source"] == "path"
        assert page["identity"]["clue_source"] == "folder"
        assert page["review_reasons"] == []


def test_dragged_student_folders_pair_but_top_folder_is_not_an_identity():
    pages = [
        _page(1, "张三/a.jpg", "page_1", _recognized(1, "张三"), reasons=[]),
        _page(2, "张三/b.jpg", "page_2"),
        _page(3, "李四/a.jpg", "page_1"),
        _page(4, "李四/b.jpg", "page_2"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert pages[1]["identity"]["student_id"] == 1
    assert pages[1]["identity"]["source"] == "paired_page"
    for page in pages[2:]:
        assert page["pairing"]["status"] == "identity_missing"
        assert "path_pair_identity_missing" in page["review_reasons"]


def test_a_named_file_identifies_its_unnamed_folder_partner():
    pages = [_page(1, "收集.zip/随机1/张三_1.jpg", "page_1"), _page(2, "收集.zip/随机1/IMG_9.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert _ids(pages) == {"student_1": 1, "student_2": 1}
    assert [page["identity"]["clue_source"] for page in pages] == ["filename", "filename"]


def test_folder_rule_wins_and_names_that_disagree_inside_a_folder_conflict():
    pages = [
        _page(1, "包1/张三_正.jpg", "page_1"),
        _page(2, "包1/李四_反.jpg", "page_2"),
        _page(3, "包2/李四_正.jpg", "page_1"),
        _page(4, "包2/张三_反.jpg", "page_2"),
    ]
    assert folder_pairs(pages) is not None
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert page["pairing"]["mode"] == "folder_pairs"
        assert page["pairing"]["status"] == "identity_conflict"
        assert page["identity"]["student_id"] is None


def test_path_naming_two_students_is_a_conflict():
    pages = [_page(1, "收集.zip/张三/李四_1.jpg", "page_1"), _page(2, "收集.zip/张三/b.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert {page["pairing"]["status"] for page in pages} == {"identity_conflict"}


def test_two_fronts_in_one_folder_do_not_pair():
    pages = [_page(1, "收集.zip/张三/a.jpg", "page_1"), _page(2, "收集.zip/张三/b.jpg", "page_1")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert page["identity"]["student_id"] is None
        assert page["pairing"]["side"] is None
        assert page["pairing"]["status"] == "page_type_mismatch"
        assert "path_pair_page_type_mismatch" in page["review_reasons"]


def test_side_word_contradicting_page_type_blocks_a_folder_pair():
    pages = [_page(1, "收集.zip/张三/张三_正面.jpg", "page_2"), _page(2, "收集.zip/张三/IMG_2.jpg", "page_1")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert page["pairing"]["status"] == "page_type_mismatch"
        assert page["identity"]["student_id"] is None


def test_flat_collection_names_pair_by_roster_student():
    pages = [
        _page(1, "收集.zip/张三_1.jpg", "page_1"),
        _page(2, "收集.zip/李四_1.jpg", "page_2"),
        _page(3, "收集.zip/张三_2.jpg", "page_2"),
        _page(4, "收集.zip/李四_2.jpg", "page_1"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert _ids(pages) == {"student_1": 1, "student_2": 2, "student_3": 1, "student_4": 2}
    assert {page["pairing"]["mode"] for page in pages} == {"roster_name_pairs"}
    assert [page["pairing"]["side"] for page in pages] == ["front", "back", "back", "front"]


def test_unique_roster_name_in_side_named_files_identifies_both_pages():
    pages = [_page(1, "张三_正面.jpg", "page_1"), _page(2, "张三_反面.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page, side, partner in ((pages[0], "front", "student_2"), (pages[1], "back", "student_1")):
        assert page["identity"]["status"] == PATH_PAIRED_STATUS
        assert (page["identity"]["student_id"], page["identity"]["student_name"]) == (1, "张三")
        assert (page["identity"]["source"], page["identity"]["clue_source"]) == ("path", "filename")
        assert page["review_reasons"] == []
        assert page["pairing"] == {
            "mode": "filename_side_pairs", "pair_index": 1, "side": side,
            "status": "paired", "paired_paper_id": partner,
        }


def test_complete_student_number_in_file_name_identifies_the_pair():
    pages = [_page(1, "学号2021002_正.jpg", "page_1"), _page(2, "学号2021002_反.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert set(_ids(pages).values()) == {2}


def test_unresolved_base_name_still_pairs_and_back_takes_front_identity():
    pages = [
        _page(1, "001_正面.jpg", "page_1", _recognized(3, "王五"), reasons=[]),
        _page(2, "001_反面.jpg", "page_2"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert pages[0]["identity"]["status"] == "recognized"
    back = pages[1]["identity"]
    assert back["status"] == PATH_PAIRED_STATUS
    assert (back["student_id"], back["source"], back["source_paper_id"]) == (3, "paired_page", "student_1")
    assert pages[1]["review_reasons"] == []


def test_back_identity_may_identify_the_front_of_the_same_pair():
    pages = [
        _page(1, "001_正面.jpg", "page_1"),
        _page(2, "001_反面.jpg", "page_2", _recognized(2, "李四"), reasons=[]),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert pages[0]["identity"]["student_id"] == 2
    assert pages[0]["identity"]["source_paper_id"] == "student_2"


def test_no_identity_anywhere_goes_to_the_teacher():
    pages = [_page(1, "001_正面.jpg", "page_1"), _page(2, "001_反面.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert page["identity"]["status"] == "needs_review"
        assert page["pairing"]["status"] == "identity_missing"
        assert "path_pair_identity_missing" in page["review_reasons"]
        assert "identity_requires_teacher_confirmation" in page["review_reasons"]


def test_path_name_disagreeing_with_recognized_page_is_a_conflict():
    pages = [
        _page(1, "张三_正面.jpg", "page_1", _recognized(2, "李四"), reasons=[]),
        _page(2, "张三_反面.jpg", "page_2"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert pages[0]["identity"]["student_id"] == 2
    assert pages[1]["identity"]["student_id"] is None
    for page in pages:
        assert page["pairing"]["status"] == "identity_conflict"
        assert "path_pair_identity_conflict" in page["review_reasons"]


def test_two_side_names_resolving_differently_is_a_conflict():
    roster = [*ROSTER, {"student_id": 9, "name": "张三正", "student_no": "", "class_id": 7}]
    pages = [_page(1, "张三正.jpg", "page_1"), _page(2, "张三反.jpg", "page_2")]
    assert apply_path_pair_identity(pages, LAYOUT, roster) is True
    assert {page["pairing"]["status"] for page in pages} == {"identity_conflict"}
    assert all(page["identity"]["student_id"] is None for page in pages)


def test_teacher_confirming_both_pages_settles_a_path_conflict():
    teacher = {"status": "teacher_confirmed", "student_id": 2, "student_name": "李四"}
    pages = [
        _page(1, "张三_正面.jpg", "page_1", dict(teacher), reasons=[]),
        _page(2, "张三_反面.jpg", "page_2", dict(teacher), reasons=[]),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert (page["identity"]["status"], page["identity"]["student_id"]) == ("teacher_confirmed", 2)
        assert page["pairing"]["status"] == "paired"
        assert page["review_reasons"] == []


def test_one_teacher_confirmation_against_the_path_still_asks_for_the_other_page():
    pages = [
        _page(1, "张三_正面.jpg", "page_1",
              {"status": "teacher_confirmed", "student_id": 2, "student_name": "李四"}, reasons=[]),
        _page(2, "张三_反面.jpg", "page_2"),
    ]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert pages[1]["identity"]["student_id"] is None
    assert "path_pair_identity_conflict" in pages[1]["review_reasons"]


def test_side_word_must_match_the_template_page_type():
    pages = [_page(1, "张三_正面.jpg", "page_2"), _page(2, "张三_反面.jpg", "page_1")]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    for page in pages:
        assert page["identity"]["student_id"] is None
        assert page["pairing"]["status"] == "page_type_mismatch"
        assert "path_pair_page_type_mismatch" in page["review_reasons"]


def test_rerun_drops_identity_that_no_longer_has_a_source():
    pages = [
        _page(1, "001_正面.jpg", "page_1", _recognized(3, "王五"), reasons=[]),
        _page(2, "001_反面.jpg", "page_2"),
    ]
    apply_path_pair_identity(pages, LAYOUT, ROSTER)
    assert pages[1]["identity"]["student_id"] == 3
    pages[0]["identity"] = {"status": "needs_review", "student_id": None, "candidates": []}
    apply_path_pair_identity(pages, LAYOUT, ROSTER)
    assert pages[1]["identity"]["status"] == "needs_review"
    assert pages[1]["identity"]["student_id"] is None
    assert "source_paper_id" not in pages[1]["identity"]
    assert "path_pair_identity_missing" in pages[1]["review_reasons"]


def test_only_two_page_templates_pair_by_path():
    pages = [_page(1, "张三_正面.jpg", "page_1"), _page(2, "张三_反面.jpg", "page_2")]
    assert apply_path_pair_identity(pages, ["page_1"], ROSTER) is False
    assert apply_path_pair_identity(pages, ["p1", "p2", "p3", "p4"], ROSTER) is False


def test_pages_uploaded_before_paths_were_kept_still_pair_by_file_name():
    pages = [_page(1, "张三_正面.jpg", "page_1"), _page(2, "张三_反面.jpg", "page_2")]
    for page in pages:
        del page["source_relative_path"]
    assert apply_path_pair_identity(pages, LAYOUT, ROSTER) is True
    assert set(_ids(pages).values()) == {1}


def _session(pages):
    return SimpleNamespace(
        student_pages=pages,
        answer_sheet_pages=[{"page_id": "page_1"}, {"page_id": "page_2"}],
        student_upload={"pairing_mode": "ordered_duplex"},
    )


def _roster_rows():
    return [
        SimpleNamespace(id=row["student_id"], name=row["name"], student_no=row["student_no"], class_id=7)
        for row in ROSTER
    ]


def test_router_pairs_by_path_when_every_page_fits_a_rule():
    pages = [
        _page(1, "张三_反面.jpg", "page_2"),
        _page(2, "李四_正面.jpg", "page_1"),
        _page(3, "李四_反面.jpg", "page_2"),
        _page(4, "张三_正面.jpg", "page_1"),
    ]
    router_module._apply_student_page_pairing(_session(pages), _roster_rows())
    assert _ids(pages) == {"student_1": 1, "student_2": 2, "student_3": 2, "student_4": 1}
    assert PATH_PAIRED_STATUS in router_module._IDENTITY_READY_STATUSES


def test_router_falls_back_to_upload_order_without_a_path_rule():
    pages = [
        _page(1, "scan_001.jpg", "page_1", _recognized(1, "张三"), reasons=[]),
        _page(2, "scan_002.jpg", "page_2"),
    ]
    router_module._apply_student_page_pairing(_session(pages), _roster_rows())
    assert pages[1]["identity"]["status"] == "ordered_pair_inherited"
    assert pages[1]["identity"]["student_id"] == 1
    assert pages[1]["identity"]["source"] == "physical_order_front"
    assert pages[1]["pairing"]["mode"] == "ordered_duplex"


def test_student_upload_keeps_the_import_path_and_rejects_unsafe_paths(client_env):  # noqa: F811
    make_client, upload_dir = client_env
    client = make_client(1)
    session = _student_upload_session(upload_dir, "8" * 16)
    upload_id = client.post(
        f"/api/answer-sheet/sessions/{session.session_id}/students/upload/start",
        json={"restart": False},
    ).json()["student_upload"]["upload_id"]
    url = f"/api/answer-sheet/sessions/{session.session_id}/students/upload"
    raw = _png_bytes().getvalue()

    for unsafe in ["../张三/a.png", "/张三/a.png", r"C:\张三\a.png", "包//a.png"]:
        rejected = client.post(
            url,
            data={"upload_id": upload_id, "source_path": unsafe},
            files=[("files", ("a.png", io.BytesIO(raw), "image/png"))],
        )
        assert rejected.status_code == 400, unsafe

    accepted = client.post(
        url,
        data={"upload_id": upload_id, "source_path": r"收集.zip\张三\a.png"},
        files=[("files", ("a.png", io.BytesIO(raw), "image/png"))],
    )
    assert accepted.status_code == 200, accepted.text
    staged = AnswerSheetSession.load(session.session_id, upload_dir).student_upload["pages"]
    assert [page["source_relative_path"] for page in staged] == ["收集.zip/张三/a.png"]
