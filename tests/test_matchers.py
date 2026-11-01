"""Tests for matchers — hash, trie, and math matching."""

import pytest
from services.matchers.hash_matcher import HashMatcher
from services.matchers.trie_matcher import TrieMatcher
from services.matchers.math_matcher import MathMatcher


class MockQuestion:
    def __init__(self, question, answer, source="manual", chapter=None, topic=None,
                 grading_method="exact", answer_variants="[]"):
        self.question = question
        self.answer = answer
        self.source = source
        self.chapter = chapter
        self.topic = topic
        self.grading_method = grading_method
        self.answer_variants = answer_variants

    def get_variants(self):
        import json
        try:
            return json.loads(self.answer_variants)
        except Exception:
            return []


class TestHashMatcher:
    def test_put_and_get(self):
        hm = HashMatcher()
        q = MockQuestion("1+1", "2")
        hm.put("1+1", q)
        assert hm.get("1+1") is q

    def test_get_missing_key(self):
        hm = HashMatcher()
        assert hm.get("nonexistent") is None

    def test_collision_handling(self):
        hm = HashMatcher(initial_capacity=2)
        q1 = MockQuestion("q1", "a1")
        q2 = MockQuestion("q2", "a2")
        q3 = MockQuestion("q3", "a3")
        hm.put("q1", q1)
        hm.put("q2", q2)
        hm.put("q3", q3)
        assert hm.get("q1") is q1
        assert hm.get("q2") is q2
        assert hm.get("q3") is q3

    def test_resize_triggered(self):
        hm = HashMatcher(initial_capacity=4, load_factor=0.5)
        for i in range(10):
            hm.put(f"key{i}", MockQuestion(f"q{i}", f"a{i}"))
        for i in range(10):
            assert hm.get(f"key{i}") is not None


class TestTrieMatcher:
    def test_insert_and_exact_search(self):
        trie = TrieMatcher()
        q = MockQuestion("计算1+1", "2")
        trie.insert("计算1+1", q)
        result = trie.search_exact("计算1+1")
        assert result is q

    def test_exact_search_miss(self):
        trie = TrieMatcher()
        q = MockQuestion("三角形内角和", "180")
        trie.insert("三角形内角和", q)
        assert trie.search_exact("四边形的内角和") is None

    def test_fuzzy_search(self):
        trie = TrieMatcher()
        q = MockQuestion("三角形内角和", "180°")
        trie.insert("三角形内角和", q)
        results = trie.search_fuzzy("三角形内角合", max_edits=1)
        assert len(results) > 0


class TestMathMatcher:
    def test_exact_numeric_match(self):
        assert MathMatcher.is_equivalent("2", "2")

    def test_numeric_inequality(self):
        assert not MathMatcher.is_equivalent("2", "3")

    def test_equivalent_expressions(self):
        assert MathMatcher.is_equivalent("a+b", "b+a")

    def test_normalized_spacing(self):
        assert MathMatcher.is_equivalent("1 + 2", "1+2")

    def test_with_variants(self):
        assert MathMatcher.is_equivalent("对", "✓", variants=["对", "✓", "正确"])

    def test_none_handling(self):
        assert not MathMatcher.is_equivalent(None, "2")
        assert not MathMatcher.is_equivalent("2", None)
