"""Inject (or refresh) the lesson-flow presentation layer into v4 chapter HTML.

The layer is two exactly reversible blocks:

* ``<style id="lesson-flow-style">…</style>`` immediately before ``</head>``
* ``<script id="lesson-flow">…</script>`` immediately before ``</body>``

Removing both blocks gives back the chapter's base HTML byte for byte; the
manifest records that base hash so tests can prove the mathematics, scene
scripts and vendored libraries were not edited.  The public sample copy and
``catalog_v4.json`` sha256 are updated in the same step.

``--restore-archive`` rebuilds a chapter from its catalogued v4 source archive
entry, and ``--restore-from FILE`` takes an already rebuilt file.  Both are only
used when the working file no longer matches the catalog: the bytes must hash
to the catalogued sha256, so the restored base is the catalogued chapter, not a
new variant.

Usage (repo root):
    ./venv_local/Scripts/python.exe scripts/courseware_lesson_layer/apply.py P05 J08
    ./venv_local/Scripts/python.exe scripts/courseware_lesson_layer/apply.py P10 --restore-archive D:/浏览器下载
    ./venv_local/Scripts/python.exe scripts/courseware_lesson_layer/apply.py H12 --restore-from rebuilt/H12.html --origin "..."
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CATALOG = ROOT / "courseware" / "catalog_v4.json"
MANIFEST = HERE / "manifest.json"
STYLE_OPEN, STYLE_CLOSE = '<style id="lesson-flow-style">', "</style>"
SCRIPT_OPEN, SCRIPT_CLOSE = '<script id="lesson-flow">', "</script>"
STYLE_RE = re.compile(re.escape(STYLE_OPEN) + r".*?" + re.escape(STYLE_CLOSE) + r"(?=</head>|<main>)", re.S)
SCRIPT_RE = re.compile(re.escape(SCRIPT_OPEN) + r".*?" + re.escape(SCRIPT_CLOSE) + r"(?=</body>|</html>)", re.S)
# Render-only depth upgrade for 3D solids: inserted right before the chapter's app script
# (so the first frame uses it), only where a known 3D renderer is present.
SOLID_OPEN, SOLID_CLOSE = '<script id="solid-depth">', "</script>"
SOLID_RE = re.compile(re.escape(SOLID_OPEN) + r".*?" + re.escape(SOLID_CLOSE), re.S)
# Offline pages that ship with the courseware library but are not catalog_v4 chapters
# (public copy kept byte-identical; there is no catalog hash to update).
EXTRA_PAGES = {
    "SPACE": {"coursewarePath": "courseware/高中/空间几何关系.html", "publicPath": "frontend/public/courseware/samples/高中/空间几何关系.html"},
}
SOLID_ANCHORS = (
    # (renderer marker in the base, anchor = start of the app script the block must precede)
    ("var W3 = (function", "<script>/* All parameter changes (pointer, slider, numeric input, keyboard, preset, animation)"),
    ("const Space={tried:false", "<script>/* P10 — dimensions"),
    ("const G3=(()=>", "<script>/* Shared UI shell."),
    ("root.H16Space=", "<script>/* MathAgent H16. Each scene owns"),
    # teacher-made 空间几何关系: its app script builds on load, so the block goes after it
    ("function panel(color,y=0,angle=0)", "</html>"),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_layer(html: str) -> str:
    """Return the chapter without the lesson layer (inverse of inject)."""
    return SOLID_RE.sub("", SCRIPT_RE.sub("", STYLE_RE.sub("", html, count=1), count=1), count=1)


def solid_part() -> str:
    js = (HERE / "solid-depth.js").read_text(encoding="utf-8").strip()
    for bad in ("</script", "http://", "https://"):
        assert bad not in js, f"solid-depth must not contain {bad!r}"
    return js


def solid_anchor(base: str) -> str | None:
    for marker, anchor in SOLID_ANCHORS:
        if marker in base:
            assert base.count(anchor) == 1, f"solid-depth anchor not unique: {anchor[:40]}"
            return anchor
    return None


def layer_parts() -> tuple[str, str]:
    css = (HERE / "lesson-flow.css").read_text(encoding="utf-8").strip()
    js = (HERE / "lesson-flow.js").read_text(encoding="utf-8").strip()
    for bad in ("</style", "</script", "http://", "https://"):
        assert bad not in css and bad not in js, f"layer must not contain {bad!r}"
    return css, js


def inject(base: str, css: str, js: str, solid: str | None = None) -> str:
    # Pages without explicit </head> / </body> (implicit in HTML) use <main> / </html>.
    head = "</head>" if "</head>" in base else "<main>"
    body = "</body>" if "</body>" in base else "</html>"
    assert base.count(head) == 1 and base.count(body) == 1, f"expected exactly one {head} and {body}"
    assert STYLE_OPEN not in base and SCRIPT_OPEN not in base and SOLID_OPEN not in base, "base still contains a lesson layer"
    html = base
    anchor = solid_anchor(base) if solid else None
    if anchor:
        html = html.replace(anchor, f"{SOLID_OPEN}{solid}{SOLID_CLOSE}{anchor}", 1)
    html = html.replace(head, f"{STYLE_OPEN}{css}{STYLE_CLOSE}{head}", 1)
    return html.replace(body, f"{SCRIPT_OPEN}{js}{SCRIPT_CLOSE}{body}", 1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("codes", nargs="+")
    parser.add_argument("--restore-archive", metavar="DIR", help="rebuild the base from the catalogued v4 archive in DIR")
    parser.add_argument("--restore-from", metavar="FILE", help="use FILE as the base; it must hash to the catalogued sha256")
    parser.add_argument("--origin", default="", help="provenance note recorded with --restore-from")
    args = parser.parse_args(argv)
    assert not (args.restore_archive and args.restore_from), "choose one restore source"
    assert not args.restore_from or len(args.codes) == 1, "--restore-from takes exactly one chapter"
    catalog_raw = CATALOG.read_bytes()
    catalog = json.loads(catalog_raw.decode("utf-8"))
    entries = {item["code"]: item for item in catalog["entries"]}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"layer": {}, "chapters": {}}
    css, js = layer_parts()
    solid = solid_part()
    manifest["layer"] = {"version": 1, "css_sha256": sha(css.encode()), "js_sha256": sha(js.encode()), "solid_depth_sha256": sha(solid.encode())}
    for code in args.codes:
        extra = code in EXTRA_PAGES
        entry = dict(EXTRA_PAGES[code]) if extra else entries[code]
        assert not (extra and (args.restore_archive or args.restore_from)), "extra pages have no catalogued source to restore from"
        source = ROOT / entry["coursewarePath"]
        public = ROOT / entry["publicPath"]
        if args.restore_archive:
            archive = Path(args.restore_archive) / entry["sourceArchive"]
            data = zipfile.ZipFile(archive).read(entry["sourceEntry"])
            expected = manifest["chapters"].get(code, {}).get("base_sha256") or entry["sha256"]
            assert sha(data) == expected, f"{code}: archive entry {sha(data)[:12]} != catalogued {expected[:12]}"
            base = data.decode("utf-8")
            origin = f"archive:{entry['sourceArchive']}!{entry['sourceEntry']}"
        elif args.restore_from:
            data = Path(args.restore_from).read_bytes()
            expected = manifest["chapters"].get(code, {}).get("base_sha256") or entry["sha256"]
            assert sha(data) == expected, f"{code}: {args.restore_from} {sha(data)[:12]} != catalogued {expected[:12]}"
            base = data.decode("utf-8")
            origin = "catalog-verified rebuild" + (f": {args.origin}" if args.origin else "")
        else:
            # bytes, not read_text(): universal newlines would rewrite CRLF inside vendored libraries
            raw = source.read_bytes()
            base = strip_layer(raw.decode("utf-8"))
            if STYLE_OPEN.encode() not in raw:
                assert base.encode("utf-8") == raw, f"{code}: base must be the file's exact bytes"
            origin = manifest["chapters"].get(code, {}).get("base_origin", "working-tree")
        base_hash = sha(base.encode("utf-8"))
        known = manifest["chapters"].get(code, {}).get("base_sha256")
        assert known in (None, base_hash), f"{code}: base drifted from manifest ({known[:12]} -> {base_hash[:12]})"
        html = inject(base, css, js, solid)
        assert strip_layer(html) == base, f"{code}: layer is not exactly reversible"
        has_solid = SOLID_OPEN in html
        data = html.encode("utf-8")
        old_hash = sha(source.read_bytes()) if extra else entry["sha256"]
        source.write_bytes(data)
        public.write_bytes(data)
        new_hash = sha(data)
        if old_hash != new_hash and not extra:
            assert catalog_raw.count(old_hash.encode()) == 1, f"{code}: catalog hash not unique"
            catalog_raw = catalog_raw.replace(old_hash.encode(), new_hash.encode(), 1)
            entry["sha256"] = new_hash
        manifest["chapters"][code] = {
            "path": entry["coursewarePath"],
            "base_origin": origin,
            "base_sha256": base_hash,
            "sha256": new_hash,
            "solid_depth": has_solid,
            "catalog": not extra,
        }
        print(f"{code}: base {base_hash[:12]} ({origin}) -> {new_hash[:12]}  catalog {old_hash[:12]} -> {new_hash[:12]}")
    CATALOG.write_bytes(catalog_raw)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
