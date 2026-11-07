"""scripts/check_file_sizes.py: listed large files only shrink, new files stay small."""
import json
import subprocess

from scripts.check_file_sizes import (
    BASELINE_PATH,
    LIMIT,
    count_lines,
    findings,
    is_source,
    load_baseline,
    raised,
    recorded,
    staged_baseline,
    staged_sizes,
    tracked_sizes,
    write_baseline,
)

BIG = LIMIT + 200


def _git(repo, *args):
    subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo, check=True, capture_output=True)


def test_counts_lines_as_an_editor_shows_them():
    assert count_lines(b"") == 0
    assert count_lines(b"a\nb\n") == 2
    assert count_lines(b"a\r\nb\r\n") == 2
    assert count_lines(b"a\r\nb\nc") == 3


def test_counts_source_files_but_not_tests():
    for path in ("app.py", "routers/grade.py", "frontend/src/views/Upload.vue", "frontend/src/api/index.js"):
        assert is_source(path), path
    for path in (
        "tests/test_grade_layers.py",
        "tests/patch_reach.py",
        "desktop/worker/tests/helpers.py",
        "frontend/tests/theme-mode.test.js",
        "frontend/src/styles/global.css",
        "frontend/vite.config.js",
        "README.md",
    ):
        assert not is_source(path), path


def test_a_new_file_may_reach_the_limit_but_not_pass_it():
    assert findings({"a.py": LIMIT}, {}) == []
    assert findings({"a.py": LIMIT + 1}, {}) == [
        f"a.py: {LIMIT + 1} lines, over the {LIMIT}-line limit; put new work in a new module"
    ]


def test_a_listed_file_may_keep_its_size_but_not_grow():
    assert findings({"a.py": BIG}, {"a.py": BIG}) == []
    [finding] = findings({"a.py": BIG + 1}, {"a.py": BIG})
    assert "may only shrink" in finding


def test_a_stale_entry_fails_until_the_list_is_rewritten():
    for sizes in ({"a.py": BIG - 1}, {"a.py": LIMIT}, {}):
        [finding] = findings(sizes, {"a.py": BIG})
        assert finding.endswith(("record the smaller size", "drop it from the list", "run --update")), finding


def test_the_recorded_list_holds_every_file_over_the_limit():
    sizes = {"b.py": BIG, "a.py": LIMIT, "c.vue": LIMIT + 1}
    assert recorded(sizes) == {"b.py": BIG, "c.vue": LIMIT + 1}
    assert findings(sizes, recorded(sizes)) == []


def test_only_growth_needs_to_be_accepted():
    assert raised({"a.py": BIG - 5}, {"a.py": BIG}) == []
    assert raised({}, {"a.py": BIG}) == []
    assert raised({"a.py": BIG + 5}, {"a.py": BIG}) == [f"a.py: {BIG} -> {BIG + 5} lines"]
    assert raised({"b.py": BIG}, {"a.py": BIG}) == [f"b.py: {BIG} lines, not listed before"]


def test_the_baseline_is_written_sorted_with_lf(tmp_path):
    baseline = tmp_path / "baseline.json"
    write_baseline({"b.py": BIG, "a.py": BIG}, baseline)
    assert list(load_baseline(baseline).items()) == [("a.py", BIG), ("b.py", BIG)]
    assert b"\r" not in baseline.read_bytes()
    assert load_baseline(tmp_path / "missing.json") == {}


def test_staged_mode_counts_what_the_next_commit_holds(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "big.py").write_bytes(b"x\n" * 10)
    (tmp_path / "added.py").write_bytes(b"x\r\n" * 3)
    (tmp_path / "notes.md").write_bytes(b"x\n")
    (tmp_path / BASELINE_PATH).write_text(json.dumps({"big.py": 10}), encoding="utf-8")
    _git(tmp_path, "add", ".")
    (tmp_path / "later.py").write_bytes(b"x\n" * 4)
    _git(tmp_path, "add", "--intent-to-add", "later.py")
    (tmp_path / "big.py").write_bytes(b"x\n" * 20)
    (tmp_path / BASELINE_PATH).write_text(json.dumps({"big.py": 20}), encoding="utf-8")
    (tmp_path / "untracked.py").write_bytes(b"x\n")

    assert staged_sizes(tmp_path) == {"added.py": 3, "big.py": 10, "later.py": 0}
    assert staged_baseline(tmp_path) == {"big.py": 10}
    assert tracked_sizes(tmp_path) == {"added.py": 3, "big.py": 20, "later.py": 4}


def test_staged_mode_without_a_staged_baseline_lists_nothing(tmp_path):
    _git(tmp_path, "init", "-q")
    assert staged_baseline(tmp_path) == {}
    assert staged_sizes(tmp_path) == {}
