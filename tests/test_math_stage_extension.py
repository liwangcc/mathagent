from types import SimpleNamespace

from services.exercise_bank_selector import filter_curriculum_scope
from services.math_curriculum import get_knowledge_point, get_math_catalog


def _rows(stage):
    return [SimpleNamespace(chapter=m["chapter"], topic=t)
            for m in get_math_catalog(stage) for t in m["topics"]]


def _point(row, stage):
    return get_knowledge_point(stage, row.chapter, row.topic)


def test_default_generation_takes_core_and_taught_supplements_only():
    for stage in ("primary", "junior", "senior"):
        rows = _rows(stage)
        unknown = SimpleNamespace(chapter="未知", topic="未知")
        default = filter_curriculum_scope(rows + [unknown], stage)
        assert default and unknown not in default
        for row in default:
            point = _point(row, stage)
            assert point["scope"] == "core" or (
                point["origin"] == "supplement" and point["basis"] in {"textbook", "exam_practice"})
        excluded = [row for row in rows if row not in default]
        for row in excluded:
            point = _point(row, stage)
            assert point["scope"] == "extension"
            assert point["origin"] == "catalog" or point["basis"] == "legacy_textbook"
        opted_in = filter_curriculum_scope(rows + [unknown], stage, include_extension=True)
        assert opted_in == rows


def test_new_textbook_topics_join_default_practice_but_legacy_content_does_not():
    primary = [SimpleNamespace(chapter="数量关系与规律", topic="解简易方程"),
               SimpleNamespace(chapter="比与正比例", topic="反比例关系的识别与应用")]
    assert filter_curriculum_scope(primary, "primary") == primary
    legacy = [SimpleNamespace(chapter="旧教材选修：算法、坐标系与参数方程、不等式选讲", topic="算法与程序框图"),
              SimpleNamespace(chapter="立体几何初步", topic="三视图")]
    assert filter_curriculum_scope(legacy, "senior") == []
    assert filter_curriculum_scope(legacy, "senior", include_extension=True) == legacy


def test_explicitly_picked_extension_topic_is_drawn_without_opt_in():
    bayes = SimpleNamespace(chapter="随机变量及其分布", topic="贝叶斯公式")
    flowchart = SimpleNamespace(chapter="旧教材选修：算法、坐标系与参数方程、不等式选讲", topic="算法与程序框图")
    assert filter_curriculum_scope([bayes, flowchart], "senior") == []
    assert filter_curriculum_scope([bayes, flowchart], "senior", explicit_topics=["贝叶斯公式"]) == [bayes]


def test_legacy_selection_keeps_original_contract():
    rows = [SimpleNamespace(chapter="历史教材", topic="未分类")]
    assert filter_curriculum_scope(rows, None) == rows


def test_generate_forwards_canonical_picked_topics(monkeypatch):
    import asyncio
    from routers import exercise
    from services.teaching_context import make_teaching_context, use_teaching_context

    captured = {}

    def select(db, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(exercise, "select_exercises_from_bank", select)
    request = exercise.ExerciseGenerateRequest(mode="bank", question_count=3, topics=["简易方程"])
    with use_teaching_context(make_teaching_context("primary")):
        asyncio.run(exercise._generate_exercises(request, db=object(), user=SimpleNamespace(id=1)))
    assert "解简易方程" in captured["explicit_topics"]
    assert captured["include_extension"] is False
