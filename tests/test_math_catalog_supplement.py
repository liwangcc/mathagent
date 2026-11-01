"""Append-only catalog supplements and classifier label repair (offline)."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from services import analyzer
from services import math_curriculum as curriculum
from services.teaching_context import make_teaching_context, use_teaching_context, validate_teaching_context

ROOT = Path(__file__).resolve().parents[1] / "references" / "math_curricula"
STAGES = ("primary", "junior", "senior")
# Every stored context, bank row and archived question carries these strings.
FROZEN_VERSIONS = {
    "primary": "math:primary:2026-09-09.v1:babf9180ee59",
    "junior": "math:junior:2026-09-09.v1:94f08fd4a0bc",
    "senior": "math:senior:2026-09-09.v1:0511b9cf6dd6",
}


def _json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("stage", STAGES)
def test_supplement_keeps_frozen_version_and_old_contexts_valid(stage):
    assert curriculum.catalog_version(stage) == FROZEN_VERSIONS[stage]
    stored = {"education_stage": stage, "curriculum_version": FROZEN_VERSIONS[stage],
              "rules_version": make_teaching_context(stage)["rules_version"]}
    assert validate_teaching_context(stored) == stored


@pytest.mark.parametrize("stage", STAGES)
def test_supplement_is_append_only_over_base(stage):
    base = _json(f"{stage}.v1.json")
    for module in base["modules"]:
        for point in module["topics"]:
            merged = curriculum.get_knowledge_point(stage, module["chapter"], point["name"])
            assert (merged["id"], merged["scope"], merged["course"]) == (point["id"], point["scope"], point["course"])
            assert merged["origin"] == "catalog"
            assert set(point.get("aliases", [])) <= set(merged["aliases"])
    supplement = _json(f"{stage}.supplement.json")
    chapters = {module["id"]: module["chapter"] for module in curriculum.get_math_catalog(stage)}
    for point in supplement["topics"]:
        merged = curriculum.get_knowledge_point(stage, chapters[point["module_id"]], point["name"])
        assert merged["id"] == point["id"] and ".s" in point["id"]
        assert (merged["origin"], merged["scope"], merged["source_refs"]) == ("supplement", "extension", [])


def test_supplement_does_not_change_version(tmp_path, monkeypatch):
    for name in ("junior.v1.json", "sources.json"):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(curriculum, "_CATALOG_ROOT", tmp_path)
    curriculum._load_catalog.cache_clear()
    try:
        without = curriculum.catalog_version("junior")
        topics_without = sum(len(module["topics"]) for module in curriculum.get_math_catalog("junior"))
        (tmp_path / "junior.supplement.json").write_text(
            (ROOT / "junior.supplement.json").read_text(encoding="utf-8"), encoding="utf-8")
        curriculum._load_catalog.cache_clear()
        assert curriculum.catalog_version("junior") == without == FROZEN_VERSIONS["junior"]
        assert sum(len(module["topics"]) for module in curriculum.get_math_catalog("junior")) > topics_without
    finally:
        curriculum._load_catalog.cache_clear()


def _first_topic(supplement):
    return supplement["topics"][0]


@pytest.mark.parametrize("mutation,pattern", [
    (lambda s, b: s.update(base_version="other"), "another base"),
    (lambda s, b: s.update(stage="senior"), "stage/subject"),
    (lambda s, b: _first_topic(s).update(scope="core"), "scope/course"),
    (lambda s, b: _first_topic(s).update(basis="guess"), "basis"),
    (lambda s, b: _first_topic(s).update(note=""), "basis"),
    (lambda s, b: _first_topic(s).update(id=b["modules"][0]["topics"][0]["id"],
                                         module_id=b["modules"][0]["id"]), "point ID"),
    (lambda s, b: _first_topic(s).update(name=b["modules"][0]["topics"][0]["name"],
                                         module_id=b["modules"][0]["id"],
                                         id=b["modules"][0]["id"] + ".s99"), "topic name"),
    (lambda s, b: _first_topic(s).update(module_id="junior.math.unknown"), "no module"),
    (lambda s, b: s.update(aliases={"junior.math.unknown.01": ["别名"]}), "alias target"),
    (lambda s, b: s.update(modules=[{"id": "junior.math.empty", "chapter": "空模块", "domain": "测试"}]), "no topics"),
    (lambda s, b: s.update(modules=[{"id": b["modules"][0]["id"], "chapter": "重复", "domain": "测试"}]), "module ID"),
])
def test_malformed_supplement_rejected(mutation, pattern):
    base = _json("junior.v1.json")
    supplement = deepcopy(_json("junior.supplement.json"))
    mutation(supplement, base)
    with pytest.raises(ValueError, match=pattern):
        curriculum._validate_supplement(supplement, base, "junior")


@pytest.mark.parametrize("stage,chapter,topic,expected", [
    ("junior", "一元二次方程", "韦达定理", ("一元二次方程", "一元二次方程根与系数的关系")),
    ("primary", "比与正比例", "解比例", ("比与正比例", "比例的基本性质与解比例")),
    ("primary", "比与正比例", "简易方程", ("数量关系与规律", "解简易方程")),
    ("senior", "函数", "函数的奇偶性", ("函数的概念与性质", "函数的奇偶性")),
    ("senior", "立体几何初步", "外接球", ("立体几何初步", "球的切接问题")),
])
def test_classifier_label_repair_uses_declared_names(stage, chapter, topic, expected):
    point = curriculum.resolve_classifier_label(stage, chapter, topic)
    assert (point["chapter"], point["topic"]) == expected


@pytest.mark.parametrize("stage,chapter,topic", [
    ("junior", "一元二次方程", "完全不存在的知识点"),
    ("senior", "导数及其应用", ""),
    ("senior", "一元二次方程", "韦达定理"),
    ("primary", "一次函数", "一次函数的图象与性质"),
])
def test_classifier_label_repair_never_guesses(stage, chapter, topic):
    assert curriculum.resolve_classifier_label(stage, chapter, topic) is None


@pytest.mark.parametrize("stage", STAGES)
def test_every_declared_name_identifies_one_point(stage):
    by_name, by_alias = curriculum._label_index(stage)
    assert all(len(pairs) == 1 for pairs in by_name.values())
    assert all(len(pairs) == 1 for pairs in by_alias.values())
    assert not set(by_alias) & set(by_name)


def test_persisted_lookup_stays_exact():
    assert curriculum.get_knowledge_point("junior", "一元二次方程", "韦达定理") is None


def test_classifier_validation_repairs_alias_and_chapter_but_rejects_unknown():
    stage = "primary"
    context = make_teaching_context(stage)
    raw = {"results": [
        {"id": "1", "knowledge_points": [{"chapter": "比与正比例", "topic": "简易方程"}]},
        {"id": "2", "knowledge_points": [{"chapter": "比与正比例", "topic": "解比例"}]},
        {"id": "3", "knowledge_points": [{"chapter": "比与正比例", "topic": "目录外知识点"}]},
    ]}
    with use_teaching_context(context):
        rows = analyzer._validate_knowledge_result(
            raw, ["1", "2", "3"], curriculum.get_math_catalog(stage), context["curriculum_version"],
            per_question=True,
        )
    assert (rows[0]["chapter"], rows[0]["topic"]) == ("数量关系与规律", "解简易方程")
    assert rows[0]["knowledge_point_id"] == "primary.math.quantity_relations.s02"
    assert rows[1]["topic"] == "比例的基本性质与解比例"
    assert rows[2]["status"] == "rejected"


def test_classifier_prompt_offers_supplement_topics():
    with use_teaching_context(make_teaching_context("primary")):
        messages, *_ = analyzer.build_knowledge_classification_messages([{"id": "1", "question": "解方程 3x+5=20"}])
    assert "解简易方程" in messages[-1]["content"]
    assert "鸽巢问题" in messages[-1]["content"]
