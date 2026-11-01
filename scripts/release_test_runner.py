"""Run the release test suite with an isolated, disposable test environment.

This is deliberately a file-backed runner rather than ``python -c`` or stdin:
Windows multiprocessing tests need a real ``__main__`` path when they spawn a
child process.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import sys
import tempfile


def _remove_test_database(path: Path) -> None:
    try:
        # The application keeps a module-level SQLAlchemy engine. Dispose it
        # before removing the temporary SQLite file on Windows, where an open
        # handle otherwise leaves a harmless but noisy atexit warning.
        import database

        engine = getattr(database, "engine", None)
        if engine is not None:
            engine.dispose()
    except Exception:
        pass
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except (FileNotFoundError, PermissionError):
            pass


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit(
            "usage: release_test_runner.py RELEASE_ID TEST_JWT_SECRET PYTEST_ARGS..."
        )
    release_id, test_secret = argv[1:3]
    test_db = Path(tempfile.gettempdir()) / f"mathagent-release-tests-{release_id}.db"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["JWT_SECRET"] = test_secret
    os.environ["DATABASE_URL"] = "sqlite:///" + test_db.as_posix()
    atexit.register(_remove_test_database, test_db)

    import pytest

    return int(pytest.main(argv[3:]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
