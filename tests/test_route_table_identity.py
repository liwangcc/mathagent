"""Splitting a module must not move a route or change the API contract.

This is a behavioural contract test: it imports the real app, asks it for its
route table and its OpenAPI document, and compares both with the frozen copy in
tests/golden/route_table.json. Nothing here reads source text.

A failure means the app no longer answers the same API. That is sometimes
intended (a route is added, removed or renamed). When it is intended, freeze it
in the same commit and say why:

    python scripts/freeze_route_table.py
"""

import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden" / "route_table.json"
HELPER = Path(__file__).resolve().parent / "route_contract.py"


def _observed() -> dict:
    # Loaded by path: `import tests.*` can resolve to another `tests` package
    # that is earlier on sys.path in this environment.
    import importlib.util

    spec = importlib.util.spec_from_file_location("_route_contract", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.observed()


def _golden() -> dict:
    assert GOLDEN.exists(), (
        f"{GOLDEN} is missing; restore it from git or run scripts/freeze_route_table.py"
    )
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _describe_route(route) -> str:
    path, methods, endpoint = route
    return f"{','.join(methods) or '-'} {path} -> {endpoint}"


def test_route_table_is_unchanged():
    golden = _golden()
    fresh = _observed()
    expected, actual = golden["routes"], fresh["routes"]

    if expected != actual:
        lines = [f"route table changed: {len(expected)} -> {len(actual)}"]
        for index, (old, new) in enumerate(zip(expected, actual)):
            if old != new:
                lines.append(f"first difference at #{index}:")
                lines.append(f"  expected {_describe_route(old)}")
                lines.append(f"  actual   {_describe_route(new)}")
                break
        else:
            longer = actual[len(expected):] if len(actual) > len(expected) else expected[len(actual):]
            what = "added" if len(actual) > len(expected) else "removed"
            lines.append(f"routes {what} at the end:")
            lines.extend(f"  {_describe_route(route)}" for route in longer[:10])
        lines.append("")
        lines.append("If the change is intended: python scripts/freeze_route_table.py,")
        lines.append("then say in the commit message which route moved and why.")
        raise AssertionError("\n".join(lines))


def test_openapi_document_is_unchanged():
    golden = _golden()
    fresh = _observed()
    if golden["openapi_sha256"] == fresh["openapi_sha256"]:
        return

    expected, actual = golden["openapi_paths"], fresh["openapi_paths"]
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(
        path for path in set(expected) & set(actual) if expected[path] != actual[path]
    )
    lines = [
        "the OpenAPI document changed",
        f"  sha256 {golden['openapi_sha256']} -> {fresh['openapi_sha256']}",
    ]
    for label, paths in (("added", added), ("removed", removed), ("operations changed", changed)):
        if paths:
            lines.append(f"  {label}: {', '.join(paths[:10])}")
    lines.append("")
    lines.append("If the change is intended: python scripts/freeze_route_table.py,")
    lines.append("and describe the contract change in the commit message.")
    raise AssertionError("\n".join(lines))