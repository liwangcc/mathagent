import json

import pytest

from services.collection_identity import parse_source_paths, resolve_collection_identity


ROSTER = [
    {"student_id": 1, "name": "张三", "student_no": "08", "class_id": 1},
    {"student_id": 2, "name": "李四", "student_no": "56", "class_id": 1},
    {"student_id": 3, "name": "王平", "student_no": "09", "class_id": 1},
    {"student_id": 4, "name": "阿卜杜拉·阿里", "student_no": "10", "class_id": 1},
    {"student_id": 5, "name": "𠮷小明", "student_no": "11", "class_id": 1},
]


def resolve(path, roster=ROSTER, **kwargs):
    return resolve_collection_identity(roster, filename=path.rsplit("/", 1)[-1], source_relative_path=path, **kwargs)


@pytest.mark.parametrize("path", [
    "收集.zip/张三/ab9567.jpg", "主文件夹/张三08/a1b2.jpg",
    "收集.zip/张三(1)/IMG_0001.jpg", "收集.zip/张三(2)/IMG_0001.jpg",
    "收集.zip/张三_正/IMG_0001.jpg", "主文件夹/张三背/abcdef.jpg",
    "收集.zip/随机文件夹1/张三_1.jpg", "收集.zip/随机文件夹2/张三_2.jpg",
    "主文件夹/随机文件夹1/张三08(2).jpg", "收集.zip/张三.jpg",
    "主文件夹/张三1.jpg", "收集.zip/张三正.jpg", "收集.zip/张三背面.jpg",
    "收集.zip/张三_1699999999.jpg", "收集.zip/张三_123456.jpg",
    "收集.zip/张三_20260909.jpg", "张三.ocr-ready-v1.jpg",
    "收集.zip/数学作业_张三_1eax24.jpg",
])
def test_three_collection_layouts_use_unique_name_without_reading_number(path, monkeypatch):
    def numbers_must_not_run(*_args, **_kwargs):
        raise AssertionError("a unique name must not trigger number parsing")

    monkeypatch.setattr("services.collection_identity._component_numbers", numbers_must_not_run)
    identity = resolve(path)
    assert identity["status"] == "matched"
    assert identity["student_id"] == 1
    assert identity["resolved_student_no"] == "08"
    assert identity["number_used"] is False


@pytest.mark.parametrize("path,student_id", [
    ("收集.zip/王平_2.jpg", 3), ("收集.zip/阿卜杜拉·阿里/abc.jpg", 4),
    ("收集.zip/𠮷小明背面/abc.jpg", 5),
])
def test_real_roster_names_have_no_two_to_four_character_or_stopword_limit(path, student_id):
    assert resolve(path)["student_id"] == student_id


def test_unique_ocr_name_ignores_wrong_number_and_collection_serial():
    result = resolve("收集.zip/收集件_123456.jpg", ocr_name="张三", ocr_student_no="56")
    assert result["student_id"] == 1
    assert result["number_used"] is False


@pytest.mark.parametrize("path,ocr_name", [
    ("收集.zip/张三/李四_1.jpg", ""),
    ("收集.zip/张三/a.jpg", "李四"),
    ("收集.zip/李四_1.jpg", "张三"),
    ("收集.zip/张三_李四.jpg", ""),
])
def test_different_known_names_require_confirmation_even_with_a_number(path, ocr_name):
    result = resolve(path, ocr_name=ocr_name, ocr_student_no="08")
    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "identity_name_conflict"
    assert not result.get("confirmation_group")
    assert {item["student_id"] for item in result["candidates"]} == {1, 2}


TWINS = [
    {"student_id": 10, "name": "张三", "student_no": "08", "class_id": 1},
    {"student_id": 11, "name": "张三", "student_no": "09", "class_id": 1},
]


@pytest.mark.parametrize("path,student_id", [
    ("收集.zip/张三08/abcdef.jpg", 10), ("收集.zip/张三09_2.jpg", 11),
    ("收集.zip/张三_学号09_2.jpg", 11),
    ("主文件夹/随机目录/张三09(2).jpg", 11), ("收集.zip/08张三/IMG_99999999.jpg", 10),
])
def test_short_exact_roster_numbers_only_disambiguate_same_name(path, student_id):
    result = resolve(path, TWINS)
    assert result["student_id"] == student_id
    assert result["number_used"] is True


@pytest.mark.parametrize("suffix", ["1", "(2)", "08", "09_2", "20260909", "123456", "1699999999", "000008a"])
def test_suffixes_cannot_select_a_short_roster_number(suffix):
    result = resolve(f"收集.zip/张三_{suffix}.jpg", TWINS)
    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "name_ambiguous"


def test_same_directory_has_one_confirmation_group_but_other_directories_do_not():
    first = resolve("收集.zip/随机目录1/张三_1.jpg", TWINS)
    second = resolve("收集.zip/随机目录1/张三_2.jpg", TWINS)
    other = resolve("收集.zip/随机目录2/张三_1.jpg", TWINS)
    assert first["confirmation_group"] == second["confirmation_group"]
    assert other["confirmation_group"] != first["confirmation_group"]
    assert not resolve("收集.zip/张三_1.jpg", TWINS).get("confirmation_group")


def test_plain_random_filenames_do_not_supply_student_numbers():
    for path in ["收集.zip/08.jpg", "主文件夹/随机目录/IMG_56.jpg", "收集.zip/1699999999.jpg", "收集.zip/目录08/abc.jpg", "收集.zip/收集件08/abc.jpg"]:
        assert resolve(path)["status"] == "needs_confirmation"


def test_mistyped_folder_name_can_use_an_exact_full_student_number():
    assert resolve("收集.zip/张山08/abc.jpg")["student_id"] == 1


@pytest.mark.parametrize("path", [
    "作业/张三.zip/1.jpg", "作业/张三.rar/1.jpg", "作业/张三.ZIP/1.jpg",
    "作业/张三.zip (2)/1.jpg", "多项导入/张三/1.jpg", "多项导入/张三.zip/1.jpg",
])
def test_archive_or_folder_below_the_top_level_names_its_contents(path):
    assert resolve(path)["student_id"] == 1


def test_archive_below_the_top_level_conflicts_with_a_different_inner_name():
    assert resolve("作业/张三.zip/李四_1.jpg")["reason"] == "identity_name_conflict"


def test_main_folder_name_is_not_an_identity_or_a_class_selection():
    assert resolve("张三/随机目录/李四_1.jpg")["student_id"] == 2
    assert resolve("张三/随机目录/abc.jpg")["status"] == "needs_confirmation"


def test_two_matching_numbers_are_not_silently_resolved_or_grouped():
    result = resolve("收集.zip/张三08/张三09.jpg", TWINS)
    assert result["reason"] == "identity_number_conflict"
    assert not result.get("confirmation_group")


def test_real_longer_name_is_not_truncated_to_a_roster_prefix():
    students = [dict(ROSTER[0], name="王伟"), dict(ROSTER[1], name="王军")]
    assert resolve("收集.zip/王军_1.jpg", students)["student_id"] == 2
    assert resolve("收集.zip/王军_1.jpg", students[:1])["status"] == "needs_confirmation"


def test_low_confidence_ocr_alone_still_requires_teacher():
    result = resolve("IMG_001.jpg", ocr_name="张三", roster_match_status="low_confidence")
    assert result["reason"] == "roster_name_low_confidence"


def test_transport_preserves_order_same_basenames_and_windows_separators():
    assert parse_source_paths(json.dumps(["包.zip/张三/a.jpg", r"包.zip\李四\a.jpg"]), 2) == [
        "包.zip/张三/a.jpg", "包.zip/李四/a.jpg",
    ]
    assert parse_source_paths("", 2) == ["", ""]


@pytest.mark.parametrize("paths", [
    ["../张三/a.jpg"], ["/张三/a.jpg"], [r"C:\张三\a.jpg"], ["包/../a.jpg"],
    ["包//a.jpg"], ["包/./a.jpg"], ["包/张三/\0.jpg"], ["a" * 2049],
    ["/".join(["a"] * 33)], [None], {"path": "张三/a.jpg"}, [], ["a.jpg", "b.jpg"],
])
def test_source_paths_reject_unsafe_or_misaligned_metadata(paths):
    with pytest.raises(ValueError):
        parse_source_paths(json.dumps(paths), 1)


def test_source_paths_reject_bad_json():
    with pytest.raises(ValueError):
        parse_source_paths("{", 1)
