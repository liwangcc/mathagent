"""New source files stay within 900 lines; changed legacy files within 1800.

Count tracked Python files and .vue/.js/.ts files under frontend/src, excluding
tests. Physical lines include comments and blank lines.

* Files not in the committed baseline stay within 900 lines.
* Existing baseline entries may only shrink or disappear. Compare against a
  committed reference, not the candidate JSON, so joint edits cannot waive growth.
* Changed legacy files must be at most 1800 lines at the PR's final revision.
  Untouched files do not require a drive-by split. The approved roughly 1700-line
  modules remain valid as-is, but their recorded size still may not increase.
* Locally --staged allows strictly shrinking intermediate splits; CI with
  --base-ref requires the final changed file to be within 1800 lines.
* --update records reductions only. There is no --accept-growth escape hatch.

    python scripts/check_file_sizes.py
    python scripts/check_file_sizes.py --staged
    python scripts/check_file_sizes.py --update
    python scripts/check_file_sizes.py --base-ref <PR-base-sha>

The default comparison is HEAD; the hook reads the real candidate index.
CI supplies the trusted PR base or previous main SHA and fetches its history.
For untrusted PRs, run the base-branch checker with --base-ref and --head-ref:
only Git objects are read, never candidate Python, tests or workflow code.
Missing explicit references fail closed. Stage an updated baseline with the
corresponding source change. Tests exercise synthetic repositories and hooks,
not source-text assertions about product behaviour.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = "scripts/file_size_baseline.json"
BASELINE = ROOT / BASELINE_PATH
LIMIT = 900
LEGACY_LIMIT = 1800

_TEST_NAME_ENDINGS = ("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")


def is_source(path: str) -> bool:
    """Whether a repository-relative POSIX path is a counted source file."""
    *folders, name = path.split("/")
    if "tests" in folders or name.startswith("test_") or name.endswith(_TEST_NAME_ENDINGS):
        return False
    if name.endswith(".py"):
        return True
    return path.startswith("frontend/src/") and name.endswith((".vue", ".js", ".ts"))


def count_lines(data: bytes) -> int:
    """Lines as an editor shows them, with LF or CRLF endings and an unterminated last line."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _git(root: Path, *args: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, check=check)


def tracked_sizes(root: Path = ROOT) -> dict[str, int]:
    """Line counts of the tracked source files, as they are in the working tree."""
    listed = _git(root, "ls-files", "-z").stdout.decode("utf-8").split("\0")
    return {
        path: count_lines((root / path).read_bytes())
        for path in listed
        if path and is_source(path) and (root / path).is_file()
    }


def staged_sizes(root: Path = ROOT) -> dict[str, int]:
    """Line counts of the source files as they are staged for the next commit."""
    blobs = {}
    for entry in _git(root, "ls-files", "-z", "--stage").stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        info, path = entry.split("\t", 1)
        _mode, blob, stage = info.split()
        if stage == "0" and is_source(path):
            blobs[path] = blob
    return blob_sizes(root, blobs)


def revision_sizes(reference: str, root: Path = ROOT) -> dict[str, int]:
    """Read a candidate tree as Git objects, never checking out or executing it."""
    blobs = {}
    for entry in _git(root, "ls-tree", "-rz", reference).stdout.decode("utf-8").split("\0"):
        if entry:
            info, path = entry.split("\t", 1)
            _mode, kind, blob = info.split()
            if kind == "blob" and is_source(path):
                blobs[path] = blob
    return blob_sizes(root, blobs)


def blob_sizes(root: Path, blobs: dict[str, str]) -> dict[str, int]:
    request = "".join(blob + "\n" for blob in blobs.values()).encode()
    output = _git(root, "cat-file", "--batch", stdin=request).stdout
    sizes, offset = {}, 0
    for path in blobs:
        header_end = output.index(b"\n", offset)
        header = output[offset:header_end].split()
        if header[-1] == b"missing":  # the empty blob of an intent-to-add entry
            sizes[path], offset = 0, header_end + 1
            continue
        start = header_end + 1
        end = start + int(header[2])
        sizes[path], offset = count_lines(output[start:end]), end + 1
    return sizes


def recorded(sizes: dict[str, int]) -> dict[str, int]:
    """The baseline that matches ``sizes``: every source file over the limit, by path."""
    return {path: sizes[path] for path in sorted(sizes) if sizes[path] > LIMIT}


def findings(sizes: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Every way the files disagree with the baseline; empty when the check passes."""
    found = []
    for path in sorted(set(sizes) | set(baseline)):
        now, was = sizes.get(path), baseline.get(path)
        if was is None:
            if now > LIMIT:
                found.append(f"{path}: {now} lines, over the {LIMIT}-line limit; put new work in a new module")
        elif now is None:
            found.append(f"{path}: listed at {was} lines but no longer a tracked source file; run --update")
        elif now > was:
            found.append(f"{path}: grew from {was} to {now} lines; a listed file may only shrink")
        elif now <= LIMIT:
            found.append(f"{path}: {now} lines, within the limit now; run --update to drop it from the list")
        elif now < was:
            found.append(f"{path}: shrank from {was} to {now} lines; run --update to record the smaller size")
    return found


def raised(new: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """Entries of a new baseline that allow more than the current one does."""
    return [
        f"{path}: {baseline[path]} -> {lines} lines" if path in baseline else f"{path}: {lines} lines, not listed before"
        for path, lines in new.items()
        if lines > baseline.get(path, LIMIT)
    ]


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def staged_baseline(root: Path = ROOT) -> dict[str, int]:
    shown = _git(root, "show", ":" + BASELINE_PATH, check=False)
    return json.loads(shown.stdout.decode("utf-8")) if shown.returncode == 0 else {}


def write_baseline(entries: dict[str, int], path: Path = BASELINE) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(sorted(entries.items())), indent=2, ensure_ascii=False) + "\n")


def comparison_ref(requested: str | None) -> str | None:
    """Resolve a trusted commit; a missing explicit ref never disables the check."""
    if requested is not None and not requested.strip():
        raise ValueError(f"cannot resolve comparison ref: {requested!r}")
    result = _git(
        ROOT, "rev-parse", "--verify", "--end-of-options",
        (requested if requested is not None else "HEAD") + "^{commit}", check=False,
    )
    if result.returncode:
        if requested is not None:
            raise ValueError(f"cannot resolve comparison ref: {requested}")
        return None  # initial repository: no legacy exemptions exist yet
    return result.stdout.decode("ascii").strip()


def reference_baseline(reference: str | None) -> dict[str, int]:
    if reference is None:
        return {}
    shown = _git(ROOT, "show", reference + ":" + BASELINE_PATH, check=False)
    return json.loads(shown.stdout.decode("utf-8")) if shown.returncode == 0 else {}


def hard_limit_findings(
    sizes: dict[str, int], reference: str | None, *, staged: bool, intermediate: bool,
    head: str | None = None,
) -> list[str]:
    """Enforce the legacy ceiling on changed files, not unrelated existing code."""
    if reference is None:
        changed = set(sizes)
    else:
        args = ["diff", "--name-only", "-z", "--no-renames"]
        if staged:
            args.append("--cached")
        changed = set(_git(ROOT, *args, reference, *([head] if head else []), "--").stdout.decode("utf-8").split("\0"))
    found = []
    for path in sorted(changed & sizes.keys()):
        now = sizes[path]
        if now <= LEGACY_LIMIT:
            continue
        if intermediate and reference is not None:
            old = _git(ROOT, "show", reference + ":" + path, check=False)
            if old.returncode == 0 and now < count_lines(old.stdout):
                continue  # strictly shrinking staged step, not a completed PR
        found.append(
            f"{path}: {now} lines, over the {LEGACY_LIMIT}-line hard limit "
            "for changed legacy files; finish the split"
        )
    return found


def history_findings(reference: str, head: str = "HEAD") -> list[str]:
    """A later commit cannot undo a tightening, even if the final budget shrank."""
    revisions = _git(ROOT, "rev-list", reference + ".." + head).stdout.decode("ascii").splitlines()
    found = []
    for revision in revisions:
        parent = _git(ROOT, "rev-parse", revision + "^", check=False)
        before = reference_baseline(parent.stdout.decode("ascii").strip()) if parent.returncode == 0 else {}
        for growth in raised(reference_baseline(revision), before):
            found.append(f"{revision[:12]}: committed baseline grew: {growth}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check the files and baseline staged for the next commit")
    parser.add_argument("--update", action="store_true", help="rewrite the baseline after files shrink")
    parser.add_argument("--base-ref", help="trusted PR base or previous main commit (required in CI)")
    parser.add_argument("--head-ref", help="read an untrusted PR tree as Git objects; requires --base-ref")
    args = parser.parse_args(argv)
    if args.staged and args.update:
        parser.error("--update records the working tree; stage the new baseline afterwards")
    if args.head_ref is not None and (args.base_ref is None or args.staged or args.update):
        parser.error("--head-ref requires --base-ref and cannot be combined with --staged/--update")

    try:
        reference = comparison_ref(args.base_ref)
        previous = reference_baseline(reference)
        head = comparison_ref(args.head_ref) if args.head_ref is not None else None
        sizes = revision_sizes(head) if head else (staged_sizes() if args.staged else tracked_sizes())
        baseline = reference_baseline(head) if head else (staged_baseline() if args.staged else load_baseline())
        growth = raised(baseline, previous)
        if args.update:
            new = recorded(sizes)
            growth.extend(raised(new, previous))
            if growth:
                print("\n".join(dict.fromkeys(growth)))
                print("refusing to raise the committed baseline: split the file; growth waivers are not allowed")
                return 1
            write_baseline(new)
            print(f"{BASELINE.name}: {len(new)} source files over {LIMIT} lines")
            return 0

        found = growth + findings(sizes, baseline)
        if args.base_ref is not None:
            found.extend(history_findings(reference, head or "HEAD"))
        found.extend(hard_limit_findings(
            sizes, reference, staged=args.staged,
            intermediate=args.staged and args.base_ref is None, head=head,
        ))
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"source-size check failed: {error}")
        return 1
    if found:
        print("\n".join(found))
        print(f"{len(found)} finding(s); the rules and how to update {BASELINE_PATH} are in scripts/check_file_sizes.py")
        return 1
    print(f"ok: {len(sizes)} source files, {len(baseline)} listed over {LIMIT} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
