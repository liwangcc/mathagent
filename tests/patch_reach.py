"""Fail a test whose module-attribute patch would silently stop reaching code.

``monkeypatch.setattr(routers.grade, "SessionLocal", fake)`` only replaces the
binding inside ``routers.grade``.  Once a function moves out of
``routers/grade.py`` into a module with its own ``from database import
SessionLocal``, that patch no longer reaches it and the test keeps passing.

Every patch of a project module attribute is checked against a static index
of the source (no imports, independent of test order):

* reach    the target module reads the name as a global, or project or test
           code reads ``<target module>.<name>``; otherwise the patch is
           *inert*;
* shadows  modules of the target's split family — ``routers/grade_*.py`` and
           ``routers/grade/`` for ``routers.grade`` — that bind the same origin
           under the same name and read it as a global; code there does not
           see the patch, unless the same ``monkeypatch`` has already patched
           that module's own binding (patch the split module first).

Split a large module into files of its family so this applies.  Findings that
exist today are listed in ``patch_reach_baseline.json``; any other finding
fails the test and names the binding to patch.  Regenerate the baseline only
in the same change that updates the tests it lists, and only to shrink it:

    MATHAGENT_PATCH_REACH=record pytest
"""
from __future__ import annotations

import ast
import json
import os
import sys
import types
from functools import lru_cache
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
BASELINE = TESTS / "patch_reach_baseline.json"
_SOURCE_DIRS = ("routers", "services", "worker")
_SOURCE_FILES = ("app.py", "config.py", "database.py")
_observed: set[tuple[str, str, str]] = set()


def _mode() -> str:
    return os.environ.get("MATHAGENT_PATCH_REACH", "enforce")


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _project_files() -> list[Path]:
    files = [ROOT / name for name in _SOURCE_FILES]
    files += sorted(ROOT.glob("models*.py"))
    for directory in _SOURCE_DIRS:
        files += sorted((ROOT / directory).rglob("*.py"))
    return [path for path in files if path.is_file()]


class _ModuleIndex:
    """Bindings, global reads and ``alias.attr`` reads of one source file."""

    def __init__(self, name: str, tree: ast.Module):
        self.name = name
        self.origins: dict[str, str] = {}
        self.module_aliases: dict[str, str] = {}
        self.reads: set[str] = set()
        self.attr_reads: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.module_aliases[alias.asname] = alias.name
                    else:
                        self.module_aliases.setdefault(alias.name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = name.split(".")[:-node.level]
                    base = ".".join([*parts, base] if base else parts)
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.origins[local] = f"{base}.{alias.name}"
                    self.module_aliases[local] = f"{base}.{alias.name}"
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.reads.add(node.id)
            elif isinstance(node, ast.Attribute):
                chain = []
                value = node.value
                while isinstance(value, ast.Attribute):
                    chain.append(value.attr)
                    value = value.value
                if isinstance(value, ast.Name):
                    dotted = ".".join([value.id, *reversed(chain)])
                    self.attr_reads.add((dotted, node.attr))

    def origin(self, local: str) -> str:
        return self.origins.get(local, f"{self.name}.{local}")

    def reads_attr_of(self, module: str, attr: str) -> bool:
        for dotted, read in self.attr_reads:
            if read != attr:
                continue
            head, _, rest = dotted.partition(".")
            resolved = self.module_aliases.get(head, head)
            if (f"{resolved}.{rest}" if rest else resolved) == module or dotted == module:
                return True
        return False


def _parse(path: Path, name: str) -> _ModuleIndex | None:
    try:
        return _ModuleIndex(name, ast.parse(path.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


@lru_cache(maxsize=1)
def _project() -> dict[str, _ModuleIndex]:
    indexes = (_parse(path, _module_name(path)) for path in _project_files())
    return {index.name: index for index in indexes if index is not None}


@lru_cache(maxsize=1)
def _tests() -> tuple[_ModuleIndex, ...]:
    indexes = (_parse(path, f"tests.{path.stem}") for path in sorted(TESTS.rglob("*.py")))
    return tuple(index for index in indexes if index is not None)


def _family(module: str, other: str) -> bool:
    package, _, stem = module.rpartition(".")
    other_package, _, other_stem = other.rpartition(".")
    return other.startswith(module + ".") or (
        other_package == package and other_stem.startswith(stem + "_")
    )


def reach_in(
    project: dict[str, _ModuleIndex],
    tests: tuple[_ModuleIndex, ...],
    module: str,
    name: str,
) -> tuple[bool, tuple[str, ...]]:
    """``(reached, shadows)`` for a patch of ``module.name`` over given sources."""
    target = project.get(module)
    if target is None:
        return True, ()
    reached = name in target.reads or any(
        index.reads_attr_of(module, name) for index in (*project.values(), *tests)
    )
    origin = target.origin(name)
    shadows = tuple(sorted(
        other.name for other in project.values()
        if _family(module, other.name) and name in other.reads and other.origin(name) == origin
    ))
    return reached, shadows


@lru_cache(maxsize=None)
def reach(module: str, name: str) -> tuple[bool, tuple[str, ...]]:
    return reach_in(_project(), _tests(), module, name)


@lru_cache(maxsize=1)
def _baseline() -> frozenset[tuple[str, str, str]]:
    if not BASELINE.exists():
        return frozenset()
    return frozenset(tuple(item) for item in json.loads(BASELINE.read_text(encoding="utf-8"))["allowed"])


def _check(target, name: str, patched: frozenset[tuple[str, str]] = frozenset()) -> None:
    """``patched``: what the same MonkeyPatch already replaced; a shadow whose
    own binding is among them is reached by that companion patch."""
    if not isinstance(target, types.ModuleType) or target.__name__ not in _project():
        return
    module = target.__name__
    reached, shadows = reach(module, name)
    findings = [] if reached else [(module, name, "<inert>")]
    findings += [(module, name, shadow) for shadow in shadows if (shadow, name) not in patched]
    if _mode() == "record":
        _observed.update(findings)
        return
    new = [item for item in findings if item not in _baseline()]
    if new:
        lines = "\n".join(
            f"  {module}.{name}: "
            + ("nothing reads it through this module" if shadow == "<inert>"
               else f"{shadow} reads its own binding — patch {shadow}.{name} too, first")
            for module, name, shadow in new
        )
        pytest.fail("monkeypatch does not reach every reader:\n" + lines
                    + "\n(see tests/patch_reach.py)", pytrace=False)


def _resolve(dotted: str):
    module_name, _, attr = dotted.rpartition(".")
    return sys.modules.get(module_name), attr


def _patched_by(patcher: pytest.MonkeyPatch) -> set[tuple[str, str]]:
    return patcher.__dict__.setdefault("_patch_reach_patched", set())


def install() -> None:
    """Check every ``monkeypatch.setattr`` and ``unittest.mock.patch``.

    A shadow is satisfied when the same MonkeyPatch has already patched the
    shadow module's own binding of the name (``undo`` forgets those patches).
    """
    original_setattr = pytest.MonkeyPatch.setattr
    original_undo = pytest.MonkeyPatch.undo

    def setattr_checked(self, target, *args, **kwargs):
        patched = _patched_by(self)
        if isinstance(target, str):
            result = original_setattr(self, target, *args, **kwargs)
            module, attr = _resolve(target)
            _check(module, attr, frozenset(patched))
        elif args and isinstance(args[0], str):
            module, attr = target, args[0]
            _check(module, attr, frozenset(patched))
            result = original_setattr(self, target, *args, **kwargs)
        else:
            return original_setattr(self, target, *args, **kwargs)
        if isinstance(module, types.ModuleType):
            patched.add((module.__name__, attr))
        return result

    def undo_forgetting(self):
        _patched_by(self).clear()
        return original_undo(self)

    original_enter = mock._patch.__enter__

    def enter_checked(self):
        _check(self.getter(), self.attribute)
        return original_enter(self)

    pytest.MonkeyPatch.setattr = setattr_checked
    pytest.MonkeyPatch.undo = undo_forgetting
    mock._patch.__enter__ = enter_checked


def finish() -> None:
    if _mode() != "record":
        return
    BASELINE.write_text(json.dumps({
        "comment": (
            "Patches that miss a reader today (inert, or a split-family module reads its own "
            "binding). See tests/patch_reach.py; only shrink this list."
        ),
        "allowed": [list(item) for item in sorted(_observed)],
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
