"""What every step of template evidence shares.

- The image runtimes, loaded on first use (``_LazyModule``): ``cv2``, ``np`` and
  the Pillow modules ``Image``, ``ImageDraw``, ``ImageFont``, ``ImageOps``.  Only
  node-side pixel operations load them; importing template evidence does not.
- Limits and patterns several steps check: ``MAX_IMAGE_EDGE``,
  ``MAX_SOURCE_PIXELS``, ``_PAGE_TYPE_RE``, ``_SHA256_RE``.
- ``TemplateEvidenceError``, and ``EvidenceResult``, what an evidence build returns.

Moved unchanged from services/template_evidence.py, which still exposes these names.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any


class _LazyModule:
    """Load image runtimes only inside node-side pixel operations."""

    def __init__(self, module_name: str):
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_module", None)

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(
                object.__getattribute__(self, "_module_name")
            )
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, name: str):
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._load(), name, value)


cv2 = _LazyModule("cv2")


np = _LazyModule("numpy")


Image = _LazyModule("PIL.Image")


ImageDraw = _LazyModule("PIL.ImageDraw")


ImageFont = _LazyModule("PIL.ImageFont")


ImageOps = _LazyModule("PIL.ImageOps")


MAX_IMAGE_EDGE = 2200


MAX_SOURCE_PIXELS = 16_000_000


_PAGE_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class TemplateEvidenceError(ValueError):
    """Raised when an artifact or student page cannot be used safely."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class EvidenceResult:
    page_type_id: str
    evidence_path: str
    question_numbers: list[str]
    blank_question_numbers: list[str]
    diagnostics: dict[str, Any]
    aligned_path: str = ""
    evidence_paths: tuple[str, ...] = ()
    # Opt-in (``match_and_build(include_source_mask=True)``): 255 where the
    # aligned pixel comes from real student-page pixels, 0 where it is
    # perspective fill.  Never persisted; excluded from comparisons.
    aligned_source_mask: Any = field(default=None, compare=False, repr=False)
