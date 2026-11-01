"""Single-context CV-CUDA microbatch processor for the controlled WSL node."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from PIL import Image

LOWE_RATIO = 0.66
HOMOGRAPHY_THRESHOLD = 7.0
MAX_GPU_MICROBATCH = 6
MAX_TEMPLATE_CACHE_ENTRIES = 8
MAX_DECODE_PIXELS = 25_000_000


def _validated_encoded_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as encoded:
            width, height = encoded.size
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise RuntimeError("image_decode_failed") from exc
    if height < 32 or width < 32 or width * height > MAX_DECODE_PIXELS:
        raise RuntimeError("image_dimensions_invalid")
    return height, width


def _restore_coordinates(points, scale: float):
    if scale == 1.0:
        return points
    restored = points.copy()
    restored[:, 0] /= scale
    restored[:, 1] /= scale
    return restored


def _coverage(source_shape, template_shape, homography) -> float:
    import cv2
    import numpy as np

    source_mask = np.ones(source_shape, dtype=np.uint8)
    height, width = template_shape
    warped = cv2.warpPerspective(
        source_mask,
        homography,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return float(np.count_nonzero(warped)) / float(warped.size)


def _extract_batch(images, *, max_edge: int, max_features: int, contrast: float):
    import cupy as cp
    import cvcuda
    import numpy as np

    if not images:
        return []
    shapes = {tuple(image.shape) for image in images}
    if len(shapes) != 1:
        raise RuntimeError("gpu_batch_shape_mismatch")
    batch, height, width = len(images), images[0].shape[0], images[0].shape[1]
    scale = min(1.0, max_edge / float(max(height, width)))
    out_h = max(1, round(height * scale))
    out_w = max(1, round(width * scale))
    stacked = cp.stack([cp.asarray(image) for image in images])
    tensor = cvcuda.as_tensor(stacked, "NHWC")
    small = cvcuda.resize(tensor, (batch, out_h, out_w, 3), cvcuda.Interp.AREA)
    gray = cvcuda.cvtcolor(small, cvcuda.ColorConversion.RGB2GRAY)
    points, _meta, descriptors, counts = cvcuda.sift(
        gray,
        max_features=max_features,
        contrast_threshold=contrast,
        flags=cvcuda.SIFT.USE_ORIGINAL_INPUT,
    )
    cvcuda.Stream.current.sync()
    point_host = cp.asnumpy(cp.asarray(points.cuda()))
    descriptor_host = cp.asnumpy(cp.asarray(descriptors.cuda())).astype(np.float32)
    count_host = cp.asnumpy(cp.asarray(counts.cuda())).reshape(-1)
    result = []
    for index, raw_count in enumerate(count_host):
        count = int(raw_count)
        coords = point_host[index, :count, :2].copy()
        coords[:, 0] *= width / float(out_w)
        coords[:, 1] *= height / float(out_h)
        result.append((coords, descriptor_host[index, :count]))
    return result


def _homography(source_points, source_desc, target_points, target_desc, source_shape, target_shape):
    import cv2
    import numpy as np

    if source_desc is None or target_desc is None or len(source_desc) < 2 or len(target_desc) < 2:
        raise RuntimeError("sift_descriptors_missing")
    pairs = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False).knnMatch(source_desc, target_desc, k=2)
    good = [
        pair[0]
        for pair in pairs
        if len(pair) == 2 and pair[0].distance < LOWE_RATIO * pair[1].distance
    ]
    if len(good) < 25:
        raise RuntimeError("too_few_matches")
    source = np.float32([source_points[item.queryIdx] for item in good]).reshape(-1, 1, 2)
    target = np.float32([target_points[item.trainIdx] for item in good]).reshape(-1, 1, 2)
    method = getattr(cv2, "USAC_ACCURATE", cv2.RANSAC)
    matrix, raw_mask = cv2.findHomography(
        source, target, method, HOMOGRAPHY_THRESHOLD
    )
    if matrix is None or raw_mask is None or not np.isfinite(matrix).all():
        raise RuntimeError("homography_failed")
    if abs(float(matrix[2, 2])) < 1e-12:
        raise RuntimeError("homography_singular")
    matrix = matrix / matrix[2, 2]
    mask = raw_mask.ravel().astype(bool)
    inliers = int(mask.sum())
    if inliers < 25:
        raise RuntimeError("too_few_inliers")
    projected = cv2.perspectiveTransform(source.reshape(-1, 2)[mask].reshape(-1, 1, 2), matrix).reshape(-1, 2)
    expected = target.reshape(-1, 2)[mask]
    rmse = float(np.sqrt(np.mean(np.sum((projected - expected) ** 2, axis=1))))
    coverage = _coverage(source_shape, target_shape, matrix)
    return {
        "homography": matrix.tolist(),
        "good_matches": len(good),
        "inliers": inliers,
        "inlier_ratio": inliers / len(good),
        "reprojection_rmse": rmse,
        "coverage": coverage,
    }


class CVCudaBatchProcessor:
    """One persistent GPU context with a bounded microbatch contract."""

    backend = "cvcuda_single_context_v2"

    def __init__(self, *, batch_size: int = 2, max_edge: int = 1280, max_features: int = 3000, contrast: float = 0.03):
        if not 1 <= batch_size <= MAX_GPU_MICROBATCH:
            raise ValueError("GPU microbatch size must be between 1 and 6")
        self.batch_size = batch_size
        self.max_edge = max_edge
        self.max_features = max_features
        self.contrast = contrast
        self._template_cache: OrderedDict[
            str, tuple[tuple[int, int], Any, Any]
        ] = OrderedDict()
        self._decoder = None
        self._lock = threading.Lock()

    def _decode(self, path: Path):
        import numpy as np

        expected_shape = _validated_encoded_dimensions(path)
        from nvidia import nvimgcodec

        if self._decoder is None:
            self._decoder = nvimgcodec.Decoder()
        decoded = self._decoder.read(str(path))
        image = np.asarray(decoded.cpu())
        if image.ndim != 3 or image.shape[2] != 3:
            raise RuntimeError("image_decode_failed")
        if image.shape[:2] != expected_shape:
            raise RuntimeError("image_dimensions_invalid")
        return image

    def _prepare_image(self, path: Path):
        import cv2
        import numpy as np

        image = self._decode(path)
        height, width = image.shape[:2]
        scale = min(1.0, self.max_edge / float(max(height, width)))
        if scale == 1.0:
            return np.ascontiguousarray(image), (height, width), scale
        working = cv2.resize(
            image,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        return np.ascontiguousarray(working), (height, width), scale

    def _template_features(self, template: Path, expected_sha: str):
        cached = self._template_cache.get(expected_sha)
        if cached is not None:
            self._template_cache.move_to_end(expected_sha)
            return cached
        image, original_shape, scale = self._prepare_image(template)
        features = _extract_batch(
            [image],
            max_edge=self.max_edge,
            max_features=self.max_features,
            contrast=self.contrast,
        )[0]
        cached = (
            original_shape,
            _restore_coordinates(features[0], scale),
            features[1],
        )
        self._template_cache[expected_sha] = cached
        self._template_cache.move_to_end(expected_sha)
        while len(self._template_cache) > MAX_TEMPLATE_CACHE_ENTRIES:
            self._template_cache.popitem(last=False)
        return cached

    def process_batch(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items or len(items) > self.batch_size:
            raise ValueError(
                f"GPU batch must contain 1..{self.batch_size} items"
            )
        with self._lock:
            return self._process_batch_locked(items)

    def _process_batch_locked(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import cv2

        cv2.setNumThreads(1)
        prepared = [
            self._prepare_image(Path(item["source_path"]))
            for item in items
        ]
        grouped: dict[tuple[int, int, int], list[int]] = {}
        for index, (image, _original_shape, _scale) in enumerate(prepared):
            grouped.setdefault(tuple(image.shape), []).append(index)
        source_features: list[Any] = [None] * len(items)
        for indexes in grouped.values():
            features = _extract_batch(
                [prepared[index][0] for index in indexes],
                max_edge=self.max_edge,
                max_features=self.max_features,
                contrast=self.contrast,
            )
            for index, value in zip(indexes, features):
                source_features[index] = (
                    _restore_coordinates(value[0], prepared[index][2]),
                    value[1],
                )
        results = []
        for item, prepared_image, (source_points, source_desc) in zip(
            items,
            prepared,
            source_features,
        ):
            _image, source_shape, _scale = prepared_image
            template_shape, target_points, target_desc = self._template_features(
                Path(item["template_path"]),
                item["template_sha256"],
            )
            metrics = _homography(
                source_points,
                source_desc,
                target_points,
                target_desc,
                source_shape,
                template_shape,
            )
            page_type_id = str(item["layout_snapshot"]["page_type_id"])
            results.append({
                "status": "succeeded",
                "page_type_id": page_type_id,
                **metrics,
                "worker_metrics": {
                    "backend": self.backend,
                    "batch_size": len(items),
                    "max_batch_size": self.batch_size,
                    "max_edge": self.max_edge,
                    "max_features": self.max_features,
                    "contrast_threshold": self.contrast,
                    "lowe_ratio": LOWE_RATIO,
                    "homography_method": (
                        "usac_accurate"
                        if hasattr(cv2, "USAC_ACCURATE")
                        else "ransac"
                    ),
                    "homography_threshold": HOMOGRAPHY_THRESHOLD,
                },
            })
        return results
