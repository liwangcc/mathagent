from __future__ import annotations

import sys
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

for path in (REPOSITORY_ROOT, WORKER_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
