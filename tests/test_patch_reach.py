"""The patch-reach guard catches what a file split would silently break."""
import ast

import pytest

from tests import patch_reach
from tests.patch_reach import _ModuleIndex, reach_in


def _sources(**modules: str) -> dict[str, _ModuleIndex]:
    return {
        name.replace("__", "."): _ModuleIndex(name.replace("__", "."), ast.parse(source))
        for name, source in modules.items()
    }


GRADE_BEFORE = """
from database import SessionLocal

def recognize():
    return SessionLocal()

def confirm():
    return SessionLocal()
"""


def test_reader_in_the_module_is_reached():
    project = _sources(routers__grade=GRADE_BEFORE)
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (True, ())


def test_moving_a_reader_into_the_split_family_is_a_shadow():
    project = _sources(
        routers__grade="from database import SessionLocal\n\ndef confirm():\n    return SessionLocal()\n",
        routers__grade_recognition="from database import SessionLocal\n\ndef recognize():\n    return SessionLocal()\n",
    )
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (True, ("routers.grade_recognition",))


def test_moving_every_reader_out_makes_the_patch_inert():
    project = _sources(
        routers__grade="from database import SessionLocal\n",
        routers__grade_recognition="from database import SessionLocal\n\ndef recognize():\n    return SessionLocal()\n",
    )
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (False, ("routers.grade_recognition",))


def test_reading_through_the_original_module_keeps_the_patch_effective():
    project = _sources(
        routers__grade="from database import SessionLocal\n",
        routers__grade_recognition="from routers import grade\n\ndef recognize():\n    return grade.SessionLocal()\n",
    )
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (True, ())


def test_unrelated_modules_with_the_same_import_are_not_shadows():
    project = _sources(
        routers__grade=GRADE_BEFORE,
        routers__upload="from database import SessionLocal\n\ndef upload():\n    return SessionLocal()\n",
    )
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (True, ())


def test_a_test_helper_reading_the_attribute_counts_as_reach():
    project = _sources(routers__answer_sheet="from services.x import canonicalize\n")
    helper = _ModuleIndex("tests.helper", ast.parse(
        "from routers import answer_sheet\n\ndef adopt():\n    return answer_sheet.canonicalize()\n"
    ))
    assert reach_in(project, (helper,), "routers.answer_sheet", "canonicalize") == (True, ())


def test_package_split_is_family_too():
    project = _sources(
        routers__grade="from database import SessionLocal\n\ndef confirm():\n    return SessionLocal()\n",
        routers__grade__stream="from database import SessionLocal\n\ndef recognize():\n    return SessionLocal()\n",
    )
    assert reach_in(project, (), "routers.grade", "SessionLocal") == (True, ("routers.grade.stream",))


def test_unlisted_finding_fails_the_test(monkeypatch):
    import routers.grade as grade

    monkeypatch.setattr(patch_reach, "_mode", lambda: "enforce")
    with pytest.raises(pytest.fail.Exception, match="nothing reads it through this module"):
        patch_reach._check(grade, "name_no_code_reads")
