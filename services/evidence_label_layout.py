"""Ownership labels for full-height OCR columns.

All geometry is half-open source-column pixels except label_bbox (inclusive).
A generated rail is a presentation offset, never a new template coordinate space.
"""
from __future__ import annotations

# Coordinate/PNG schema stays v2 so persisted drawing consumers remain compatible.
# The visual policy has its own style and qwen37 contract versions.
SLOT_LABEL_LAYOUT_VERSION = "attached-slot-labels-v2"
ATTACHED_SLOT_MODES = {"attached_stacked_slot", "attached_horizontal_slot"}
SLOT_LABEL_STYLE = "blue-hollow-box-v2"
LABEL_BORDER_WIDTH = 1
SLOT_OVERLAP_OPACITY = 0.65


def render_labels(crop, targets, protected_boxes, *, font, color, padding_x=6, padding_y=3, gap=8):
    if any(target.get("slot_id") for target in targets):
        return _attached_slot_labels(
            crop, targets, protected_boxes, font=font, color=color,
            padding_x=padding_x, padding_y=padding_y, gap=gap)
    return _render_safe_labels(crop, targets, protected_boxes, font=font, color=color,
                               padding_x=padding_x, padding_y=padding_y, gap=gap)


def _render_safe_labels(crop, targets, protected_boxes, *, font, color, padding_x, padding_y, gap):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    height, width = crop.shape[:2]
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    foreground = np.any(crop != 255, axis=2).astype(np.uint8)
    blocked = cv2.dilate(foreground, np.ones((5, 5), np.uint8))
    for x1, y1, x2, y2 in protected_boxes:
        if x2 > x1 and y2 > y1:
            blocked[max(0, y1 - 2):min(height, y2 + 2), max(0, x1 - 2):min(width, x2 + 2)] = 1
    integral = cv2.integral(blocked, sdepth=cv2.CV_32S)
    planned = []
    needs_rail = False
    for target in targets:
        label_font = font
        text = "Q" + target.get("slot_id", target["question_number"])
        tb = scratch.textbbox((0, 0), text, font=font)
        px, py = padding_x + LABEL_BORDER_WIDTH, padding_y + LABEL_BORDER_WIDTH
        bw, bh = tb[2] - tb[0] + 2 * px, tb[3] - tb[1] + 2 * py
        ax, ay = target["anchor_px"]
        ideal = [round(ax - bw + 1), round(ay - (bh - 1) / 2)]
        bounds = target.get("region_px")
        if bounds:
            ideal[0] = bounds[0] - gap - bw
        def safe(x, y):
            if x < 2 or y < 2 or x + bw > width - 2 or y + bh > height - 2:
                return False
            if integral[y + bh, x + bw] - integral[y, x + bw] - integral[y + bh, x] + integral[y, x]:
                return False
            return not any(x < p["rect"][2] + 3 and x + bw > p["rect"][0] - 2
                           and y < p["rect"][3] + 3 and y + bh > p["rect"][1] - 2 for p in planned)
        chosen = None
        if safe(*ideal):
            chosen = ideal
        elif bounds and not target.get("slot_id"):
            # Search horizontal gutters at the correct row, never jump across
            # neighbouring answers to a distant blank row.
            y = max(2, min(ideal[1], height - bh - 2))
            for x in range(min(ideal[0], width - bw - 2), 1, -1):
                if safe(x, y):
                    chosen = [x, y]
                    break
            if chosen is None:
                for x in range(max(2, bounds[2] + gap), width - bw - 1):
                    if safe(x, y):
                        chosen = [x, y]
                        break
        if chosen is None:
            needs_rail = True
            chosen = ideal
        x, y = chosen
        planned.append({"target": target, "text": text, "text_box": tb, "size": [bw, bh],
                        "font": label_font,
                        "ideal": ideal, "rect": [x, y, x + bw - 1, y + bh - 1]})

    rail_width = 0
    canvas_height = height
    if needs_rail:
        # Allocate parallel lanes for dense/same-row/overlapping boxes. Labels
        # stay at their target's row; the image is never shrunk to make room.
        lane_width = max(p["size"][0] for p in planned) + 2 * gap
        canvas_height = max(height, max(p["size"][1] for p in planned) + 4)
        lanes = []
        for p in sorted(planned, key=lambda p: (p["target"]["anchor_px"][1], p["target"]["anchor_px"][0])):
            bw, bh = p["size"]
            y = max(2, min(round(p["target"]["anchor_px"][1] - (bh - 1) / 2), canvas_height - bh - 2))
            lane = next((i for i, bottom in enumerate(lanes) if bottom + 4 < y), len(lanes))
            if lane == len(lanes):
                lanes.append(-1)
            lanes[lane] = y + bh - 1
            x = lane * lane_width + gap
            p["rect"] = [x, y, x + bw - 1, y + bh - 1]
        rail_width = len(lanes) * lane_width + gap
    canvas = Image.new("RGB", (width + rail_width, canvas_height), "white")
    canvas.paste(Image.fromarray(crop), (rail_width, 0))
    draw = ImageDraw.Draw(canvas)
    placements = []
    for p in planned:
        target, tb = p["target"], p["text_box"]
        x, y, x2, y2 = p["rect"]
        ax, ay = target["anchor_px"]
        bounds = target.get("region_px")
        end_x = (bounds[0] - 1 if bounds else ax) + rail_width
        end_y = max(0, min(height - 1, round(ay)))
        start = [x2 + 1, (y + y2) // 2]
        if not rail_width and bounds and x > bounds[2]:
            start = [x - 1, (y + y2) // 2]
            end_x = bounds[2]
        end = [max(0, min(canvas.width - 1, round(end_x))), end_y]
        # Leaders are allowed on blank paper only, outside ALL answer areas.
        # Do not erase faint marks, cross other labels or recolor handwriting.
        mask = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(mask).line((tuple(start), tuple(end)), fill=255, width=1)
        line = np.asarray(mask).copy().astype(bool)
        line[:height, rail_width:rail_width + width] &= ~blocked.astype(bool)
        for other in planned:
            ox1, oy1, ox2, oy2 = other["rect"]
            line[max(0, oy1 - 1):oy2 + 2, max(0, ox1 - 1):ox2 + 2] = False
        pixels = np.asarray(canvas).copy()
        pixels[line] = color
        canvas = Image.fromarray(pixels)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((x, y, x2, y2), outline=color, width=LABEL_BORDER_WIDTH)
        draw.text((x + padding_x + LABEL_BORDER_WIDTH - tb[0],
                   y + padding_y + LABEL_BORDER_WIDTH - tb[1]),
                  p["text"], font=p["font"], fill=color)
        placement = {key: value for key, value in target.items() if key not in {"anchor_px", "region_px"}}
        placement.update({
            "label_text": p["text"],
            "label_bbox": [x, y, x2, y2], "label_fully_visible": True,
            "label_left_of_region": bool(rail_width or (bounds and x2 < bounds[0])),
            "label_at_printed_question_number": not rail_width and [x, y] == p["ideal"],
            "fixed_position_overwrite_allowed": False, "foreground_preserved": True,
            "label_opacity": 1.0,
            "label_style": SLOT_LABEL_STYLE, "label_background_opacity": 0,
            "label_border_width_px": LABEL_BORDER_WIDTH, "orientation": "horizontal",
            "placement_offset_px": [x - p["ideal"][0], y - p["ideal"][1]],
            "placement_mode": "external_rail" if rail_width else "safe_whitespace",
            "target_anchor_px": [ax + rail_width, ay],
            "connector_line": [start, end],
        })
        placements.append(placement)
    actual = np.asarray(canvas)[:height, rail_width:rail_width + width]
    ink = foreground.astype(bool)
    if not np.array_equal(actual[ink], crop[ink]):
        raise ValueError("ownership labels modified evidence foreground")
    return canvas, placements, {
        "version": "non-occluding-labels-v1", "source_offset_px": [rail_width, 0],
        "source_size_px": [width, height],
        "source_rect_px": [rail_width, 0, rail_width + width, height],
        "foreground_preserved": True,
        "foreground_preserved_outside_labels": True,
        "foreground_preserved_outside_translucent_labels": True,
        "source_pixels_recoverable": bool(np.array_equal(actual, crop)),
    }


def _attached_slot_labels(crop, targets, protected_boxes, *, font, color, padding_x, padding_y, gap):
    """Attach horizontal IDs; stack only when their measured box hits a left answer."""
    import numpy as np
    from PIL import Image, ImageDraw
    from services.template_slots import slot_label_lines
    from services.processing_errors import TemplateSchemaError

    height, width = crop.shape[:2]
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    slots = [t for t in targets if t.get("slot_id")]
    for target in slots:
        bounds = target.get("region_px")
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(type(v) is not int for v in bounds)
                or not 0 <= bounds[0] < bounds[2] <= width
                or not 0 <= bounds[1] < bounds[3] <= height):
            raise TemplateSchemaError("answer_regions_invalid", target["question_number"])
    # Frozen geometry, not ink/answer content, is the only orientation input.
    obstacles = list(dict.fromkeys(tuple(box) for box in [
        *protected_boxes, *(t["region_px"] for t in targets if t.get("region_px"))]
        if len(box) == 4 and box[2] > box[0] and box[3] > box[1]))
    planned = []
    for target in slots:
        bounds = target["region_px"]
        stacked = slot_label_lines(target["question_number"], target["slot_id"])
        text = "".join(stacked)
        px, py = padding_x + LABEL_BORDER_WIDTH, padding_y + LABEL_BORDER_WIDTH
        horizontal_box = scratch.textbbox((0, 0), text, font=font)
        required = horizontal_box[2] - horizontal_box[0] + 2 * px
        horizontal_height = horizontal_box[3] - horizontal_box[1] + 2 * py
        # A tall work area is written from its top, so its label starts level
        # with the box top; a one-line blank keeps the label centred on it.
        top = target.get("label_align") == "top"
        top_y = target.get("label_top_px", bounds[1])
        anchor = [bounds[0], (bounds[1] + bounds[3]) / 2]
        hy = top_y if top else round(anchor[1] - (horizontal_height - 1) / 2)
        horizontal_rect = [bounds[0] - required, hy, bounds[0], hy + horizontal_height]
        left_neighbours = [box for box in obstacles if box[0] < bounds[0]
                           and box[1] < horizontal_rect[3] and box[3] > horizontal_rect[1]]
        available = min((bounds[0] - box[2] for box in left_neighbours), default=None)
        vertical = available is not None and available < required
        lines = stacked if vertical else [text]
        boxes = [scratch.textbbox((0, 0), line, font=font) for line in lines]
        line_height = max(box[3] - box[1] for box in boxes)
        bw = max(box[2] - box[0] for box in boxes) + 2 * px
        bh = len(lines) * line_height + (len(lines) - 1) * 2 + 2 * py
        x = bounds[0] - bw
        y = top_y if top else round(anchor[1] - (bh - 1) / 2)
        if top:
            anchor = [bounds[0], y + (bh - 1) / 2]
        rect = [x, y, x + bw, y + bh]
        preceding_boxes = [box for box in obstacles if box[0] < bounds[0] and _intersects(rect, box)]
        preceding = [other for other in targets if other is not target
                     and other.get("region_px") and other["region_px"][0] < bounds[0]
                     and _intersects(rect, other["region_px"])]
        if any(_intersects(rect, p["rect"]) for p in planned):
            raise TemplateSchemaError("slot_labels_overlap", target["question_number"])
        planned.append({"target": target, "anchor": anchor, "rect": rect, "lines": lines, "boxes": boxes,
                        "line_height": line_height, "padding": [px, py],
                        "orientation": "vertical" if vertical else "horizontal",
                        "orientation_reason": "preceding_region_collision" if vertical else
                            ("horizontal_fits" if left_neighbours else "no_left_neighbour"),
                        "available_gap_px": available, "required_horizontal_width_px": required,
                        "horizontal_label_bbox_source_px": horizontal_rect,
                        "horizontal_obstacle_boxes_source_px": [list(b) for b in left_neighbours],
                        "overlapping_preceding_boxes_source_px": [list(b) for b in preceding_boxes],
                        "opacity": SLOT_OVERLAP_OPACITY if preceding_boxes else 1.0,
                        "overlap_ids": [p.get("slot_id", p["question_number"]) for p in preceding]})

    # Ordinary/drawing labels retain safe whitespace/rail behaviour. Reserve
    # attached labels so legacy markers cannot paint over them.
    attached_ids = {id(p["target"]) for p in planned}
    legacy = [t for t in targets if id(t) not in attached_ids]
    base, placements, mapping = _render_safe_labels(
        crop, legacy, [*protected_boxes, *(p["rect"] for p in planned)], font=font, color=color,
        padding_x=padding_x, padding_y=padding_y, gap=gap)
    if not planned:
        return base, placements, mapping
    base_dx, base_dy = mapping["source_offset_px"]
    dx = max(base_dx, -min(p["rect"][0] for p in planned))
    dy = max(base_dy, -min(p["rect"][1] for p in planned))
    rail_right = max((p["label_bbox"][2] + 1 for p in placements
                      if p["placement_mode"] == "external_rail"), default=0)
    if rail_right:
        dx = max(dx, rail_right + gap - min(p["rect"][0] for p in planned))
    bottom = max(base.height, max(p["rect"][3] for p in planned))
    canvas = Image.new("RGB", (width + dx, bottom + dy), "white")
    if base_dx:
        canvas.paste(base.crop((0, 0, base_dx, base.height)), (0, dy - base_dy))
    canvas.paste(base.crop(tuple(mapping["source_rect_px"])), (dx, dy))
    for placement in placements:
        sx = 0 if placement["placement_mode"] == "external_rail" else dx - base_dx
        sy = dy - base_dy
        rect = placement["label_bbox"]
        placement["label_bbox"] = [rect[0] + sx, rect[1] + sy, rect[2] + sx, rect[3] + sy]
        placement["target_anchor_px"][0] += dx - base_dx
        placement["target_anchor_px"][1] += sy
        if placement["connector_line"]:
            start, end = placement["connector_line"]
            placement["connector_line"] = [[start[0] + sx, start[1] + sy],
                                            [end[0] + dx - base_dx, end[1] + sy]]
    for p in planned:
        target = p["target"]
        x1, y1, x2, y2 = p["rect"]
        rect = (x1 + dx, y1 + dy, x2 + dx, y2 + dy)
        patch = canvas.crop(rect)
        # Transparent interior; only the one-pixel outline and glyphs paint.
        # Existing answer ink takes priority over opaque blue annotations.
        # Retain source patches for both orientations and translucent replay.
        label = Image.new("RGBA", patch.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(label)
        rgba = (*color, round(255 * p["opacity"]))
        draw.rectangle((0, 0, label.width - 1, label.height - 1),
                       outline=rgba, width=LABEL_BORDER_WIDTH)
        _, py = p["padding"]
        for index, (line, tb) in enumerate(zip(p["lines"], p["boxes"])):
            draw.text(((label.width - (tb[2] - tb[0])) / 2 - tb[0],
                       py + index * (p["line_height"] + 2) - tb[1]),
                      line, font=font, fill=rgba)
        pixels = np.array(Image.alpha_composite(patch.convert("RGBA"), label).convert("RGB"))
        old = np.array(patch)
        ink = np.any(old != 255, axis=2)
        clipped_label_pixels = 0
        if p["opacity"] == 1.0:
            clipped_label_pixels = int(((np.asarray(label)[:, :, 3] > 0) & ink).sum())
            pixels[ink] = old[ink]
        preserved = np.array_equal(pixels[ink], old[ink])
        canvas.paste(Image.fromarray(pixels), rect[:2])
        placements.append({**{k: v for k, v in target.items() if k not in {"anchor_px", "region_px"}},
            "label_text": "".join(p["lines"]), "label_lines": p["lines"],
            "label_bbox": [rect[0], rect[1], rect[2] - 1, rect[3] - 1],
            "label_fully_visible": clipped_label_pixels == 0, "label_left_of_region": True,
            "clipped_label_pixels": clipped_label_pixels,
            "label_at_printed_question_number": False, "fixed_position_overwrite_allowed": not preserved,
            "foreground_preserved": preserved, "label_opacity": p["opacity"],
            "label_style": SLOT_LABEL_STYLE,
            "label_background_opacity": 0, "label_border_width_px": LABEL_BORDER_WIDTH,
            "label_padding_px": [padding_x, padding_y],
            **{key: p[key] for key in ("orientation", "orientation_reason", "available_gap_px",
                "required_horizontal_width_px", "horizontal_label_bbox_source_px",
                "horizontal_obstacle_boxes_source_px", "overlapping_preceding_boxes_source_px")},
            "overlapping_preceding_regions": p["overlap_ids"],
            "placement_mode": "attached_stacked_slot" if p["orientation"] == "vertical" else "attached_horizontal_slot",
            "target_anchor_px": [p["anchor"][0] + dx, p["anchor"][1] + dy],
            "connector_line": [], "placement_offset_px": [0, 0]})
    actual = np.array(canvas)[dy:dy + height, dx:dx + width]
    changed = np.any(actual != crop, axis=2) & np.any(crop != 255, axis=2)
    permitted = np.zeros((height, width), dtype=bool)
    translucent = np.zeros((height, width), dtype=bool)
    for p in planned:
        x1, y1, x2, y2 = p["rect"]
        permitted[max(0, y1):min(height, y2), max(0, x1):min(width, x2)] = True
        if p["opacity"] < 1:
            translucent[max(0, y1):min(height, y2), max(0, x1):min(width, x2)] = True
    if np.any(changed & ~permitted):
        raise ValueError("ownership labels modified evidence outside attached labels")
    def identity(target):
        return tuple(target.get(key) for key in ("question_number", "slot_id", "source", "region_index"))
    order = {identity(target): index for index, target in enumerate(targets)}
    placements.sort(key=lambda placement: order[identity(placement)])
    return canvas, placements, {
        "version": SLOT_LABEL_LAYOUT_VERSION, "source_offset_px": [dx, dy],
        "attached_slot_label_style": SLOT_LABEL_STYLE,
        "source_size_px": [width, height], "source_rect_px": [dx, dy, dx + width, dy + height],
        "foreground_preserved": not bool(changed.any()),
        "foreground_preserved_outside_labels": True,
        "unpatched_source_pixels_preserved": mapping["source_pixels_recoverable"],
        "source_pixels_recoverable": False,  # becomes true only after PNG patches are serialized
        "foreground_preserved_outside_translucent_labels": not bool((changed & ~translucent).any()),
        "blended_foreground_pixels": int((changed & translucent).sum()),
        "opaque_foreground_pixels": int((changed & ~translucent).sum()),
        "overlap_opacity": SLOT_OVERLAP_OPACITY,
    }


def _intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def source_patch_pnginfo(crop, placements, mapping):
    """Keep originals under attached labels inside the same PNG for pixel replay.

    These PNG metadata bytes are not another visual panel/model image. No
    answer is cropped out of the visible full-column image.
    """
    import base64
    import hashlib
    import json
    import zlib
    from PIL.PngImagePlugin import PngInfo

    if mapping.get("version") != SLOT_LABEL_LAYOUT_VERSION:
        return None
    height, width = crop.shape[:2]
    dx, dy = mapping["source_offset_px"]
    patches = []
    for placement in placements:
        if placement["placement_mode"] not in ATTACHED_SLOT_MODES:
            continue
        x1, y1, x2, y2 = placement["label_bbox"]
        x1, y1 = max(0, x1 - dx), max(0, y1 - dy)
        x2, y2 = min(width, x2 + 1 - dx), min(height, y2 + 1 - dy)
        if x2 > x1 and y2 > y1:
            patches.append({"rect": [x1, y1, x2, y2],
                            "rgb": base64.b64encode(zlib.compress(crop[y1:y2, x1:x2].tobytes())).decode("ascii")})
    payload = json.dumps(patches, separators=(",", ":"))
    mapping["source_patch_sha256"] = hashlib.sha256(payload.encode("ascii")).hexdigest()
    mapping["source_pixels_recoverable"] = bool(mapping.get("unpatched_source_pixels_preserved", False))
    metadata = PngInfo()
    metadata.add_text("label_source_patches_v1", payload)
    return metadata


def restore_source_patches(image, pixels, mapping):
    """Fail closed if an annotated graphic loses its original label pixels."""
    import base64
    import hashlib
    import json
    import zlib
    import numpy as np

    if mapping.get("version") != SLOT_LABEL_LAYOUT_VERSION:
        return pixels
    payload = image.info.get("label_source_patches_v1")
    if (not isinstance(payload, str) or len(payload) > 16_000_000
            or hashlib.sha256(payload.encode("utf-8")).hexdigest() != mapping.get("source_patch_sha256")):
        raise ValueError("missing or invalid label source patches")
    try:
        patches = json.loads(payload)
        if not isinstance(patches, list) or len(patches) > 400:
            raise ValueError("invalid patch count")
        height, width = pixels.shape[:2]
        for patch in patches:
            rect = patch["rect"]
            if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
                raise ValueError("invalid patch rectangle")
            x1, y1, x2, y2 = rect
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ValueError("patch outside source")
            expected = (y2 - y1) * (x2 - x1) * 3
            decoder = zlib.decompressobj()
            raw = decoder.decompress(base64.b64decode(patch["rgb"], validate=True), expected + 1)
            if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("invalid patch pixels")
            pixels[y1:y2, x1:x2] = np.frombuffer(raw, dtype=np.uint8).reshape(y2 - y1, x2 - x1, 3)
    except (KeyError, TypeError, ValueError, zlib.error) as exc:
        raise ValueError("invalid label source patches") from exc
    return pixels
