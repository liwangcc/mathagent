"""Image preprocessing for better OCR: rotate, deskew, contrast, denoise."""

from __future__ import annotations

import importlib
import logging
import os
import uuid

logger = logging.getLogger(__name__)

_EXIF_ORIENTATION_TAG = 274
_DISPLAY_FORMATS = {"JPEG", "PNG", "WEBP"}
_PREPROCESS_MODES = {"off", "fast", "enhanced"}
_DEFAULT_MAX_EDGE = 1920
_MAX_SOURCE_PIXELS = 16_000_000
OCR_READY_IMAGE_SUFFIX = ".ocr-ready-v1.jpg"


class _LazyModule:
    """Load legacy pixel dependencies only when a pixel operation is requested."""

    def __init__(self, module_name: str):
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_module", None)

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_module_name"))
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)


Image = _LazyModule("PIL.Image")
ImageEnhance = _LazyModule("PIL.ImageEnhance")
ImageFilter = _LazyModule("PIL.ImageFilter")
ImageOps = _LazyModule("PIL.ImageOps")
np = _LazyModule("numpy")


def _validate_source_dimensions(image: Image.Image) -> tuple[int, int]:
    """Reject pathological dimensions before a decoder allocates pixels."""
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > _MAX_SOURCE_PIXELS:
        raise ValueError("image dimensions are invalid")
    return width, height


def ocr_ready_storage_suffix(filename: str | os.PathLike[str]) -> str:
    """Preserve the browser preprocessing contract in server-owned filenames."""
    name = os.fspath(filename or "")
    if name.lower().endswith(OCR_READY_IMAGE_SUFFIX):
        return OCR_READY_IMAGE_SUFFIX
    return os.path.splitext(name)[1].lower()


def is_ocr_ready_image(
    image_path: str | os.PathLike[str],
    *,
    max_edge: int,
) -> bool:
    """Check the bounded JPEG header for the browser ``ocr-ready-v1`` contract.

    Upload validation already authenticated the encoded image. This check reads
    metadata only; it deliberately does not decode a full pixel buffer.
    """
    path = os.fspath(image_path)
    if not path.lower().endswith(OCR_READY_IMAGE_SUFFIX):
        return False
    try:
        edge_limit = int(max_edge)
    except (TypeError, ValueError):
        return False
    if edge_limit <= 0:
        return False
    try:
        size = os.path.getsize(path)
        if size <= 4:
            return False
        with open(path, "rb") as stream:
            prefix = stream.read(64 * 1024)
            stream.seek(max(0, size - 2))
            suffix = stream.read(2)
        metadata = _jpeg_metadata(prefix)
        if metadata is None or suffix != b"\xff\xd9":
            return False
        width, height, channels, orientation = metadata
        return bool(
            width > 0
            and height > 0
            and width * height <= _MAX_SOURCE_PIXELS
            and max(width, height) <= edge_limit
            and channels in {1, 3}
            and orientation == 1
        )
    except (OSError, TypeError, ValueError):
        return False


def _tiff_orientation(data: bytes) -> int:
    if len(data) < 8 or data[:2] not in {b"II", b"MM"}:
        return 1
    byte_order = "little" if data[:2] == b"II" else "big"
    if int.from_bytes(data[2:4], byte_order) != 42:
        return 1
    directory_offset = int.from_bytes(data[4:8], byte_order)
    if directory_offset < 8 or directory_offset + 2 > len(data):
        return 1
    count = int.from_bytes(data[directory_offset:directory_offset + 2], byte_order)
    for index in range(min(count, 512)):
        entry = directory_offset + 2 + index * 12
        if entry + 12 > len(data):
            break
        tag = int.from_bytes(data[entry:entry + 2], byte_order)
        if tag != _EXIF_ORIENTATION_TAG:
            continue
        kind = int.from_bytes(data[entry + 2:entry + 4], byte_order)
        values = int.from_bytes(data[entry + 4:entry + 8], byte_order)
        if kind != 3 or values != 1:
            return 1
        orientation = int.from_bytes(data[entry + 8:entry + 10], byte_order)
        return orientation if 1 <= orientation <= 8 else 1
    return 1


def _jpeg_metadata(data: bytes) -> tuple[int, int, int, int] | None:
    """Read bounded JPEG SOF/EXIF metadata without invoking a pixel decoder."""

    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3,
        0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB,
        0xCD, 0xCE, 0xCF,
    }
    orientation = 1
    offset = 2
    while offset + 1 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]
        offset += 1
        if marker in {0x00, 0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[offset:offset + 2], "big")
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(data):
            return None
        if marker == 0xE1 and data[offset + 2:offset + 8] == b"Exif\x00\x00":
            orientation = _tiff_orientation(data[offset + 8:segment_end])
        if marker in start_of_frame:
            if segment_length < 8:
                return None
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            channels = int(data[offset + 7])
            return width, height, channels, orientation
        offset = segment_end
    return None


def preprocess_paper_image(
    image_path: str,
    mode: str = "enhanced",
    max_edge: int = _DEFAULT_MAX_EDGE,
    suppress_review_ink: bool = False,
) -> str:
    """Prepare a paper image for OCR and return the resulting image path.

    ``off`` returns the upload unchanged. ``fast`` normalizes EXIF
    orientation, downsizes oversized images, optionally removes saturated
    red/green review ink, and encodes a compact JPEG.
    ``enhanced`` additionally retains the historical deskew, perspective,
    contrast, and sharpening pipeline.
    """
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in _PREPROCESS_MODES:
        raise ValueError(
            f"Unsupported OCR preprocess mode {mode!r}; "
            f"expected one of {sorted(_PREPROCESS_MODES)}"
        )
    if normalized_mode == "off":
        return image_path

    try:
        max_edge = int(max_edge)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_edge must be a positive integer") from exc
    if max_edge <= 0:
        raise ValueError("max_edge must be a positive integer")

    output_path = (
        os.path.splitext(image_path)[0]
        + f"_preprocessed-{uuid.uuid4().hex}.jpg"
    )
    temporary_path = f"{output_path}.{uuid.uuid4().hex}.tmp"
    try:
        with Image.open(image_path) as source:
            _validate_source_dimensions(source)
            # JPEG decoders may satisfy this request at a reduced native
            # resolution, avoiding a full-size pixel buffer for phone photos.
            source.draft("RGB", (max_edge, max_edge))
            source.load()

            # Keep EXIF normalization in both processing modes. The helper is
            # intentionally conservative and performs no geometric guessing.
            img = auto_rotate(source)
            img = _resize_to_max_edge(img, max_edge)

            # JPEG output requires a provider-stable pixel mode.
            if img.mode != "RGB":
                img = img.convert("RGB")

            if suppress_review_ink:
                img = suppress_colored_review_ink(img)

            if normalized_mode == "enhanced":
                img = auto_deskew(img)
                img = fix_perspective(img)
                img = ImageEnhance.Contrast(img).enhance(1.3)
                img = img.filter(ImageFilter.SHARPEN)

            # A lossless PNG made 1440x1920 photographed papers roughly 10x
            # larger than their source JPEGs, materially increasing Vision
            # upload latency and timeout probability.  4:4:4 JPEG preserves
            # thin math strokes while keeping the request payload bounded.
            save_kwargs = {
                "format": "JPEG",
                "quality": 88 if normalized_mode == "fast" else 90,
                "subsampling": 1 if normalized_mode == "fast" else 0,
                "optimize": normalized_mode == "enhanced",
            }
            img.save(temporary_path, **save_kwargs)
            os.replace(temporary_path, output_path)
            logger.info(
                "Preprocessed %s -> %s (mode=%s, size=%dx%d)",
                image_path,
                output_path,
                normalized_mode,
                img.width,
                img.height,
            )
        return output_path

    except Exception as e:
        for path in (temporary_path, output_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Failed to clean partial preprocess output %s", path)
        logger.warning("Image preprocessing failed for %s: %s", image_path, e)
        if isinstance(e, (ValueError, Image.DecompressionBombError)):
            raise ValueError("image preprocessing failed") from e
        # Upload validation has already authenticated the original bytes. A
        # transient local encoder/filesystem failure may use that immutable
        # source, while dimension-policy failures above remain fail-closed.
        return image_path


def _resize_to_max_edge(img: Image.Image, max_edge: int) -> Image.Image:
    """Downsize while preserving aspect ratio; never upscale small images."""
    if max(img.size) <= max_edge:
        return img

    resized = img.copy()
    resized.thumbnail(
        (max_edge, max_edge),
        resample=Image.Resampling.LANCZOS,
        reducing_gap=3.0,
    )
    return resized


def suppress_colored_review_ink(img: Image.Image) -> Image.Image:
    """Remove saturated red/green marking strokes from a student-paper copy.

    The original upload remains untouched and is still used for teacher review.
    Blue and neutral black/gray strokes are deliberately preserved.  This is a
    conservative color filter, not an attempt to reconstruct handwriting hidden
    underneath an opaque correction.
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    channels = rgb.astype(np.int16)
    red = channels[:, :, 0]
    green = channels[:, :, 1]
    blue = channels[:, :, 2]
    spread = channels.max(axis=2) - channels.min(axis=2)

    red_ink = (
        (red >= 110)
        & ((red - green) >= 35)
        & ((red - blue) >= 35)
    )
    green_ink = (
        (green >= 90)
        & ((green - red) >= 25)
        & ((green - blue) >= 20)
    )
    review_mask = (red_ink | green_ink) & (spread >= 40)
    if not np.any(review_mask):
        return img

    cleaned = rgb.copy()
    cleaned[review_mask] = (255, 255, 255)
    return Image.fromarray(cleaned)


def get_display_image_path(image_path: str) -> str:
    """Return an EXIF-normalized display copy without changing the upload.

    Camera images often store rotation in EXIF instead of pixel data.  The
    upload itself remains the source of truth; this helper only creates a
    cached, orientation-normalized sibling for the authenticated image view.
    Invalid or unsupported files fall back to the original path.
    """
    if is_ocr_ready_image(image_path, max_edge=_DEFAULT_MAX_EDGE):
        return image_path

    output_path = None
    temp_path = None
    try:
        with Image.open(image_path) as source:
            _validate_source_dimensions(source)
            orientation = int(source.getexif().get(_EXIF_ORIENTATION_TAG, 1) or 1)
            if orientation not in range(2, 9):
                return image_path

            image_format = str(source.format or "").upper()
            if image_format not in _DISPLAY_FORMATS:
                return image_path

            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image_format]
            output_path = os.path.splitext(image_path)[0] + "_oriented" + extension
            source_mtime = os.stat(image_path).st_mtime_ns
            if (
                os.path.isfile(output_path)
                and os.stat(output_path).st_mtime_ns >= source_mtime
            ):
                return output_path

            source.load()
            oriented = ImageOps.exif_transpose(source)
            if image_format == "JPEG" and oriented.mode not in ("RGB", "L"):
                oriented = oriented.convert("RGB")

            temp_path = f"{output_path}.{uuid.uuid4().hex}.tmp"
            save_kwargs = {}
            if image_format == "JPEG":
                save_kwargs = {"quality": 95, "subsampling": 0, "optimize": True}
            elif image_format == "WEBP":
                save_kwargs = {"quality": 95, "method": 6}
            oriented.save(temp_path, format=image_format, **save_kwargs)

        os.replace(temp_path, output_path)
        temp_path = None
        return output_path
    except Exception as exc:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        logger.warning("EXIF display normalization failed for %s: %s", image_path, exc)
        return image_path


def auto_rotate(img: Image.Image) -> Image.Image:
    """检测并修正90/180/270度旋转（改进版 + EXIF支持）。

    优先级：
    1. 先检查 EXIF Orientation 标记，如果存在则应用
    2. EXIF 处理后再做算法检测（避免误判已标记图片）

    原理：检测横向文字行的方向，而非上下密度对比（避免手写解题区域误判）。
    尝试4个方向，选择横向边缘（文字行）最多的方向。
    """
    try:
        # ── Step 1: 应用 EXIF Orientation（如果存在）──
        # 手机/相机拍照时会在 EXIF 元数据中记录"显示时请旋转X度"
        # 必须先应用这个标记，否则算法会误判已标记图片
        try:
            img = ImageOps.exif_transpose(img)
            logger.debug("auto_rotate: applied EXIF orientation")
        except Exception as e:
            logger.debug("auto_rotate: no EXIF orientation or failed to apply: %s", e)
        
        # ── Step 2: 几何算法已禁用（误判率高）──
        # 原算法会把竖直笔画误判为横向文字行，导致错误旋转。
        # 现在只信任 EXIF Orientation（Step 1已处理）。
        # 如果 EXIF 不存在或已应用，直接返回原图，不做进一步旋转。
        logger.debug("auto_rotate: EXIF applied, skipping geometric detection (unreliable)")

        return img
    except Exception as e:
        logger.debug("Auto-rotate failed: %s", e)
        return img


def auto_deskew(img: Image.Image) -> Image.Image:
    """Simple deskew by detecting dominant horizontal lines.
    
    If skew angle is small (<5 degrees), rotate to correct.
    """
    try:
        gray = img.convert("L")
        arr = np.array(gray)

        # 检测水平线：每行的平均亮度
        row_means = arr.mean(axis=1)
        # 找到亮度突变的行（可能是文字行）
        row_diff = np.abs(np.diff(row_means))
        threshold = row_diff.mean() * 2
        bright_rows = np.where(row_diff > threshold)[0]

        if len(bright_rows) < 10:
            return img

        # 简单的倾斜检测：比较左右两侧的亮度分布
        h, w = arr.shape
        left_half = arr[:, :w // 2]
        right_half = arr[:, w // 2:]

        left_rows = np.where(left_half.mean(axis=1) < 128)[0]
        right_rows = np.where(right_half.mean(axis=1) < 128)[0]

        if len(left_rows) > 5 and len(right_rows) > 5:
            # 计算倾斜角度
            left_center = left_rows.mean()
            right_center = right_rows.mean()
            angle = np.arctan2(right_center - left_center, w // 2) * 180 / np.pi

            # 如果倾斜角度小于5度，旋转修正
            if abs(angle) < 5 and abs(angle) > 0.5:
                img = img.rotate(-angle, fillcolor='white', expand=False)
                logger.info("Deskewed by %.1f degrees", angle)

        return img
    except Exception as e:
        logger.debug("Deskew failed: %s", e)
        return img


def fix_perspective(img: Image.Image) -> Image.Image:
    """透视矫正（需要cv2，不可用时跳过）。"""
    try:
        import cv2
    except ImportError:
        return img

    try:
        arr = np.array(img)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

        # 检测边缘
        edges = cv2.Canny(gray, 50, 150)

        # 找轮廓
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return img

        # 找最大的轮廓（应该是试卷纸张）
        largest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(largest)
        total_area = img.width * img.height

        # 如果最大轮廓面积不到图片面积的30%，可能不是试卷
        if area < total_area * 0.3:
            return img

        # 近似四边形
        peri = cv2.arcLength(largest, True)
        approx = cv2.approxPolyDP(largest, 0.02 * peri, True)

        if len(approx) == 4:
            pts = approx.reshape(4, 2).astype(np.float32)

            # 排序：左上、右上、右下、左下
            s = pts.sum(axis=1)
            d = np.diff(pts, axis=1)
            rect = np.zeros((4, 2), dtype=np.float32)
            rect[0] = pts[np.argmin(s)]  # 左上
            rect[2] = pts[np.argmax(s)]  # 右下
            rect[1] = pts[np.argmin(d)]  # 右上
            rect[3] = pts[np.argmax(d)]  # 左下

            # 计算目标尺寸
            width = max(
                np.linalg.norm(rect[1] - rect[0]),
                np.linalg.norm(rect[2] - rect[3])
            )
            height = max(
                np.linalg.norm(rect[3] - rect[0]),
                np.linalg.norm(rect[2] - rect[1])
            )

            if width < 100 or height < 100:
                return img

            dst = np.array([
                [0, 0], [width - 1, 0],
                [width - 1, height - 1], [0, height - 1]
            ], dtype=np.float32)

            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(arr, M, (int(width), int(height)))
            img = Image.fromarray(warped)
            logger.info("Perspective corrected")

        return img
    except Exception as e:
        logger.debug("Perspective correction skipped: %s", e)
        return img
