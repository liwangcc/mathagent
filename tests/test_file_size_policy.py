"""Exercise source-size decisions with synthetic Git objects and real hooks."""
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

budget = importlib.import_module("scripts.check_file_sizes")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, check=check,
    )
    return result if not check else result.stdout.decode("utf-8").strip()


def source(repo, path, lines, marker="before"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((f"# {marker}\n" * lines).encode())


def baseline(repo, entries):
    budget.write_baseline(entries, repo / budget.BASELINE_PATH)


def entries(**extra):
    return {"large.py": 1000, "legacy.py": 2000, **extra}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Size Policy Test")
    git(tmp_path, "config", "user.email", "size-policy@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "core.hooksPath", ".fixture-hooks-disabled")
    (tmp_path / "scripts").mkdir()
    source(tmp_path, "small.py", 20)
    source(tmp_path, "large.py", 1000)
    source(tmp_path, "legacy.py", 2000)
    source(tmp_path, "approved.py", 1700)
    baseline(tmp_path, {**entries(), "approved.py": 1700})
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture base")
    original_tracked = budget.tracked_sizes
    original_staged = budget.staged_sizes
    original_load = budget.load_baseline
    original_write = budget.write_baseline
    original_staged_baseline = budget.staged_baseline
    monkeypatch.setattr(budget, "ROOT", tmp_path)
    monkeypatch.setattr(budget, "BASELINE", tmp_path / budget.BASELINE_PATH)
    monkeypatch.setattr(budget, "tracked_sizes", lambda: original_tracked(tmp_path))
    monkeypatch.setattr(budget, "staged_sizes", lambda: original_staged(tmp_path))
    monkeypatch.setattr(budget, "load_baseline", lambda: original_load(tmp_path / budget.BASELINE_PATH))
    monkeypatch.setattr(budget, "staged_baseline", lambda: original_staged_baseline(tmp_path))
    monkeypatch.setattr(
        budget, "write_baseline",
        lambda values, path=None: original_write(values, path or tmp_path / budget.BASELINE_PATH),
    )
    return tmp_path


def changed_baseline(repo, **extra):
    baseline(repo, {**entries(), "approved.py": 1700, **extra})


def test_staged_growth_cannot_be_hidden_by_raising_the_baseline(repo, capsys):
    source(repo, "large.py", 1001)
    changed_baseline(repo, **{"large.py": 1001})
    git(repo, "add", ".")
    assert budget.main(["--staged"]) == 1
    assert "1000 -> 1001" in capsys.readouterr().out


def test_worktree_growth_cannot_be_hidden_by_raising_the_baseline(repo):
    source(repo, "large.py", 1001)
    changed_baseline(repo, **{"large.py": 1001})
    assert budget.main([]) == 1


def test_new_file_cannot_be_registered_as_a_legacy_exception(repo, capsys):
    source(repo, "new.py", 901)
    changed_baseline(repo, **{"new.py": 901})
    git(repo, "add", ".")
    assert budget.main(["--staged"]) == 1
    assert "not listed before" in capsys.readouterr().out


def test_ci_compares_to_the_base_commit_not_an_inflated_head(repo):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "large.py", 1001)
    changed_baseline(repo, **{"large.py": 1001})
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture bypass local policy")
    assert budget.main(["--base-ref", base]) == 1


@pytest.mark.parametrize("lines, expected", [(900, 0), (901, 1)])
def test_new_source_boundary_in_the_index(repo, lines, expected):
    source(repo, "new.py", lines)
    git(repo, "add", "new.py")
    assert budget.main(["--staged"]) == expected


def test_unrelated_change_does_not_require_splitting_untouched_legacy(repo):
    source(repo, "small.py", 21)
    assert budget.main([]) == 0


def test_approved_1700_line_file_can_be_edited_without_splitting(repo):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "approved.py", 1700, "changed logic")
    git(repo, "add", "approved.py")
    assert budget.main(["--staged"]) == 0
    git(repo, "commit", "-qm", "fixture approved same-size edit")
    assert budget.main(["--base-ref", base]) == 0


def test_listed_1700_line_file_cannot_grow_even_below_1800(repo):
    source(repo, "approved.py", 1701)
    changed_baseline(repo, **{"approved.py": 1701})
    git(repo, "add", ".")
    assert budget.main(["--staged"]) == 1


def test_legacy_logic_edit_above_1800_is_rejected_at_the_same_size(repo, capsys):
    source(repo, "legacy.py", 2000, "changed")
    git(repo, "add", "legacy.py")
    assert budget.main(["--staged"]) == 1
    assert "1800" in capsys.readouterr().out


def test_local_decomposition_can_shrink_in_stages(repo):
    source(repo, "legacy.py", 1999)
    assert budget.main(["--update"]) == 0
    git(repo, "add", ".")
    assert budget.main(["--staged"]) == 0


def test_pr_must_finish_below_1800_even_when_legacy_file_shrank(repo, capsys):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "legacy.py", 1999)
    assert budget.main(["--update"]) == 0
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture partial decomposition")
    assert budget.main(["--base-ref", base]) == 1
    assert "1800" in capsys.readouterr().out


@pytest.mark.parametrize("lines, expected", [(1800, 0), (1801, 1)])
def test_pr_legacy_hard_boundary(repo, lines, expected):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "legacy.py", lines)
    assert budget.main(["--update"]) == 0
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture decomposition")
    assert budget.main(["--base-ref", base]) == expected


def test_a_fully_split_file_leaves_the_baseline(repo):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "legacy.py", 900)
    assert budget.main(["--update"]) == 0
    assert "legacy.py" not in budget.load_baseline()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture complete decomposition")
    assert budget.main(["--base-ref", base]) == 0


def test_update_rejects_growth_even_if_worktree_baseline_was_raised(repo):
    source(repo, "large.py", 1001)
    changed_baseline(repo, **{"large.py": 1001})
    assert budget.main(["--update"]) == 1


def test_accept_growth_is_no_longer_an_escape_hatch(repo):
    with pytest.raises(SystemExit) as error:
        budget.main(["--update", "--accept-growth"])
    assert error.value.code == 2


@pytest.mark.parametrize("reference", ["missing-policy-base", "", "0" * 40])
def test_invalid_explicit_ref_fails_closed(repo, reference, capsys):
    assert budget.main(["--base-ref", reference]) == 1
    assert "cannot resolve comparison ref" in capsys.readouterr().out


def test_unstaged_growth_neither_fails_nor_passes_a_staged_commit(repo):
    source(repo, "large.py", 1001)
    assert budget.main(["--staged"]) == 0
    source(repo, "large.py", 999)
    git(repo, "add", "large.py")
    assert budget.main(["--staged"]) == 1
    changed_baseline(repo, **{"large.py": 999})
    git(repo, "add", budget.BASELINE_PATH)
    source(repo, "large.py", 1200)
    assert budget.main(["--staged"]) == 0


def test_deleted_legacy_file_and_baseline_entry_can_disappear(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "legacy.py")
    assert budget.main(["--update"]) == 0
    git(repo, "add", budget.BASELINE_PATH)
    git(repo, "commit", "-qm", "fixture remove legacy")
    assert budget.main(["--base-ref", base]) == 0


def test_renaming_a_legacy_file_cannot_create_a_new_large_exception(repo):
    git(repo, "mv", "legacy.py", "renamed.py")
    changed_baseline(repo, **{"renamed.py": 2000})
    git(repo, "add", budget.BASELINE_PATH)
    assert budget.main(["--staged"]) == 1


def test_baseline_never_grows_across_multiple_commits(repo):
    base = git(repo, "rev-parse", "HEAD")
    source(repo, "large.py", 950)
    assert budget.main(["--update"]) == 0
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture tighten")
    source(repo, "large.py", 975)
    changed_baseline(repo, **{"large.py": 975})
    git(repo, "add", ".")
    assert budget.main(["--staged"]) == 1
    git(repo, "commit", "-qm", "fixture bypass local ratchet")
    assert budget.main(["--base-ref", base]) == 1


def test_ci_rejects_regrowth_hidden_by_a_later_shrink(repo, capsys):
    base = git(repo, "rev-parse", "HEAD")
    for lines in (950, 975, 950):
        source(repo, "large.py", lines)
        changed_baseline(repo, **{"large.py": lines})
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"fixture budget {lines}")
    assert budget.main(["--base-ref", base]) == 1
    assert "950 -> 975" in capsys.readouterr().out


def test_changing_to_900_only_retires_entries_without_raising_any(repo):
    source(repo, "former-large.py", 850)
    changed_baseline(repo, **{"former-large.py": 850})
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture previous 800-line policy")
    assert budget.main(["--update"]) == 0
    assert "former-large.py" not in budget.load_baseline()
    assert budget.load_baseline()["approved.py"] == 1700
    assert budget.main([]) == 0


@pytest.fixture
def hooked_repo(repo):
    # Copy executable artifacts to exercise the actual CLI/hook; no text assertions.
    shutil.copy2(budget.__file__, repo / "scripts/check_file_sizes.py")
    hook = repo / ".githooks/pre-commit"
    hook.parent.mkdir()
    shutil.copy2(PROJECT_ROOT / ".githooks/pre-commit", hook)
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", ".githooks")
    git(repo, "add", "scripts/check_file_sizes.py", ".githooks/pre-commit")
    git(repo, "commit", "-qm", "fixture install real hook")
    return repo


@pytest.mark.parametrize("mode", ["staged", "all", "path"])
def test_real_hook_blocks_joint_source_and_baseline_growth(hooked_repo, mode):
    before = git(hooked_repo, "rev-parse", "HEAD")
    source(hooked_repo, "large.py", 1001)
    changed_baseline(hooked_repo, **{"large.py": 1001})
    if mode == "staged":
        git(hooked_repo, "add", ".")
        args = ["commit", "-qm", "fixture forbidden growth"]
    elif mode == "all":
        args = ["commit", "-aqm", "fixture forbidden growth"]
    else:
        args = ["commit", "-qm", "fixture forbidden growth", "large.py", budget.BASELINE_PATH]
    result = git(hooked_repo, *args, check=False)
    assert result.returncode != 0
    assert b"1000 -> 1001" in result.stdout + result.stderr
    assert git(hooked_repo, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("lines, succeeds", [(900, True), (901, False)])
def test_real_hook_enforces_new_file_boundary(hooked_repo, lines, succeeds):
    before = git(hooked_repo, "rev-parse", "HEAD")
    source(hooked_repo, "new.py", lines)
    git(hooked_repo, "add", "new.py")
    result = git(hooked_repo, "commit", "-qm", "fixture new module", check=False)
    assert (result.returncode == 0) == succeeds
    assert (git(hooked_repo, "rev-parse", "HEAD") != before) == succeeds


def test_real_hook_accepts_same_size_1700_line_approved_edit(hooked_repo):
    source(hooked_repo, "approved.py", 1700, "changed")
    git(hooked_repo, "add", "approved.py")
    git(hooked_repo, "commit", "-qm", "fixture approved edit")


def test_real_cli_uses_explicit_base_for_committed_growth(hooked_repo):
    base = git(hooked_repo, "rev-parse", "HEAD")
    source(hooked_repo, "large.py", 1001)
    changed_baseline(hooked_repo, **{"large.py": 1001})
    git(hooked_repo, "add", ".")
    # Deliberately simulate an actor bypassing the hook, only in this fixture.
    git(hooked_repo, "-c", "core.hooksPath=.fixture-hooks-disabled", "commit", "-qm", "fixture bypass")
    result = subprocess.run(
        [sys.executable, "scripts/check_file_sizes.py", "--base-ref", base],
        cwd=hooked_repo, capture_output=True,
    )
    assert result.returncode == 1
    assert b"1000 -> 1001" in result.stdout


@pytest.fixture
def trusted_base(repo):
    shutil.copy2(budget.__file__, repo / "scripts/check_file_sizes.py")
    git(repo, "add", "scripts/check_file_sizes.py")
    git(repo, "commit", "-qm", "fixture trusted policy")
    return git(repo, "rev-parse", "HEAD")


def inspect_untrusted_head(repo, base):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--detach", base)
    result = subprocess.run(
        [sys.executable, "-I", "scripts/check_file_sizes.py",
         "--base-ref", base, "--head-ref", head],
        cwd=repo, capture_output=True,
    )
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "status", "--porcelain") == ""
    return result


@pytest.mark.parametrize("lines, expected", [(900, 0), (901, 1)])
def test_trusted_policy_reads_candidate_objects_without_checkout(repo, trusted_base, lines, expected):
    source(repo, "new.py", lines)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture untrusted module")
    result = inspect_untrusted_head(repo, trusted_base)
    assert result.returncode == expected
    assert not (repo / "new.py").exists()


def test_pr_cannot_disable_checker_and_workflow_to_waive_oversized_source(repo, trusted_base):
    source(repo, "new.py", 901)
    changed_baseline(repo, **{"new.py": 901})
    # If candidate Python were executed, the sentinel would expose it.
    (repo / "scripts/check_file_sizes.py").write_bytes(
        b"from pathlib import Path\nPath('untrusted-ran').touch()\nraise SystemExit(0)\n"
    )
    workflow = repo / ".github/workflows/source-size-policy.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(b"name: fake\non: push\njobs: {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture tries to disable its own checks")
    result = inspect_untrusted_head(repo, trusted_base)
    assert result.returncode == 1
    assert b"new.py: 901 lines, not listed before" in result.stdout
    assert not (repo / "untrusted-ran").exists()


def test_trusted_head_checks_full_history_not_the_base_checkout_history(repo, trusted_base):
    for lines in (950, 975, 950):
        source(repo, "large.py", lines)
        changed_baseline(repo, **{"large.py": lines})
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"fixture candidate budget {lines}")
    result = inspect_untrusted_head(repo, trusted_base)
    assert result.returncode == 1
    assert b"950 -> 975" in result.stdout


def test_trusted_head_allows_approved_1700_line_edit(repo, trusted_base):
    source(repo, "approved.py", 1700, "changed")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture approved candidate")
    assert inspect_untrusted_head(repo, trusted_base).returncode == 0


@pytest.mark.parametrize("flags", [
    ["--head-ref", "HEAD"],
    ["--base-ref", "HEAD", "--head-ref", "HEAD", "--staged"],
    ["--base-ref", "HEAD", "--head-ref", "HEAD", "--update"],
])
def test_candidate_object_mode_rejects_ambiguous_mutating_options(repo, flags):
    with pytest.raises(SystemExit) as error:
        budget.main(flags)
    assert error.value.code == 2
