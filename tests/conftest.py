"""Hermetic defaults for the whole backend test suite.

pytest imports this file before any test module, i.e. before ``config`` runs
``load_dotenv``.  It mirrors ``scripts/run_math_stage_tests.py`` so that a bare
``pytest -q`` is as isolated as that runner:

* the developer's ``.env`` / ``.env.local`` are never loaded, and credential,
  endpoint and proxy variables inherited from the shell are dropped, so neither
  in-process tests nor the subprocesses they spawn (which copy ``os.environ``)
  can see a real provider key;
* SQLite, uploads, outputs and archives live in a disposable temporary root;
* only loopback listeners created by the tests are reachable and remote host
  names do not resolve, so a test that forgets to stub a provider fails like an
  offline machine instead of calling a live API.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

_ROOT_PREFIX = "mathagent-tests-"
_CREDENTIAL_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY")


def _isolate_environment(root: Path) -> None:
    for key in tuple(os.environ):
        upper = key.upper()
        if (
            any(marker in upper for marker in _CREDENTIAL_MARKERS)
            or upper.endswith("_BASE_URL")
            or upper in _PROXY_VARIABLES
        ):
            os.environ.pop(key, None)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["JWT_SECRET"] = "test-only-jwt-secret-with-at-least-32-characters"
    os.environ["REDIS_URL"] = ""
    os.environ["KNOWLEDGE_CLASSIFIER_WORKER_ENABLED"] = "false"
    os.environ["DATABASE_URL"] = f"sqlite:///{(root / 'test.db').as_posix()}"
    os.environ["MATHAGENT_DATA_DIR"] = str(root)


def _redirect_storage(root: Path) -> None:
    import config

    for name in ("UPLOAD_DIR", "OUTPUT_DIR", "DATA_DIR", "FAILED_DIR", "ARCHIVE_DIR"):
        folder = root / name.lower()
        folder.mkdir(exist_ok=True)
        setattr(config, name, str(folder))
    # Importing app.py points tempfile at this spool; unit tests bypass the
    # storage admission that normally creates it.
    (Path(config.UPLOAD_DIR) / ".multipart-tmp").mkdir(exist_ok=True)


def _cleanup(root: Path) -> None:
    database = sys.modules.get("database")
    engine = getattr(database, "engine", None)
    if engine is not None:
        engine.dispose()
    shutil.rmtree(root, ignore_errors=True)


def _remove_stale_roots(max_age_s: float = 12 * 3600) -> None:
    # On Windows a thread-local SQLite handle can outlive the atexit cleanup,
    # so an earlier run may have left its (synthetic) root behind.
    cutoff = time.time() - max_age_s
    for stale in Path(tempfile.gettempdir()).glob(f"{_ROOT_PREFIX}*"):
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass


# ``scripts/run_math_stage_tests.py`` imports config before handing over to
# pytest; its own isolation is already in force and must not be replaced.
if "config" not in sys.modules:
    from scripts.run_math_stage_tests import restrict_network_to_test_servers

    _remove_stale_roots()
    _ROOT = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX))
    atexit.register(_cleanup, _ROOT)
    _isolate_environment(_ROOT)
    restrict_network_to_test_servers()
    _redirect_storage(_ROOT)
