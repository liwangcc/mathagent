from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from .core import (
    EvidenceResult,
    QuestionRegion,
    RegionSet,
    render_region_sheets,
    summarize_regions,
    validate_sample_id,
)


UNREADABLE_ANSWER = "[无法识别]"


class RecognitionContractError(ValueError):
    """Raised when OCR output cannot be tied to the requested question rows."""


@dataclass(frozen=True)
class Transcription:
    question_number: str
    student_answer: str


@dataclass(frozen=True)
class VisionPolicy:
    enable_thinking: bool = False
    temperature: float = 0.0
    strict_json: bool = True


TRANSCRIPTION_VISION_POLICY = VisionPolicy()
VisionCall = Callable[[Path, str, VisionPolicy], Awaitable[str]]


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecognitionContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _question_numbers(values: Sequence[str]) -> list[str]:
    if not values:
        raise ValueError("at least one question number is required")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("question numbers must be strings")
        number = value.strip()
        if not number or len(number) > 64 or any(ord(char) < 32 for char in number):
            raise ValueError("question number is invalid")
        result.append(number)
    if len(set(result)) != len(result):
        raise ValueError("question numbers must be unique")
    return result


def build_transcription_prompt(question_numbers: Sequence[str]) -> str:
    numbers = _question_numbers(question_numbers)
    numbers_json = json.dumps(numbers, ensure_ascii=False)
    response_skeleton = json.dumps(
        {
            "questions": [
                {
                    "question_number": number,
                    "student_answer": "",
                }
                for number in numbers
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""我是老师，要亲自批阅学生写下的原始答案。

图片是按题号分行的学生笔迹证据表：左侧 Q 编号是固定题号，右侧是该题答题区实际保留下来的笔迹。告诉我学生对应题号的手写答案是什么。

题号顺序：{numbers_json}

只抄右侧实际可见的学生笔迹。不要解题，不要推断正确答案，不要补全、纠正、化简或改写。空白写空字符串 ""；确有笔迹但无法可靠辨认写 "{UNREADABLE_ANSWER}"。每个题号恰好输出一次，顺序必须完全一致。

只返回 JSON，不要 Markdown 或其他文字：
{response_skeleton}"""


def parse_transcription_response(
    raw_response: str,
    question_numbers: Sequence[str],
) -> list[Transcription]:
    expected = _question_numbers(question_numbers)
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise RecognitionContractError("OCR response is empty")
    try:
        payload = json.loads(
            raw_response,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except json.JSONDecodeError as exc:
        raise RecognitionContractError("OCR response must be strict JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"questions"}:
        raise RecognitionContractError("OCR response must contain only questions")
    questions = payload["questions"]
    if not isinstance(questions, list) or len(questions) != len(expected):
        raise RecognitionContractError("OCR response question count does not match")

    result: list[Transcription] = []
    actual: list[str] = []
    for item in questions:
        if not isinstance(item, dict) or set(item) != {
            "question_number",
            "student_answer",
        }:
            raise RecognitionContractError("each OCR question has an invalid schema")
        number = item["question_number"]
        answer = item["student_answer"]
        if not isinstance(number, str) or not isinstance(answer, str):
            raise RecognitionContractError("OCR question fields must be strings")
        if "\x00" in answer or len(answer) > 4000:
            raise RecognitionContractError("student_answer is invalid")
        actual.append(number)
        result.append(Transcription(number, answer))
    if actual != expected:
        raise RecognitionContractError(
            "OCR question numbers are missing, duplicated, or out of order"
        )
    return result


async def recognize_region_sheets(
    sheet_paths: Sequence[str | Path],
    question_groups: Sequence[Sequence[str]],
    vision_call: VisionCall,
    *,
    concurrency: int = 2,
) -> list[list[Transcription]]:
    if len(sheet_paths) != len(question_groups) or not sheet_paths:
        raise ValueError("sheet paths and question groups must have equal non-zero length")
    if concurrency < 1 or concurrency > 16:
        raise ValueError("concurrency must be between 1 and 16")
    paths = [Path(path) for path in sheet_paths]
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    groups = [_question_numbers(group) for group in question_groups]
    semaphore = asyncio.Semaphore(concurrency)

    async def recognize_one(path: Path, numbers: list[str]):
        prompt = build_transcription_prompt(numbers)
        async with semaphore:
            raw = await vision_call(path, prompt, TRANSCRIPTION_VISION_POLICY)
        return parse_transcription_response(raw, numbers)

    return await asyncio.gather(
        *(recognize_one(path, numbers) for path, numbers in zip(paths, groups))
    )


async def recognize_evidence(
    evidence: EvidenceResult,
    regions: RegionSet,
    sheets_output: str | Path,
    vision_call: VisionCall,
    *,
    group_size: int = 10,
    concurrency: int = 2,
    boundary_ratio: float = 0.20,
    boundary_pixels: int = 8,
) -> dict:
    """Run the fixed-region transcription flow without caller-supplied safety."""
    preservation_safe = evidence.stats.get("answer_preservation_safe")
    if not isinstance(preservation_safe, bool):
        raise RecognitionContractError(
            "evidence is missing answer_preservation_safe"
        )
    try:
        sample_id = validate_sample_id(evidence.stats.get("sample_id"))
    except ValueError as exc:
        raise RecognitionContractError(str(exc)) from exc
    artifact_hash = evidence.stats.get("artifact_registration_sha256")
    try:
        regions.require_artifact(artifact_hash)
    except ValueError as exc:
        raise RecognitionContractError(str(exc)) from exc
    region_evidence = summarize_regions(evidence, regions)
    numbers = [
        str(item["question_number"])
        for item in region_evidence
    ]
    sheets_root = Path(sheets_output)
    sheets_root.mkdir(parents=True, exist_ok=True)
    private_output = Path(tempfile.mkdtemp(prefix="run-", dir=sheets_root))
    sheets = render_region_sheets(
        evidence,
        regions,
        private_output,
        group_size=group_size,
    )
    question_groups = [
        numbers[index : index + group_size]
        for index in range(0, len(numbers), group_size)
    ]
    grouped = await recognize_region_sheets(
        sheets,
        question_groups,
        vision_call,
        concurrency=concurrency,
    )
    transcriptions = [
        transcription
        for group in grouped
        for transcription in group
    ]
    result = reconcile_transcriptions(
        transcriptions,
        region_evidence,
        answer_preservation_safe=preservation_safe,
        boundary_ratio=boundary_ratio,
        boundary_pixels=boundary_pixels,
    )
    result["sheet_count"] = len(sheets)
    result["sample_id"] = sample_id
    result["run_id"] = private_output.name
    result["source_mode"] = evidence.stats.get("source_mode")
    result["answer_preservation_safe"] = preservation_safe
    result["artifact_registration_sha256"] = artifact_hash
    result["region_contract_sha256"] = regions.contract_sha256
    return result


def reconcile_transcriptions(
    transcriptions: Sequence[Transcription],
    region_evidence: Sequence[dict],
    *,
    answer_preservation_safe: bool,
    boundary_ratio: float = 0.20,
    boundary_pixels: int = 8,
) -> dict:
    if not 0 <= boundary_ratio <= 1 or boundary_pixels < 1:
        raise ValueError("boundary review thresholds are invalid")
    if not transcriptions:
        raise RecognitionContractError(
            "at least one transcription is required"
        )
    expected = _question_numbers(
        [item.question_number for item in transcriptions]
    )
    actual = [str(item.get("question_number", "")) for item in region_evidence]
    if actual != expected:
        raise RecognitionContractError(
            "local evidence and OCR question numbers are not in the same order"
        )

    questions: list[dict] = []
    for transcription, evidence in zip(transcriptions, region_evidence):
        answer = transcription.student_answer
        local_state = evidence.get("state")
        if local_state not in {"blank", "uncertain", "visible"}:
            raise RecognitionContractError("local evidence state is invalid")
        reasons: list[str] = []
        if answer_preservation_safe is not True:
            reasons.append("unsafe_artifact_source")
        if local_state == "blank" and answer != "":
            reasons.append("ocr_on_program_blank")
        if local_state == "visible" and answer == "":
            reasons.append("program_ink_ocr_blank")
        if local_state == "uncertain":
            reasons.append("uncertain_local_ink")
        if answer == UNREADABLE_ANSWER:
            reasons.append("ocr_unreadable")

        if (
            "edge_ink_pixels" not in evidence
            or "edge_ink_ratio" not in evidence
        ):
            raise RecognitionContractError(
                "local boundary evidence is missing"
            )
        edge_pixels = evidence["edge_ink_pixels"]
        edge_ratio = evidence["edge_ink_ratio"]
        if (
            isinstance(edge_pixels, bool)
            or not isinstance(edge_pixels, int)
            or edge_pixels < 0
            or isinstance(edge_ratio, bool)
            or not isinstance(edge_ratio, (int, float))
            or not 0 <= float(edge_ratio) <= 1
        ):
            raise RecognitionContractError("local boundary evidence is invalid")
        if edge_pixels >= boundary_pixels or (
            edge_pixels > 0 and edge_ratio >= boundary_ratio
        ):
            reasons.append("ink_touches_region_boundary")

        question_type = evidence.get("question_type")
        if question_type not in {"choice", "fill", "solve"}:
            raise RecognitionContractError("local question type is invalid")
        if (
            question_type == "choice"
            and answer not in {"", UNREADABLE_ANSWER, "A", "B", "C", "D"}
        ):
            reasons.append("noncanonical_choice_answer")

        questions.append({
            "question_number": transcription.question_number,
            "student_answer": answer,
            "local_state": local_state,
            "decision": "review" if reasons else "accept",
            "review_reasons": reasons,
        })

    review_count = sum(item["decision"] == "review" for item in questions)
    return {
        "questions": questions,
        "question_count": len(questions),
        "review_count": review_count,
        "review_rate": round(review_count / len(questions), 4) if questions else 0.0,
    }
