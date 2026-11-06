"""Patch a global of the routers.grade family in every module that reads it.

routers/grade.py is split into routers/grade_*.py, and each of those modules
binds names such as ``UPLOAD_DIR`` itself.  ``monkeypatch.setattr(routers.grade,
"UPLOAD_DIR", path)`` alone would miss the split modules, so a test sets the
name once here and does not need to know which file reads it.
"""
from __future__ import annotations

import importlib

from tests.patch_reach import reach


def set_grade_global(monkeypatch, name: str, value) -> None:
    """Set ``name`` to ``value`` in each grade-family module that reads it.

    The readers come from the same static index as tests/patch_reach.py, and
    the split modules are patched before routers.grade, as that guard requires.
    """
    reached, split_readers = reach("routers.grade", name)
    readers = [*split_readers, *(["routers.grade"] if reached else [])]
    assert readers, f"no module of the routers.grade family reads {name}"
    for module in readers:
        monkeypatch.setattr(importlib.import_module(module), name, value)
