"""Immutable, globally shareable answer-sheet template codes.

Only blank template geometry and registration assets are persisted here.  The
registry never stores source questions, standard answers, student images, or
grading results.  A teacher's recent-code history and aliases are private to
that account.

v2 templates are pure layout (regions with question/sub-question numbers,
identity regions, choice bubble grids and registration pages).  They bind to a
paper template by question number when applied; v1 manifests stay readable.

Bubble grids live only here.  Their geometry is part of the fingerprinted
content (same sheet -> same code); the teacher confirmation record (who, when,
proposal vs. adjustment) sits beside it under its own hash.  Batches keep only
the template code and fingerprint and resolve grid pixels from this registry.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from config import DATA_DIR
from services.answer_sheet_acceleration import build_answer_sheet_acceleration_plan
from services.answer_sheet_layout_contract import (
    BUBBLE_GRID_CONTRACT_VERSION,
    bubble_grid_coverage,
    freeze_grid_rows,
    grid_error_message,
    join_error_message,
    question_number_join,
    resolve_bubble_grid_pixels,
    validate_frozen_bubble_grids,
    validate_grid_confirmations,
)


REGISTRY_CONTRACT_VERSION = "answer-sheet-template-registry-v2"
_READABLE_REGISTRY_VERSIONS = {"answer-sheet-template-registry-v1", REGISTRY_CONTRACT_VERSION}
MAX_TEMPLATE_HISTORY = 20
_CODE_RE = re.compile(r"^AS-[0-9A-F]{4}(?:-[0-9A-F]{4}){3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_registry_locks_guard = threading.Lock()
_registry_locks: dict[str, threading.RLock] = {}
logger = logging.getLogger(__name__)


class AnswerSheetTemplateNotFound(ValueError):
    """Raised when a public template code does not exist."""


class AnswerSheetTemplateIncompatible(ValueError):
    """Raised when a code does not match the current question contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_template_code(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    compact = text.replace("-", "")
    if compact.startswith("AS"):
        compact = compact[2:]
    if len(compact) == 16 and all(char in "0123456789ABCDEF" for char in compact):
        text = "AS-" + "-".join(compact[index:index + 4] for index in range(0, 16, 4))
    if not _CODE_RE.fullmatch(text):
        raise ValueError("模板码格式无效")
    return text


def _template_code(fingerprint: str) -> str:
    compact = fingerprint[:16].upper()
    return "AS-" + "-".join(compact[index:index + 4] for index in range(0, 16, 4))


def _local_registry_lock(lock_path: str) -> threading.RLock:
    with _registry_locks_guard:
        return _registry_locks.setdefault(lock_path, threading.RLock())


@contextmanager
def _registry_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = str((root / ".registry.lock").resolve())
    local_lock = _local_registry_lock(lock_path)
    with local_lock:
        handle = open(lock_path, "a+b")
        locked = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    logger.warning("failed to unlock answer-sheet template registry")
            handle.close()


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    _replace_with_retry(temporary, path)


def _replace_with_retry(source: Path, destination: Path, *, attempts: int = 6) -> None:
    """Tolerate short-lived Windows file scanner locks without weakening atomicity."""

    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _artifact_for_registry(artifact: dict[str, Any], *, template_asset: str) -> dict[str, Any]:
    result = copy.deepcopy(artifact)
    result["template_image"] = template_asset
    result.pop("canonicalization", None)
    result.pop("difference_template_image", None)
    result.pop("difference_template_sha256", None)
    return result


def _grid_confirmation_record(fingerprint: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "contract_version": BUBBLE_GRID_CONTRACT_VERSION,
        "fingerprint": fingerprint,
        "rows": rows,
    }
    return {**body, "sha256": hashlib.sha256(_stable_json(body).encode("utf-8")).hexdigest()}


def _verify_bubble_grids(manifest: dict[str, Any], content: dict[str, Any], fingerprint: str) -> None:
    """Frozen grids must be well-formed, teacher-confirmed and attributed."""
    grids = content.get("bubble_grids")
    record = manifest.get("bubble_grid_confirmations")
    if not grids:
        if record is not None:
            raise ValueError("涂卡网格确认记录没有对应的网格")
        return
    validate_frozen_bubble_grids(grids, content.get("answer_regions") or [])
    if not isinstance(record, dict):
        raise ValueError("涂卡网格教师确认记录缺失")
    expected = _grid_confirmation_record(fingerprint, record.get("rows"))
    if record != expected:
        raise ValueError("涂卡网格教师确认记录完整性校验失败")
    validate_grid_confirmations(grids, record["rows"])


def _page_sizes(content: dict[str, Any]) -> dict[str, tuple[int, int]]:
    return {
        str(page.get("page_id")): (page.get("width"), page.get("height"))
        for page in content.get("pages") or []
        if isinstance(page, dict)
    }


def _safe_asset_path(template_dir: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("模板资源路径无效")
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("模板资源路径无效")
    candidate = (template_dir / Path(*pure.parts)).resolve(strict=False)
    try:
        candidate.relative_to(template_dir.resolve())
    except ValueError as exc:
        raise ValueError("模板资源路径越界") from exc
    return candidate


class AnswerSheetTemplateRegistry:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root or (Path(DATA_DIR) / "answer_sheet_templates"))
        self.templates_dir = self.root / "templates"
        self.history_dir = self.root / "history"

    def _template_dir(self, code: str) -> Path:
        return self.templates_dir / normalize_template_code(code)

    def _load_manifest(self, code: object, *, verify_assets: bool = True) -> dict[str, Any]:
        normalized = normalize_template_code(code)
        template_dir = self._template_dir(normalized)
        manifest_path = template_dir / "manifest.json"
        if not manifest_path.is_file():
            raise AnswerSheetTemplateNotFound("模板码不存在或已停用")
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise AnswerSheetTemplateNotFound("模板码不存在或已停用") from exc
        content = manifest.get("content") if isinstance(manifest, dict) else None
        fingerprint = str(manifest.get("fingerprint") or "") if isinstance(manifest, dict) else ""
        if (
            manifest.get("version") not in _READABLE_REGISTRY_VERSIONS
            or manifest.get("template_code") != normalized
            or not isinstance(content, dict)
            or not _SHA256_RE.fullmatch(fingerprint)
            or hashlib.sha256(_stable_json(content).encode("utf-8")).hexdigest() != fingerprint
            or _template_code(fingerprint) != normalized
        ):
            raise ValueError("模板码数据完整性校验失败")
        try:
            _verify_bubble_grids(manifest, content, fingerprint)
        except ValueError as exc:
            raise ValueError(f"模板码数据完整性校验失败：{exc}") from exc
        if verify_assets:
            pages = content.get("pages")
            if not isinstance(pages, list) or not pages:
                raise ValueError("模板码页面数据无效")
            for page in pages:
                if not isinstance(page, dict):
                    raise ValueError("模板码页面数据无效")
                asset = _safe_asset_path(template_dir, page.get("template_asset"))
                expected_hash = str(page.get("sha256") or "")
                if not asset.is_file() or not _SHA256_RE.fullmatch(expected_hash):
                    raise ValueError("模板码页面资源缺失")
                if _sha256_file(asset) != expected_hash:
                    raise ValueError("模板码页面资源完整性校验失败")
        return manifest

    def publish(self, session: Any, *, creator_teacher_id: int) -> dict[str, Any]:
        """Persist a teacher-confirmed layout and return its immutable code."""

        if session.status not in {"layout_draft", "frozen"}:
            raise ValueError("请先确认并保存答题卡版面")
        if getattr(session, "shared_template_code", ""):
            # Already bound: the session holds no grid copy, so re-publishing
            # must return the bound code rather than mint a grid-less one.
            existing = self._load_manifest(session.shared_template_code)
            if existing["fingerprint"] != getattr(session, "shared_template_hash", ""):
                raise ValueError("答题卡版面与已绑定的模板码不一致")
            summary = self.public_summary(existing)
            summary["_fingerprint"] = existing["fingerprint"]
            return summary
        if not session.layout_summary().get("can_freeze"):
            raise ValueError("答题卡版面尚未完成教师确认")
        if not isinstance(session.layout_artifacts, dict) or not session.layout_artifacts:
            raise ValueError("答题卡配准资源尚未生成")

        page_records: list[dict[str, Any]] = []
        source_assets: list[tuple[Path, str]] = []
        for page_index, page in enumerate(session.answer_sheet_pages):
            page_id = str(page.get("page_id") or "").strip()
            artifact = session.layout_artifacts.get(page_id)
            if not page_id or not isinstance(artifact, dict):
                raise ValueError("答题卡页面与配准资源不一致")
            relative_source = artifact.get("template_image")
            source_path = _safe_asset_path(Path(session.session_dir), relative_source)
            if not source_path.is_file():
                raise ValueError("答题卡配准资源不存在")
            digest = _sha256_file(source_path)
            if digest != str(artifact.get("sha256") or ""):
                raise ValueError("答题卡配准资源完整性校验失败")
            asset_name = f"pages/page_{page_index + 1:03d}.png"
            source_assets.append((source_path, asset_name))
            page_records.append({
                "page_id": page_id,
                "index": page_index,
                "sha256": digest,
                "width": artifact.get("width"),
                "height": artifact.get("height"),
                "template_asset": asset_name,
                "artifact": _artifact_for_registry(artifact, template_asset=asset_name),
            })

        # Only teacher-confirmed grid rows can be frozen; a pending model
        # proposal refuses the publish instead of being dropped or promoted.
        frozen_grids, grid_confirmations = freeze_grid_rows(
            getattr(session, "bubble_grids", []) or [], session.answer_regions,
        )
        # Layout identity only: the same sheet re-registers to the same code no
        # matter which paper template it was first annotated against, or who
        # confirmed its grids when.
        content = {
            "contract_version": REGISTRY_CONTRACT_VERSION,
            "pages": page_records,
            "answer_regions": copy.deepcopy(session.answer_regions),
            "identity_regions": copy.deepcopy(session.identity_regions),
            "bubble_grids": frozen_grids,
        }
        fingerprint = hashlib.sha256(_stable_json(content).encode("utf-8")).hexdigest()
        code = _template_code(fingerprint)
        manifest = {
            "version": REGISTRY_CONTRACT_VERSION,
            "template_code": code,
            "fingerprint": fingerprint,
            "created_at": _now(),
            "created_by_teacher_id": int(creator_teacher_id),
            "content": content,
        }
        if frozen_grids:
            manifest["bubble_grid_confirmations"] = _grid_confirmation_record(
                fingerprint, grid_confirmations,
            )

        with _registry_lock(self.root):
            destination = self._template_dir(code)
            if destination.exists():
                existing = self._load_manifest(code)
                if existing.get("fingerprint") != fingerprint:
                    raise ValueError("模板码发生冲突，请联系管理员")
                summary = self.public_summary(existing)
                summary["_fingerprint"] = fingerprint
                return summary
            temporary = self.templates_dir / f".{code}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.mkdir(parents=True, exist_ok=False)
                for source, relative in source_assets:
                    target = _safe_asset_path(temporary, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                _write_json_atomic(temporary / "manifest.json", manifest)
                self.templates_dir.mkdir(parents=True, exist_ok=True)
                _replace_with_retry(temporary, destination)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        summary = self.public_summary(manifest)
        summary["_fingerprint"] = fingerprint
        return summary

    def public_summary(
        self,
        manifest: dict[str, Any],
        *,
        last_used_at: str | None = None,
        alias: str = "",
    ) -> dict[str, Any]:
        content = manifest["content"]
        kinds: dict[str, set[str]] = {"choice": set(), "fill": set(), "solve": set()}
        for region in content.get("answer_regions") or []:
            region_type = str(region.get("region_type") or "")
            kind = "choice" if region_type.startswith("choice_") else region_type
            if kind in kinds:
                kinds[kind].add(str(region.get("question_no") or ""))
        summary = {
            "template_code": manifest["template_code"],
            "alias": alias,
            "created_at": manifest.get("created_at"),
            "page_count": len(content.get("pages") or []),
            "question_count": len(set().union(*kinds.values())),
            "question_counts": {key: len(value) for key, value in kinds.items()},
            "bubble_grid_count": len(content.get("bubble_grids") or []),
        }
        if last_used_at:
            summary["last_used_at"] = last_used_at
        return summary

    def apply_to_session(self, session: Any, code: object) -> dict[str, Any]:
        """Materialize a shared code in a session without any model request."""

        if session.status not in {"answers_confirmed", "layout_draft"}:
            raise ValueError("当前会话状态不能使用模板码")
        manifest = self._load_manifest(code)
        content = manifest["content"]
        # Bind by question number; no signature gate.  Fail closed only for a
        # required question without a region, an unknown region number, or a
        # question/sub-question conflict.
        join = question_number_join(
            content.get("answer_regions") or [],
            session.answer_spec_snapshot,
        )
        if not join["ok"]:
            raise AnswerSheetTemplateIncompatible(
                "模板码与试卷模板按题号绑定失败：" + join_error_message(join)
            )
        if getattr(session, "is_batch", False):
            # Batches judge marks locally: every choice question (and each of
            # its declared small questions) needs a teacher-confirmed grid row
            # that resolves to pixels before anything is materialized.
            self._resolve_bubble_grids(content, session.answer_spec_snapshot)

        template_dir = self._template_dir(manifest["template_code"])
        namespace = f"layout_from_template_{uuid.uuid4().hex}"
        destination = Path(session.session_dir) / namespace
        pages: list[dict[str, Any]] = []
        artifacts: dict[str, dict[str, Any]] = {}
        try:
            destination.mkdir(parents=True, exist_ok=False)
            for page in content["pages"]:
                page_id = str(page["page_id"])
                source = _safe_asset_path(template_dir, page["template_asset"])
                target = destination / f"page_{int(page['index']) + 1:03d}.png"
                shutil.copy2(source, target)
                if _sha256_file(target) != page["sha256"]:
                    raise ValueError("模板码页面复制校验失败")
                relative = target.relative_to(Path(session.session_dir)).as_posix()
                pages.append({
                    "page_id": page_id,
                    "index": int(page["index"]),
                    "path": str(target.resolve()),
                    "working_path": str(target.resolve()),
                    "sha256": page["sha256"],
                    "working_sha256": page["sha256"],
                    "source": "shared_template_code",
                })
                artifact = copy.deepcopy(page["artifact"])
                artifact["template_image"] = relative
                artifact.pop("canonicalization", None)
                artifacts[page_id] = artifact

            answer_regions = copy.deepcopy(content["answer_regions"])
            identity_regions = copy.deepcopy(content["identity_regions"])
            build_answer_sheet_acceleration_plan(
                artifacts,
                identity_regions,
                answer_regions,
            )
            session.answer_sheet_pages = pages
            session.pending_layout_pages = []
            session.answer_regions = answer_regions
            session.identity_regions = identity_regions
            if hasattr(session, "bubble_grids"):
                # Grid geometry stays on the template; the session keeps only
                # the code and fingerprint set below.
                session.bubble_grids = []
            session.layout_artifacts = artifacts
            session.shared_template_code = manifest["template_code"]
            session.shared_template_hash = manifest["fingerprint"]
            session.layout_localization = {
                "status": "reused",
                "provider_route": "none",
                "reasoning_effort": "none",
                "template_code": manifest["template_code"],
            }
            session.error = None
            session.status = "layout_draft"
            session.freeze_layout()
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return self.public_summary(manifest)

    @staticmethod
    def _resolve_bubble_grids(
        content: dict[str, Any],
        question_specs: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        grids = content.get("bubble_grids") or []
        coverage = bubble_grid_coverage(
            content.get("answer_regions") or [], grids, question_specs,
        )
        if not coverage["ok"]:
            raise AnswerSheetTemplateIncompatible(
                "模板码的涂卡网格不可用：" + grid_error_message(coverage)
                + "；请重新标注并生成新模板码"
            )
        try:
            return resolve_bubble_grid_pixels(grids, _page_sizes(content))
        except ValueError as exc:
            raise AnswerSheetTemplateIncompatible(f"模板码的涂卡网格不可用：{exc}") from exc

    def resolve_bubble_regions(self, session: Any) -> dict[str, Any]:
        """Resolve a batch's question numbers to bubble-grid pixel boxes.

        Reads only the frozen template named by the session's code and
        fingerprint; the session itself holds no grid geometry.  Fails closed
        when the template is missing, altered, diverged from the session's
        regions, or lacks a teacher-confirmed row for any choice answer key.
        """
        code = str(getattr(session, "shared_template_code", "") or "")
        expected = str(getattr(session, "shared_template_hash", "") or "")
        if not code or not expected:
            raise AnswerSheetTemplateIncompatible("批次未绑定已发布的答题卡模板码，涂卡网格不可用")
        manifest = self._load_manifest(code, verify_assets=False)
        if manifest["fingerprint"] != expected:
            raise AnswerSheetTemplateIncompatible("答题卡模板指纹与批次记录不一致")
        content = manifest["content"]
        if _stable_json(content.get("answer_regions") or []) != _stable_json(session.answer_regions):
            raise AnswerSheetTemplateIncompatible("批次答题区域与答题卡模板不一致")
        join = question_number_join(
            content.get("answer_regions") or [], session.answer_spec_snapshot,
        )
        if not join["ok"]:
            raise AnswerSheetTemplateIncompatible(
                "模板码与试卷模板按题号绑定失败：" + join_error_message(join)
            )
        return {
            "template_code": manifest["template_code"],
            "template_fingerprint": manifest["fingerprint"],
            "questions": self._resolve_bubble_grids(content, session.answer_spec_snapshot),
        }

    def remember(self, teacher_id: int, code: object) -> None:
        normalized = normalize_template_code(code)
        self._load_manifest(normalized)
        history_path = self.history_dir / f"teacher_{int(teacher_id)}.json"
        now = _now()
        with _registry_lock(self.root):
            history: list[dict[str, str]] = []
            if history_path.is_file():
                try:
                    loaded = json.loads(history_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        history = [item for item in loaded if isinstance(item, dict)]
                except (OSError, json.JSONDecodeError):
                    history = []
            history = [item for item in history if item.get("template_code") != normalized]
            history.insert(0, {"template_code": normalized, "last_used_at": now})
            _write_json_atomic(history_path, history[:MAX_TEMPLATE_HISTORY])

    def history(self, teacher_id: int) -> list[dict[str, Any]]:
        history_path = self.history_dir / f"teacher_{int(teacher_id)}.json"
        with _registry_lock(self.root):
            if not history_path.is_file():
                return []
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
        result: list[dict[str, Any]] = []
        for item in loaded if isinstance(loaded, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                manifest = self._load_manifest(item.get("template_code"))
            except (ValueError, AnswerSheetTemplateNotFound):
                continue
            result.append(self.public_summary(
                manifest,
                last_used_at=str(item.get("last_used_at") or ""),
            ))
        return result[:MAX_TEMPLATE_HISTORY]


def get_answer_sheet_template_registry() -> AnswerSheetTemplateRegistry:
    return AnswerSheetTemplateRegistry()
