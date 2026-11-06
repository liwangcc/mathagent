"""Text-only, derived template knowledge; never writes the answer contract.

The atomic sidecar owns jobs, leases and explicit local-to-global bindings.
Template meta.json snapshots cannot erase it. Provider work is shared locally
and leased across workers; failures are durable and retryable at report time.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from services.teaching_context import record_teaching_fields, use_teaching_context
from services.template_session import _meta_io_lock

logger = logging.getLogger(__name__)
UNKNOWN = frozenset({"", "未知", "未指定", "未标注", "未分类", "综合"})
PROVIDER_TIMEOUT = 90.0
MAX_ATTEMPTS = 3
RETRY_DELAY = 60.0
RECONCILE_TIMEOUT = MAX_ATTEMPTS * PROVIDER_TIMEOUT + 30.0
_tasks: dict[tuple[str, str], asyncio.Task] = {}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _scope(sess) -> str:
    context = sess.teaching_context
    if context:
        catalog = context["curriculum_version"]
    else:
        # Historical templates have no stage snapshot. Preserve that explicit
        # legacy meaning while still binding cache identity to its real catalog.
        from services.analyzer import _knowledge_catalog
        with use_teaching_context({}):
            _, catalog = _knowledge_catalog()
    return _hash([sess.template_id, sess.teacher_id, context, catalog])


def _path(sess) -> Path:
    return Path(sess.template_dir) / "knowledge.json"


def _read(sess) -> dict:
    path = _path(sess)
    try:
        # Windows denies opening a file another thread is replacing, and an
        # open reader makes that os.replace fail: read and replace under the
        # same per-path I/O lock as template meta.json.
        with _meta_io_lock(path), path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "entries": {}, "bindings": {}, "sources": {}}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unsupported template knowledge ledger")
    for key in ("entries", "bindings", "sources"):
        if not isinstance(data.get(key), dict) or any(not isinstance(row, dict) for row in data[key].values()):
            raise ValueError("invalid template knowledge ledger")
    for entry in data["entries"].values():
        if entry.get("status") == "ready" and not isinstance(entry.get("classification"), dict):
            raise ValueError("invalid template knowledge result")
    return data


def _mutate(sess, change):
    # Short filesystem-only transaction. Never hold this lock across a provider
    # await, and never use the template meta lock (recognition owns that file).
    from services.quick_grader import BatchOperationBusy, batch_operation_lock
    for attempt in range(100):
        try:
            with batch_operation_lock("knowledge", sess.template_dir):
                data = _read(sess)
                result = change(data)
                path = _path(sess)
                temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                        json.dump(data, handle, ensure_ascii=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    with _meta_io_lock(path):
                        os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                return result
        except BatchOperationBusy:
            if attempt == 99:
                raise
            time.sleep(0.01)


def _number(question) -> str:
    return str(question.get("question_no") or question.get("question_number") or "").strip()


def _binding_hash(question) -> str:
    # Text changes and replacement/reparenting invalidate a binding. Teacher
    # answers, scores, rubric, geometry and chapter/topic are deliberately absent.
    return _hash([_number(question), _text(question.get("question_text")),
                  question.get("question_type"), question.get("choice_mode", "unknown"),
                  question.get("directory_node_id"), question.get("printed_path") or []])


def _entry(sess, page_index, number, text, *, local=False, binding_identity=None) -> tuple[str, dict]:
    text = _text(text)
    identity = _hash([_scope(sess), int(page_index), "local" if local else "global", number, text, binding_identity])
    return identity, {
        "id": identity, "scope": _scope(sess), "page_index": int(page_index),
        "question_no": str(number), "question": text,
        "question_text_hash": sess.question_text_hash(text),
        "status": "queued" if text else "unavailable", "attempts": 0,
        "error": "" if text else "题干缺失，知识点尚未分类", "retry_after": 0,
    }


def _applicable(data, sess, page_index, question):
    key = sess.knowledge_classification_key(page_index, _number(question))
    binding = data["bindings"].get(key) or {}
    if binding.get("scope") != _scope(sess) or binding.get("bound_hash") != _binding_hash(question):
        return None
    entry = data["entries"].get(binding.get("entry_id"))
    if not entry or entry.get("scope") != _scope(sess) or entry.get("page_index") != int(page_index):
        return None
    return entry


def _teacher_complete(question) -> bool:
    return all(_text(question.get(key)) not in UNKNOWN for key in ("chapter", "topic"))


def _legacy(sess, page_index, question):
    value = sess.knowledge_classifications.get(sess.knowledge_classification_key(page_index, _number(question))) or {}
    if value.get("question_text_hash") != sess.question_text_hash(question.get("question_text")):
        return None
    # Sidecar-derived projections must be validated through their binding, not
    # recycled as legacy after a teacher edits/replaces a question.
    if value.get("ledger_entry_id"):
        return None
    if sess.teaching_context and (
        value.get("education_stage") != sess.teaching_context["education_stage"]
        or value.get("catalog_version") != sess.teaching_context["curriculum_version"]
    ):
        return None
    return value if _teacher_complete(value) else None


def load_classifications(sess) -> dict:
    """Project only currently bound, ready rows into the historical read API."""
    data = _read(sess)
    result = {}
    for page in sess.pages:
        index = int(page.get("page_index", 0))
        for question in page.get("questions") or []:
            entry = _applicable(data, sess, index, question)
            if not entry or entry.get("status") != "ready":
                continue
            result[sess.knowledge_classification_key(index, _number(question))] = {
                **copy.deepcopy(entry["classification"]),
                "question_text_hash": sess.question_text_hash(question.get("question_text")),
                "ledger_entry_id": entry["id"], "bound_hash": _binding_hash(question),
                "scope": _scope(sess),
            }
    return result


def classification_is_current(sess, question, value) -> bool:
    if value.get("question_text_hash") != sess.question_text_hash(question.get("question_text")):
        return False
    if value.get("ledger_entry_id"):
        return value.get("scope") == _scope(sess) and value.get("bound_hash") == _binding_hash(question)
    return not sess.teaching_context or (
        value.get("education_stage") == sess.teaching_context["education_stage"]
        and value.get("catalog_version") == sess.teaching_context["curriculum_version"]
    )


def start_page_classification(sess, page_index: int, transcription: dict, *, classifier=None):
    """Synchronous text-ready callback: register, schedule; never await a model."""
    def register(data):
        sources = {}
        for row in transcription.get("questions") or []:
            qid = str(row.get("q_id") or "")
            if not qid.startswith("Q") or not qid[1:]:
                raise ValueError("knowledge transcription requires explicit Q identity")
            number = qid[1:]
            if number in sources:
                raise ValueError("duplicate knowledge transcription identity")
            identity, entry = _entry(sess, page_index, number, row.get("question_text"), local=True)
            data["entries"].setdefault(identity, entry)
            sources[number] = identity
        data["sources"][f"{_scope(sess)}:{page_index}"] = sources
        return list(sources.values())
    identities = _mutate(sess, register)
    _schedule(sess, identities, classifier)


def bind_page_knowledge(sess, page_index: int, global_questions: list[dict]):
    """Bind using page_local_number -> global number, never list positions.

    The finalized AnswerSpec text is the invalidation authority. Choice text
    discarded by merge can remain classifier-only evidence; it is never put
    back into the teacher's answer contract.
    """
    normalized = {_number(q): q for page in sess.pages if int(page.get("page_index", 0)) == page_index
                  for q in page.get("questions") or []}

    def bind(data):
        sources = data["sources"].get(f"{_scope(sess)}:{page_index}") or {}
        for raw in global_questions:
            number = _number(raw)
            question = normalized.get(number)
            local = str(raw.get("page_local_number") or "")
            entry = data["entries"].get(sources.get(local)) or {}
            if question is None or not entry.get("question"):
                continue
            text = _text(question.get("question_text"))
            if text != entry["question"] and not (question.get("question_type") == "choice" and not text):
                continue
            data["bindings"][sess.knowledge_classification_key(page_index, number)] = {
                "scope": _scope(sess), "entry_id": entry["id"],
                "page_local_number": local, "bound_hash": _binding_hash(question),
            }
    _mutate(sess, bind)


def _prepare_session(sess) -> list[str]:
    def prepare(data):
        identities = []
        for page in sess.pages:
            index = int(page.get("page_index", 0))
            for question in page.get("questions") or []:
                if _teacher_complete(question) or _legacy(sess, index, question):
                    continue
                entry = _applicable(data, sess, index, question)
                if entry is None:
                    identity, entry = _entry(sess, index, _number(question), question.get("question_text"),
                                             binding_identity=_binding_hash(question))
                    data["entries"].setdefault(identity, entry)
                    data["bindings"][sess.knowledge_classification_key(index, _number(question))] = {
                        "scope": _scope(sess), "entry_id": identity, "bound_hash": _binding_hash(question),
                    }
                identities.append(entry["id"])
        return identities
    return _mutate(sess, prepare)


def knowledge_state(sess) -> dict:
    data = _read(sess)
    pending = failed = unavailable = 0
    for page in sess.pages:
        index = int(page.get("page_index", 0))
        for question in page.get("questions") or []:
            if _teacher_complete(question) or _legacy(sess, index, question):
                continue
            entry = _applicable(data, sess, index, question)
            if entry is None or entry.get("status") in {"queued", "running"}:
                pending += 1
            elif entry.get("status") == "unavailable":
                unavailable += 1
            elif entry.get("status") != "ready":
                failed += 1
    return {"status": "pending" if pending else "failed" if failed else "ready",
            "pending": pending, "failed": failed, "unavailable": unavailable,
            "error": ("知识点分类仍在处理中，报告将等待最新标签" if pending else
                      "知识点分类暂未完成，稍后重试报告可恢复；已有成绩不受影响" if failed else
                      "部分题目缺少可分类文字，统计中保留未分类项" if unavailable else "")}


def _schedule(sess, identities, classifier=None):
    data = _read(sess)
    missing = []
    now = time.time()
    path = str(_path(sess).resolve())
    for identity in identities:
        task = _tasks.get((path, identity))
        if task is not None and not task.done():
            continue
        entry = data["entries"].get(identity) or {}
        if (not entry.get("question") or entry.get("status") == "ready"
                or float(entry.get("retry_after") or 0) > now
                or (entry.get("status") == "running" and float(entry.get("lease_until") or 0) > now)):
            continue
        missing.append(identity)
    if not missing:
        return set()
    task = asyncio.create_task(_classify(copy.copy(sess), missing, classifier), name="template-knowledge")
    for identity in missing:
        _tasks[(path, identity)] = task

    def done(completed):
        for identity in missing:
            if _tasks.get((path, identity)) is completed:
                _tasks.pop((path, identity), None)
        if not completed.cancelled() and completed.exception():
            logger.warning("template knowledge worker failed: %s", type(completed.exception()).__name__)
    task.add_done_callback(done)
    return set(missing)


async def _classify(sess, identities, classifier):
    owner = uuid.uuid4().hex

    def claim(data):
        claimed = []
        now = time.time()
        for identity in identities:
            entry = data["entries"][identity]
            if (entry.get("status") == "ready" or float(entry.get("retry_after") or 0) > now
                    or (entry.get("status") == "running" and float(entry.get("lease_until") or 0) > now)):
                continue
            entry.update(status="running", owner=owner, lease_until=now + MAX_ATTEMPTS * PROVIDER_TIMEOUT + 30)
            claimed.append(copy.deepcopy(entry))
        return claimed
    claimed = await asyncio.to_thread(_mutate, sess, claim)
    if not claimed:
        return
    resolved: dict[str, dict] = {}
    error = "知识点分类失败"
    attempts = 0
    try:
        if classifier is None:
            from services.analyzer import classify_questions_knowledge
            classifier = functools.partial(classify_questions_knowledge, per_question=True)
        from services.analyzer import KnowledgeClassifierConfigurationError
        for attempts in range(1, MAX_ATTEMPTS + 1):
            pending = [row for row in claimed if row["id"] not in resolved]
            try:
                with use_teaching_context(sess.teaching_context):
                    rows = await asyncio.wait_for(classifier([
                        {key: row[key] for key in ("id", "page_index", "question_no", "question", "question_text_hash")}
                        for row in pending
                    ]), PROVIDER_TIMEOUT)
                rows_by_id = {str(row.get("id") or ""): row for row in rows}
                if (len(rows) != len(pending) or len(rows_by_id) != len(pending)
                        or set(rows_by_id) != {row["id"] for row in pending}):
                    raise ValueError("模板知识点分类未覆盖全部题目")
                # Canonical analyzer owns strict curriculum validation. This
                # layer does not repair or guess: a catalog-valid row is kept,
                # any other row is retried alone instead of failing its peers.
                resolved.update({identity: row for identity, row in rows_by_id.items()
                                 if row.get("status") == "suggested" and _teacher_complete(row)})
                if len(resolved) == len(claimed):
                    break
                error = "知识点分类结果不在目录内"
            except KnowledgeClassifierConfigurationError:
                error = "知识点分类服务未配置"
                break
            except Exception as exc:
                error = f"知识点分类暂时失败（{type(exc).__name__}）"
    except asyncio.CancelledError:
        error = "知识点分类中断，可重试恢复"
        raise
    finally:
        def finish(data):
            for original in claimed:
                entry = data["entries"].get(original["id"]) or {}
                if entry.get("owner") != owner:
                    continue
                entry.update(attempts=int(entry.get("attempts") or 0) + attempts,
                             lease_until=0, owner="", updated_at=time.time())
                row = resolved.get(entry["id"])
                if row is None:
                    entry.update(status="failed", error=error, retry_after=time.time() + RETRY_DELAY)
                else:
                    entry.update(status="ready", error="", retry_after=0, classification={
                        **record_teaching_fields(sess.teaching_context, chapter=row["chapter"], topic=row["topic"]),
                        **{key: str(row.get(key) or "") for key in
                           ("chapter", "topic", "confidence", "classification_version")},
                    })
        await asyncio.to_thread(_mutate, sess, finish)


async def ensure_session_knowledge(sess, *, classifier=None, timeout=None) -> dict:
    """Await applicable work, shield shared tasks and return a recoverable state."""
    identities = await asyncio.to_thread(_prepare_session, sess)
    deadline = asyncio.get_running_loop().time() + (RECONCILE_TIMEOUT if timeout is None else timeout)
    attempted = {identity for identity in identities
                 if (task := _tasks.get((str(_path(sess).resolve()), identity))) is not None and not task.done()}
    while True:
        attempted.update(_schedule(sess, [identity for identity in identities if identity not in attempted], classifier))
        state = knowledge_state(sess)
        if state["status"] != "pending" or asyncio.get_running_loop().time() >= deadline:
            # A failed row can have just been scheduled for retry. Await that
            # local task rather than reporting the previous failure prematurely.
            active = {task for identity in identities
                      if (task := _tasks.get((str(_path(sess).resolve()), identity))) is not None and not task.done()}
            if not active or asyncio.get_running_loop().time() >= deadline:
                if active:
                    return {"status": "pending", "error": "知识点分类仍在处理中，报告将等待最新标签"}
                return state
        await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))


def reconcile_batch_knowledge(batch, sess) -> bool:
    """Refresh labels by parent question ID, preserving every grading field.

    Legacy knowledge_items have an established detail-aligned storage format;
    stamp their identities during migration. The *label lookup* always joins
    the parent question number, never a classifier's output/array position.
    """
    answers = sess.build_answer_map()
    changed = False
    # Validate the entire join before touching any review. A legacy batch with
    # no frozen hash still cannot borrow labels from a different question text.
    for number, entry in (batch.answer_map or {}).items():
        source = answers.get(str(number))
        if source is None or _text(entry.get("question_text")) != _text(source.get("question_text")):
            raise ValueError("batch question no longer matches template knowledge identity")
    for number, entry in (batch.answer_map or {}).items():
        source = answers[str(number)]
        for key in ("chapter", "topic"):
            if entry.get(key) != source.get(key):
                entry[key] = source.get(key)
                changed = True
    for review in batch.review_results or []:
        details = (review.get("result") or {}).get("details") or []
        old = review.get("knowledge_items") or []
        keyed = {str(item.get("detail_question_no")): item for item in old
                 if isinstance(item, dict) and item.get("detail_question_no")}
        updated = []
        for index, detail in enumerate(details):
            detail_number = str(detail.get("question_no") or detail.get("question_number") or "")
            parent = str(detail.get("parent_question_no") or detail_number).strip()
            previous = keyed.get(detail_number)
            if previous is None:
                legacy = old[index] if index < len(old) and isinstance(old[index], dict) else {}
                previous = legacy if not legacy.get("detail_question_no") and (
                    not legacy.get("question_no") or str(legacy["question_no"]) == parent
                ) else {}
            item = copy.deepcopy(previous)
            source = (batch.answer_map or {}).get(parent)
            if source is not None and parent in answers:
                item.update(question_no=parent, detail_question_no=detail_number,
                            chapter=source.get("chapter") or "未知", topic=source.get("topic") or "未知")
                item.update(record_teaching_fields(sess.teaching_context, chapter=item["chapter"], topic=item["topic"]))
            updated.append(item)
        if updated != old:
            review["knowledge_items"] = updated
            changed = True
    return changed
