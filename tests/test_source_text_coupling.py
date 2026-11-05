"""Tests must assert behaviour, not the text of Python source files.

A test that reads ``routers/*.py`` or ``services/*.py`` and matches strings
pins how a function is written, so a behaviour-preserving split of that file
fails it (or forces the test to be rewritten alongside the code it guards).
New tests may not do this.  The files below still do; each one must be turned
into a behaviour test before the module it reads is split, and is then removed
from this list (a stale entry fails too).
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

KNOWN_SOURCE_TEXT_TESTS = {
    # Reads routers/grade_template.py and routers/answer_sheet.py: heartbeats,
    # checkpoints off the event loop, component settling.
    "test_template_recognition_streaming.py",
    # Reads config.py and routers/answer_sheet.py: grading provider defaults
    # and the answer-sheet readiness gate.
    "test_flash_grading.py",
}


def _reads_python_source(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "getsource":
            return True
        reader = (
            isinstance(func, ast.Attribute) and func.attr in {"read_text", "read_bytes", "open"}
        ) or (isinstance(func, ast.Name) and func.id == "open")
        if not reader:
            continue
        scope = [func.value] if isinstance(func, ast.Attribute) else list(node.args[:1])
        for part in scope:
            for leaf in ast.walk(part):
                if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str) and leaf.value.endswith(".py"):
                    return True
    return False


def test_only_known_tests_read_python_source():
    readers = {
        path.name
        for path in TESTS.glob("test_*.py")
        if _reads_python_source(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert readers - KNOWN_SOURCE_TEXT_TESTS == set(), "write a behaviour test instead"
    assert KNOWN_SOURCE_TEXT_TESTS - readers == set(), "remove the converted test from the list"


def test_detector_recognises_both_path_styles():
    samples = {
        'Path("routers/grade.py").read_text(encoding="utf-8")': True,
        '(root / "routers" / "answer_sheet.py").read_text(encoding="utf-8")': True,
        'open("services/x.py").read()': True,
        'inspect.getsource(module)': True,
        '(ROOT / "deploy.sh").read_text(encoding="utf-8")': False,
        'Path("golden/x.json").read_text()': False,
    }
    for source, expected in samples.items():
        assert _reads_python_source(ast.parse(source)) is expected, source
