"""Offline primary catalog schema, provenance and cross-stage identity checks."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services import math_curriculum as curriculum


ROOT = Path(__file__).resolve().parents[1] / "references" / "math_curricula"


def test_primary_catalog_is_independent_and_versioned():
    catalog = curriculum.get_math_catalog("primary")
    assert len(catalog) == 17
    base = json.loads((ROOT / "primary.v1.json").read_text(encoding="utf-8"))
    assert sum(len(module["topics"]) for module in base["modules"]) == 123
    supplement = json.loads((ROOT / "primary.supplement.json").read_text(encoding="utf-8"))
    assert sum(len(module["topics"]) for module in catalog) == 123 + len(supplement["topics"])
    primary_ids = set()
    for module in catalog:
        assert module["id"].startswith("primary.math.")
        assert set(module["topics"]) == set(module["topic_ids"])
        for topic in module["topics"]:
            point = curriculum.get_knowledge_point("primary", module["chapter"], topic)
            assert point["id"] == module["topic_ids"][topic]
            assert point["stage"] == "primary"
            assert point["catalog_version"] == curriculum.catalog_version("primary")
            assert point["id"] not in primary_ids
            primary_ids.add(point["id"])
    for stage in ("junior", "senior"):
        other_ids = {value for module in curriculum.get_math_catalog(stage) for value in module["topic_ids"].values()}
        assert primary_ids.isdisjoint(other_ids)
    assert curriculum.catalog_version("primary") == "math:primary:2026-09-09.v1:babf9180ee59"


def test_primary_sources_are_verified_with_explicit_limits():
    raw = json.loads((ROOT / "primary.v1.json").read_text(encoding="utf-8"))
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    curriculum._validate_catalog(raw, "primary", sources)
    source = next(item for item in sources["sources"] if item["id"] == "moe-primary-2022-final")
    assert source["verification"] == "content_verified"
    assert source["url"].startswith("https://www.moe.gov.cn/")
    assert source["page_count"] == 189
    assert source["sha256"] == "1183b95c58a65eaac4c456f2d2b329bbe42f65a6482993edb537e3eaf8baa144"
    assert "17–53" in source["checked_sections"][0]
    assert "未核对小学2025" in raw["coverage_note"]
    assert "全部审定教材" in raw["coverage_note"]
    for module in raw["modules"]:
        assert all(ref["source_id"] == source["id"] and "印刷页" in ref["locator"] for ref in module["source_refs"])


@pytest.mark.parametrize("chapter,topic", [
    ("整数的认识", "数的含义与数数"),
    ("整数四则运算", "整数四则混合运算"),
    ("分数的认识与运算", "分数乘除法"),
    ("数量关系与规律", "速度、时间与路程"),
    ("比与正比例", "正比例关系的识别"),
    ("平面图形的认识", "角的测量与画角"),
    ("长度、周长与面积", "圆的周长与面积"),
    ("立体图形与体积", "圆锥的认识与体积"),
    ("统计与数据分析", "平均数的意义与应用"),
    ("百分数及其应用", "百分数的意义"),
    ("可能性", "可能性大小的定性比较"),
    ("常见的量与单位", "人民币与元角分换算"),
    ("生活中的负数", "温度、海拔与负数"),
])
def test_primary_representative_points_are_exact_catalog_members(chapter, topic):
    assert curriculum.get_knowledge_point("primary", chapter, topic)["scope"] == "core"
    assert curriculum.get_knowledge_point("primary", chapter, topic + " ") is None


def test_primary_catalog_is_not_mutable_through_public_results():
    original = curriculum.get_math_catalog("primary")
    returned = curriculum.get_math_catalog("primary")
    returned[0]["topics"].append("不允许的修改")
    returned[0]["source_refs"].clear()
    assert curriculum.get_math_catalog("primary") == original
    assert curriculum.get_knowledge_point("primary", "导数及其应用", "导数的运算") is None


def test_primary_catalog_rejects_foreign_ids_and_unverified_sources():
    raw = json.loads((ROOT / "primary.v1.json").read_text(encoding="utf-8"))
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    invalid = deepcopy(raw)
    invalid["modules"][0]["topics"][0]["id"] = "junior.math.numbers.01"
    with pytest.raises(ValueError, match="point ID"):
        curriculum._validate_catalog(invalid, "primary", sources)
    invalid = deepcopy(raw)
    invalid["modules"][0]["source_refs"][0]["source_id"] = "not-verified"
    with pytest.raises(ValueError, match="source reference"):
        curriculum._validate_catalog(invalid, "primary", sources)
