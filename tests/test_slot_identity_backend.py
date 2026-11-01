import pytest

from services.scoring_engine import split_student_slots


def test_legacy_empty_second_slot_does_not_shift():
    assert split_student_slots('101；；103；104；105；106；107；108', 8) == [
        '101', '', '103', '104', '105', '106', '107', '108'
    ]
