"""services/template_column_layout_*.py import each other in one direction only.

services/template_column_layout.py is split into one module per part of
recognizing a template's column layout (the layout contract, the template image,
the provider route, prompts, local evidence, drafts, recognition); each imports
only the parts below it, and template_column_layout.py re-exports their names.

Checked by what importing does, not by reading the files: a fresh interpreter
imports the layers from the lowest up, and each import may load only itself and
the layers below it.  (An import inside a function body runs later and is not
seen here; keep cross-module imports at the top of the file.)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Lowest first: a module may import only the modules listed before it.
LAYERS = [
    "template_column_layout_contract",
    "template_column_layout_image",
]
# The module every caller imports: it may use any layer, and no layer imports it.
TOP = ["template_column_layout"]

_PROBE = """
import json, sys
loaded = {}
for name in sys.argv[1:]:
    __import__("services." + name)
    loaded[name] = sorted(m.split(".")[1] for m in sys.modules if m.startswith("services.template_column_layout"))
print(json.dumps(loaded))
"""


def loaded_after_importing(*names: str) -> dict[str, list[str]]:
    """The services.template_column_layout* modules loaded after each import, in a fresh interpreter."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, *names],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_template_column_layout_module_has_a_place():
    files = {path.stem for path in (ROOT / "services").glob("template_column_layout*.py")}
    assert files == set(LAYERS) | set(TOP)


def test_each_layer_loads_only_the_layers_below():
    loaded = loaded_after_importing(*LAYERS)
    upward = {
        name: sorted(set(loaded[name]) - set(LAYERS[: index + 1]))
        for index, name in enumerate(LAYERS)
        if set(loaded[name]) - set(LAYERS[: index + 1])
    }
    assert upward == {}


def test_the_facade_loads_only_layers():
    for name in TOP:
        assert set(loaded_after_importing(name)[name]) - set(LAYERS) == {name}, name
