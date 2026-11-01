"""Roster-scoped, name-first identity clues from collection folders and files.

Source paths are untrusted display metadata, never paths to read or write.
Numbers are considered only after names fail to identify one student. File
numbers must match a complete roster number, never a numeric suffix of one.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from services.filename_identity import _looks_like_date, _looks_like_timestamp, parse_filename_identity


def normalize_source_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("图片相对路径必须是文字")
    path = value.replace("\\", "/")
    parts = path.split("/")
    if (not path or len(path) > 2048 or len(parts) > 32
            or re.search(r"[\x00-\x1f\x7f:]", path)
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError("图片相对路径无效")
    return path


def parse_source_paths(value: str, count: int) -> list[str]:
    if not value:
        return [""] * count  # Older clients still send only filenames.
    if len(value) > count * 12_300 + 2:
        raise ValueError("图片相对路径数据过长")
    try:
        paths = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("图片相对路径必须是 JSON 数组") from exc
    if not isinstance(paths, list) or len(paths) != count:
        raise ValueError("图片相对路径数量与上传文件数量不一致")
    return [normalize_source_path(path) for path in paths]


def _text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).replace("•", "·")


_HAN = "\u3400-\u9fff\U00020000-\U0003134f"
_PREFIX_WORDS = ("收集件", "数学作业", "答题卡", "学生", "姓名", "学号", "数学", "作业", "试卷", "答卷", "扫描", "图片", "照片")
_SUFFIX_WORDS = ("已提交", "最终版", "正面", "反面", "背面", "第一页", "第二页", "副本", "扫描件", "作业", "试卷", "图片", "照片", "正", "反", "背")
_CONTAINER_NAMES = {"目录", "文件夹", "文件", "附件", "记录", "收集件", "收集", "用户", "学生", "下载", "提交"}
# The browser names extracted archive members "<archive>.zip/..." and a second
# archive of the same name "<archive>.zip (2)/...".
_ARCHIVE_SUFFIX_RE = re.compile(r"\.(?:zip|rar)(?: \(\d+\))?\Z", re.I)


def _only_noise(text: str, words: tuple[str, ...]) -> bool:
    for word in words:
        text = text.replace(word, "")
    return not text


def _component_names(text: str, names: set[str]) -> set[str]:
    """Match actual full roster names, not a guessed two-to-four-Hanzi slice."""
    found: list[tuple[int, int, str]] = []
    for name in names:
        for match in re.finditer(re.escape(name), text):
            before, after = text[:match.start()], text[match.end():]
            left = re.search(f"[{_HAN}]+$", before)
            right = re.match(f"[{_HAN}]+", after)
            if left and not _only_noise(left.group(), _PREFIX_WORDS):
                continue
            if right and not _only_noise(right.group(), _SUFFIX_WORDS):
                continue
            # Latin names must not match letters inside a random identifier.
            if name[0].isascii() and name[0].isalpha() and before[-1:].isalpha():
                continue
            if name[-1].isascii() and name[-1].isalpha() and after[:1].isalpha():
                continue
            found.append((match.start(), match.end(), name))
    return {
        name for start, end, name in found
        if not any(a <= start and b >= end and b - a > end - start for a, b, _ in found)
    }


def _number_key(value: str) -> str:
    value = _text(value).casefold()
    return (value.lstrip("0") or "0") if value.isdecimal() else value


def _component_numbers(text: str, names: set[str]) -> set[str]:
    # Windows/tool duplicate counters, front/back labels and page counters are
    # not student numbers. Single-digit unlabelled tokens remain ambiguous.
    text = re.sub(r"\(\d+\)", "", text)
    text = re.sub(r"第?\d+页", "", text)
    found: set[str] = set()
    for name in names:
        for match in re.finditer(re.escape(name), text):
            tails = [text[match.end():]]
            before = re.search(r"([A-Za-z0-9]+)[_\-]*$", text[:match.start()])
            if before:
                tails.append(before.group(1))
            for tail in tails:
                number = re.match(r"^([_\-]*)(学号[:：]?)?([A-Za-z0-9]+)", tail)
                if not number:
                    continue
                raw = number.group(3)
                if len(raw) < 2 and not number.group(2):
                    continue
                # _01/_12 may be page or duplicate indices. In an ambiguous
                # name group a short number needs an explicit label or direct
                # adjacency (张三08 / 张三_学号08), not a guessed suffix.
                if raw.isdecimal() and len(raw) <= 3 and number.group(1) and not number.group(2):
                    continue
                if raw.isdecimal() and (_looks_like_date(raw) or _looks_like_timestamp(raw)):
                    continue
                found.add(_number_key(raw))
    for match in re.finditer(r"学号[:：]?([A-Za-z0-9]+)", text):
        found.add(_number_key(match.group(1)))
    return found


def resolve_collection_identity(
    roster: list[dict], *, ocr_name: str = "", ocr_student_no: str = "",
    filename: str = "", source_relative_path: str = "",
    roster_candidate_names: list[str] | None = None, roster_match_status: str = "",
) -> dict:
    """Return the existing teacher-confirmation identity contract, without I/O."""
    ocr_name, ocr_student_no = str(ocr_name or "").strip(), str(ocr_student_no or "").strip()
    roster_names = {_text(student["name"]) for student in roster if _text(student["name"])}
    path = normalize_source_path(source_relative_path) if source_relative_path else ""
    parts = (path or filename.replace("\\", "/")).split("/")
    basename = re.sub(r"\.ocr-ready-v1\.jpg$", "", parts[-1], flags=re.I)
    basename = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", basename)
    sources = [{"source": "filename", "text": _text(basename)}]
    if path:
        # The first component is the selected ZIP/main folder, not a student
        # or class declaration. Only its descendant folders supply identities;
        # an archive below it (one ZIP/RAR per student) names its contents like
        # a folder does.
        sources.extend({"source": "folder", "text": _text(_ARCHIVE_SUFFIX_RE.sub("", part))} for part in reversed(parts[1:-1]))
    for source in sources:
        source["names"] = _component_names(source["text"], roster_names)
    if _text(ocr_name) in roster_names:
        sources.insert(0, {"source": "ocr", "text": ocr_name, "names": {_text(ocr_name)}})
    named_sources = [source for source in sources if source["names"]]
    names = set().union(*(source["names"] for source in named_sources)) if named_sources else set()
    name_students = [student for student in roster if _text(student["name"]) in names]
    clue_source = "+".join(dict.fromkeys(source["source"] for source in named_sources)) or "ocr"
    result = {
        "status": "needs_confirmation", "student_id": None,
        "reason": "missing_identity", "ocr_name": ocr_name,
        "ocr_student_no": ocr_student_no, "candidates": [],
        "source_relative_path": path, "clue_source": clue_source,
        "number_used": False,
        "name_sources": [
            {"source": source["source"], "names": sorted(source["names"])}
            for source in named_sources
        ],
    }

    def matched(student: dict, reason: str) -> dict:
        return {**result, "status": "matched", "student_id": student["student_id"],
                "reason": reason, "candidates": [student],
                "resolved_name": student["name"], "resolved_student_no": student.get("student_no") or ""}

    def pending(reason: str, candidates: list[dict]) -> dict:
        output = {**result, "reason": reason, "candidates": candidates}
        # A directory is a confirmation group only when it has consistent name
        # evidence. Root-level mixed-class collections are never one group.
        if len(parts) >= 3 and len(names) == 1 and reason == "name_ambiguous":
            key = "/".join(parts[:-1]) + "\0" + next(iter(names))
            output["confirmation_group"] = hashlib.sha256(key.encode()).hexdigest()[:24]
        return output

    # This early return is intentional: no number extraction, comparison or
    # correction when the collected/OCR name uniquely identifies the student.
    if len(names) == 1 and len(name_students) == 1:
        if roster_match_status != "low_confidence" or any(s["source"] != "ocr" for s in named_sources):
            return matched(name_students[0], "unique_name")

    relevant = name_students or roster
    numbered: list[dict] = []
    numbers: set[str] = set()
    if ocr_student_no:
        raw = re.sub(r"^学号[:：\s]*", "", ocr_student_no)
        numbers.add(_number_key(raw))
    for source in sources:
        if source["source"] == "ocr":
            continue
        possible_names = source["names"]
        if not possible_names and not names:
            # A mistyped name with an exact complete number may still help.
            # Random numeric/image identifiers alone must never identify anyone.
            parsed = parse_filename_identity(source["text"] + ".jpg")
            possible_names = {_text(parsed.name)} if parsed and parsed.name and _text(parsed.name) not in _CONTAINER_NAMES else set()
        if possible_names or "学号" in source["text"]:
            numbers.update(_component_numbers(source["text"], possible_names))
    if numbers:
        result["number_used"] = True
        numbered = [student for student in relevant if student.get("student_no") and _number_key(student["student_no"]) in numbers]
    if len(names) > 1:
        ids = {student["student_id"] for student in numbered}
        return pending("identity_name_conflict", [*numbered, *[student for student in name_students if student["student_id"] not in ids]])
    if roster_match_status == "low_confidence" and not any(s["source"] != "ocr" for s in named_sources):
        return pending("roster_name_low_confidence", relevant)
    if len(numbered) == 1:
        return matched(numbered[0], "unique_student_no")
    if len(numbered) > 1:
        return pending("identity_number_conflict", numbered)
    if name_students:
        return pending("name_ambiguous", name_students)
    suggested = {_text(name) for name in roster_candidate_names or []}
    candidates = sorted(roster, key=lambda student: _text(student["name"]) not in suggested)
    reason = {"ambiguous": "roster_name_ambiguous", "no_match": "roster_name_not_found"}.get(roster_match_status)
    return pending(reason or ("name_not_found" if ocr_name else "missing_identity"), candidates)
