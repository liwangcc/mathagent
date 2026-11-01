"""Persistent, teacher-owned answer-sheet grading session.

The source exam owns semantics.  The blank answer sheet owns geometry.  Student
answer sheets are the only grading images.  This module keeps those assets
separate and enforces the irreversible confirmation/freeze boundaries.
"""
from __future__ import annotations

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
from pathlib import Path
from typing import Any, Iterator

from services.answer_sheet_layout_contract import (
    bubble_grid_coverage,
    confirm_grid_rows,
    discard_grid_rows,
    normalize_choice_options,
    propose_grid_rows,
    question_number_join,
    reconcile_grid_rows,
)
from services.grading_contract import AnswerSpec, finalize_answer_spec_for_category
from services.teaching_context import validate_teaching_context

_SAFE_ID = re.compile(r"^[a-f0-9]{12,64}$")
_VALID_STATUS = {
    "draft", "source_uploaded", "source_recognized", "answers_confirmed", "layout_draft",
    "frozen", "recognizing", "review", "archived", "failed",
}
_REGION_TYPES = {"choice_mark", "choice_handwritten", "fill", "solve"}
_IDENTITY_TYPES = {
    "name", "student_no", "exam_no_text", "qr", "barcode",
    "exam_no_marks", "page_number",
}
MAX_ANSWER_REGIONS = 200
MAX_IDENTITY_REGIONS = 100
MAX_ANSWER_REGIONS_PER_PAGE = 128
MAX_IDENTITY_REGIONS_PER_PAGE = 8
MAX_ANSWER_REGION_AREA = 1_000_000
MAX_IDENTITY_REGION_AREA = 250_000
MAX_PAGE_REGION_AREA = 1_500_000
MAX_REGION_OVERLAP_RATIO = 0.25
# New batches are identified by their answer-sheet template code plus the
# binding time.  Sessions without this marker are historical and read-only
# with respect to their legacy ``category`` field.
BATCH_CONTRACT_VERSION = "answer-sheet-batch-v2"
BATCH_INDEX_NAME = "batch.json"
MAX_LISTED_BATCHES = 50

logger = logging.getLogger(__name__)


def make_batch_label(answer_sheet_template_code: str, when: datetime) -> str:
    """Return the teacher-facing storage label ``{code}-{YYYYMMDD-HHMMSS}``."""
    code = str(answer_sheet_template_code or "").strip()
    if not code:
        raise ValueError("批次标识缺少答题卡模板号")
    return f"{code}-{when.strftime('%Y%m%d-%H%M%S')}"


class AnswerSheetOperationBusy(RuntimeError):
    """The same answer-sheet session or batch label is held by another worker."""


_session_locks_guard = threading.Lock()
_session_local_locks: dict[str, threading.Lock] = {}


def _local_session_lock(lock_path: str) -> threading.Lock:
    with _session_locks_guard:
        return _session_local_locks.setdefault(lock_path, threading.Lock())


def _answer_sheet_lock_path(upload_dir: str, name: str) -> str:
    lock_dir = os.path.abspath(os.path.join(upload_dir, ".answer-sheet-locks"))
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.abspath(os.path.join(lock_dir, name))


@contextmanager
def answer_sheet_operation_lock(session_id: str, upload_dir: str) -> Iterator[None]:
    """Acquire a non-blocking cross-worker lock for one persisted session."""
    session = AnswerSheetSession(session_id, upload_dir)
    if not os.path.isfile(session.meta_path):
        raise FileNotFoundError(session.meta_path)
    lock_path = _answer_sheet_lock_path(upload_dir, f"{session_id}.lock")
    with _nonblocking_lock_file(lock_path, session_id):
        yield


@contextmanager
def answer_sheet_batch_label_lock(
    upload_dir: str,
    teacher_id: int,
    answer_sheet_template_code: str,
) -> Iterator[None]:
    """Serialize one teacher's label uniqueness check and save per template.

    A label has one-second resolution, so two workers binding the same
    template must not both pass the scan before either ``batch.json`` exists.
    """
    code = str(answer_sheet_template_code or "").strip()
    if not code or not all(char.isalnum() or char == "-" for char in code):
        raise ValueError("答题卡模板号无效")
    name = f"batch-label-{int(teacher_id)}-{code}.lock"
    with _nonblocking_lock_file(_answer_sheet_lock_path(upload_dir, name), name):
        yield


@contextmanager
def _nonblocking_lock_file(lock_path: str, busy_key: str) -> Iterator[None]:
    """Hold a thread-local and cross-worker lock, or raise busy immediately."""
    local_lock = _local_session_lock(lock_path)
    if not local_lock.acquire(blocking=False):
        raise AnswerSheetOperationBusy(busy_key)

    handle = None
    locked = False
    try:
        handle = open(lock_path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise AnswerSheetOperationBusy(busy_key) from exc
        yield
    finally:
        if handle is not None:
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
                    logger.warning(
                        "failed to unlock answer-sheet operation lock: %s",
                        lock_path,
                    )
            handle.close()
        local_lock.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bbox(value: Any, label: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label}坐标必须是四个整数")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{label}坐标必须是四个整数")
    left, top, right, bottom = value
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        raise ValueError(f"{label}坐标越界或面积无效")
    return [left, top, right, bottom]


def _overlap_area(a: list[int], b: list[int]) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _bbox_area(bbox: list[int]) -> int:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _validate_page_region_budget(
    answer_regions: list[dict],
    identity_regions: list[dict],
) -> None:
    by_page: dict[str, dict[str, list[dict]]] = {}
    for region in answer_regions:
        area = _bbox_area(region["bbox"])
        if area > MAX_ANSWER_REGION_AREA:
            raise ValueError("单个答案区域面积过大，请缩小到实际作答范围")
        by_page.setdefault(
            region["page_id"], {"answers": [], "identity": []}
        )["answers"].append(region)
    for region in identity_regions:
        area = _bbox_area(region["bbox"])
        if area > MAX_IDENTITY_REGION_AREA:
            raise ValueError("单个身份区域面积过大，请缩小到实际身份栏范围")
        by_page.setdefault(
            region["page_id"], {"answers": [], "identity": []}
        )["identity"].append(region)

    for groups in by_page.values():
        answers = groups["answers"]
        identity = groups["identity"]
        if len(answers) > MAX_ANSWER_REGIONS_PER_PAGE:
            raise ValueError("单页答案区域数量过多，请拆分页型或合并重复区域")
        if len(identity) > MAX_IDENTITY_REGIONS_PER_PAGE:
            raise ValueError("单页身份区域数量过多，请删除重复区域")
        combined = [*answers, *identity]
        if sum(_bbox_area(item["bbox"]) for item in combined) > MAX_PAGE_REGION_AREA:
            raise ValueError("单页裁剪区域累计面积过大，请删除重复或过大的区域")
        for same_kind_regions in (answers, identity):
            for index, first in enumerate(same_kind_regions):
                first_area = _bbox_area(first["bbox"])
                for second in same_kind_regions[index + 1:]:
                    overlap = _overlap_area(first["bbox"], second["bbox"])
                    smaller_area = min(first_area, _bbox_area(second["bbox"]))
                    if overlap > smaller_area * MAX_REGION_OVERLAP_RATIO:
                        raise ValueError("同页同类裁剪区域存在大面积重叠，请调整或删除重复区域")


def list_teacher_batches(
    upload_dir: str,
    teacher_id: int,
    *,
    limit: int = MAX_LISTED_BATCHES,
) -> list[dict]:
    """Return the teacher's template-code batches, newest first.

    Only the small ``batch.json`` records are read; historical sessions have no
    record and remain reachable through their session id.
    """
    root = Path(upload_dir)
    if not root.is_dir():
        return []
    records: list[dict] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith("answer_sheet_"):
                continue
            index_path = Path(entry.path) / BATCH_INDEX_NAME
            try:
                with index_path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError):
                continue
            if (
                not isinstance(record, dict)
                or record.get("teacher_id") != teacher_id
                or f"answer_sheet_{record.get('session_id')}" != entry.name
            ):
                continue
            record.pop("teacher_id", None)
            records.append(record)
    records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return records[:limit]


class AnswerSheetSession:
    def __init__(
        self,
        session_id: str,
        upload_dir: str,
        *,
        teacher_id: int | None = None,
        class_id: int | None = None,
        teaching_context: dict | None = None,
    ):
        if not _SAFE_ID.fullmatch(str(session_id or "")):
            raise ValueError("非法的答题卡会话标识")
        self.session_id = session_id
        self.upload_dir = upload_dir
        self.teacher_id = teacher_id
        self.teaching_context = validate_teaching_context(teaching_context)
        self.class_id = class_id
        self.status = "draft"
        # Historical sessions carry a read-only daily/exam value; new batches
        # never write one.
        self.category = ""
        self.exam_name = ""
        self.batch_contract_version = ""
        self.batch_label = ""
        self.answer_sheet_template_code = ""
        self.paper_template_code = ""
        self.answer_source = "embedded"
        self.source_recognition_generation: int = 0
        self.source_question_drafts: list[dict] = []
        self.source_exam_pages: list[dict] = []
        self.answer_pages: list[dict] = []
        self.answer_spec_snapshot: list[dict] = []
        self.answer_spec_hash = ""
        self.answer_sheet_pages: list[dict] = []
        self.pending_layout_pages: list[dict] = []
        self.answer_regions: list[dict] = []
        self.identity_regions: list[dict] = []
        self.bubble_grids: list[dict] = []
        self.layout_hash = ""
        self.layout_artifacts: dict[str, dict] = {}
        self.layout_localization: dict = {}
        self.shared_template_code = ""
        self.shared_template_hash = ""
        self.student_pages: list[dict] = []
        self.student_upload: dict = {}
        self.grouping_decisions: list[dict] = []
        self.review_revision = 0
        self.archived_result_ids: list[int] = []
        self.asset_lifecycle: dict = {}
        self.error: str | None = None
        self.created_at = _now()
        self.updated_at = self.created_at

    @property
    def session_dir(self) -> str:
        return os.path.join(self.upload_dir, f"answer_sheet_{self.session_id}")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.session_dir, "meta.json")

    def to_dict(self, *, public: bool = False) -> dict:
        data = {
            "session_id": self.session_id,
            "teaching_context": validate_teaching_context(self.teaching_context),
            "teacher_id": self.teacher_id,
            "class_id": self.class_id,
            "status": self.status,
            "exam_name": self.exam_name,
            "batch_contract_version": self.batch_contract_version,
            "batch_label": self.batch_label,
            "answer_sheet_template_code": self.answer_sheet_template_code,
            "paper_template_code": self.paper_template_code,
            "answer_source": self.answer_source,
            "source_recognition_generation": self.source_recognition_generation,
            "source_question_drafts": self.source_question_drafts,
            "source_exam_pages": self.source_exam_pages,
            "answer_pages": self.answer_pages,
            "answer_spec_snapshot": self.answer_spec_snapshot,
            "answer_spec_hash": self.answer_spec_hash,
            "answer_sheet_pages": self.answer_sheet_pages,
            "pending_layout_pages": self.pending_layout_pages,
            "answer_regions": self.answer_regions,
            "identity_regions": self.identity_regions,
            "bubble_grids": self.bubble_grids,
            "layout_hash": self.layout_hash,
            "layout_artifacts": self.layout_artifacts,
            "layout_localization": self.layout_localization,
            "shared_template_code": self.shared_template_code,
            "shared_template_hash": self.shared_template_hash,
            "student_pages": self.student_pages,
            "student_upload": self.student_upload,
            "grouping_decisions": self.grouping_decisions,
            "review_revision": self.review_revision,
            "archived_result_ids": self.archived_result_ids,
            "asset_lifecycle": self.asset_lifecycle,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.category:
            # Historical value only: preserved byte-for-byte, never written for
            # a new batch and never used to choose a new batch's behavior.
            data["category"] = self.category
        if public:
            data.pop("pending_layout_pages", None)
            data.pop("shared_template_hash", None)
            for key in ("source_exam_pages", "answer_pages", "answer_sheet_pages", "student_pages"):
                public_pages = []
                for page in data[key]:
                    public_page = {
                        k: v for k, v in page.items()
                        if k not in {"path", "working_path", "orientation_working_path"}
                    }
                    canonicalization = public_page.get("canonicalization")
                    if isinstance(canonicalization, dict):
                        public_page["canonicalization"] = {
                            k: v for k, v in canonicalization.items()
                            if k != "source_image"
                        }
                    public_pages.append(public_page)
                data[key] = public_pages
            public_artifacts = {}
            for page_id, artifact in data["layout_artifacts"].items():
                public_artifact = {
                    k: v for k, v in artifact.items()
                    if k != "template_image"
                }
                canonicalization = public_artifact.get("canonicalization")
                if isinstance(canonicalization, dict):
                    public_artifact["canonicalization"] = {
                        k: v for k, v in canonicalization.items()
                        if k != "source_image"
                    }
                public_artifacts[page_id] = public_artifact
            data["layout_artifacts"] = public_artifacts
            upload = data.get("student_upload")
            if isinstance(upload, dict):
                staged_pages = upload.get("pages")
                public_upload = {
                    key: value
                    for key, value in upload.items()
                    if key not in {"pages", "directory"}
                }
                if isinstance(staged_pages, list):
                    public_upload["uploaded_count"] = len(staged_pages)
                    public_upload["processed_count"] = sum(
                        1
                        for page in staged_pages
                        if isinstance(page, dict)
                        and isinstance(page.get("upload_checkpoint"), dict)
                        and page["upload_checkpoint"].get("state") == "orientation_complete"
                    )
                data["student_upload"] = public_upload
            else:
                data["student_upload"] = {}
        return data

    def save(self) -> None:
        if self.status not in _VALID_STATUS:
            raise ValueError("非法的答题卡会话状态")
        os.makedirs(self.session_dir, exist_ok=True)
        self.updated_at = _now()
        # Match TemplateSession's atomic writer: independent temp names avoid
        # collisions, and transient Windows file sharing must not lose a save.
        tmp = f"{self.meta_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.meta_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
        if self.is_batch:
            self._write_batch_index()

    @property
    def batch_index_path(self) -> str:
        return os.path.join(self.session_dir, BATCH_INDEX_NAME)

    @property
    def is_batch(self) -> bool:
        """True for sessions created under the template-code batch contract."""
        return self.batch_contract_version == BATCH_CONTRACT_VERSION

    def batch_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "batch_label": self.batch_label,
            "answer_sheet_template_code": self.answer_sheet_template_code,
            "paper_template_code": self.paper_template_code,
            "class_id": self.class_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def _write_batch_index(self) -> None:
        """Persist the small listing record next to the authoritative metadata."""
        payload = {**self.batch_summary(), "teacher_id": self.teacher_id}
        tmp = f"{self.batch_index_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, self.batch_index_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    def bind_batch_label(self, answer_sheet_template_code: str, when: datetime | None = None) -> str:
        """Bind the answer-sheet template once and derive the storage label."""
        if not self.is_batch:
            raise ValueError("历史答题卡会话不能生成新的批次标识")
        code = str(answer_sheet_template_code or "").strip()
        if self.batch_label:
            if code != self.answer_sheet_template_code:
                raise ValueError("批次已绑定答题卡模板，不能改绑")
            return self.batch_label
        moment = when or datetime.now().astimezone()
        self.answer_sheet_template_code = code
        self.batch_label = make_batch_label(code, moment)
        return self.batch_label

    def bind_paper_template(
        self,
        template_code: str,
        questions: list[dict],
        answer_spec_hash: str,
    ) -> None:
        """Bind a frozen paper template; its snapshot is never edited here."""
        if not self.is_batch:
            raise ValueError("历史答题卡会话不能绑定试卷模板")
        if self.status != "draft" or self.paper_template_code:
            raise ValueError("批次已绑定试卷模板")
        if not questions or not answer_spec_hash:
            raise ValueError("试卷模板内容无效")
        self.paper_template_code = str(template_code)
        self.answer_spec_snapshot = [dict(item) for item in questions]
        self.answer_spec_hash = str(answer_spec_hash)
        self.status = "answers_confirmed"
        self.error = None

    def result_category(self) -> str:
        """Category value required by the unchanged result system."""
        if self.category in {"daily", "exam"}:
            return self.category
        return "daily"

    def result_exam_name(self) -> str:
        return self.exam_name

    @classmethod
    def load(cls, session_id: str, upload_dir: str) -> "AnswerSheetSession":
        session = cls(session_id, upload_dir)
        if not os.path.isfile(session.meta_path):
            return session
        with open(session.meta_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        session.teaching_context = validate_teaching_context(data.get("teaching_context"))
        for key in (
            "teacher_id", "class_id", "status", "category", "exam_name",
            "batch_contract_version", "batch_label", "answer_sheet_template_code",
            "paper_template_code", "answer_source", "source_question_drafts",
            "source_exam_pages", "answer_pages", "answer_spec_snapshot", "answer_spec_hash",
            "answer_sheet_pages", "pending_layout_pages", "answer_regions", "identity_regions",
            "bubble_grids", "layout_hash", "layout_artifacts",
            "layout_localization", "shared_template_code", "shared_template_hash",
            "student_pages", "student_upload", "grouping_decisions", "review_revision", "archived_result_ids",
            "asset_lifecycle", "error", "created_at", "updated_at",
        ):
            if key in data:
                setattr(session, key, data[key])
        try:
            session.source_recognition_generation = max(
                0,
                int(data.get("source_recognition_generation", 0) or 0),
            )
        except (TypeError, ValueError):
            session.source_recognition_generation = 0
        if not isinstance(session.student_upload, dict):
            session.student_upload = {}
        if session.status not in _VALID_STATUS:
            raise ValueError("答题卡会话状态损坏")
        return session

    def invalidate_grading_drafts(self) -> None:
        """Invalidate stale verdicts whenever their evidence or identity changes."""
        self.grouping_decisions = []
        self.review_revision += 1

    def replace_grading_drafts(self, drafts: list[dict]) -> None:
        self.grouping_decisions = drafts
        self.review_revision += 1

    def mark_archived(self, result_ids: list[int]) -> None:
        archived_at = _now()
        self.status = "archived"
        self.archived_result_ids = [int(item) for item in result_ids]
        self.asset_lifecycle = {
            "policy": "raw_images_expire_after_36h",
            "status": "retained_until_expiry",
            "retention_hours": 36,
            "archived_at": archived_at,
            "result_ids": list(self.archived_result_ids),
        }
        self.review_revision += 1

    def ensure_owned(self, teacher_id: int) -> None:
        if self.teacher_id != teacher_id:
            raise PermissionError("答题卡会话不存在")

    def delete(self) -> None:
        if self.status not in {"draft", "failed"}:
            raise ValueError("只有草稿或失败会话可以删除")
        shutil.rmtree(self.session_dir, ignore_errors=False)

    def confirm_answers(self, questions: list[dict]) -> None:
        if self.status not in {"source_recognized", "answers_confirmed"}:
            raise ValueError("请先完成原始试卷识别")
        if not isinstance(questions, list) or not questions:
            raise ValueError("确认题目不能为空")
        normalized: list[dict] = []
        seen: set[str] = set()
        for raw in questions:
            spec = finalize_answer_spec_for_category(
                AnswerSpec.model_validate(raw), self.result_category(),
            )
            if not spec.question_no or spec.question_no in seen:
                raise ValueError("题号不能为空且不能重复")
            if not spec.question_text:
                raise ValueError(f"第{spec.question_no}题缺少题干")
            if not spec.standard_answer:
                raise ValueError(f"第{spec.question_no}题缺少标准答案")
            seen.add(spec.question_no)
            normalized.append(spec.model_dump(mode="json"))
        self.answer_spec_snapshot = normalized
        self.answer_spec_hash = _stable_hash(
            {"questions": normalized, "teaching_context": self.teaching_context}
            if self.teaching_context else normalized
        )
        self.status = "answers_confirmed"
        self.error = None
        self.save()

    def save_layout(self, answer_regions: list[dict], identity_regions: list[dict]) -> dict:
        if self.status not in {"answers_confirmed", "layout_draft"}:
            raise ValueError("请先确认题目与答案")
        if len(answer_regions or []) > MAX_ANSWER_REGIONS:
            raise ValueError("答案区域数量过多")
        if len(identity_regions or []) > MAX_IDENTITY_REGIONS:
            raise ValueError("身份区域数量过多")
        page_ids = {str(page.get("page_id")) for page in self.answer_sheet_pages}
        if not page_ids:
            raise ValueError("请先上传空白答题卡")
        question_specs = {item["question_no"]: item for item in self.answer_spec_snapshot}
        normalized_answers: list[dict] = []
        region_ids: set[str] = set()
        for index, raw in enumerate(answer_regions or [], 1):
            if not isinstance(raw, dict):
                raise ValueError("答案区域格式错误")
            region_id = str(raw.get("region_id") or f"answer_{index}").strip()
            page_id = str(raw.get("page_id") or "").strip()
            question_no = str(raw.get("question_no") or "").strip()
            region_type = str(raw.get("region_type") or "").strip()
            if region_id in region_ids or page_id not in page_ids or question_no not in question_specs:
                raise ValueError("答案区域的标识、页型或题号无效")
            if region_type not in _REGION_TYPES:
                raise ValueError("答案区域类型无效")
            spec = question_specs[question_no]
            expected = "choice" if region_type.startswith("choice_") else region_type
            if spec["question_type"] != expected:
                raise ValueError(f"第{question_no}题题型与答题区域类型冲突")
            region_ids.add(region_id)
            expected_multiple = str(spec.get("choice_mode") or "unknown") == "multiple"
            if region_type in {"choice_mark", "choice_handwritten"} and bool(
                raw.get("allow_multiple", False)
            ) != expected_multiple:
                raise ValueError(f"第{question_no}题答案区的单选/多选设置与冻结答案不一致")
            normalized_answers.append({
                "region_id": region_id,
                "page_id": page_id,
                "question_no": question_no,
                "subquestion_no": str(raw.get("subquestion_no") or "").strip(),
                "bbox": _bbox(raw.get("bbox"), f"第{question_no}题"),
                "region_type": region_type,
                "reading_order": int(raw.get("reading_order") or index),
                "required": bool(raw.get("required", True)),
                "teacher_confirmed": bool(raw.get("teacher_confirmed", False)),
                "choice_options": (
                    normalize_choice_options(raw.get("choice_options"))
                    if region_type.startswith("choice_")
                    else list(raw.get("choice_options") or ["A", "B", "C", "D"])
                ),
                "allow_multiple": expected_multiple,
            })

        normalized_identity: list[dict] = []
        identity_region_ids: set[str] = set()
        for index, raw in enumerate(identity_regions or [], 1):
            if not isinstance(raw, dict):
                raise ValueError("身份区域格式错误")
            page_id = str(raw.get("page_id") or "").strip()
            identity_type = str(raw.get("identity_type") or "").strip()
            region_id = str(raw.get("region_id") or f"identity_{index}").strip()
            if (
                page_id not in page_ids
                or identity_type not in _IDENTITY_TYPES
                or region_id in region_ids
                or region_id in identity_region_ids
            ):
                raise ValueError("身份区域的页型或类型无效")
            identity_region_ids.add(region_id)
            config = dict(raw.get("config") or {})
            if identity_type == "exam_no_marks":
                digits = int(config.get("digits") or 0)
                if digits < 1 or digits > 32:
                    raise ValueError("准考证号涂格位数必须在1到32之间")
            normalized_identity.append({
                "region_id": region_id,
                "page_id": page_id,
                "bbox": _bbox(raw.get("bbox"), "身份区域"),
                "identity_type": identity_type,
                "teacher_confirmed": bool(raw.get("teacher_confirmed", False)),
                "config": config,
            })
        _validate_page_region_budget(normalized_answers, normalized_identity)
        # Grid rows are edited only through the explicit propose/confirm
        # methods below; a layout save keeps a row only while its region is
        # unchanged.
        self.bubble_grids = reconcile_grid_rows(
            self.bubble_grids, self.answer_regions, normalized_answers, self.answer_spec_snapshot,
        )
        self.answer_regions = normalized_answers
        self.identity_regions = normalized_identity
        self.layout_hash = ""
        self.layout_artifacts = {}
        self.shared_template_code = ""
        self.shared_template_hash = ""
        self.status = "layout_draft"
        self.save()
        return self.layout_summary()

    def layout_summary(self) -> dict:
        required = {item["question_no"] for item in self.answer_spec_snapshot}
        bound = {item["question_no"] for item in self.answer_regions if item["teacher_confirmed"]}
        conflicts: list[list[str]] = []
        by_page: dict[str, list[dict]] = {}
        for region in self.answer_regions:
            by_page.setdefault(region["page_id"], []).append(region)
        for regions in by_page.values():
            for i, first in enumerate(regions):
                for second in regions[i + 1:]:
                    if first["question_no"] != second["question_no"] and _overlap_area(first["bbox"], second["bbox"]) > 0:
                        conflicts.append([first["region_id"], second["region_id"]])
        summary = {
            "question_count": len(required),
            "bound_count": len(bound),
            "missing_questions": sorted(required - bound),
            "conflicts": conflicts,
            "identity_region_count": len(self.identity_regions),
            "can_freeze": not (required - bound) and not conflicts
                and bool(self.identity_regions)
                and all(item["teacher_confirmed"] for item in self.identity_regions),
        }
        if not self.is_batch:
            if self.bubble_grids:
                # Historical sessions do not need grids, but an unconfirmed
                # proposal can never be frozen into a template either.
                coverage = bubble_grid_coverage(
                    self.answer_regions, self.bubble_grids, self.answer_spec_snapshot,
                )
                summary["unconfirmed_bubble_grids"] = coverage["unconfirmed"]
                summary["bubble_grid_conflicts"] = coverage["conflicts"]
                summary["can_freeze"] = bool(
                    summary["can_freeze"] and not coverage["unconfirmed"] and not coverage["conflicts"]
                )
            return summary
        # Template-code batches join by question number and judge bubble marks
        # locally, so every choice_mark needs a teacher-confirmed grid row.
        join = question_number_join(self.answer_regions, self.answer_spec_snapshot)
        if self.shared_template_code:
            # A template-bound batch holds no grid copy; the registry verified
            # the frozen template's grids when binding and re-resolves them
            # from the template at grading time.
            coverage = {"ok": True, "missing": [], "unconfirmed": [], "conflicts": []}
        else:
            coverage = bubble_grid_coverage(
                self.answer_regions, self.bubble_grids, self.answer_spec_snapshot,
            )
        summary.update({
            "missing_questions": sorted(set(summary["missing_questions"]) | set(join["missing"])),
            "unknown_regions": join["unknown_regions"],
            "join_conflicts": join["conflicts"],
            "missing_bubble_grids": coverage["missing"],
            "unconfirmed_bubble_grids": coverage["unconfirmed"],
            "bubble_grid_conflicts": coverage["conflicts"],
            "bubble_grid_source": "template" if self.shared_template_code else "draft",
        })
        summary["can_freeze"] = bool(summary["can_freeze"] and join["ok"] and coverage["ok"])
        return summary

    def _require_grid_editing(self) -> None:
        if self.status != "layout_draft" or self.shared_template_code:
            raise ValueError("请先保存答题卡版面，冻结或复用模板后不能再修改涂卡网格")

    def propose_bubble_grids(self, proposals: list[dict], *, proposer: dict) -> list[str]:
        """Record model placement proposals; returns keys skipped because confirmed."""
        self._require_grid_editing()
        rows, skipped = propose_grid_rows(
            self.bubble_grids,
            proposals,
            answer_regions=self.answer_regions,
            question_specs=self.answer_spec_snapshot,
            proposer=proposer,
            now=datetime.now(timezone.utc).isoformat(),
        )
        self.bubble_grids = rows
        self.save()
        return skipped

    def confirm_bubble_grids(self, grids: list[dict], *, teacher_id: int) -> dict:
        """The teacher's explicit confirm/adjust; the server stamps who and when."""
        self._require_grid_editing()
        if self.teacher_id is not None and int(teacher_id) != int(self.teacher_id):
            raise PermissionError("答题卡会话不存在")
        self.bubble_grids = confirm_grid_rows(
            self.bubble_grids,
            grids,
            answer_regions=self.answer_regions,
            question_specs=self.answer_spec_snapshot,
            teacher_id=int(teacher_id),
            now=datetime.now(timezone.utc).isoformat(),
        )
        self.save()
        return self.layout_summary()

    def discard_bubble_grids(self, keys: list[dict]) -> dict:
        self._require_grid_editing()
        self.bubble_grids = discard_grid_rows(self.bubble_grids, keys)
        self.save()
        return self.layout_summary()

    def freeze_layout(self) -> None:
        if self.status not in {"layout_draft", "frozen"}:
            raise ValueError("请先保存答题卡版面")
        summary = self.layout_summary()
        if not summary["can_freeze"]:
            raise ValueError("答题卡仍有漏绑定、冲突或未确认身份区域")
        payload = {
            "pages": self.answer_sheet_pages,
            "answer_regions": self.answer_regions,
            "identity_regions": self.identity_regions,
            "layout_artifacts": self.layout_artifacts,
            "answer_spec_hash": self.answer_spec_hash,
            "shared_template_code": self.shared_template_code,
            "shared_template_hash": self.shared_template_hash,
        }
        if self.bubble_grids:
            payload["bubble_grids"] = self.bubble_grids
        self.layout_hash = _stable_hash(payload)
        self.status = "frozen"
        self.save()
