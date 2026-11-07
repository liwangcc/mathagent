"""Keep large source files from growing, and new source files from becoming large.

Most of this repository's largest files grew one reasonable change at a time: a
branch in the nearest function, a helper beside the code that needed it.  This
check makes each such step visible.

* A source file not listed in ``file_size_baseline.json`` stays within 800
  lines: put new work in a new module instead.
* A listed file must not grow past its recorded size.
* The list must be current.  A listed file that shrank, fell within the limit,
  moved or was deleted fails until the list is rewritten, so recorded sizes
  only ever go down.

Source files are the tracked ``*.py`` files and the ``.vue``, ``.js`` and
``.ts`` files under ``frontend/src``; tests are not counted.

    python scripts/check_file_sizes.py                          # check; exit 1 on any finding
    python scripts/check_file_sizes.py --staged                 # check what the next commit holds
    python scripts/check_file_sizes.py --update                 # record sizes after files shrank
    python scripts/check_file_sizes.py --update --accept-growth

``--update`` refuses to raise a recorded size or to list a new file unless
``--accept-growth`` is given.  Accept growth only for a fix that has no smaller
place to live, in the same commit, and say why in the commit message.

``--staged`` reads the files and the baseline from the index instead of the
working tree, so unstaged edits neither fail nor pass a commit; a pre-commit
hook runs it.

This is a script, not a test: tests assert behaviour and never the text of
source files (tests/test_source_text_coupling.py), and splitting a file must not
fail the test suite.
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
LIMIT = 800

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="check the files and baseline staged for the next commit")
    parser.add_argument("--update", action="store_true", help="rewrite the baseline from the current sizes")
    parser.add_argument(
        "--accept-growth", action="store_true",
        help="with --update: allow raising a recorded size or listing a new file",
    )
    args = parser.parse_args(argv)
    if args.accept_growth and not args.update:
        parser.error("--accept-growth only applies with --update")
    if args.staged and args.update:
        parser.error("--update records the working tree; stage the new baseline afterwards")

    sizes = staged_sizes() if args.staged else tracked_sizes()
    baseline = staged_baseline() if args.staged else load_baseline()
    if args.update:
        new = recorded(sizes)
        growth = raised(new, baseline)
        if growth and not args.accept_growth:
            print("\n".join(growth))
            print("refusing to raise the baseline: split the file, or pass --accept-growth and say why in the commit")
            return 1
        write_baseline(new)
        print(f"{BASELINE.name}: {len(new)} source files over {LIMIT} lines")
        return 0

    found = findings(sizes, baseline)
    if found:
        print("\n".join(found))
        print(f"{len(found)} finding(s); the rules and how to update {BASELINE_PATH} are in scripts/check_file_sizes.py")
        return 1
    print(f"ok: {len(sizes)} source files, {len(baseline)} listed over {LIMIT} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
