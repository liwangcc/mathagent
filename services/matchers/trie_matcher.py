"""Trie 模糊匹配 — 前缀匹配 + 编辑距离≤1的模糊搜索"""

from typing import Any, Optional

from services.utils import normalize_text as _normalize
from services.matchers.hash_matcher import matches_teaching_context
from services.teaching_context import current_teaching_context, validate_teaching_context


class TrieNode:
    __slots__ = ("children", "value", "is_end")

    def __init__(self):
        self.children: dict[str, "TrieNode"] = {}
        self.value: Any = None
        self.is_end: bool = False


class TrieMatcher:
    def __init__(self, *, teaching_context=None):
        self.teaching_context = validate_teaching_context(current_teaching_context() if teaching_context is None else teaching_context)
        self.root = TrieNode()

    def insert(self, key: str, value: Any) -> None:
        if not matches_teaching_context(value, self.teaching_context):
            return
        norm = _normalize(key)
        node = self.root
        for ch in norm:
            if ch not in node.children:
                node.children[ch] = TrieNode()
            node = node.children[ch]
        node.is_end = True
        node.value = value

    def search_exact(self, key: str) -> Optional[Any]:
        norm = _normalize(key)
        node = self.root
        for ch in norm:
            if ch not in node.children:
                return None
            node = node.children[ch]
        return node.value if node.is_end and matches_teaching_context(node.value, self.teaching_context) else None

    def search_fuzzy(self, key: str, max_edits: int = 1) -> list[Any]:
        """DFS with edit-distance ≤ max_edits. Returns all matching values."""
        norm = _normalize(key)
        results: list[Any] = []
        visited: set[tuple[int, int, int]] = set()

        def dfs(node: TrieNode, pos: int, edits: int, state_hash: int):
            if edits > max_edits:
                return
            sig = (id(node), pos, edits)
            if sig in visited:
                return
            visited.add(sig)
            if pos == len(norm):
                if node.is_end:
                    results.append(node.value)
                return
            ch = norm[pos]
            # match
            if ch in node.children:
                dfs(node.children[ch], pos + 1, edits, state_hash)
            # substitution
            for c, child in node.children.items():
                if c != ch:
                    dfs(child, pos + 1, edits + 1, state_hash)
            # deletion
            dfs(node, pos + 1, edits + 1, state_hash)
            # insertion
            for child in node.children.values():
                dfs(child, pos, edits + 1, state_hash)

        dfs(self.root, 0, 0, 0)
        return [value for value in results if matches_teaching_context(value, self.teaching_context)]

    def __contains__(self, key: str) -> bool:
        return self.search_exact(key) is not None
