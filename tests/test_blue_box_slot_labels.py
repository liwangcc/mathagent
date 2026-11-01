"""2026-10-07 user contract: hollow blue labels, measured gap switching."""
import copy
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from services.evidence_label_layout import render_labels, source_patch_pnginfo, restore_source_patches
from services.template_evidence import _evidence_question_label_font

BLUE = (20, 84, 178)


def targets(boxes, number='2', indices=(3, 8)):
    return [dict(question_number=number, slot_id=f'{number}[{i}]',
                 region_px=b, anchor_px=[b[0], (b[1] + b[3]) / 2])
            for i, b in zip(indices, boxes)]


def render(crop, items, **kwargs):
    return render_labels(crop, items, [t['region_px'] for t in items],
                         font=_evidence_question_label_font(), color=BLUE, **kwargs)


def horizontal_width(text, px=6):
    bbox = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox(
        (0, 0), text, font=_evidence_question_label_font())
    return bbox[2] - bbox[0] + 2 * (px + 1)  # explicit one-pixel border


@pytest.mark.parametrize('delta,orientation', [(30, 'horizontal'), (0, 'horizontal'), (-1, 'vertical')])
def test_orientation_uses_measured_bbox_border_and_padding(delta, orientation):
    required = horizontal_width('2.8')
    boxes = [[150, 100, 300, 220], [300 + required + delta, 100, 700, 220]]
    items = targets(boxes)
    before = copy.deepcopy(items)
    canvas, labels, mapping = render(np.full((350, 800, 3), 255, np.uint8), items)
    assert labels[0]['placement_mode'] == 'attached_horizontal_slot'
    assert labels[1]['orientation'] == orientation
    assert labels[1]['available_gap_px'] == required + delta
    assert labels[1]['required_horizontal_width_px'] == required
    assert labels[1]['horizontal_label_bbox_source_px'][0] == 300 + delta
    assert labels[1]['label_lines'] == (['2.8'] if orientation == 'horizontal' else ['2', '.8'])
    assert all(p['label_opacity'] == 1 for p in labels)  # vertical fits even at width-1
    assert labels[1]['orientation_reason'] == ('horizontal_fits' if delta >= 0 else 'preceding_region_collision')
    assert items == before and canvas.size == (800, 350)


def test_no_left_neighbour_and_multiple_rows_keep_horizontal_hierarchical_ids():
    boxes = [[0, 0, 180, 30], [400, 200, 690, 320], [0, 380, 180, 400]]
    items = targets(boxes, '12.3', (10, 2, 7))
    crop = np.full((400, 700, 3), 255, np.uint8)
    canvas, labels, mapping = render(crop, items)
    assert [p['label_text'] for p in labels] == ['12.3.10', '12.3.2', '12.3.7']
    assert all(p['orientation'] == 'horizontal' and p['available_gap_px'] is None for p in labels)
    dx, dy = mapping['source_offset_px']
    assert dx > 0
    for label, box in zip(labels, boxes):
        x1, y1, x2, y2 = label['label_bbox']
        assert 0 <= x1 <= x2 < canvas.width and 0 <= y1 <= y2 < canvas.height
        assert x2 + 1 == box[0] + dx and label['connector_line'] == []
    assert mapping['source_size_px'] == [700, 400]


@pytest.mark.parametrize('slot', [False, True])
def test_zero_background_fill_blue_outline_and_blue_glyphs(slot):
    crop = np.full((220, 700, 3), 255, np.uint8)
    items = targets([[230, 80, 600, 170]], indices=(6,))
    if not slot:
        items[0].pop('slot_id')
    canvas, labels, mapping = render(crop, items)
    p = labels[0]
    l, t, r, b = p['label_bbox']
    patch = np.asarray(canvas)[t:b+1, l:r+1]
    assert np.all(patch[0] == BLUE) and np.all(patch[-1] == BLUE)
    assert np.all(patch[:, 0] == BLUE) and np.all(patch[:, -1] == BLUE)
    assert np.all(patch[1, 1:-1] == 255)
    assert np.any(np.all(patch[2:-2, 2:-2] == BLUE, axis=2))
    assert p['label_background_opacity'] == 0 and p['label_border_width_px'] == 1
    assert p['label_style'] == 'blue-hollow-box-v2'


def test_horizontal_blue_label_never_erases_overflow_ink():
    crop = np.full((250, 800, 3), 255, np.uint8)
    items = targets([[250, 80, 650, 180]], indices=(6,))
    blank, labels, _ = render(crop, items)
    l, t, r, b = labels[0]['label_bbox']
    footprint = np.any(np.asarray(blank)[t:b+1, l:r+1] != 255, axis=2)
    crop[t:b+1, l:r+1] = 120
    crop[220:225, 20:760] = 20
    canvas, labels, mapping = render(crop, items)
    patch = np.asarray(canvas)[t:b+1, l:r+1]
    np.testing.assert_array_equal(patch, crop[t:b+1, l:r+1])
    assert labels[0]['foreground_preserved'] is True
    assert labels[0]['label_fully_visible'] is False
    assert labels[0]['clipped_label_pixels'] == int(footprint.sum())
    assert mapping['foreground_preserved'] is True
    assert mapping['foreground_preserved_outside_translucent_labels'] is True
    assert mapping['foreground_preserved_outside_labels'] is True
    assert mapping['opaque_foreground_pixels'] == 0
    np.testing.assert_array_equal(np.asarray(canvas)[220:225, 20:760], crop[220:225, 20:760])
    metadata = source_patch_pnginfo(crop, labels, mapping)
    assert mapping['source_pixels_recoverable'] is True
    stream = io.BytesIO()
    canvas.save(stream, format='PNG', pnginfo=metadata)
    stream.seek(0)
    with Image.open(stream) as saved:
        recovered = restore_source_patches(saved, np.array(saved.crop(mapping['source_rect_px'])), mapping)
    np.testing.assert_array_equal(recovered, crop)


def test_other_question_and_short_vertical_overlap_are_real_obstacles():
    crop = np.full((250, 800, 3), 255, np.uint8)
    tb = ImageDraw.Draw(Image.new('RGB', (1, 1))).textbbox((0, 0), '2.8', font=_evidence_question_label_font())
    hy = round(160 - (tb[3] - tb[1] + 8 - 1) / 2)
    items = targets([[140, hy - 15, 390, hy + 1], [400, 110, 720, 210]])
    items[0].update(question_number='1', slot_id='1[3]')
    _, labels, _ = render(crop, items)
    assert labels[1]['orientation'] == 'vertical'
    assert labels[1]['available_gap_px'] == 10


def test_legacy_v2_png_pixels_remain_recoverable():
    import base64
    import hashlib
    import json
    import zlib
    crop = np.full((10, 20, 3), 19, np.uint8)
    payload = json.dumps([{'rect': [0, 0, 20, 10],
                           'rgb': base64.b64encode(zlib.compress(crop.tobytes())).decode('ascii')}])
    image = Image.new('RGB', (20, 10), 'blue')
    image.info['label_source_patches_v1'] = payload
    mapping = {'version': 'attached-slot-labels-v2',
               'source_patch_sha256': hashlib.sha256(payload.encode('ascii')).hexdigest()}
    np.testing.assert_array_equal(restore_source_patches(image, np.array(image), mapping), crop)


@pytest.mark.parametrize('neighbour_gap,orientation', [(None, 'horizontal'), (5, 'vertical')])
def test_tall_work_area_label_starts_level_with_the_box_top(neighbour_gap, orientation):
    # 2026-10-13: a solve work area is written from its top, so a label at its
    # vertical middle let the next question's first line sit above its own label.
    box = [300, 100, 900, 400]
    items = targets([box], '7', (1,))
    items[0]['label_align'] = 'top'
    protected = [box] + ([[100, 120, 300 - neighbour_gap, 160]] if neighbour_gap else [])
    canvas, labels, mapping = render_labels(
        np.full((500, 1000, 3), 255, np.uint8), items, protected,
        font=_evidence_question_label_font(), color=BLUE)
    dx, dy = mapping['source_offset_px']
    label = labels[0]
    assert label['orientation'] == orientation and label['label_align'] == 'top'
    assert label['label_bbox'][1] == box[1] + dy
    assert label['label_bbox'][2] + 1 == box[0] + dx


def test_one_line_blank_keeps_the_centred_label():
    box = [300, 100, 600, 160]
    canvas, labels, mapping = render(np.full((300, 800, 3), 255, np.uint8), targets([box], '4', (1,)))
    top, bottom = labels[0]['label_bbox'][1], labels[0]['label_bbox'][3]
    assert abs((top + bottom) / 2 - mapping['source_offset_px'][1] - 130) <= 1
