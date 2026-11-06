import assert from 'node:assert/strict'
import test from 'node:test'
import { toRaw } from 'vue'
import { openTemplateEditor } from './helpers/templateGeometryCut.js'

const { serializeTemplateDrawingReview } = await import('../src/utils/templateReview.js')

// The template editor's geometry tools on the real 批改 page: the teacher arms
// a toolbar tool (and names its Q), draws on the page canvas, drags, resizes
// and deletes markers; the page keeps the result as the template draft.

const plain = value => JSON.parse(JSON.stringify(value))
const TOOL = { question: '红色题号', answer: '蓝色作答框', drawing: '绿色画图区', 'drawing-label': '作图题题号' }
const REFERENCE = Object.freeze({
  version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode: 'manual',
  variants: [], context_regions: [], teacher_confirmed: true,
})
const TWO_COLUMNS = [{ column_index: 1, x1_1000: 0, x2_1000: 500 }, { column_index: 2, x1_1000: 500, x2_1000: 1000 }]

// The recognised template: one page with Q2.1 (a drawing question whose
// standard reference the teacher already confirmed), its printed Q marker at
// [30, 100, 70, 150] and two columns. `question`, `schema`, `anchor` and
// `page` add to the template question, its page schema entry, its Q anchor
// and the cut page; `more` adds { question, schema, anchor } entries.
function geometryTemplate({ question = {}, schema = {}, anchor = {}, page = {}, more = [], pages = [] } = {}) {
  const entries = [{ question, schema, anchor, number: '2.1' }, ...more]
  return {
    pages: [{
      page_index: 0,
      page_label: 'page-0',
      questions: entries.map(entry => ({
        question_no: entry.number ?? entry.schema.question_number,
        question_text: '合成题干',
        standard_answer: '合成答案',
        answer_kind: 'drawing',
        drawing_reference: plain(REFERENCE),
        ...entry.question,
      })),
    }, ...pages.map(item => item.template)],
    cut: () => ({
      status: 'complete',
      image_type: 'scan',
      progress: { completed: 1 + pages.length, total: 1 + pages.length, remaining: 0 },
      pages: [{
        page_index: 0,
        page_label: 'page-0',
        question_anchors: entries.map(entry => ({
          question_number: entry.number ?? entry.schema.question_number,
          question_number_bbox: [30, 100, 70, 150],
          directory_node_id: 'synthetic-leaf',
          ...entry.anchor,
        })),
        question_schema: entries.map(entry => ({
          question_number: entry.number ?? entry.schema.question_number,
          answer_kind: 'drawing',
          printed_path: ['二', '1'],
          directory_node_id: 'synthetic-leaf',
          ...entry.schema,
        })),
        anchor_crop_plan: { parameters: { column_count: 2, columns: TWO_COLUMNS } },
        ...page,
      }, ...pages.map(item => item.cut)],
    }),
  }
}

function openGeometry(t, template = {}, options = {}) {
  return openTemplateEditor(t, { ...geometryTemplate(template), ...options })
}

// Arm a toolbar tool for the Q it suggests, then draw with it on the canvas.
async function create(editor, kind, from = [120, 170], to = [320, 330], extra = {}) {
  await editor.tool(TOOL[kind])
  const surface = editor.drawSurface()
  assert.ok(surface, `${kind} tool armed`)
  await editor.drag(surface, from, to, extra)
}

// Pick an option of a plain <select> (its handler reads event.target.value).
async function choose(editor, select, value) {
  for (const option of select.options) option.selected = String(option.value) === String(value)
  select.value = String(value)
  await select.dispatch('change')
  await editor.settle()
}

const selectedMarker = (elements) => elements.find(el => el.classList.contains('is-selected')) ?? null
const isDisabled = el => el.disabled === true || el.hasAttribute('disabled')
const strokes = question => question.drawing_reference?.variants?.[0]?.strokes ?? []

test('four toolbar categories create normalized full-page geometry with original Q identity', async (t) => {
  for (const kind of ['question', 'answer', 'drawing', 'drawing-label']) {
    // The Q marker tool sits beside an existing drawing area.
    const editor = await openGeometry(t, kind === 'drawing-label' ? { schema: { drawing_regions: [[400, 400, 600, 600]] } } : {})
    const page = editor.preview()
    const question = editor.question('2.1')
    const identity = plain(page.questionSchema)
    const text = question.question_text
    const answer = question.standard_answer
    await editor.tool(TOOL[kind])
    const prompt = editor.page.boxes.at(-1)
    assert.equal(prompt.kind, 'prompt')
    assert.equal(prompt.title, `设置${TOOL[kind]}题号`)
    assert.equal(prompt.options.inputValue, '2.1', 'the tool asks which Q the new marker belongs to')
    await editor.drag(editor.drawSurface(), [120, 170], [320, 330])
    const target = page.geometrySelection.target
    assert.equal(page.geometrySelection.kind, kind)
    assert.equal(target.question_number, '2.1')
    assert.deepEqual(plain(kind === 'drawing-label' ? target.label_anchor : kind === 'question' ? target.question_number_bbox : target.bbox), kind === 'drawing-label' ? [300, 400] : [100, 200, 300, 400])
    assert.equal(target.human_confirmed, true)
    assert.equal(target.confidence_source, 'teacher')
    assert.equal(page.dirty, true)
    assert.equal(page.geometryTool, '')
    assert.equal(page.geometryDraft, null)
    assert.equal(editor.setup.templateCropGesture, null)
    assert.equal(editor.drawSurface(), null, 'the tool is released after one marker')
    assert.match(editor.article().text, /已调整 · 随模板保存/)
    assert.equal(editor.hint(), `已选中${TOOL[kind]}，可拖动或删除；只删标记，不删题目。`)
    const shown = { question: editor.anchors(), drawing: editor.drawingRegions(), 'drawing-label': editor.labels() }[kind]
    if (shown) {
      assert.deepEqual(selectedMarker(shown).style, kind === 'drawing-label'
        ? { left: '30%', top: '40%' }
        : { left: '10%', top: '20%', width: '20%', height: '20%' })
    }
    assert.deepEqual(plain(page.questionSchema), identity)
    assert.equal(question.question_text, text)
    assert.equal(question.standard_answer, answer)
    if (kind === 'answer') assert.equal(page.hasAnswerRegionContract, true)
    editor.close()
  }
})

test('choice/fill drawing classification, box creation and dragging never rewrite answer type or create a duplicate Q', async (t) => {
  for (const question_type of ['choice', 'fill']) {
    for (const answer_kind of ['drawing', 'mixed']) {
      const choice_mode = question_type === 'choice' ? 'single' : 'unknown'
      const standard_answer = question_type === 'choice' ? 'B' : '4'
      const editor = await openGeometry(t, {
        schema: { question_type, choice_mode, answer_kind: 'non_graphic' },
        question: { question_type, choice_mode, standard_answer, answer_kind: 'non_graphic' },
      })
      const page = editor.preview()
      const question = editor.question('2.1')
      const originalAnswer = question.standard_answer
      // The teacher sets 作图属性 in the question list, then frames the drawing.
      const kindSelect = editor.page.root.find(el => el.tagName === 'select' && el.attributes['aria-label'] === '第 2.1 题作图属性')
      await choose(editor, kindSelect, answer_kind)
      assert.equal(kindSelect.value, answer_kind)
      await create(editor, 'drawing')
      await create(editor, 'drawing-label', [120, 170], [120, 170])
      const box = page.drawingBoxes[0]
      const before = plain(box.bbox)
      await editor.drag(editor.drawingRegions()[0], [120, 170], [220, 250])
      assert.notDeepEqual(plain(box.bbox), before)
      // An older template may repeat the drawing's Q point as a label-anchor 作答框 row.
      page.answerBoxes.push({ question_number: '2.1', geometry_kind: 'label_anchor', bbox: [100, 200, 110, 210] })
      await editor.settle()
      assert.equal(editor.answerRegions().length, 0, 'a label-anchor row is not shown as an 作答框')
      assert.match(editor.article().byClass('tpl-page-counts').text, /0 个作答框/)
      assert.equal(page.drawingLabels.length, 1)
      assert.equal(editor.labels().length, 1)
      const saved = serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, page.drawingLabels)[0]
      for (const item of [question, page.questionSchema[0], saved]) {
        assert.equal(item.question_type, question_type)
        assert.equal(item.choice_mode, choice_mode)
        assert.equal(item.answer_kind, answer_kind)
      }
      assert.equal(question.standard_answer, originalAnswer)
      assert.equal(question.drawing_reference.teacher_confirmed, false, 'geometry change still requires teacher reference review')
      assert.deepEqual(saved.drawing_regions, [plain(box.bbox)])
      assert.deepEqual(saved.label_anchor, plain(page.drawingLabels[0].label_anchor))
      editor.close()
    }
  }
})

test('labels follow boxes: moving the main drawing area carries its Q marker, cancel restores both', async (t) => {
  const editor = await openGeometry(t, { schema: { drawing_regions: [[400, 400, 600, 600], [700, 400, 900, 600]], label_anchor: [385, 500] } })
  const page = editor.preview()
  const [main, second] = page.drawingBoxes
  assert.deepEqual(plain(page.drawingLabels), [{ question_number: '2.1', label_anchor: [385, 500] }])
  // Dragging the main area left past its marker used to strand it and block save.
  const [mainRegion, secondRegion] = editor.drawingRegions()
  await editor.down(mainRegion, [520, 410])
  await editor.move(mainRegion, [320, 490])
  await editor.settle()
  assert.deepEqual(plain(main.bbox), [200, 500, 400, 700])
  assert.deepEqual(plain(page.drawingLabels[0].label_anchor), [185, 600])
  assert.deepEqual(editor.labels()[0].style, { left: '18.5%', top: '60%' })
  await editor.cancel(mainRegion)
  await editor.settle()
  assert.deepEqual(plain(main.bbox), [400, 400, 600, 600])
  assert.deepEqual(plain(page.drawingLabels[0].label_anchor), [385, 500])
  assert.deepEqual(editor.labels()[0].style, { left: '38.5%', top: '50%' })
  await editor.down(mainRegion, [520, 410])
  await editor.up(mainRegion, [320, 490])
  await editor.settle()
  assert.deepEqual(plain(page.drawingLabels[0].label_anchor), [185, 600])
  // Another drawing area of the same question does not own the marker.
  await editor.down(secondRegion, [820, 410])
  await editor.up(secondRegion, [870, 410])
  await editor.settle()
  assert.deepEqual(plain(second.bbox), [750, 400, 950, 600])
  assert.deepEqual(plain(page.drawingLabels[0].label_anchor), [185, 600])
  const saved = serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, page.drawingLabels)[0]
  assert.deepEqual(saved.label_anchor, [185, 600])
})

test('reverse drag is normalized and endpoints stay within the page', async (t) => {
  const reverse = await openGeometry(t)
  await create(reverse, 'answer', [520, 650], [220, 170])
  assert.deepEqual(plain(reverse.preview().answerBoxes[0].bbox), [200, 200, 500, 800])
  reverse.close()
  const clamped = await openGeometry(t)
  await create(clamped, 'answer', [520, 410], [-900, 1900])
  assert.deepEqual(plain(clamped.preview().answerBoxes[0].bbox), [0, 500, 500, 1000])
  clamped.close()
  const label = await openGeometry(t, { schema: { drawing_regions: [[400, 400, 600, 600]] } })
  await create(label, 'drawing-label', [520, 410], [1900, -900])
  assert.deepEqual(plain(label.preview().drawingLabels[0].label_anchor), [1000, 0])
  assert.deepEqual(label.labels()[0].style, { left: '100%', top: '0%' })
})

test('tap and tiny accidental drags create no rectangle; drawing Q is deliberately a point tool', async (t) => {
  for (const kind of ['handwriting', 'question', 'answer', 'drawing']) {
    for (const end of [[120, 170], [122, 172]]) {
      const editor = await openGeometry(t)
      const page = editor.preview()
      const before = plain(page.questionAnchors)
      if (kind === 'handwriting') {
        // 作答框 replaced the old handwriting tool: there is nothing to arm.
        assert.deepEqual(editor.article().byClass('tpl-add-tools').byTag('button').map(el => el.text), [...Object.values(TOOL), '删除所选'])
        assert.equal(await editor.setup.armTemplateGeometryTool(page, 'handwriting'), false)
        await editor.settle()
        assert.equal(editor.drawSurface(), null)
        await editor.drag(editor.emptyLayer(), [120, 170], end)
      } else {
        await create(editor, kind, [120, 170], end)
      }
      assert.equal(page.dirty, false)
      assert.deepEqual(plain(page.questionAnchors), before)
      assert.equal(page.handwritingBoxes.length + page.answerBoxes.length + page.drawingBoxes.length, 0)
      assert.equal(page.geometrySelection, null)
      assert.doesNotMatch(editor.article().text, /已调整/)
      editor.close()
    }
  }
  const editor = await openGeometry(t, { schema: { drawing_regions: [[400, 400, 600, 600]] } })
  await create(editor, 'drawing-label', [120, 170], [120, 170])
  assert.deepEqual(plain(editor.preview().drawingLabels[0].label_anchor), [100, 200])
})

test('touch pointercancel never commits a new box or marks a question reference dirty', async (t) => {
  const editor = await openGeometry(t)
  const page = editor.preview()
  await editor.tool(TOOL.answer)
  const surface = editor.drawSurface()
  await editor.down(surface, [120, 170], { pointerType: 'touch' })
  await editor.move(surface, [320, 330], { pointerType: 'touch' })
  await editor.cancel(surface, [320, 330], { pointerId: 8, pointerType: 'touch' })
  await editor.settle()
  assert.ok(editor.setup.templateCropGesture, 'different pointer cannot cancel this gesture')
  assert.ok(editor.stage().byClass('tpl-draw-draft'), 'the box being drawn is still shown')
  await editor.cancel(surface, [320, 330], { pointerType: 'touch' })
  await editor.settle()
  assert.equal(page.answerBoxes.length, 0)
  assert.equal(page.dirty, false)
  assert.equal(page.geometryDraft, null)
  assert.equal(editor.stage().byClass('tpl-draw-draft'), null)
  assert.equal(editor.question('2.1').drawing_reference.teacher_confirmed, true)
})

test('cancel restores moved, resized, label, and divider geometry with its original dirty state', async (t) => {
  // Each marker as the server returns it; 作答框 also come from the legacy
  // handwriting field, and they show on a question that is not a pure drawing.
  const cases = {
    question: { template: {}, target: page => page.questionAnchors[0], handle: editor => editor.anchors()[0].byClass('tpl-handwriting-resize') },
    handwriting: {
      template: { schema: { answer_kind: 'non_graphic' }, page: { template_handwriting_regions: [{ question_number: '2.1', bbox: [100, 200, 300, 400] }] } },
      target: page => page.answerBoxes[0],
      handle: editor => editor.answerRegions()[0].byClass('tpl-handwriting-resize'),
    },
    answer: {
      template: { schema: { answer_kind: 'non_graphic' }, page: { template_answer_regions: [{ question_number: '2.1', regions: [[100, 200, 300, 400]] }] } },
      target: page => page.answerBoxes[0],
      handle: editor => editor.answerRegions()[0].byClass('tpl-handwriting-resize'),
    },
    drawing: {
      template: { schema: { drawing_regions: [[100, 200, 300, 400]] } },
      target: page => page.drawingBoxes[0],
      handle: editor => editor.drawingRegions()[0].byClass('tpl-handwriting-resize'),
    },
    'drawing-label': {
      template: { schema: { drawing_regions: [[400, 400, 600, 600]], label_anchor: [100, 200] } },
      target: page => page.drawingLabels[0],
      handle: editor => editor.labels()[0],
    },
    divider: { template: {}, target: page => page.columns, handle: editor => editor.dividers()[0] },
  }
  for (const [kind, { template, target, handle }] of Object.entries(cases)) {
    const editor = await openGeometry(t, template)
    const page = editor.preview()
    const item = target(page)
    const before = plain(item)
    const columns = plain(page.columns)
    const grip = handle(editor)
    assert.ok(grip, `${kind} has a handle on the canvas`)
    await editor.down(grip, [120, 170])
    await editor.move(grip, [250, 280], { pointerType: 'touch' })
    await editor.settle()
    assert.equal(page.dirty, true, kind)
    assert.notDeepEqual(plain(item), before, `${kind} follows the pointer`)
    assert.match(editor.article().text, /已调整 · 随模板保存/)
    await editor.cancel(grip)
    await editor.settle()
    assert.deepEqual(plain(item), before, kind)
    assert.deepEqual(plain(page.columns), columns)
    assert.equal(page.dirty, false)
    assert.doesNotMatch(editor.article().text, /已调整/)
    assert.equal(editor.question('2.1').drawing_reference.teacher_confirmed, true)
    editor.close()
  }
})

test('click selection alone is not an edit and does not invalidate a confirmed reference', async (t) => {
  const editor = await openGeometry(t)
  const page = editor.preview()
  const anchor = page.questionAnchors[0]
  await editor.tap(editor.anchors()[0], [60, 110])
  assert.equal(page.geometrySelection.target, anchor)
  assert.equal(page.geometrySelection.kind, 'question')
  assert.ok(editor.anchors()[0].classList.contains('is-selected'))
  assert.equal(isDisabled(editor.deleteButton()), false)
  assert.equal(page.dirty, false)
  assert.doesNotMatch(editor.article().text, /已调整/)
  assert.equal(editor.question('2.1').drawing_reference.teacher_confirmed, true)
  assert.equal(anchor.human_confirmed, undefined)
})

test('shared deletion only removes the selected marker and leaves question text, identity, and sibling boxes', async (t) => {
  const cases = {
    // An older payload also carried the Q box as question_bbox / bbox.
    question: { anchor: { question_bbox: [1, 2, 3, 4], bbox: [1, 2, 3, 4] } },
    // A legacy handwriting region, shown and deleted as an 作答框.
    handwriting: { schema: { answer_kind: 'non_graphic' }, page: { template_handwriting_regions: [{ question_number: '2.1', bbox: [100, 200, 300, 400] }] } },
    answer: {},
    // The Q marker sits beside a drawing area that stays.
    'drawing-label': { schema: { drawing_regions: [[400, 400, 600, 600]] } },
  }
  for (const [kind, template] of Object.entries(cases)) {
    const editor = await openGeometry(t, template)
    const page = editor.preview()
    const question = editor.question('2.1')
    const schema = plain(page.questionSchema)
    const text = question.question_text
    const answer = question.standard_answer
    if (kind === 'handwriting') await editor.click(editor.legend('Q2.1 作答框 1'))
    else await create(editor, kind)
    assert.equal(isDisabled(editor.deleteButton()), false, kind)
    await editor.click(editor.deleteButton())
    assert.equal(page.questionAnchors.length, 1)
    assert.equal(page.questionAnchors[0].question_number, '2.1')
    assert.equal(page.questionAnchors[0].directory_node_id, 'synthetic-leaf')
    assert.deepEqual(plain(page.questionSchema), schema)
    assert.equal(question.question_text, text)
    assert.equal(question.standard_answer, answer)
    assert.equal(page.geometrySelection, null)
    if (kind === 'question') {
      assert.deepEqual(plain(page.questionAnchors[0].question_number_bbox), [])
      assert.equal(page.questionAnchors[0].question_bbox, undefined)
      assert.equal(page.questionAnchors[0].bbox, undefined)
      assert.equal(editor.anchors().length, 0)
    } else if (kind === 'drawing-label') {
      assert.equal(page.drawingLabels.length, 0)
      assert.equal(editor.labels().length, 0)
      assert.equal(page.drawingBoxes.length, 1, 'the drawing area stays')
      // Saving re-derives the marker beside the remaining drawing area.
      assert.deepEqual(plain(serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, page.drawingLabels)[0].label_anchor), [385, 500])
    } else {
      assert.equal(page.handwritingBoxes.length + page.answerBoxes.length + page.drawingBoxes.length + page.drawingLabels.length, 0)
      assert.equal(editor.answerRegions().length, 0)
    }
    editor.close()
  }
  // Deleting the last drawing area: its Q marker and standard figure go with
  // it and the question saves as non-drawing; text and identity stay.
  const drawn = await openGeometry(t)
  const drawnPage = drawn.preview()
  const drawnQuestion = drawn.question('2.1')
  await create(drawn, 'drawing')
  assert.equal(drawnPage.drawingLabels.length, 1, 'a new drawing area gets its Q marker automatically')
  await drawn.click(drawn.legend('Q2.1 画图区 1'))
  assert.equal(drawnPage.geometrySelection.target, drawnPage.drawingBoxes[0])
  await drawn.click(drawn.deleteButton())
  assert.equal(drawnPage.drawingBoxes.length + drawnPage.drawingLabels.length, 0)
  assert.equal(drawn.drawingRegions().length + drawn.labels().length, 0)
  assert.equal(drawnPage.questionSchema[0].answer_kind, 'non_graphic')
  assert.equal(drawnPage.questionSchema[0].directory_node_id, 'synthetic-leaf')
  assert.equal(drawnQuestion.drawing_reference, undefined)
  assert.equal(drawnQuestion.question_text, '合成题干')
  assert.equal(drawnQuestion.standard_answer, '合成答案')
  drawn.close()
  const editor = await openGeometry(t)
  const page = editor.preview()
  await editor.tap(editor.dividers()[0], [520, 300])
  assert.equal(page.geometrySelection.kind, 'divider')
  await editor.click(editor.deleteButton())
  assert.deepEqual(plain(page.columns), [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }])
  assert.equal(page.columnCount, 1)
  assert.equal(editor.dividers().length, 0)
})

// Q2.1 with a confirmed standard figure of two strokes inside its drawing area.
const FIGURE = {
  question: {
    drawing_reference: {
      ...plain(REFERENCE),
      comparison_mode: 'fixed',
      variants: [{ strokes: [{ kind: 'polyline', points: [[420, 420], [480, 480]] }, { kind: 'polyline', points: [[500, 420], [560, 480]] }] }],
    },
  },
  schema: { drawing_regions: [[400, 400, 600, 600]] },
}

// The page image has loaded, so the standard-figure editor accepts input.
async function showFigure(editor) {
  await editor.stage().byTag('img')[0].dispatch('load')
  await editor.settle()
}

const figureStrokes = editor => editor.stage().findAll(el => el.tagName === 'polyline' && el.classList.contains('reference-stroke'))

test('reference stroke deletion delegates to the exposed editor without deleting a geometry marker', async (t) => {
  const editor = await openGeometry(t, FIGURE)
  const page = editor.preview()
  const question = editor.question('2.1')
  await showFigure(editor)
  // Selecting Q2.1's marker makes its standard figure the one being edited.
  await editor.tap(editor.anchors()[0], [60, 110])
  assert.equal(page.referenceQuestionNumber, '2.1')
  await editor.down(figureStrokes(editor)[0], [460, 470])
  await editor.settle()
  assert.equal(page.referenceStrokeSelected, true)
  assert.equal(page.geometrySelection, null)
  assert.equal(editor.hint(), '已选中标准图笔画，可点击“删除所选”。')
  await editor.click(editor.deleteButton())
  assert.deepEqual(plain(strokes(question)), [{ kind: 'polyline', points: [[500, 420], [560, 480]] }], 'the selected stroke is deleted')
  assert.equal(question._drawingReferenceDirty, true)
  assert.deepEqual(plain(page.questionAnchors[0].question_number_bbox), [30, 100, 70, 150])
  assert.equal(editor.anchors().length, 1)
  // Clearing the selection (a tap on an empty spot) also clears the editor's.
  await editor.down(figureStrokes(editor)[0], [520, 470])
  await editor.settle()
  assert.ok(figureStrokes(editor)[0].classList.contains('selected'))
  assert.equal(page.referenceStrokeSelected, true)
  await editor.down(editor.emptyLayer(), [900, 700])
  await editor.settle()
  assert.equal(figureStrokes(editor)[0].classList.contains('selected'), false)
  assert.equal(page.referenceStrokeSelected, false)
  assert.equal(isDisabled(editor.deleteButton()), true)
  assert.equal(strokes(question).length, 1, 'clearing deletes nothing')
})

// While a cut run is in flight (global lock) or the page is saving.
async function lock(editor, kind) {
  if (kind === 'global') {
    editor.control.receipt = () => new Promise(() => {})
    await editor.upload.click('重新自动裁剪')
    assert.equal(editor.setup.templateCutBusy, true)
  } else {
    editor.preview().saving = true
    await editor.settle()
  }
}

test('existing save and global locks block new creation, dragging, deletion, and reference deletion', async (t) => {
  for (const kind of ['global', 'saving']) {
    // An armed tool and a selected Q marker (picked in the legend) are both kept.
    const editor = await openGeometry(t, {}, { timers: true })
    const page = editor.preview()
    const before = plain(page.questionAnchors)
    await editor.tool(TOOL.answer)
    await editor.click(editor.legend('Q2.1 题号位置 1'))
    assert.equal(page.geometryTool, 'answer')
    assert.equal(page.geometrySelection.kind, 'question')
    await lock(editor, kind)
    for (const label of Object.values(TOOL)) assert.equal(isDisabled(editor.toolButton(label)), true, label)
    assert.equal(isDisabled(editor.deleteButton()), true)
    const surface = editor.drawSurface()
    await editor.down(surface, [120, 170])
    await editor.settle()
    assert.equal(editor.setup.templateCropGesture, null, 'no box is started')
    assert.equal(editor.stage().byClass('tpl-draw-draft'), null)
    await editor.move(surface, [320, 330])
    await editor.up(surface, [320, 330])
    await editor.settle()
    assert.equal(editor.setup.templateCropGesture, null)
    assert.equal(page.answerBoxes.length, 0)
    assert.equal(await editor.setup.armTemplateGeometryTool(page, 'question'), false)
    assert.equal(page.geometryTool, 'answer')
    // The armed tool is still shown pressed (aria-pressed is on only for it).
    assert.deepEqual(Object.values(TOOL).filter(label => editor.toolButton(label).hasAttribute('aria-pressed')), [TOOL.answer])
    editor.setup.deleteTemplateSelection(page)
    await editor.settle()
    assert.deepEqual(plain(page.questionAnchors), before)
    editor.close()

    // Dragging a marker with no tool armed.
    const dragging = await openGeometry(t, {}, { timers: true })
    const draggingPage = dragging.preview()
    await lock(dragging, kind)
    const marker = dragging.anchors()[0]
    await dragging.down(marker, [60, 110])
    await dragging.settle()
    assert.equal(dragging.setup.templateCropGesture, null, 'no drag is started')
    assert.equal(draggingPage.geometrySelection ?? null, null, 'nor is the marker selected')
    await dragging.move(marker, [300, 400])
    await dragging.up(marker, [300, 400])
    await dragging.settle()
    assert.equal(dragging.setup.templateCropGesture, null)
    assert.deepEqual(plain(draggingPage.questionAnchors), before)
    assert.equal(draggingPage.dirty, false)
    dragging.close()

    // A selected standard-figure stroke.
    const figure = await openGeometry(t, FIGURE, { timers: true })
    const figurePage = figure.preview()
    await showFigure(figure)
    await figure.tap(figure.anchors()[0], [60, 110])
    await figure.down(figureStrokes(figure)[0], [460, 470])
    await figure.settle()
    assert.equal(figurePage.referenceStrokeSelected, true)
    await lock(figure, kind)
    assert.equal(isDisabled(figure.deleteButton()), true)
    figure.setup.deleteTemplateSelection(figurePage)
    await figure.settle()
    assert.equal(strokes(figure.question('2.1')).length, 2)
    figure.close()
  }
})

test('a lock arriving mid-gesture rolls back geometry rather than committing during save', async (t) => {
  for (const type of ['create', 'move']) {
    const editor = await openGeometry(t, {}, { timers: true })
    const page = editor.preview()
    const before = plain(page.questionAnchors)
    let grip
    if (type === 'create') {
      await editor.tool(TOOL.answer)
      grip = editor.drawSurface()
    } else grip = editor.anchors()[0]
    await editor.down(grip, [120, 170])
    await editor.move(grip, [320, 330])
    await lock(editor, 'global')
    await editor.move(grip, [400, 410])
    await editor.settle()
    // Rolled back as soon as the pointer moves under the lock, not on release.
    assert.equal(editor.setup.templateCropGesture, null, type)
    assert.deepEqual(plain(page.questionAnchors), before)
    assert.equal(page.geometryDraft, null)
    assert.equal(page.dirty, false)
    await editor.up(grip, [420, 430])
    await editor.settle()
    assert.deepEqual(plain(page.questionAnchors), before)
    assert.equal(page.answerBoxes.length, 0)
    assert.equal(page.dirty, false)
    assert.equal(editor.question('2.1').drawing_reference.teacher_confirmed, true)
    assert.equal(editor.setup.templateCropGesture, null)
    editor.close()
  }
})

test('a question can retain multiple boxes, while the ninth box is refused without replacing siblings', async (t) => {
  for (const kind of ['answer', 'drawing']) {
    // 作答框 show (and are listed) on a question that is not a pure drawing.
    const editor = await openGeometry(t, kind === 'answer' ? { schema: { answer_kind: 'non_graphic' }, question: { answer_kind: 'non_graphic' } } : {})
    const page = editor.preview()
    const boxes = () => ({ answer: page.answerBoxes, drawing: page.drawingBoxes })[kind]
    const regions = () => ({ answer: editor.answerRegions(), drawing: editor.drawingRegions() })[kind]
    for (let index = 0; index < 8; index++) await create(editor, kind, [120 + index * 10, 170], [320 + index * 10, 330])
    assert.equal(boxes().length, 8)
    assert.equal(regions().length, 8)
    const before = plain(boxes())
    await create(editor, kind)
    assert.equal(boxes().length, 8)
    assert.deepEqual(plain(boxes()), before)
    assert.match(editor.upload.client.messages.filter(message => message.type === 'warning').at(-1).message, /最多添加 8/)
    await editor.click(editor.legend(`Q2.1 ${kind === 'answer' ? '作答框' : '画图区'} 4`))
    assert.equal(page.geometrySelection.target, boxes()[3])
    await editor.click(editor.deleteButton())
    assert.deepEqual(plain(boxes()), before.filter((_, index) => index !== 3))
    assert.equal(regions().length, 7)
    editor.close()
  }
})

// 重叠层选择器已按教师标注移除（点击即选择）；原选择 API 一并删除。

test('selecting another Q updates ownership without dirtying it, and fixed footer pagination retains both drafts', async (t) => {
  const editor = await openGeometry(t, {
    more: [{ number: '2.2', question: { drawing_reference: undefined }, schema: { directory_node_id: 'synthetic-second-leaf' }, anchor: { question_number_bbox: [30, 500, 70, 550], directory_node_id: 'synthetic-second-leaf' } }],
    pages: [{
      template: { page_index: 1, page_label: 'page-1', questions: [{ question_no: '3', question_text: '第三题', standard_answer: '3', answer_kind: 'non_graphic' }] },
      cut: {
        page_index: 1,
        page_label: 'page-1',
        question_anchors: [{ question_number: '3', question_number_bbox: [20, 30, 40, 50] }],
        question_schema: [{ question_number: '3', answer_kind: 'non_graphic' }],
        template_answer_regions: [{ question_number: '3', regions: [[40, 100, 200, 250]] }],
        anchor_crop_plan: { parameters: { column_count: 1, columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }] } },
      },
    }],
  })
  const first = editor.preview(0)
  const second = editor.preview(1)
  const pagesBefore = toRaw(editor.setup.templateCutPreviewPages).map(toRaw)
  const scrolls = []
  for (const index of [0, 1]) {
    const article = editor.page.root.find(el => el.id === `template-reference-page-${index}`)
    article.scrollIntoView = options => scrolls.push({ id: article.id, options: plain(options) })
  }
  const pageArticles = () => editor.page.root.findAll(el => el.tagName === 'article' && el.classList.contains('tpl-cut-preview-card'))
  const shownPage = () => pageArticles().filter(el => el.style.display !== 'none').map(el => el.id)
  assert.deepEqual(shownPage(), ['template-reference-page-0'], 'one template page is shown at a time')

  const other = first.questionAnchors[1]
  await editor.tap(editor.anchors()[1], [60, 430])
  assert.equal(first.geometrySelection.target, other)
  for (const field of ['geometryQuestionNumber', 'drawingQuestionNumber', 'editQuestionNumber', 'newHandwritingQuestion', 'referenceQuestionNumber']) assert.equal(first[field], '2.2', field)
  assert.equal(first.dirty, false)
  assert.equal(editor.question('2.1').drawing_reference.teacher_confirmed, true)
  await create(editor, 'answer')
  assert.equal(editor.page.boxes.at(-1).options.inputValue, '2.2', 'the tool suggests the selected Q')
  assert.equal(first.answerBoxes[0].question_number, '2.2')
  const firstDraft = plain(first.answerBoxes)

  // The pager sits in the dialog's fixed footer.
  const footer = editor.dialog().find(el => el.tagName === 'el-slot' && el.attributes.name === 'footer')
  const pager = footer.byClass('tpl-page-tabs')
  assert.ok(pager, 'the page pager is part of the dialog footer')
  const current = pager.find(el => el.tagName === 'select' && el.attributes['aria-label'] === '当前模板页')
  assert.deepEqual(current.options.map(option => option.text), ['第 1 页 · 已调整', '第 2 页'])
  await editor.click(pager.byText('button', '下一页'))
  assert.equal(editor.setup.templatePreviewPageIndex(), 1)
  assert.deepEqual(shownPage(), ['template-reference-page-1'])
  assert.equal(current.value, '1')
  // A teacher edit on page 2 is a draft of its own.
  await editor.drag(editor.answerRegions()[0], [60, 90], [80, 110])
  const secondDraft = plain(second.answerBoxes)
  assert.notDeepEqual(secondDraft[0].bbox, [40, 100, 200, 250])
  assert.equal(second.dirty, true)
  await editor.click(pager.byText('button', '上一页'))
  assert.equal(editor.setup.templatePreviewPageIndex(), 0)
  assert.deepEqual(shownPage(), ['template-reference-page-0'])
  assert.deepEqual(scrolls, [
    { id: 'template-reference-page-1', options: { block: 'start' } },
    { id: 'template-reference-page-0', options: { block: 'start' } },
  ])
  assert.deepEqual(plain(first.answerBoxes), firstDraft)
  assert.equal(first.dirty, true)
  assert.equal(second.dirty, true)
  assert.deepEqual(plain(second.answerBoxes), secondDraft)
  assert.deepEqual(current.options.map(option => option.text), ['第 1 页 · 已调整', '第 2 页 · 已调整'])
  assert.deepEqual(toRaw(editor.setup.templateCutPreviewPages).map((item, index) => toRaw(item) === pagesBefore[index]), [true, true], 'the same page drafts, in order')
  // Picking a page that is not there does nothing.
  await choose(editor, current, '77')
  assert.equal(scrolls.length, 2)
  assert.deepEqual(shownPage(), ['template-reference-page-0'])
  assert.doesNotThrow(() => editor.setup.selectTemplatePreviewPage({ pageIndex: 77 }))
  await editor.settle()
  assert.equal(scrolls.length, 2, 'a missing DOM page is safe to ignore')
  assert.equal(editor.setup.templatePreviewPageIndex(), 0, 'unknown page falls back to an existing draft')
  assert.deepEqual(shownPage(), ['template-reference-page-0'])
})

test('deleting every box of a question marks it for removal at save; a new box keeps it', async (t) => {
  const editor = await openGeometry(t)
  const question = editor.question('2.1')
  const removal = () => editor.dialog().find(el => el.tagName === 'section' && el.attributes['aria-label'] === '保存时删除的题目')
  await create(editor, 'answer')
  await editor.click(editor.deleteButton())
  assert.equal(question._pendingRemoval, true, 'the question number disappears with its last box')
  assert.match(removal().text, /第 2\.1 题的作答框和画图区已全部删除，保存模板时将删除这道题/)
  assert.equal(question.question_text, '合成题干', 'nothing is lost until the template is saved')
  await create(editor, 'answer')
  assert.equal(question._pendingRemoval, undefined, 're-framing a position keeps the question')
  assert.equal(removal(), null)
})
