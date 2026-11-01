"""Local-only identity code and OMR extraction from aligned answer sheets."""
from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np
from PIL import Image


def load_rgb_image(image_path: str) -> np.ndarray:
    with Image.open(image_path) as source:
        return np.ascontiguousarray(
            np.asarray(source.convert("RGB"), dtype=np.uint8)
        )


def crop_normalized_rgb(image_rgb: np.ndarray, bbox: list[int]) -> np.ndarray:
    height, width = image_rgb.shape[:2]
    left, top, right, bottom = bbox
    x1, x2 = int(left * width / 1000), max(1, int(np.ceil(right * width / 1000)))
    y1, y2 = int(top * height / 1000), max(1, int(np.ceil(bottom * height / 1000)))
    return np.ascontiguousarray(image_rgb[y1:y2, x1:x2])


def decode_machine_code(crop_rgb: np.ndarray, code_type: str) -> dict:
    gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
    value = ""
    if code_type == "qr":
        value, _points, _straight = cv2.QRCodeDetector().detectAndDecode(gray)
    elif code_type == "barcode":
        detector = cv2.barcode_BarcodeDetector()
        try:
            decoded, values, _types, _points = detector.detectAndDecodeWithType(gray)
            if decoded and values:
                value = str(values[0])
        except (cv2.error, ValueError):
            value = ""
    normalized = re.sub(r"\s+", "", str(value or ""))
    return {
        "value": normalized,
        "confidence": 1.0 if normalized else 0.0,
        "status": "recognized" if normalized else "needs_review",
        "reason": "decoded_locally" if normalized else "code_not_decoded",
    }


def read_exam_no_marks(crop_rgb: np.ndarray, config: dict[str, Any]) -> dict:
    """Read a digits×10 mark grid; each digit column must have one clear winner."""
    digits = int(config.get("digits") or 0)
    if digits < 1 or digits > 32:
        return {"value": "", "confidence": 0.0, "status": "needs_review", "reason": "invalid_grid_config"}
    gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    results: list[str] = []
    margins: list[float] = []
    for column in range(digits):
        values: list[float] = []
        x1, x2 = int(column * width / digits), int((column + 1) * width / digits)
        for number in range(10):
            y1, y2 = int(number * height / 10), int((number + 1) * height / 10)
            cell = gray[y1:y2, x1:x2]
            pad_y, pad_x = max(1, cell.shape[0] // 5), max(1, cell.shape[1] // 5)
            inner = cell[pad_y:-pad_y or None, pad_x:-pad_x or None]
            values.append(float(np.mean(inner < 150)) if inner.size else 0.0)
        order = np.argsort(values)[::-1]
        best, second = values[int(order[0])], values[int(order[1])]
        margin = best - second
        if best < 0.12 or margin < 0.07:
            return {"value": "", "confidence": round(max(0.0, margin), 4), "status": "needs_review", "reason": "ambiguous_exam_no_marks"}
        results.append(str(int(order[0])))
        margins.append(margin)
    return {"value": "".join(results), "confidence": round(min(margins), 4), "status": "recognized", "reason": "unique_marks"}


def read_choice_marks(crop_rgb: np.ndarray, options: list[str]) -> dict:
    """Read horizontal option cells, rejecting blank, weak, and multiple marks."""
    labels = [str(item).upper() for item in options if str(item).upper() in {"A", "B", "C", "D"}]
    if not labels:
        labels = ["A", "B", "C", "D"]
    gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    values: list[float] = []
    for index in range(len(labels)):
        x1, x2 = int(index * width / len(labels)), int((index + 1) * width / len(labels))
        cell = gray[:, x1:x2]
        pad_y, pad_x = max(1, height // 5), max(1, cell.shape[1] // 5)
        inner = cell[pad_y:-pad_y or None, pad_x:-pad_x or None]
        values.append(float(np.mean(inner < 140)) if inner.size else 0.0)
    order = np.argsort(values)[::-1]
    best, second = values[int(order[0])], values[int(order[1])]
    if best < 0.12:
        return {"answer": "", "confidence": 0.0, "status": "needs_review", "reason": "blank_or_weak_mark"}
    if second >= 0.12 or best - second < 0.07:
        return {"answer": "", "confidence": round(best - second, 4), "status": "needs_review", "reason": "multiple_or_ambiguous_marks"}
    return {"answer": labels[int(order[0])], "confidence": round(best - second, 4), "status": "recognized", "reason": "unique_mark"}
