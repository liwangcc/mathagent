"""Versioned, stage-isolated mathematics catalogs with no database/model access.

Public callers receive copies. Legacy chapter names are deliberately not migrated
or inferred here: the task owning the record must choose its teaching context.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re


_CATALOG_ROOT = Path(__file__).resolve().parents[1] / "references" / "math_curricula"
_CATALOG_FILES = {"primary": "primary.v1.json", "junior": "junior.v1.json", "senior": "senior.v1.json"}
# Append-only additions that keep the base version (see catalog_version).
_SUPPLEMENT_FILES = {stage: f"{stage}.supplement.json" for stage in _CATALOG_FILES}
# textbook: a unit of a currently used textbook; legacy_textbook: content of
# earlier textbooks that older papers still test; exam_practice: a commonly
# examined topic that the base catalog only covers implicitly.
_SUPPLEMENT_BASES = {"textbook", "legacy_textbook", "exam_practice"}
# Supplement points that current classes teach join default practice; legacy
# textbook content and the standard's optional/elective topics stay opt-in.
_PRACTICE_DEFAULT_BASES = {"textbook", "exam_practice"}
_COURSES = {"compulsory", "selective_required", "optional", "elective"}


def _validate_stage(stage: str) -> str:
    # Strict by design: no omitted-stage fallback, aliases, coercion, or paths.
    if not isinstance(stage, str) or stage not in _CATALOG_FILES:
        raise ValueError("education_stage must be primary, junior or senior")
    return stage


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _validate_catalog(payload: dict, stage: str, sources: dict) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported math catalog schema")
    if payload.get("stage") != stage or payload.get("subject") != "math":
        raise ValueError("math catalog stage/subject mismatch")
    if not _nonempty_string(payload.get("version")):
        raise ValueError("math catalog version is missing")
    if not isinstance(sources, dict) or sources.get("schema_version") != 1:
        raise ValueError("unsupported math catalog sources schema")
    source_ids = set()
    verified_source_ids = set()
    for source in sources.get("sources", []):
        if not isinstance(source, dict) or not _nonempty_string(source.get("id")):
            raise ValueError("math catalog source identity is invalid")
        if source["id"] in source_ids:
            raise ValueError("duplicate math catalog source")
        if not all(_nonempty_string(source.get(k)) for k in ("title", "publisher", "url")):
            raise ValueError("math catalog source attribution is missing")
        if not source["url"].startswith("https://") or not source.get("checked_sections"):
            raise ValueError("math catalog source provenance is incomplete")
        source_ids.add(source["id"])
        if source.get("verification") == "content_verified":
            verified_source_ids.add(source["id"])
    modules = payload.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ValueError("math catalog modules are missing")
    ids: set[str] = set()
    chapters: set[str] = set()
    prefix = f"{stage}.math."
    for module in modules:
        if not isinstance(module, dict):
            raise ValueError("math catalog module must be an object")
        module_id = module.get("id")
        chapter = module.get("chapter")
        if (not _nonempty_string(module_id) or not module_id.startswith(prefix)
                or not re.fullmatch(r"[a-z0-9_.]+", module_id) or module_id in ids):
            raise ValueError("math catalog module ID is invalid or duplicated")
        if not _nonempty_string(chapter) or chapter in chapters:
            raise ValueError("math catalog chapter is invalid or duplicated")
        if not _nonempty_string(module.get("domain")):
            raise ValueError("math catalog domain is missing")
        ids.add(module_id)
        chapters.add(chapter)
        refs = module.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("math catalog module has no source references")
        for ref in refs:
            if (not isinstance(ref, dict) or ref.get("source_id") not in verified_source_ids
                    or not _nonempty_string(ref.get("locator"))):
                raise ValueError("math catalog source reference is not verified")
        topics = module.get("topics")
        if not isinstance(topics, list) or not topics:
            raise ValueError("math catalog topics are missing")
        names: set[str] = set()
        for point in topics:
            if not isinstance(point, dict):
                raise ValueError("math catalog point must be an object")
            point_id, name = point.get("id"), point.get("name")
            if (not _nonempty_string(point_id) or not point_id.startswith(module_id + ".")
                    or not re.fullmatch(r"[a-z0-9_.]+", point_id) or point_id in ids):
                raise ValueError("math catalog point ID is invalid or duplicated")
            if not _nonempty_string(name) or name in names:
                raise ValueError("math catalog topic name is invalid or duplicated")
            if point.get("scope") not in {"core", "extension"} or point.get("course") not in _COURSES:
                raise ValueError("math catalog point scope/course is invalid")
            if point["course"] in {"optional", "elective"} and point["scope"] != "extension":
                raise ValueError("optional/elective topics cannot be core")
            aliases = point.get("aliases", [])
            if not isinstance(aliases, list) or any(not _nonempty_string(x) for x in aliases):
                raise ValueError("math catalog aliases are invalid")
            ids.add(point_id)
            names.add(name)


def _validate_supplement(supplement: dict, payload: dict, stage: str) -> None:
    """Append-only additions: new names and IDs only, never a base edit."""
    if not isinstance(supplement, dict) or supplement.get("schema_version") != 1:
        raise ValueError("unsupported math catalog supplement schema")
    if supplement.get("stage") != stage or supplement.get("subject") != "math":
        raise ValueError("math catalog supplement stage/subject mismatch")
    if supplement.get("base_version") != payload["version"]:
        raise ValueError("math catalog supplement targets another base catalog")
    if not all(_nonempty_string(supplement.get(key)) for key in ("version", "review_status", "coverage_note")):
        raise ValueError("math catalog supplement metadata is missing")
    ids = {module["id"] for module in payload["modules"]}
    ids.update(point["id"] for module in payload["modules"] for point in module["topics"])
    chapters = {module["chapter"] for module in payload["modules"]}
    names = {module["id"]: {point["name"] for point in module["topics"]} for module in payload["modules"]}
    prefix = f"{stage}.math."
    modules = supplement.get("modules", [])
    if not isinstance(modules, list) or any(not isinstance(module, dict) for module in modules):
        raise ValueError("math catalog supplement modules are invalid")
    for module in modules:
        module_id, chapter = module.get("id"), module.get("chapter")
        if (not _nonempty_string(module_id) or not module_id.startswith(prefix)
                or not re.fullmatch(r"[a-z0-9_.]+", module_id) or module_id in ids):
            raise ValueError("math catalog supplement module ID is invalid or duplicated")
        if not _nonempty_string(chapter) or chapter in chapters or not _nonempty_string(module.get("domain")):
            raise ValueError("math catalog supplement chapter is invalid or duplicated")
        ids.add(module_id)
        chapters.add(chapter)
        names[module_id] = set()
    topics = supplement.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError("math catalog supplement topics are missing")
    for point in topics:
        if not isinstance(point, dict) or point.get("module_id") not in names:
            raise ValueError("math catalog supplement topic has no module")
        point_id, name = point.get("id"), point.get("name")
        if (not _nonempty_string(point_id) or not point_id.startswith(point["module_id"] + ".")
                or not re.fullmatch(r"[a-z0-9_.]+", point_id) or point_id in ids):
            raise ValueError("math catalog supplement point ID is invalid or duplicated")
        if not _nonempty_string(name) or name in names[point["module_id"]]:
            raise ValueError("math catalog supplement topic name is invalid or duplicated")
        # A supplement never claims the verified core of the curriculum standard.
        if point.get("scope") != "extension" or point.get("course") not in _COURSES:
            raise ValueError("math catalog supplement point scope/course is invalid")
        if point.get("basis") not in _SUPPLEMENT_BASES or not _nonempty_string(point.get("note")):
            raise ValueError("math catalog supplement point basis is missing")
        aliases = point.get("aliases", [])
        if not isinstance(aliases, list) or any(not _nonempty_string(x) for x in aliases):
            raise ValueError("math catalog supplement aliases are invalid")
        ids.add(point_id)
        names[point["module_id"]].add(name)
    for module_id, topic_names in names.items():
        if not topic_names:
            raise ValueError("math catalog supplement module has no topics")
    alias_additions = supplement.get("aliases", {})
    if not isinstance(alias_additions, dict):
        raise ValueError("math catalog supplement aliases are invalid")
    for point_id, aliases in alias_additions.items():
        if point_id not in ids or not isinstance(aliases, list) or not aliases:
            raise ValueError("math catalog supplement alias target is invalid")
        if any(not _nonempty_string(x) for x in aliases):
            raise ValueError("math catalog supplement aliases are invalid")


def _merge_supplement(payload: dict, supplement: dict) -> dict:
    merged = deepcopy(payload)
    for module in supplement.get("modules", []):
        merged["modules"].append({
            "id": module["id"], "chapter": module["chapter"], "domain": module["domain"],
            "source_refs": [], "topics": [], "origin": "supplement",
        })
    by_id = {module["id"]: module for module in merged["modules"]}
    for point in supplement["topics"]:
        by_id[point["module_id"]]["topics"].append({
            **{key: deepcopy(value) for key, value in point.items() if key != "module_id"},
            "origin": "supplement",
        })
    points = {point["id"]: point for module in merged["modules"] for point in module["topics"]}
    for point_id, aliases in supplement.get("aliases", {}).items():
        point = points[point_id]
        point["aliases"] = list(point.get("aliases", [])) + [x for x in aliases if x not in point.get("aliases", [])]
    return merged


@lru_cache(maxsize=3)
def _load_catalog(stage: str) -> tuple[dict, str]:
    stage = _validate_stage(stage)
    payload = json.loads((_CATALOG_ROOT / _CATALOG_FILES[stage]).read_text(encoding="utf-8"))
    sources = json.loads((_CATALOG_ROOT / "sources.json").read_text(encoding="utf-8"))
    _validate_catalog(payload, stage, sources)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    version = f"math:{stage}:{payload['version']}:{digest}"
    supplement_path = _CATALOG_ROOT / _SUPPLEMENT_FILES[stage]
    if supplement_path.exists():
        supplement = json.loads(supplement_path.read_text(encoding="utf-8"))
        _validate_supplement(supplement, payload, stage)
        payload = _merge_supplement(payload, supplement)
    return payload, version


def catalog_version(stage: str) -> str:
    """Version of the frozen base catalog, suitable for a task/cache snapshot.

    Every stored teaching context, bank row and archived question carries this
    string, so it binds the base file only. An append-only supplement adds
    names but cannot change or remove a base point, so earlier snapshots stay
    valid without a data migration.
    """
    return _load_catalog(_validate_stage(stage))[1]


def get_math_catalog(stage: str) -> list[dict]:
    """Return the existing chapter/topics contract plus stable IDs and scope.

`core` for senior includes compulsory and selective-required courses. Consumers
must inspect `topic_courses` when distinguishing graduation and elective scope.
`extension` is searchable/classifiable, not a default claim of required learning.
"""
    payload, _ = _load_catalog(_validate_stage(stage))
    return [{
        "id": module["id"],
        "chapter": module["chapter"],
        "domain": module["domain"],
        "topics": [point["name"] for point in module["topics"]],
        "topic_ids": {point["name"]: point["id"] for point in module["topics"]},
        "topic_scopes": {point["name"]: point["scope"] for point in module["topics"]},
        "topic_courses": {point["name"]: point["course"] for point in module["topics"]},
        "source_refs": deepcopy(module["source_refs"]),
    } for module in payload["modules"]]


def get_knowledge_point(stage: str, chapter: str, topic: str) -> dict | None:
    """Resolve an exact in-stage chapter/topic pair; never guess from aliases."""
    payload, version = _load_catalog(_validate_stage(stage))
    if not isinstance(chapter, str) or not isinstance(topic, str):
        return None
    for module in payload["modules"]:
        if module["chapter"] != chapter:
            continue
        for point in module["topics"]:
            if point["name"] == topic:
                return {
                    "id": point["id"],
                    "module_id": module["id"],
                    "stage": stage,
                    "catalog_version": version,
                    "chapter": chapter,
                    "topic": topic,
                    "scope": point["scope"],
                    "course": point["course"],
                    "aliases": list(point.get("aliases", [])),
                    "note": point.get("note", ""),
                    # A supplement point is not covered by the module's
                    # verified curriculum pages; report its own basis instead.
                    "origin": point.get("origin", "catalog"),
                    "basis": point.get("basis", ""),
                    "source_refs": [] if point.get("origin") == "supplement" else deepcopy(module["source_refs"]),
                }
    return None


def practice_eligible(point: dict | None, *, include_extension: bool = False) -> bool:
    """Whether automatic practice may draw questions labelled with this point."""
    if point is None:
        return False
    if include_extension or point["scope"] == "core":
        return True
    return point.get("origin") == "supplement" and point.get("basis") in _PRACTICE_DEFAULT_BASES


def _label_index(stage: str) -> tuple[dict, dict]:
    payload, _ = _load_catalog(stage)
    by_name: dict[str, set] = {}
    by_alias: dict[str, set] = {}
    for module in payload["modules"]:
        for point in module["topics"]:
            pair = (module["chapter"], point["name"])
            by_name.setdefault(point["name"], set()).add(pair)
            for alias in point.get("aliases", []):
                by_alias.setdefault(alias, set()).add(pair)
    return by_name, by_alias


def resolve_classifier_label(stage: str, chapter: str, topic: str) -> dict | None:
    """Map one classifier answer onto a catalog point using declared names only.

    The exact pair wins. Otherwise a declared alias inside the named chapter,
    then a topic name or alias that identifies exactly one point in the stage
    (the model kept the topic but misnamed its chapter). Anything ambiguous
    stays unresolved; persisted data must keep using get_knowledge_point.
    """
    stage = _validate_stage(stage)
    point = get_knowledge_point(stage, chapter, topic)
    if point is not None or not isinstance(topic, str) or not topic:
        return point
    by_name, by_alias = _label_index(stage)
    aliased = by_alias.get(topic, set())
    in_chapter = {pair for pair in aliased if pair[0] == chapter}
    for candidates in (in_chapter, by_name.get(topic, set()), aliased):
        if len(candidates) == 1:
            return get_knowledge_point(stage, *next(iter(candidates)))
        if candidates:
            return None
    return None
