"""Offline checks for probe verdicts; never spend live model calls in pytest."""
import copy

from scripts.run_translucent_slot_label_probe import assess, canonical, definitions


def fixture_and_result():
    fixture = {"id": "probe", "cases": [
        {"slot_id": "1[1]", "text": "7.5", "state": "visible"},
        {"slot_id": "1[2]", "text": "", "state": "blank"},
        {"slot_id": "1[3]", "text": "3/4", "state": "visible"},
    ]}
    result = {"questions": [{"question_number": "1", "student_slots": [
        {"slot_id": c["slot_id"], "student_answer": c["text"], "answer_state": c["state"]}
        for c in fixture["cases"]]}]}
    return fixture, result


def test_slot_coverage_does_not_hide_wrong_binding():
    fixture, result = fixture_and_result()
    slots = result["questions"][0]["student_slots"]
    slots[0]["student_answer"], slots[2]["student_answer"] = slots[2]["student_answer"], slots[0]["student_answer"]
    rows = assess([fixture], {"probe": result})
    assert all(r["id_coverage"] for r in rows)
    assert [r["content_and_binding_correct"] for r in rows] == [False, True, False]


def test_same_content_with_foreign_id_fails_coverage():
    fixture, result = fixture_and_result()
    result["questions"][0]["student_slots"][0]["slot_id"] = "2[1]"
    assert not any(r["content_and_binding_correct"] for r in assess([fixture], {"probe": result}))


def test_blank_and_unreadable_are_not_equivalent():
    fixture, result = fixture_and_result()
    result["questions"][0]["student_slots"][1]["answer_state"] = "unreadable"
    rows = assess([fixture], {"probe": result})
    assert not rows[1]["content_and_binding_correct"]


def test_format_normalization_never_repairs_mathematics():
    assert canonical(r"$\frac{3}{4}$") == canonical("3 / 4")
    assert canonical("−0.6") == canonical("-0.6")
    assert canonical("5/3") != canonical("5/8")
    assert canonical("-1.2 3/4") != canonical("-1.2")
    assert canonical(r"x=5\nx=2") != canonical("x=5\nx=2")


def test_row_order_is_not_used_as_slot_identity():
    fixture, result = fixture_and_result()
    original = copy.deepcopy(result)
    result["questions"][0]["student_slots"].reverse()
    assert assess([fixture], {"probe": result}) == assess([fixture], {"probe": original})


def test_probe_contains_ink_blank_and_shuffled_hierarchy():
    samples = definitions()
    assert len(samples) == 2
    hierarchy = samples[1]["rows"][0]
    assert hierarchy["parent"] == "12.3" and hierarchy["ids"] == [10, 2, 7]
    assert any(not t for s in samples for r in s["rows"] for t in r["text"])
    assert any(g > 100 for s in samples for r in s["rows"] for g in r["gray"])
