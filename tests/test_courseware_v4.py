import hashlib
import json
import re
from pathlib import Path

from services.courseware import bundled_courseware_for_stage


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "courseware" / "catalog_v4.json"
STAGES = {"primary": 17, "junior": 20, "senior": 21}
EXTERNAL_ASSET = re.compile(
    r"<(?:script|link|img)\b[^>]*(?:src|href)\s*=\s*['\"](?:https?:)?//",
    re.IGNORECASE,
)


def test_v4_catalog_contains_all_imported_chapters_and_matching_public_copies():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert len(catalog["entries"]) == sum(STAGES.values())

    for stage, expected_count in STAGES.items():
        entries = [item for item in catalog["entries"] if item["stage"] == stage]
        assert len(entries) == expected_count
        assert len({item["offlineFileName"] for item in entries}) == expected_count
        for item in entries:
            source = ROOT / item["coursewarePath"]
            public = ROOT / item["publicPath"]
            html = source.read_text(encoding="utf-8")
            assert source.is_file() and public.is_file()
            assert source.read_bytes() == public.read_bytes()
            assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest(), item["code"]
            assert 'aria-live="polite"' in html
            assert re.search(r"pointerdown|mousedown|touchstart", html)
            assert "keydown" in html
            assert "prefers-reduced-motion" in html
            assert EXTERNAL_ASSET.search(html) is None


def test_v4_catalog_is_exposed_by_the_courseware_service():
    for stage, expected_count in STAGES.items():
        rows = bundled_courseware_for_stage(stage)
        assert len(rows) == expected_count
        assert all(Path(row["stored_path"]).is_file() for row in rows)
