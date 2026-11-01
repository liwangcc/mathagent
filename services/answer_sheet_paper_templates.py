"""Reusable, teacher-owned paper templates for answer-sheet batches.

A paper template is the semantic layer of answer-sheet grading: stems,
options, model-generated answers that a teacher confirmed, scores, sub-question
structure and grading rules.  It is registered from the existing
detailed-grading template confirmation chain (``TemplateSession``) and frozen
with an ``answer_spec_hash``.  Batches bind it by code and never edit it, so one
confirmation is reused across batches.

Codes are derived from the frozen content (including the owner), so registering
the same confirmed paper again is idempotent.  Standard answers are private to
the owning teacher; unlike answer-sheet layout codes they are never shared.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR
from services.answer_sheet_template_registry import (
    _registry_lock,
    _replace_with_retry,
    _stable_json,
    _write_json_atomic,
)
from services.grading_contract import (
    VALID_GRADING_CATEGORIES,
    AnswerSpec,
    finalize_answer_spec_for_category,
)
from services.teaching_context import validate_teaching_context


PAPER_TEMPLATE_CONTRACT_VERSION = "answer-sheet-paper-template-v1"
SOURCE_KIND = "detailed_grading_template"
MAX_ALIAS_LENGTH = 40
MAX_LISTED_SOURCES = 30
_MAX_SCANNED_SOURCES = 400
_CODE_RE = re.compile(r"^PT-[0-9A-F]{4}(?:-[0-9A-F]{4}){3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_STATUSES = {"confirmed", "frozen"}


class PaperTemplateNotFound(ValueError):
    """The code does not exist for this teacher."""


class PaperTemplateRejected(ValueError):
    """The source template cannot become an answer-sheet paper template."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_paper_template_code(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    compact = text.replace("-", "")
    if compact.startswith("PT"):
        compact = compact[2:]
    if len(compact) == 16 and all(char in "0123456789ABCDEF" for char in compact):
        text = "PT-" + "-".join(compact[index:index + 4] for index in range(0, 16, 4))
    if not _CODE_RE.fullmatch(text):
        raise ValueError("试卷模板号格式无效")
    return text


def _paper_code(fingerprint: str) -> str:
    compact = fingerprint[:16].upper()
    return "PT-" + "-".join(compact[index:index + 4] for index in range(0, 16, 4))


def normalize_alias(value: object) -> str:
    alias = " ".join(str(value or "").split())
    if len(alias) > MAX_ALIAS_LENGTH or any(ord(char) < 32 for char in alias):
        raise ValueError(f"模板别名不能超过 {MAX_ALIAS_LENGTH} 个字符")
    return alias


def paper_answer_spec_hash(
    questions: list[dict[str, Any]],
    teaching_context: dict,
    scoring_category: str,
) -> str:
    """Stable identity of what a student is graded against."""
    payload = {
        "questions": questions,
        "teaching_context": teaching_context,
        "scoring_category": scoring_category,
    }
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def detailed_template_contract(template_session: Any) -> dict[str, Any]:
    """Snapshot a teacher-confirmed detailed-grading template.

    Fail closed for anything the answer-sheet chain cannot grade faithfully:
    unconfirmed or stale confirmations, legacy stage-less contracts, and
    drawing questions (answer sheets carry no drawing evidence).
    """
    if template_session.status not in _SOURCE_STATUSES:
        raise PaperTemplateRejected("详细批改模板尚未完成教师确认")
    contract_error = template_session.recognition_contract_error()
    if contract_error:
        raise PaperTemplateRejected(contract_error)
    category = str(template_session.category or "").strip().lower()
    if category not in VALID_GRADING_CATEGORIES:
        raise PaperTemplateRejected("详细批改模板缺少已确认的评分方式，请重新确认模板")
    teaching_context = validate_teaching_context(template_session.teaching_context)
    if not teaching_context:
        raise PaperTemplateRejected("详细批改模板缺少学段，请重新导入并确认模板")

    from services.template_session import page_is_graded

    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in sorted(
        template_session.pages or [],
        key=lambda item: int(item.get("page_index", 0)),
    ):
        if not page_is_graded(page):
            continue
        for raw in page.get("questions") or []:
            spec = finalize_answer_spec_for_category(AnswerSpec.model_validate(raw), category)
            if not spec.question_no or spec.question_no in seen:
                raise PaperTemplateRejected("试卷模板题号为空或重复")
            if spec.answer_kind in {"drawing", "mixed"}:
                raise PaperTemplateRejected(
                    f"第{spec.question_no}题为作图题，答题卡批阅暂不支持作图题"
                )
            if not spec.standard_answer:
                raise PaperTemplateRejected(f"第{spec.question_no}题缺少已确认的标准答案")
            seen.add(spec.question_no)
            questions.append(spec.model_dump(mode="json"))
    if not questions:
        raise PaperTemplateRejected("详细批改模板没有可批阅的题目")
    return {
        "teaching_context": teaching_context,
        "scoring_category": category,
        "exam_name": str(template_session.exam_name or "").strip(),
        "questions": questions,
    }


def list_detailed_template_sources(
    upload_dir: str,
    teacher_id: int,
    *,
    limit: int = MAX_LISTED_SOURCES,
) -> list[dict[str, Any]]:
    """Return the teacher's confirmed detailed-grading templates, newest first."""
    from services.template_session import TemplateSession, page_is_graded

    root = Path(upload_dir)
    if not root.is_dir():
        return []
    candidates: list[tuple[float, str]] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir() and entry.name.startswith("template_"):
                meta = Path(entry.path) / "meta.json"
                try:
                    candidates.append((meta.stat().st_mtime, entry.name[len("template_"):]))
                except OSError:
                    continue
    candidates.sort(reverse=True)
    sources: list[dict[str, Any]] = []
    for _mtime, template_id in candidates[:_MAX_SCANNED_SOURCES]:
        try:
            with (root / f"template_{template_id}" / "meta.json").open(
                "r", encoding="utf-8",
            ) as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("teacher_id") != teacher_id
            or raw.get("status") not in _SOURCE_STATUSES
            or not raw.get("confirmed_contract_hash")
        ):
            continue
        try:
            session = TemplateSession.load_meta(template_id, upload_dir)
            if session.recognition_contract_error():
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        question_count = sum(
            len(page.get("questions") or [])
            for page in session.pages
            if page_is_graded(page)
        )
        sources.append({
            "template_id": template_id,
            "exam_name": session.exam_name,
            "scoring_category": session.category,
            "question_count": question_count,
            "confirmed_at": session.confirmed_at,
            "status": session.status,
        })
        if len(sources) >= limit:
            break
    return sources


class PaperTemplateRegistry:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root or (Path(DATA_DIR) / "answer_sheet_paper_templates"))

    def _teacher_dir(self, teacher_id: int) -> Path:
        return self.root / "templates" / f"teacher_{int(teacher_id)}"

    def _index_path(self, teacher_id: int) -> Path:
        return self.root / "index" / f"teacher_{int(teacher_id)}.json"

    def _read_index(self, teacher_id: int) -> list[dict[str, Any]]:
        path = self._index_path(teacher_id)
        if not path.is_file():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []

    def load(self, code: object, teacher_id: int) -> dict[str, Any]:
        """Load one frozen template after ownership and integrity checks."""
        normalized = normalize_paper_template_code(code)
        manifest_path = self._teacher_dir(teacher_id) / normalized / "manifest.json"
        if not manifest_path.is_file():
            raise PaperTemplateNotFound("试卷模板不存在或不属于当前教师")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PaperTemplateNotFound("试卷模板不存在或不属于当前教师") from exc
        content = manifest.get("content") if isinstance(manifest, dict) else None
        fingerprint = str(manifest.get("fingerprint") or "") if isinstance(manifest, dict) else ""
        if (
            manifest.get("version") != PAPER_TEMPLATE_CONTRACT_VERSION
            or manifest.get("template_code") != normalized
            or not isinstance(content, dict)
            or content.get("owner_teacher_id") != int(teacher_id)
            or not _SHA256_RE.fullmatch(fingerprint)
            or hashlib.sha256(_stable_json(content).encode("utf-8")).hexdigest() != fingerprint
            or _paper_code(fingerprint) != normalized
            or manifest.get("answer_spec_hash") != paper_answer_spec_hash(
                content.get("questions") or [],
                content.get("teaching_context") or {},
                str(content.get("scoring_category") or ""),
            )
        ):
            raise ValueError("试卷模板数据完整性校验失败")
        return manifest

    def register(
        self,
        template_session: Any,
        *,
        teacher_id: int,
        alias: str = "",
    ) -> dict[str, Any]:
        """Freeze a confirmed detailed-grading template; idempotent per content."""
        if template_session.teacher_id != int(teacher_id):
            raise PaperTemplateNotFound("详细批改模板不存在或不属于当前教师")
        contract = detailed_template_contract(template_session)
        clean_alias = normalize_alias(alias)
        content = {
            "contract_version": PAPER_TEMPLATE_CONTRACT_VERSION,
            "owner_teacher_id": int(teacher_id),
            **contract,
        }
        fingerprint = hashlib.sha256(_stable_json(content).encode("utf-8")).hexdigest()
        code = _paper_code(fingerprint)
        manifest = {
            "version": PAPER_TEMPLATE_CONTRACT_VERSION,
            "template_code": code,
            "fingerprint": fingerprint,
            "answer_spec_hash": paper_answer_spec_hash(
                contract["questions"],
                contract["teaching_context"],
                contract["scoring_category"],
            ),
            "created_at": _now(),
            # Provenance only: the frozen content, not the mutable source, is
            # the authority once registered.
            "source": {
                "kind": SOURCE_KIND,
                "template_id": str(template_session.template_id),
                "confirmed_answers_hash": str(template_session.confirmed_answers_hash or ""),
                "confirmed_at": template_session.confirmed_at,
            },
            "content": content,
        }
        with _registry_lock(self.root):
            destination = self._teacher_dir(teacher_id) / code
            if destination.exists():
                manifest = self.load(code, teacher_id)
                if manifest.get("fingerprint") != fingerprint:
                    raise ValueError("试卷模板号发生冲突，请联系管理员")
            else:
                temporary = self._teacher_dir(teacher_id) / f".{code}.{uuid.uuid4().hex}.tmp"
                try:
                    temporary.mkdir(parents=True, exist_ok=False)
                    _write_json_atomic(temporary / "manifest.json", manifest)
                    _replace_with_retry(temporary, destination)
                except BaseException:
                    shutil.rmtree(temporary, ignore_errors=True)
                    raise
            self._touch_index(teacher_id, code, alias=clean_alias if clean_alias else None)
        return self.summary(manifest, self._index_entry(teacher_id, code))

    def _index_entry(self, teacher_id: int, code: str) -> dict[str, Any]:
        return next(
            (item for item in self._read_index(teacher_id) if item.get("template_code") == code),
            {},
        )

    def _touch_index(
        self,
        teacher_id: int,
        code: str,
        *,
        alias: str | None = None,
        used: bool = False,
    ) -> None:
        """Update the private index; the caller holds the registry lock."""
        entries = self._read_index(teacher_id)
        now = _now()
        entry = next((item for item in entries if item.get("template_code") == code), None)
        if entry is None:
            entry = {"template_code": code, "alias": "", "registered_at": now}
            entries.insert(0, entry)
        if alias is not None:
            entry["alias"] = alias
        if used:
            entry["last_used_at"] = now
        _write_json_atomic(self._index_path(teacher_id), entries)

    def set_alias(self, teacher_id: int, code: object, alias: object) -> dict[str, Any]:
        normalized = normalize_paper_template_code(code)
        manifest = self.load(normalized, teacher_id)
        clean_alias = normalize_alias(alias)
        with _registry_lock(self.root):
            self._touch_index(teacher_id, normalized, alias=clean_alias)
        return self.summary(manifest, self._index_entry(teacher_id, normalized))

    def remember(self, teacher_id: int, code: object) -> None:
        normalized = normalize_paper_template_code(code)
        self.load(normalized, teacher_id)
        with _registry_lock(self.root):
            self._touch_index(teacher_id, normalized, used=True)

    def list_templates(self, teacher_id: int) -> list[dict[str, Any]]:
        with _registry_lock(self.root):
            entries = self._read_index(teacher_id)
        result = []
        for entry in entries:
            try:
                manifest = self.load(entry.get("template_code"), teacher_id)
            except ValueError:
                continue
            result.append(self.summary(manifest, entry))
        result.sort(
            key=lambda item: str(item.get("last_used_at") or item.get("registered_at") or ""),
            reverse=True,
        )
        return result

    @staticmethod
    def summary(manifest: dict[str, Any], entry: dict[str, Any] | None = None) -> dict[str, Any]:
        content = manifest["content"]
        counts = {"choice": 0, "fill": 0, "solve": 0}
        for question in content.get("questions") or []:
            question_type = str(question.get("question_type") or "")
            if question_type in counts:
                counts[question_type] += 1
        entry = entry or {}
        summary = {
            "template_code": manifest["template_code"],
            "alias": str(entry.get("alias") or ""),
            "answer_spec_hash": manifest["answer_spec_hash"],
            "scoring_category": content.get("scoring_category"),
            "exam_name": content.get("exam_name") or "",
            "question_count": len(content.get("questions") or []),
            "question_counts": counts,
            "created_at": manifest.get("created_at"),
            "registered_at": entry.get("registered_at") or manifest.get("created_at"),
        }
        if entry.get("last_used_at"):
            summary["last_used_at"] = entry["last_used_at"]
        return summary

    @staticmethod
    def questions(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        return copy.deepcopy(manifest["content"].get("questions") or [])


def get_paper_template_registry() -> PaperTemplateRegistry:
    return PaperTemplateRegistry()
