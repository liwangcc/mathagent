"""Patch a global of the routers.grade family in every module that reads it.

routers/grade.py is split into routers/grade_*.py, and each of those modules
binds names such as ``UPLOAD_DIR`` itself.  ``monkeypatch.setattr(routers.grade,
"UPLOAD_DIR", path)`` alone would miss the split modules, so a test sets the
name once here and does not need to know which file reads it.
"""
from __future__ import annotations

import importlib

from tests.patch_reach import _project, reach

_GRADE = "routers.grade"


def _readers(name: str) -> list[str]:
    """Family modules reading ``name`` bound to one origin, split modules first.

    Split modules have split modules of their own (routers/grade_template_*.py
    for routers.grade_template); reverse order patches each of those before
    the module it was split from, as tests/patch_reach.py requires.
    """
    project = _project()
    reached, _ = reach(_GRADE, name)
    split = sorted((module for module in project if module.startswith(_GRADE + "_")), reverse=True)
    if name in project[_GRADE].origins or name in project[_GRADE].reads:
        origins = {project[_GRADE].origin(name)}
    else:
        origins = {project[module].origin(name) for module in split if name in project[module].reads}
    assert len(origins) == 1, f"{name} has no single origin in the routers.grade family: {origins}"
    readers = [module for module in split
               if name in project[module].reads and project[module].origin(name) in origins]
    return readers + ([_GRADE] if reached else [])


def set_grade_global(monkeypatch, name: str, value) -> None:
    """Set ``name`` to ``value`` in each grade-family module that reads it.

    The readers come from the same static index as tests/patch_reach.py, and
    the split modules are patched before routers.grade, as that guard requires.
    """
    readers = _readers(name)
    assert readers, f"no module of the routers.grade family reads {name}"
    for module in readers:
        monkeypatch.setattr(importlib.import_module(module), name, value)
