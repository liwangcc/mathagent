"""services/accelerator_jobs_*.py import each other in one direction only.

services/accelerator_jobs.py is split into one module per part of running
accelerator jobs (configuration, input identity, nodes, the queue, leases,
result validation, completion); each imports only the parts below it, and
accelerator_jobs.py re-exports their names.

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
    "accelerator_jobs_base",
    "accelerator_jobs_identity",
    "accelerator_jobs_nodes",
]
# The module every caller imports: it may use any layer, and no layer imports it.
TOP = ["accelerator_jobs"]

_PROBE = """
import json, sys
loaded = {}
for name in sys.argv[1:]:
    __import__("services." + name)
    loaded[name] = sorted(m.split(".")[1] for m in sys.modules if m.startswith("services.accelerator_jobs"))
print(json.dumps(loaded))
"""


def loaded_after_importing(*names: str) -> dict[str, list[str]]:
    """The services.accelerator_jobs* modules loaded after each import, in a fresh interpreter."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, *names],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_every_accelerator_jobs_module_has_a_place():
    files = {path.stem for path in (ROOT / "services").glob("accelerator_jobs*.py")}
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
