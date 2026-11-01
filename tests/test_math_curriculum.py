"""Offline checks for the versioned, stage-isolated mathematics catalogs."""

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from services import math_curriculum as curriculum


CATALOG_ROOT = Path(__file__).resolve().parents[1] / "references" / "math_curricula"


def _raw(stage):
    return json.loads((CATALOG_ROOT / f"{stage}.v1.json").read_text(encoding="utf-8"))


def _sources():
    return json.loads((CATALOG_ROOT / "sources.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("stage", ["junior", "senior"])
def test_catalog_shape_and_stable_ids(stage):
    catalog = curriculum.get_math_catalog(stage)
    assert len(catalog) >= 18
    assert sum(len(module["topics"]) for module in catalog) >= 130
    ids = []
    chapters = []
    for module in catalog:
        ids.append(module["id"])
        chapters.append(module["chapter"])
        assert module["id"].startswith(f"{stage}.math.")
        assert module["domain"]
        assert not re.search(r"第[一二三四五六七八九十\d]+[章册]", module["chapter"])
        assert set(module["topics"]) == set(module["topic_ids"])
        assert set(module["topics"]) == set(module["topic_scopes"])
        assert set(module["topics"]) == set(module["topic_courses"])
        assert len(module["topics"]) == len(set(module["topics"]))
        for name in module["topics"]:
            ids.append(module["topic_ids"][name])
            point = curriculum.get_knowledge_point(stage, module["chapter"], name)
            assert point["id"] == module["topic_ids"][name]
            assert point["stage"] == stage
            assert point["catalog_version"] == curriculum.catalog_version(stage)
            if point["origin"] == "supplement":
                # Supplements never borrow the module's verified curriculum pages.
                assert point["scope"] == "extension" and point["basis"] and point["note"]
                assert point["source_refs"] == []
            else:
                assert point["source_refs"]
    assert len(ids) == len(set(ids))
    assert len(chapters) == len(set(chapters))
    assert re.fullmatch(rf"math:{stage}:2026-09-09\.v1:[0-9a-f]{{12}}", curriculum.catalog_version(stage))


def test_no_ids_are_shared_between_stages():
    ids = {}
    for stage in ("junior", "senior"):
        ids[stage] = {value for module in curriculum.get_math_catalog(stage)
                      for value in module["topic_ids"].values()}
    assert ids["junior"].isdisjoint(ids["senior"])
    assert curriculum.catalog_version("junior") != curriculum.catalog_version("senior")


@pytest.mark.parametrize("stage", [None, "", "legacy", "math", "初中", "高中", "Junior", " junior", "senior ", "../senior", 1, True, [], {}])
def test_invalid_stage_fails_closed(stage):
    for action in (
        lambda: curriculum.get_math_catalog(stage),
        lambda: curriculum.catalog_version(stage),
        lambda: curriculum.get_knowledge_point(stage, "", ""),
    ):
        with pytest.raises(ValueError, match="education_stage"):
            action()


def test_exact_match_does_not_guess_or_cross_stages():
    assert curriculum.get_knowledge_point("junior", "有理数与实数", "有理数与数轴")
    assert curriculum.get_knowledge_point("senior", "有理数与实数", "有理数与数轴") is None
    assert curriculum.get_knowledge_point("junior", "导数及其应用", "导数的运算") is None
    assert curriculum.get_knowledge_point("junior", "有理数与实数", "有理数与数轴 ") is None
    assert curriculum.get_knowledge_point("junior", "一元二次方程", "韦达定理") is None
    assert curriculum.get_knowledge_point("junior", None, []) is None


def test_callers_cannot_mutate_shared_catalog():
    before = curriculum.get_math_catalog("junior")
    changed = curriculum.get_math_catalog("junior")
    changed[0]["topics"].append("不允许污染")
    changed[0]["topic_ids"].clear()
    changed[0]["source_refs"][0]["locator"] = "错误"
    assert curriculum.get_math_catalog("junior") == before
    point = curriculum.get_knowledge_point("junior", "有理数与实数", "有理数与数轴")
    point["source_refs"].clear()
    point["aliases"].append("不允许污染")
    fresh = curriculum.get_knowledge_point("junior", "有理数与实数", "有理数与数轴")
    assert fresh["source_refs"]
    assert "不允许污染" not in fresh["aliases"]


@pytest.mark.parametrize("stage", ["junior", "senior"])
def test_every_module_has_verified_sources_and_honest_coverage(stage):
    sources = _sources()
    by_id = {source["id"]: source for source in sources["sources"]}
    data = _raw(stage)
    curriculum._validate_catalog(data, stage, sources)
    assert data["review_status"] == "core_content_checked_2025_reprints"
    assert "2025" in data["coverage_note"]
    assert "未宣称取得教育部2025原始发布文件" in data["coverage_note"]
    assert any("2025" in limitation for limitation in sources["limitations"])
    for module in data["modules"]:
        for ref in module["source_refs"]:
            source = by_id[ref["source_id"]]
            assert source["verification"] == "content_verified"
            assert source["kind"] != "revision_notice"
            assert "印刷页" in ref["locator"]
            assert source["publisher"]
            assert source["checked_sections"]


def test_new_junior_statistics_are_not_omitted():
    for name in ("四分位数与箱线图", "数据分组与组内离差平方和", "用样本估计总体"):
        point = curriculum.get_knowledge_point("junior", "统计与数据分析", name)
        assert point["scope"] == "core"


@pytest.mark.parametrize("stage,chapter,topic", [
    ("junior", "一次方程与方程组", "三元一次方程组"),
    ("junior", "圆", "过圆外一点作圆的切线"),
    ("junior", "圆", "切线长定理"),
    ("junior", "图形变换与相似", "相似三角形判定定理的证明"),
    ("junior", "几何基础与相交平行线", "平行线性质定理Ⅰ的证明"),
    ("senior", "复数", "复数的三角形式"),
    ("senior", "随机变量及其分布", "贝叶斯公式"),
    ("senior", "数列", "数学归纳法"),
    ("senior", "拓展：微积分与代数", "定积分及其应用"),
])
def test_optional_topics_are_not_claimed_as_common_required(stage, chapter, topic):
    point = curriculum.get_knowledge_point(stage, chapter, topic)
    assert point["scope"] == "extension"
    assert point["course"] in {"optional", "elective"}


def test_senior_separates_selective_required_from_compulsory():
    assert curriculum.get_knowledge_point("senior", "函数的概念与性质", "函数的定义域")["course"] == "compulsory"
    assert curriculum.get_knowledge_point("senior", "导数及其应用", "导数的运算")["course"] == "selective_required"


def test_2025_checked_content_has_stable_ids_without_invented_remainder_theorem():
    assert curriculum.get_knowledge_point("junior", "代数式与整式", "代数推理")["id"] == "junior.math.polynomials.11"
    assert curriculum.get_knowledge_point("junior", "代数式与整式", "余数定理") is None
    property_point = curriculum.get_knowledge_point("junior", "几何基础与相交平行线", "平行线的性质")
    assert property_point["id"] == "junior.math.geometry_basics.07"
    assert property_point["scope"] == "core"
    proof = curriculum.get_knowledge_point("junior", "几何基础与相交平行线", "平行线性质定理Ⅰ的证明")
    assert proof["id"] == "junior.math.geometry_basics.11"
    assert proof["course"] == "optional"
    projection = curriculum.get_knowledge_point("senior", "空间向量与立体几何", "空间向量的投影")
    assert projection["id"] == "senior.math.space_vectors.09"
    assert projection["scope"] == "core"
    assert projection["course"] == "selective_required"
    bisection = curriculum.get_knowledge_point("senior", "函数的概念与性质", "函数的零点与二分法")
    assert "程序框图" in bisection["note"]


@pytest.mark.parametrize("stage,source_id,pages", [
    ("junior", "junior-2025-public-reprint", 186),
    ("senior", "senior-2025-school-reprint", 171),
])
def test_2025_reprints_are_attributed_with_limits_not_official_authentication(stage, source_id, pages):
    source = next(item for item in _sources()["sources"] if item["id"] == source_id)
    assert source["kind"] == "curriculum_standard_reprint"
    assert source["verification"] == "content_verified"
    assert "非教育部原始下载渠道" in source["publisher"]
    assert source["page_count"] == pages
    assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
    for module in _raw(stage)["modules"]:
        if any(point["scope"] == "core" for point in module["topics"]):
            assert source_id in {ref["source_id"] for ref in module["source_refs"]}


@pytest.mark.parametrize("mutation,pattern", [
    (lambda p: p.update(stage="senior"), "stage/subject"),
    (lambda p: p.update(version=""), "version"),
    (lambda p: p["modules"].append(deepcopy(p["modules"][0])), "ID"),
    (lambda p: p["modules"][0].update(source_refs=[]), "source references"),
    (lambda p: p["modules"][0]["source_refs"][0].update(source_id="junior-2025-revision-notice"), "not verified"),
    (lambda p: p["modules"][0]["source_refs"][0].update(source_id="junior-2025-gdedu-metadata"), "not verified"),
    (lambda p: p["modules"][0]["topics"][0].update(id="senior.math.bad.01"), "point ID"),
    (lambda p: p["modules"][0]["topics"].append(deepcopy(p["modules"][0]["topics"][0])), "point ID"),
    (lambda p: p["modules"][0]["topics"][0].update(course="elective"), "cannot be core"),
])
def test_malformed_catalog_rejected(mutation, pattern):
    payload = _raw("junior")
    mutation(payload)
    with pytest.raises(ValueError, match=pattern):
        curriculum._validate_catalog(payload, "junior", _sources())


def test_version_changes_with_catalog_content_not_json_format(tmp_path, monkeypatch):
    for filename in ("junior.v1.json", "sources.json"):
        (tmp_path / filename).write_text((CATALOG_ROOT / filename).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(curriculum, "_CATALOG_ROOT", tmp_path)
    curriculum._load_catalog.cache_clear()
    try:
        initial = curriculum.catalog_version("junior")
        data = _raw("junior")
        (tmp_path / "junior.v1.json").write_text(json.dumps(data), encoding="utf-8")
        curriculum._load_catalog.cache_clear()
        assert curriculum.catalog_version("junior") == initial
        data["modules"][0]["topics"][0]["note"] = "目录语义更改"
        (tmp_path / "junior.v1.json").write_text(json.dumps(data), encoding="utf-8")
        curriculum._load_catalog.cache_clear()
        assert curriculum.catalog_version("junior") != initial
    finally:
        curriculum._load_catalog.cache_clear()
