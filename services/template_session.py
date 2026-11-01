"""多页一次性模板 — 本次批改的绝对权威基准。

老师导入 N 张模板图（N = 试卷页数）→ 只转录印刷题干 → 数学模型生成标准答案
→ 老师在确认闸门核对/改题干与答案 → 确认结果即本批次绝对权威。模板手写内容
只作为差分豁免，不作为标准答案。

设计要点：
  - 一次性：模板不持久复用（下次批改重新导入），只跟着 template_id 存活。
    但老师确认过的题目按去重沉淀进 QuestionBank，作长期资产。
  - 页数即类型数：模板有 N 页就是 N 个页型。学生卷由 services.paper_cache
    路由到已确认的某一页，直接复用该页的题干 + 标准答案。

存储：uploads/template_{id}/meta.json，原子写入（tmp + os.replace），
与 services/quick_grader.QuickBatch 一致。
"""

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from typing import Optional

from services.paper_cache import question_number_signature
from services.grading_contract import AnswerSpec, merge_directory_leaf_score_layer, reconcile_drawing_contract
from services.teaching_context import validate_teaching_context


_SAFE_TEMPLATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Windows can deny a new reader while another thread replaces the same path.
# Coordinate only the read/replace I/O; unrelated templates and all provider
# work remain independent. Weak values release idle per-path locks.
_META_IO_LOCKS = weakref.WeakValueDictionary()
_META_IO_LOCKS_GUARD = threading.Lock()


def _meta_io_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _META_IO_LOCKS_GUARD:
        lock = _META_IO_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _META_IO_LOCKS[key] = lock
        return lock


# A template always has one explicit visual-evidence mode.  ``marked`` is the
# conservative fallback for metadata written before this field existed: an
# old template may contain handwriting, so it must never silently enter the
# blank-template path.
TEMPLATE_MODES = frozenset({"blank", "marked"})


def normalize_template_mode(value: object, *, default: str = "marked") -> str:
    """Return a supported template mode, using a conservative fallback."""
    mode = str(value or "").strip().lower()
    if mode in TEMPLATE_MODES:
        return mode
    return default if default in TEMPLATE_MODES else "marked"

# Version 2 introduces an explicit choice_mode contract. A template becomes
# usable for student recognition only after a teacher confirmation writes this
# version and binds it to the exact confirmed content snapshot.
CURRENT_ANSWER_CONTRACT_VERSION = 2
MIN_COMPATIBLE_ANSWER_CONTRACT_VERSION = 2
# Version 2 covers answer-region geometry as well as anchors/exemptions. A
# legacy signature cannot establish that an answer region is still confirmed.
CURRENT_ARTIFACT_HASH_VERSION = 2
LEGACY_CONTRACT_REASON = "旧模板缺少当前答案合同，请老师重新确认模板"
LEGACY_GEOMETRY_CONTRACT_REASON = "模板定位校验已升级，请复核定位后重新确认模板（现有标准答案可复用，无需重算）"
CONTENT_CHANGED_REASON = "模板内容在确认后发生变化，请老师重新确认模板"
# A confirmation covers two contracts: what the student is graded against
# (answers) and where each answer is read from (geometry).  Moving an answer box,
# an exemption box or a Q anchor changes no answer, so it is reported as its own
# reason instead of invalidating the answers.
GEOMETRY_CHANGED_REASON = "模板定位框在确认后发生变化，请复核定位后重新确认模板（标准答案无需重算）"

_UNKNOWN_KNOWLEDGE = {"", "未知", "未指定", "未标注", "未分类", "综合"}

MAX_TEMPLATE_QUESTIONS_PER_PAGE = 200

# What a student page matched to this template page means for grading.  The
# teacher chooses it at upload; a model-inferred role is only ever a suggestion.
# ``page_role`` is stored only when chosen, so pages written before roles
# existed keep their confirmed hashes and read back as ``answer_page``.
PAGE_ROLES = ("answer_page", "answer_sheet", "question_only", "non_gradable")
DEFAULT_PAGE_ROLE = "answer_page"
GRADED_PAGE_ROLES = frozenset({"answer_page", "answer_sheet"})
MAX_PAGE_LABEL_LENGTH = 64


def normalize_page_role(value: object) -> str:
    """Return a supported page role; empty means the legacy default."""
    role = str(value or "").strip().lower()
    if not role:
        return DEFAULT_PAGE_ROLE
    if role not in PAGE_ROLES:
        raise ValueError("page_role 只能是 answer_page、answer_sheet、question_only 或 non_gradable")
    return role


def page_role_of(page: dict) -> str:
    """Effective role of a stored page; a page without a role is an answer page."""
    return normalize_page_role((page or {}).get("page_role"))


def page_is_graded(page: dict) -> bool:
    return page_role_of(page) in GRADED_PAGE_ROLES


def normalize_page_label(value: object) -> str:
    label = " ".join(str(value or "").split())
    if len(label) > MAX_PAGE_LABEL_LENGTH or any(ord(ch) < 32 for ch in label):
        raise ValueError(f"页面印刷标识不能超过 {MAX_PAGE_LABEL_LENGTH} 个字符")
    return label


def normalize_page_identity(value: object) -> dict:
    """Keep only the server-derived routing identity of one template page.

    ``printed_page_label`` is the printed page/sheet caption (for example
    ``答题卡``), ``printed_question_labels`` the printed question labels seen on
    the page, and ``visual_fingerprint`` a coarse hash of the corrected template.
    Empty parts are dropped so an unknown value never reads as a match.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("page_identity 必须为对象")
    identity: dict = {}
    page_label = normalize_page_label(value.get("printed_page_label"))
    if page_label:
        identity["printed_page_label"] = page_label
    raw_labels = value.get("printed_question_labels") or []
    if not isinstance(raw_labels, list) or len(raw_labels) > MAX_TEMPLATE_QUESTIONS_PER_PAGE:
        raise ValueError("printed_question_labels 必须为题号列表")
    labels = []
    for raw in raw_labels:
        # Printed labels come from the model's printed_path; an unusable one
        # is dropped as unknown rather than failing the whole template.
        label = " ".join(str(raw or "").split())
        if (label and label not in labels and len(label) <= 128
                and not any(ord(ch) < 32 for ch in label)):
            labels.append(label)
    if labels:
        identity["printed_question_labels"] = labels
    fingerprint = str(value.get("visual_fingerprint") or "").strip().lower()
    if fingerprint:
        if not re.fullmatch(r"dhash256:[0-9a-f]{64}", fingerprint):
            raise ValueError("visual_fingerprint 格式无效")
        identity["visual_fingerprint"] = fingerprint
    return identity


def page_metadata(page: dict) -> dict:
    """Role/identity keys to carry when a page is rebuilt; absent stays absent."""
    metadata = {}
    if isinstance(page, dict):
        if "page_role" in page:
            metadata["page_role"] = page["page_role"]
        if page.get("page_identity"):
            metadata["page_identity"] = page["page_identity"]
    return metadata


# Geometry is deliberately absent from the answer contract: ``drawing_regions``
# and ``label_anchor`` say where to look, never what the answer is, and the visual
# side of the template is tracked separately by ``artifact_hash``.  Everything
# that can change a grade -- printed text, question type, standard answer,
# scoring, directory ownership -- stays in the digest, so an edit that alters what
# the student is graded against still invalidates the confirmation.
ANSWER_CONTRACT_QUESTION_FIELDS = (
    "question_no", "question_number", "question_text", "content", "question_type",
    "choice_mode", "standard_answer", "answer_variants", "solution_steps",
    "max_score", "blank_count", "answer_slots", "fill_scoring_mode", "blank_scores",
    "subquestion_count", "subquestion_scores", "grading_method", "chapter", "topic",
    "correction_excluded", "matched", "question_bank_id", "difficulty", "source",
    "directory_node_id", "directory_parent_id", "printed_path", "answer_kind",
    "slot_ids", "region_kinds",
)
# The confirmed standard figure is answer content; ``teacher_confirmed`` is only a
# review state, so saving a moved box cannot read as a changed answer.
ANSWER_CONTRACT_REFERENCE_FIELDS = ("comparison_mode", "variants", "reference_hash")


def answer_contract_question(question: dict) -> dict:
    payload = {key: copy.deepcopy(question[key])
               for key in ANSWER_CONTRACT_QUESTION_FIELDS if key in question}
    reference = question.get("drawing_reference")
    if isinstance(reference, dict):
        payload["drawing_reference"] = {key: copy.deepcopy(reference[key])
                                        for key in ANSWER_CONTRACT_REFERENCE_FIELDS
                                        if key in reference}
    return payload


def answer_contract_pages(pages: list[dict]) -> list[dict]:
    # A role decides whether a page is graded, so it belongs to the answer
    # contract -- but only when present, like the optional artifact fields, so
    # confirmations written before roles existed keep their hash.
    return [
        {"page_index": page.get("page_index"),
         **({"page_role": page["page_role"]} if "page_role" in page else {}),
         "questions": [answer_contract_question(question)
                       for question in page.get("questions") or []
                       if isinstance(question, dict)]}
        for page in (pages or []) if isinstance(page, dict)
    ]


def _question_no_text(question: dict) -> str:
    return str(
        question.get("question_no") or question.get("question_number") or ""
    ).strip()


def _plain_int_number(text: str) -> Optional[int]:
    """Return the integer value of a plain decimal question number.

    Only a bare decimal number can be shifted.  A paper may label a question
    ``11(1)`` or ``二.3``, and there is no defined successor for those, so they
    are reported rather than guessed at.
    """
    return int(text) if text.isdigit() else None


def _page_position(pages: list[dict], page_index: int) -> int:
    for position, page in enumerate(pages):
        if int(page.get("page_index", -1)) == int(page_index):
            return position
    raise ValueError("模板页面不存在")


def _printed_page_order(pages: list[dict]) -> list[int]:
    """List positions in ``pages`` ordered by the printed page they carry."""
    return sorted(
        range(len(pages)),
        key=lambda position: int(pages[position].get("page_index", position)),
    )


def _document_positions(pages: list[dict]) -> list[tuple[int, int]]:
    """Return ``(page_position, question_index)`` pairs in reading order."""
    return [
        (page_position, question_index)
        for page_position in _printed_page_order(pages)
        for question_index in range(len(pages[page_position].get("questions") or []))
    ]


def insert_question(
    pages: list[dict],
    *,
    page_index: int,
    before_question_number: str = "",
    new_question_number: str = "",
) -> tuple[list[dict], str, dict[str, str]]:
    """Insert one question, shifting the rest of the paper up by one number.

    A vision model that misses a question labels every later question with its
    predecessor's number, so the whole tail of the paper reads one low.  The
    shift therefore continues onto later pages: question numbers are unique for
    the whole paper, and :meth:`TemplateSession.build_answer_map` resolves a
    collision by silently keeping only one of the pair.

    Every shifted question carries its own text and answer with it.  The
    question the model labelled "1" really is the paper's second question, so
    its content belongs to "2".  The inserted question starts empty, for the
    teacher to fill in before confirming.

    ``before_question_number`` names the question the new one goes in front of;
    empty appends to the end of that page.  The new number is derived from the
    position rather than supplied, so the number and the shift cannot disagree.

    ``new_question_number`` is the teacher's own number for the new question
    (digits and dots only).  It is applied verbatim without shifting the rest,
    because the teacher reads it off the paper.
    """
    if not isinstance(pages, list) or not pages:
        raise ValueError("模板尚未识别，不能新增题号")
    working = copy.deepcopy(pages)
    page_position = _page_position(working, page_index)
    if not page_is_graded(working[page_position]):
        raise ValueError("该页已标为不计分页，不能新增题号")
    questions = working[page_position].get("questions") or []
    if len(questions) >= MAX_TEMPLATE_QUESTIONS_PER_PAGE:
        raise ValueError(
            f"单页题目不能超过 {MAX_TEMPLATE_QUESTIONS_PER_PAGE} 题"
        )

    target = str(before_question_number or "").strip()
    if target:
        question_index = next(
            (
                index
                for index, question in enumerate(questions)
                if _question_no_text(question) == target
            ),
            -1,
        )
        if question_index < 0:
            raise ValueError(f"本页没有题号 {target}，无法在其之前插入")
    else:
        question_index = len(questions)

    requested = str(new_question_number or "").strip()
    if requested:
        if not re.fullmatch(r"\d+(\.\d+)*", requested):
            raise ValueError("题号只能由数字和点组成，开头和结尾必须是数字，点不能连续")
        for page in working:
            for question in page.get("questions") or []:
                if _question_no_text(question) == requested:
                    raise ValueError(f"题号 {requested} 已存在，请换一个题号")
        inserted = {
            "question_no": requested,
            "question_text": "",
            "question_type": "solve",
            "standard_answer": "",
        }
        if any(question.get("directory_node_id") for page in working for question in page.get("questions") or []):
            reference = questions[question_index] if question_index < len(questions) else (questions[-1] if questions else {})
            inserted.update(
                directory_node_id="teacher-" + uuid.uuid4().hex,
                directory_parent_id=reference.get("directory_parent_id"),
                printed_path=list(reference.get("printed_path") or [None])[:-1] + [None],
            )
        questions.insert(question_index, inserted)
        working[page_position]["questions"] = questions
        return _renormalized_pages(working), requested, {}

    if any("." in _question_no_text(question) for page in working for question in page.get("questions") or []):
        # Insert a sibling leaf only; never renumber a different group or
        # invent a new parent. The original printed_path remains provenance.
        reference = questions[question_index] if target else (questions[-1] if questions else None)
        if reference is None:
            raise ValueError("层级模板空页不能自动推断父题，请重新识别")
        parts = _question_no_text(reference).split(".")
        if not all(part.isdigit() for part in parts):
            raise ValueError("层级题号必须由数字及点组成")
        parent = parts[:-1]
        ordinal = int(parts[-1]) + (0 if target else 1)
        new_number = ".".join(parent + [str(ordinal)])
        renames = {}
        for page in working:
            for question in page.get("questions") or []:
                old = _question_no_text(question)
                sibling = old.split(".")
                if sibling[:-1] == parent and sibling[-1].isdigit() and int(sibling[-1]) >= ordinal:
                    new = ".".join(parent + [str(int(sibling[-1]) + 1)])
                    question["question_no"] = new
                    question.pop("question_number", None)
                    renames[old] = new
        questions.insert(question_index, {
            "question_no": new_number, "question_text": "", "question_type": "solve", "standard_answer": "",
            "directory_node_id": "teacher-" + uuid.uuid4().hex,
            "directory_parent_id": reference.get("directory_parent_id"),
            "printed_path": list(reference.get("printed_path") or parts)[:-1] + [str(ordinal)],
        })
        return _renormalized_pages(working), new_number, renames

    order = _document_positions(working)
    # Reading order is by page_index, which the stored list order need not
    # follow, so locate the insertion point by counting earlier questions.
    this_page = int(working[page_position].get("page_index", 0))
    insert_at = sum(
        len(page.get("questions") or [])
        for page in working
        if int(page.get("page_index", 0)) < this_page
    ) + question_index
    tail = order[insert_at:]
    if tail:
        first_text = _question_no_text(
            working[tail[0][0]]["questions"][tail[0][1]]
        )
        number = _plain_int_number(first_text)
        if number is None:
            raise ValueError(
                f"题号 {first_text} 不是纯数字，无法自动顺延，请先手工调整题号"
            )
        new_number = str(number)
    else:
        previous = [
            _plain_int_number(_question_no_text(working[pos]["questions"][idx]))
            for pos, idx in order
        ]
        known = [value for value in previous if value is not None]
        new_number = str(max(known) + 1) if known else "1"

    # Shift the tail before inserting, so the new number is free.  The caller
    # needs the old-to-new mapping: the locating artifacts key their question
    # boxes and handwriting exemptions by number, and those boxes still mark the
    # same ink on the page after a shift.
    renames: dict[str, str] = {}
    for pos, idx in tail:
        question = working[pos]["questions"][idx]
        text = _question_no_text(question)
        value = _plain_int_number(text)
        if value is None:
            raise ValueError(
                f"题号 {text} 不是纯数字，无法自动顺延，请先手工调整题号"
            )
        question["question_no"] = str(value + 1)
        question.pop("question_number", None)
        renames[text] = question["question_no"]

    inserted = {
        "question_no": new_number,
        "question_text": "",
        "question_type": "solve",
        "standard_answer": "",
    }
    if any(question.get("directory_node_id") for page in working for question in page.get("questions") or []):
        reference = questions[question_index] if question_index < len(questions) else (questions[-1] if questions else {})
        inserted.update(
            directory_node_id="teacher-" + uuid.uuid4().hex,
            directory_parent_id=reference.get("directory_parent_id"),
            printed_path=list(reference.get("printed_path") or [None])[:-1] + [None],
        )
    questions.insert(question_index, inserted)
    working[page_position]["questions"] = questions
    return _renormalized_pages(working), new_number, renames


def delete_question(
    pages: list[dict],
    *,
    page_index: int,
    question_number: str,
) -> list[dict]:
    """Remove one question without renumbering the rest.

    Deletion is deliberately not the mirror of insertion.  A teacher deletes an
    annotation the model should not have produced — a duplicate box, or a
    heading read as a question — and the surrounding numbers are already right.
    Shifting them down would corrupt a correctly numbered paper.
    """
    if not isinstance(pages, list) or not pages:
        raise ValueError("模板尚未识别，不能删除题号")
    working = copy.deepcopy(pages)
    page_position = _page_position(working, page_index)
    questions = working[page_position].get("questions") or []
    target = str(question_number or "").strip()
    remaining = [
        question for question in questions
        if _question_no_text(question) != target
    ]
    if len(remaining) == len(questions):
        raise ValueError(f"本页没有题号 {target}")
    if not remaining and len(working) == 1:
        raise ValueError("模板至少要保留一道题")
    working[page_position]["questions"] = remaining
    return _renormalized_pages(working)


def _renormalized_pages(pages: list[dict]) -> list[dict]:
    """Rebuild page signatures and reject a paper with duplicate numbers."""
    seen: dict[str, int] = {}
    for page in pages:
        for question in page.get("questions") or []:
            text = _question_no_text(question)
            if not text:
                raise ValueError("题号不能为空")
            if text in seen:
                raise ValueError(
                    f"题号 {text} 在第 {seen[text] + 1} 页已存在；"
                    "整卷题号必须唯一，否则批改会丢掉其中一题的答案"
                )
            seen[text] = int(page.get("page_index", 0))
    return [
        TemplateSession.make_page(
            int(page.get("page_index", 0)),
            page.get("questions") or [],
            **page_metadata(page),
        )
        for page in pages
    ]


class TemplateSession:
    """一次性多页模板会话。

    status 流转：pending → recognizing → ocr_done → confirmed → frozen
    confirmed 在学生卷识别前可重复保存；识别入口将其原子推进为 frozen。
    pages: [{page_index, signature, questions:[{question_no, question_text,
             question_type, standard_answer}]}]
    """

    def __init__(
        self, template_id: str, upload_dir: str, teacher_id: int | None = None,
        *, teaching_context: dict | None = None,
    ):
        template_id = str(template_id or "")
        if not _SAFE_TEMPLATE_ID.fullmatch(template_id):
            raise ValueError("非法的模板标识")
        self.template_id = template_id
        self.upload_dir = upload_dir
        self.teacher_id = int(teacher_id) if teacher_id is not None else None
        self.teaching_context = validate_teaching_context(teaching_context)
        self.status = "pending"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.recognition_started_at: Optional[str] = None
        self.recognition_deadline_at: Optional[str] = None
        # Deprecated compatibility metadata. New imports always generate
        # standard answers from transcribed printed text, regardless of this flag.
        self.has_answers = True
        # New imports overwrite this explicitly from the teacher's selection.
        # The constructor default keeps pre-mode sessions conservative.
        self.template_mode = "marked"
        # Teacher-facing page layout choice.  The documented default is a
        # double-column ordinary paper; legacy metadata without this field is
        # read back as that default and can still be corrected before freeze.
        self.column_mode = "double"
        self.category = ""              # 确认前冻结；考试模板才显示/保存分值
        self.exam_name = ""
        self.pages: list[dict] = []
        self.answer_contract_version: int = 0
        self.confirmed_contract_hash: str = ""
        self.confirmed_artifact_hash: str = ""
        self.confirmed_artifact_hash_version: int = 0
        # Answer-side identity of the same confirmation.  Empty on snapshots that
        # predate the split, which keeps them fail-closed.
        self.confirmed_answers_hash: str = ""
        self.requires_reconfirmation: bool = False
        self.reconfirmation_reason: str = ""
        self.confirmed_at: Optional[str] = None
        self.confirmed_by: int | None = None
        # Knowledge classifications are derived metadata, not part of the teacher's
        # authoritative answer contract.  Keeping them outside ``pages`` lets
        # the background classifier enrich a confirmed/frozen template without
        # invalidating its confirmation hash.
        self.knowledge_classifications: dict[str, dict] = {}
        # Server-owned visual assets are deliberately separate from the
        # teacher-editable page contract.  The confirm endpoint never accepts
        # paths or coordinates from the client.
        self.page_artifacts: dict[str, dict] = {}
        # Original hierarchy is audit context. The editable/frozen leaf list
        # remains the sole scoring authority.
        self.question_directories: dict[str, dict] = {}
        # Server-owned Aliyun layout candidates. These are review hints bound to
        # an immutable artifact SHA-256, never authoritative answer regions.
        self.layout_candidates: dict[str, dict] = {}
        self.layout_cut_runs: dict[str, dict] = {}
        # Teacher-only crop preview overrides. These are review geometry, kept
        # separate from the frozen full-column evidence layout used by student OCR.
        self.error: Optional[str] = None
        self.error_code: str = ""
        self.error_info: dict = {}
        self.bank_imported_at: Optional[str] = None
        self.bank_import_hash: str = ""
        self.bank_imported_count: int = 0

    @property
    def template_dir(self) -> str:
        return os.path.join(self.upload_dir, f"template_{self.template_id}")

    @property
    def meta_path(self) -> str:
        return os.path.join(self.template_dir, "meta.json")

    @staticmethod
    def make_page(
        page_index: int,
        questions: list[dict],
        *,
        page_role: str | None = None,
        page_identity: dict | None = None,
    ) -> dict:
        """规范化一页题目并附页签名，返回 page dict。

        OCR 阶段和老师确认阶段共用此逻辑：题号/题干/题型/标准答案统一成
        内部字段名，签名由题号集合算出（供 step 3 学生卷匹配复用）。
        question_type 非法值兜底为 solve（走 LLM，安全侧）。
        ``page_role``/``page_identity`` are written only when given, so a page
        rebuilt from legacy metadata keeps its original shape and hash.
        """
        norm_questions = []
        for q in questions:
            spec = AnswerSpec.model_validate(q)
            norm_questions.append(spec.model_dump())
        role = normalize_page_role(page_role) if page_role is not None else None
        if role is not None and role not in GRADED_PAGE_ROLES and norm_questions:
            raise ValueError("不计分页不能包含题目")
        page = {
            "page_index": page_index,
            "signature": question_number_signature(q["question_no"] for q in norm_questions),
            "questions": norm_questions,
        }
        if role is not None:
            page["page_role"] = role
        identity = normalize_page_identity(page_identity)
        if identity:
            page["page_identity"] = identity
        return page

    def add_page(
        self,
        page_index: int,
        questions: list[dict],
        artifact: dict | None = None,
        *,
        page_role: str | None = None,
        page_identity: dict | None = None,
    ) -> None:
        """加入一页 OCR 结果。questions 里每题至少含 question_number/question_text。"""
        self.pages.append(self.make_page(
            page_index, questions, page_role=page_role, page_identity=page_identity,
        ))
        if artifact is not None:
            self.page_artifacts[f"page_{int(page_index) + 1}"] = artifact

    def build_answer_map(self) -> dict:
        """把已确认的多页模板摊平成批改用 answer_map（题号 → 权威条目）。

        题号在整张卷内唯一，故跨页摊平进同一个 dict。每题都是老师确认过的绝对权威：
        matched=True、source="template_confirmed"，题干/答案/题型全部取模板值。
        学生卷批改时按题号命中此表，直接复用题干+标准答案，不走题库匹配、不走 AI 建议。
        跨页重复题号必须先由教师消歧；不能用后一页覆盖前一页。
        """
        from services.template_knowledge import classification_is_current, load_classifications
        classifications = dict(self.knowledge_classifications)
        try:
            classifications.update(load_classifications(self))
        except (OSError, ValueError):
            pass
        answer_map: dict = {}
        for page in self.pages:
            for q in page.get("questions", []):
                qn = str(q.get("question_no", "")).strip()
                if not qn:
                    continue
                if qn in answer_map:
                    raise ValueError(f"整卷存在重复题号 {qn}，请先核对层级归属；不能覆盖另一页答案")
                effective = dict(q)
                chapter = str(effective.get("chapter") or "").strip()
                topic = str(effective.get("topic") or "").strip()
                if chapter in _UNKNOWN_KNOWLEDGE or topic in _UNKNOWN_KNOWLEDGE:
                    key = self.knowledge_classification_key(
                        int(page.get("page_index", 0)), qn
                    )
                    classified = classifications.get(key) or {}
                    if classification_is_current(self, effective, classified):
                        derived_chapter = str(classified.get("chapter") or "").strip()
                        derived_topic = str(classified.get("topic") or "").strip()
                        if (
                            derived_chapter not in _UNKNOWN_KNOWLEDGE
                            and derived_topic not in _UNKNOWN_KNOWLEDGE
                        ):
                            if chapter in _UNKNOWN_KNOWLEDGE:
                                effective["chapter"] = derived_chapter
                            if topic in _UNKNOWN_KNOWLEDGE:
                                effective["topic"] = derived_topic
                spec = AnswerSpec.model_validate(effective)
                answer_map[qn] = spec.to_answer_map_entry(source="template_confirmed")
                if self.teaching_context:
                    from services.teaching_context import record_teaching_fields
                    answer_map[qn].update(record_teaching_fields(
                        self.teaching_context, chapter=spec.chapter, topic=spec.topic,
                    ))
                    answer_map[qn]["teaching_context"] = copy.deepcopy(self.teaching_context)
        return answer_map

    @staticmethod
    def knowledge_classification_key(page_index: int, question_no: object) -> str:
        return f"{int(page_index)}:{str(question_no or '').strip()}"

    @staticmethod
    def question_text_hash(question_text: object) -> str:
        text = " ".join(str(question_text or "").split())
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def answers_hash(self) -> str:
        """Stable identity of what the student is graded against.

        Ignores the coordinates that only say *where* an answer is read from, so a
        teacher who moves a定位框 keeps the standard answers; changing 题干、
        标准答案、题型 or 归属 still moves this hash.
        """
        payload = {
            "category": self.category,
            "exam_name": self.exam_name,
            "template_mode": self.template_mode,
            "column_mode": self.column_mode,
            "pages": answer_contract_pages(self.pages),
        }
        if self.teaching_context:
            payload["teaching_context"] = validate_teaching_context(self.teaching_context)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def content_hash(self) -> str:
        """Stable hash of the frozen template contract for idempotent bank import."""
        payload = {
            "category": self.category,
            "exam_name": self.exam_name,
            "template_mode": self.template_mode,
            "column_mode": self.column_mode,
            "pages": self.pages,
        }
        # Preserve the exact identity of legacy confirmations. New tasks bind
        # both school stage and rules; profile changes cannot alter this copy.
        if self.teaching_context:
            payload["teaching_context"] = validate_teaching_context(self.teaching_context)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def artifact_hash(self) -> str:
        """Stable identity of server-owned visual artifacts used by this template."""
        payload = []
        for page_label, artifact in sorted((self.page_artifacts or {}).items()):
            if not isinstance(artifact, dict):
                payload.append({"page_label": page_label, "invalid": True})
                continue
            artifact_identity = {
                "page_label": page_label,
                "version": artifact.get("version"),
                "sha256": str(artifact.get("sha256") or ""),
                "width": artifact.get("width"),
                "height": artifact.get("height"),
                "question_schema": artifact.get("question_schema") or [],
                "question_numbers": artifact.get("question_numbers") or [],
            }
            # Preserve hashes already confirmed before question anchors became
            # part of the artifact contract. New artifacts bind the field even
            # when the normalized list is empty.
            if "question_anchors" in artifact:
                artifact_identity["question_anchors"] = (
                    artifact.get("question_anchors") or []
                )
            if "template_handwriting_regions" in artifact:
                artifact_identity["template_handwriting_regions"] = (
                    artifact.get("template_handwriting_regions") or []
                )
            if "template_answer_regions" in artifact:
                artifact_identity["template_answer_regions"] = (
                    artifact.get("template_answer_regions") or []
                )
            if "question_crop_plan" in artifact:
                artifact_identity["question_crop_plan"] = (
                    artifact.get("question_crop_plan") or {}
                )
            if "difference_template_image" in artifact:
                artifact_identity["difference_template_image"] = str(
                    artifact.get("difference_template_image") or ""
                )
            if "difference_template_sha256" in artifact:
                artifact_identity["difference_template_sha256"] = str(
                    artifact.get("difference_template_sha256") or ""
                )
            if "template_mode" in artifact:
                artifact_identity["template_mode"] = str(
                    artifact.get("template_mode") or ""
                )
            if "question_label_policy" in artifact:
                artifact_identity["question_label_policy"] = str(
                    artifact.get("question_label_policy") or ""
                )
            payload.append(artifact_identity)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def confirm_current_contract(self, teacher_id: int) -> None:
        """Bind the current teacher-confirmed pages to the active contract."""
        self.answer_contract_version = CURRENT_ANSWER_CONTRACT_VERSION
        self.confirmed_contract_hash = self.content_hash()
        self.confirmed_artifact_hash = self.artifact_hash()
        self.confirmed_artifact_hash_version = CURRENT_ARTIFACT_HASH_VERSION
        self.confirmed_answers_hash = self.answers_hash()
        self.requires_reconfirmation = False
        self.reconfirmation_reason = ""
        self.confirmed_at = datetime.now(timezone.utc).isoformat()
        self.confirmed_by = int(teacher_id)

    def recognition_contract_error(self) -> str:
        """Return why this snapshot cannot grade students, or an empty string."""
        if self.answer_contract_version < MIN_COMPATIBLE_ANSWER_CONTRACT_VERSION:
            return LEGACY_CONTRACT_REASON
        if not self.confirmed_contract_hash:
            return LEGACY_CONTRACT_REASON
        if self.confirmed_contract_hash != self.content_hash():
            # Answer reuse changes the review reason, never the confirmation
            # gate. A page-only geometry edit may leave artifact_hash unchanged
            # if the two persisted views become out of sync.
            if (self.confirmed_answers_hash
                    and self.confirmed_answers_hash == self.answers_hash()):
                return GEOMETRY_CHANGED_REASON
            return CONTENT_CHANGED_REASON
        if (self.page_artifacts
                and self.confirmed_artifact_hash_version != CURRENT_ARTIFACT_HASH_VERSION):
            # Never auto-upgrade an old signature from mutable geometry. The
            # teacher can re-confirm the existing answers without a model call.
            return LEGACY_GEOMETRY_CONTRACT_REASON
        if not self.confirmed_artifact_hash or self.confirmed_artifact_hash != self.artifact_hash():
            return GEOMETRY_CHANGED_REASON
        if self.requires_reconfirmation:
            return self.reconfirmation_reason or LEGACY_CONTRACT_REASON
        return ""

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "teaching_context": copy.deepcopy(self.teaching_context),
            "status": self.status,
            "created_at": self.created_at,
            "recognition_started_at": self.recognition_started_at,
            "recognition_deadline_at": self.recognition_deadline_at,
            "teacher_id": self.teacher_id,
            "has_answers": self.has_answers,
            "template_mode": self.template_mode,
            "column_mode": self.column_mode,
            "category": self.category,
            "exam_name": self.exam_name,
            "pages": self.pages,
            "answer_contract_version": self.answer_contract_version,
            "confirmed_contract_hash": self.confirmed_contract_hash,
            "confirmed_artifact_hash": self.confirmed_artifact_hash,
            "confirmed_artifact_hash_version": self.confirmed_artifact_hash_version,
            "confirmed_answers_hash": self.confirmed_answers_hash,
            "requires_reconfirmation": self.requires_reconfirmation,
            "reconfirmation_reason": self.reconfirmation_reason,
            "confirmed_at": self.confirmed_at,
            "confirmed_by": self.confirmed_by,
            "knowledge_classifications": self.knowledge_classifications,
            "page_artifacts": self.page_artifacts,
            "question_directories": self.question_directories,
            "layout_candidates": self.layout_candidates,
            "layout_cut_runs": self.layout_cut_runs,
            "error": self.error,
            "error_code": self.error_code,
            "error_info": self.error_info,
            "bank_imported_at": self.bank_imported_at,
            "bank_import_hash": self.bank_import_hash,
            "bank_imported_count": self.bank_imported_count,
        }

    def save_meta(self) -> None:
        os.makedirs(self.template_dir, exist_ok=True)
        tmp = f"{self.meta_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    with _meta_io_lock(self.meta_path):
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

    @classmethod
    def load_meta(cls, template_id: str, upload_dir: str) -> "TemplateSession":
        sess = cls(template_id, upload_dir)
        with _meta_io_lock(sess.meta_path):
            if not os.path.exists(sess.meta_path):
                return sess
            with open(sess.meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        sess.teaching_context = validate_teaching_context(data.get("teaching_context"))
        sess.status = data.get("status", "pending")
        sess.created_at = data.get("created_at") or sess.created_at
        sess.recognition_started_at = data.get("recognition_started_at") or None
        sess.recognition_deadline_at = data.get("recognition_deadline_at") or None
        raw_teacher_id = data.get("teacher_id")
        try:
            teacher_id = int(raw_teacher_id) if raw_teacher_id is not None else None
            sess.teacher_id = teacher_id if teacher_id and teacher_id > 0 else None
        except (TypeError, ValueError):
            sess.teacher_id = None
        sess.has_answers = bool(data.get("has_answers", True))
        # Missing/invalid mode means a legacy template.  Treat it as marked so
        # any existing handwriting is preserved until the teacher reimports or
        # explicitly confirms a mode.
        sess.template_mode = normalize_template_mode(
            data.get("template_mode"),
            default="marked",
        )
        raw_column_mode = str(data.get("column_mode") or "double").strip().lower()
        sess.column_mode = raw_column_mode if raw_column_mode in {
            "single", "double", "triple",
        } else "double"
        sess.category = str(data.get("category") or "")
        sess.exam_name = str(data.get("exam_name") or "")
        # Persisted legacy rows may still carry a stale subquestion score layer
        # on directory leaves (half-flattened imports). Fold it into the root
        # total on load so the strict AnswerSpec gate sees the canonical shape;
        # new submissions stay strictly rejected.
        sess.pages = [
            {
                **page,
                "questions": [
                    reconcile_drawing_contract(merge_directory_leaf_score_layer(question))
                    for question in page.get("questions") or []
                ],
            }
            for page in (data.get("pages", []) or [])
        ]
        try:
            sess.answer_contract_version = max(0, int(data.get("answer_contract_version") or 0))
        except (TypeError, ValueError):
            sess.answer_contract_version = 0
        sess.confirmed_contract_hash = str(data.get("confirmed_contract_hash") or "")
        sess.confirmed_artifact_hash = str(data.get("confirmed_artifact_hash") or "")
        try:
            sess.confirmed_artifact_hash_version = max(
                0, int(data.get("confirmed_artifact_hash_version") or 0),
            )
        except (TypeError, ValueError):
            sess.confirmed_artifact_hash_version = 0
        sess.confirmed_answers_hash = str(data.get("confirmed_answers_hash") or "")
        sess.requires_reconfirmation = bool(data.get("requires_reconfirmation", False))
        sess.reconfirmation_reason = str(data.get("reconfirmation_reason") or "")
        sess.confirmed_at = data.get("confirmed_at") or None
        try:
            raw_confirmed_by = data.get("confirmed_by")
            confirmed_by = int(raw_confirmed_by) if raw_confirmed_by is not None else None
            sess.confirmed_by = confirmed_by if confirmed_by and confirmed_by > 0 else None
        except (TypeError, ValueError):
            sess.confirmed_by = None
        raw_classifications = data.get("knowledge_classifications") or {}
        sess.knowledge_classifications = (
            raw_classifications if isinstance(raw_classifications, dict) else {}
        )
        if (
            sess.status in {"confirmed", "frozen"}
            and sess.answer_contract_version < MIN_COMPATIBLE_ANSWER_CONTRACT_VERSION
        ):
            sess.requires_reconfirmation = True
            if not sess.reconfirmation_reason:
                sess.reconfirmation_reason = LEGACY_CONTRACT_REASON
        raw_artifacts = data.get("page_artifacts") or {}
        sess.page_artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
        directories = data.get("question_directories") or {}
        sess.question_directories = directories if isinstance(directories, dict) else {}
        raw_candidates = data.get("layout_candidates") or {}
        sess.layout_candidates = raw_candidates if isinstance(raw_candidates, dict) else {}
        raw_runs = data.get("layout_cut_runs") or {}
        sess.layout_cut_runs = raw_runs if isinstance(raw_runs, dict) else {}
        sess.error = data.get("error")
        sess.error_code = str(data.get("error_code") or "")
        sess.error_info = data.get("error_info") if isinstance(data.get("error_info"), dict) else {}
        sess.bank_imported_at = data.get("bank_imported_at") or None
        sess.bank_import_hash = str(data.get("bank_import_hash") or "")
        try:
            sess.bank_imported_count = max(0, int(data.get("bank_imported_count") or 0))
        except (TypeError, ValueError):
            sess.bank_imported_count = 0
        from services.template_knowledge import load_classifications
        try:
            sess.knowledge_classifications.update(load_classifications(sess))
        except (OSError, ValueError):
            # Derived storage failure cannot fail recognition or grading.
            # Report reconciliation exposes the failure and can retry it.
            pass
        return sess
