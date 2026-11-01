"""Hash Table 精确匹配 — DJB2 哈希 + 链地址法 + 动态扩容"""

from typing import Any, Optional

from services.utils import normalize_text as _normalize
from services.teaching_context import current_teaching_context, validate_teaching_context


def matches_teaching_context(value: Any, context: dict) -> bool:
    if not context:
        return True
    read = value.get if isinstance(value, dict) else lambda key, default: getattr(value, key, default)
    return (read("education_stage", "") == context["education_stage"]
            and read("catalog_version", "") == context["curriculum_version"])


class HashNode:
    __slots__ = ("key", "value", "next")

    def __init__(self, key: str, value: Any, next: Optional["HashNode"] = None):
        self.key = key
        self.value = value
        self.next = next


class HashMatcher:
    def __init__(self, initial_capacity: int = 16, load_factor: float = 0.75, *, teaching_context=None):
        self.teaching_context = validate_teaching_context(current_teaching_context() if teaching_context is None else teaching_context)
        self.capacity = initial_capacity
        self.load_factor = load_factor
        self.size = 0
        self.buckets: list[Optional[HashNode]] = [None] * initial_capacity

    @staticmethod
    def _djb2(key: str) -> int:
        h = 5381
        for ch in key:
            h = ((h << 5) + h) + ord(ch)
        return h & 0x7FFFFFFF

    def _index(self, key: str) -> int:
        return self._djb2(key) % self.capacity

    def _resize(self) -> None:
        old_buckets = self.buckets
        self.capacity *= 2
        self.size = 0
        self.buckets = [None] * self.capacity
        for head in old_buckets:
            cur = head
            while cur:
                self.put(cur.key, cur.value)
                cur = cur.next

    def put(self, key: str, value: Any) -> None:
        if not matches_teaching_context(value, self.teaching_context):
            return
        norm = _normalize(key)
        idx = self._index(norm)
        head = self.buckets[idx]
        cur = head
        while cur:
            if cur.key == norm:
                cur.value = value
                return
            cur = cur.next
        node = HashNode(norm, value, head)
        self.buckets[idx] = node
        self.size += 1
        if self.size / self.capacity > self.load_factor:
            self._resize()

    def get(self, key: str) -> Optional[Any]:
        norm = _normalize(key)
        idx = self._index(norm)
        cur = self.buckets[idx]
        while cur:
            if cur.key == norm:
                return cur.value if matches_teaching_context(cur.value, self.teaching_context) else None
            cur = cur.next
        return None

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __len__(self) -> int:
        return self.size
