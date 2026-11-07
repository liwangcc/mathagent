"""Freeze the app's route table and OpenAPI document as the contract.

Run only when a route change is intended; say why in the commit message.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HELPER = ROOT / "tests" / "route_contract.py"


def _load_helper():
    # Imported by path: `import tests.*` can resolve to another `tests` package.
    spec = importlib.util.spec_from_file_location("_route_contract", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOLDEN = ROOT / "tests" / "golden" / "route_table.json"


def main() -> int:
    fresh = _load_helper().observed()
    previous = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    before = previous.get("routes", [])
    after = fresh["routes"]
    print(f"routes: {len(before)} -> {len(after)}")
    if before != after:
        for index, (old, new) in enumerate(zip(before, after)):
            if old != new:
                print(f"  first change at #{index}: {old} -> {new}")
                break
    print(f"openapi_sha256: {previous.get('openapi_sha256', '(none)')} -> {fresh['openapi_sha256']}")
    print(f"written: {GOLDEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())