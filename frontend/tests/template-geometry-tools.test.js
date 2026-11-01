import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { defaultTemplateDrawingLabel, serializeTemplateDrawingReview, templateQuestionMetadata } from '../src/utils/templateReview.js'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const plain = (value) => JSON.parse(JSON.stringify(value))
function functionCode(name) {
  const start = source.indexOf(`function ${name}(`)
  assert.ok(start >= 0, name)
  const tail = source.slice(start)
  const boundary = tail.slice(1).search(/\n(?:async )?function |\nconst |\nwatch\(/)
  return boundary < 0 ? tail : tail.slice(0, boundary + 1)
}
function fixture() {
  const question = { question_no: '2.1', question_text: '合成题干', standard_answer: '合成答案', drawing_reference: { teacher_confirmed: true, variants: [] } }
  const page = {
    pageIndex: 0, dirty: false, previewEditMode: 'geometry', geometryTool: '', geometryDraft: null,
    geometryQuestionNumber: '2.1', drawingQuestionNumber: '2.1', geometrySelection: null,
    columns: [{ column_index: 1, x1_1000: 0, x2_1000: 500 }, { column_index: 2, x1_1000: 500, x2_1000: 1000 }], columnCount: 2,
    questionAnchors: [{ question_number: '2.1', question_number_bbox: [30, 100, 70, 150], directory_node_id: 'synthetic-leaf' }],
    questionSchema: [{ question_number: '2.1', answer_kind: 'drawing', printed_path: ['二', '1'], directory_node_id: 'synthetic-leaf' }],
    handwritingBoxes: [], answerBoxes: [], drawingBoxes: [], drawingLabels: [],
  }
  const warnings = [], infos = []
  const scrollRequests = [], tickRequests = []
  const sandbox = {
    templateEditorBusy: { value: false }, templateCropGesture: { value: null },
    templatePages: { value: [{ page_index: 0, questions: [question] }] },
    templateCutPreviewPages: { value: [page] }, templateActivePreviewPage: { value: 0 },
    nextTick: (callback) => { tickRequests.push(true); callback?.(); return Promise.resolve() },
    document: { getElementById: (id) => /^template-reference-page-[01]$/.test(id) ? { scrollIntoView: (options) => scrollRequests.push({ id, options: plain(options) }) } : null },
    templateReferenceEditorInstances: new Map(), ElMessage: { warning: (text) => warnings.push(text), info: (text) => infos.push(text) },
    templateQuestionMetadata, defaultTemplateDrawingLabel,
  }
  vm.createContext(sandbox)
  const constants = source.slice(source.indexOf('const TEMPLATE_GEOMETRY_TOOLS ='), source.indexOf('const templateEditorBusy ='))
  const tools = source.slice(source.indexOf('function templatePageEditingLocked('), source.indexOf('function templatePageNeedsSave('))
  vm.runInContext(constants + ['normalizeTemplateBBoxForPage', 'markTemplateGeometryConfirmed', 'templateQuestionAnchorBBox', 'templateHasDrawingQuestion', 'templateVisibleAnswerBoxes', 'setTemplatePreviewEditMode', 'setTemplateDrawingKind'].map(functionCode).join('\n') + tools, sandbox)
  const stage = { clientWidth: 1000, clientHeight: 800, getBoundingClientRect: () => ({ left: 20, top: 10, width: 1000, height: 800 }) }
  const captures = []
  function pointer(x, y, extra = {}) {
    return { button: 0, pointerId: 7, clientX: x, clientY: y, preventDefault() {}, currentTarget: { closest: () => stage, setPointerCapture: (id) => captures.push(id) }, ...extra }
  }
  function create(kind, from = [120, 170], to = [320, 330], extra = {}) {
    sandbox.armTemplateGeometryTool(page, kind)
    sandbox.startTemplateGeometryCreation(pointer(...from, extra), page)
    sandbox.moveTemplateGeometry(pointer(...to, extra))
    sandbox.finishTemplateGeometry(pointer(...to, extra))
  }
  return { ...sandbox, page, question, warnings, infos, pointer, create, captures, stage, scrollRequests, tickRequests }
}

test('four toolbar categories create normalized full-page geometry with original Q identity', () => {
  for (const kind of ['question', 'answer', 'drawing', 'drawing-label']) {
    const h = fixture(), identity = plain(h.page.questionSchema)
    // The Q marker tool sits beside an existing drawing area.
    if (kind === 'drawing-label') h.page.drawingBoxes.push({ question_number: '2.1', bbox: [400, 400, 600, 600] })
    h.create(kind)
    const target = h.page.geometrySelection.target
    assert.equal(h.page.geometrySelection.kind, kind)
    assert.equal(target.question_number, '2.1')
    assert.deepEqual(plain(kind === 'drawing-label' ? target.label_anchor : kind === 'question' ? target.question_number_bbox : target.bbox), kind === 'drawing-label' ? [300, 400] : [100, 200, 300, 400])
    assert.equal(target.human_confirmed, true)
    assert.equal(target.confidence_source, 'teacher')
    assert.equal(h.page.dirty, true)
    assert.equal(h.page.geometryTool, '')
    assert.equal(h.page.geometryDraft, null)
    assert.equal(h.templateCropGesture.value, null)
    assert.deepEqual(plain(h.page.questionSchema), identity)
    assert.equal(h.question.question_text, '合成题干')
    assert.equal(h.question.standard_answer, '合成答案')
    if (kind === 'answer') assert.equal(h.page.hasAnswerRegionContract, true)
  }
})

test('choice/fill drawing classification, box creation and dragging never rewrite answer type or create a duplicate Q', () => {
  for (const question_type of ['choice', 'fill']) {
    for (const answer_kind of ['drawing', 'mixed']) {
      const h = fixture()
      const choice_mode = question_type === 'choice' ? 'single' : 'unknown'
      Object.assign(h.page.questionSchema[0], { question_type, choice_mode, answer_kind: 'non_graphic' })
      Object.assign(h.question, { question_type, choice_mode, standard_answer: question_type === 'choice' ? 'B' : '4' })
      const originalAnswer = h.question.standard_answer
      h.setTemplateDrawingKind(h.page, answer_kind)
      h.create('drawing')
      h.create('drawing-label', [120, 170], [120, 170])
      const box = h.page.drawingBoxes[0]
      const before = plain(box.bbox)
      h.startTemplateGeometryGesture(h.pointer(120, 170), h.page, box, 'drawing-move')
      h.moveTemplateGeometry(h.pointer(220, 250))
      h.finishTemplateGeometry(h.pointer(220, 250))
      assert.notDeepEqual(plain(box.bbox), before)
      h.page.answerBoxes.push({ question_number: '2.1', geometry_kind: 'label_anchor', bbox: [100, 200, 110, 210] })
      assert.equal(h.templateVisibleAnswerBoxes(h.page).length, 0)
      assert.equal(h.page.drawingLabels.length, 1)
      const saved = serializeTemplateDrawingReview(h.page.questionSchema, h.page.drawingBoxes, h.page.drawingLabels)[0]
      for (const item of [h.question, h.page.questionSchema[0], saved]) {
        assert.equal(item.question_type, question_type)
        assert.equal(item.choice_mode, choice_mode)
        assert.equal(item.answer_kind, answer_kind)
      }
      assert.equal(h.question.standard_answer, originalAnswer)
      assert.equal(h.question.drawing_reference.teacher_confirmed, false, 'geometry change still requires teacher reference review')
      assert.deepEqual(saved.drawing_regions, [plain(box.bbox)])
      assert.deepEqual(saved.label_anchor, plain(h.page.drawingLabels[0].label_anchor))
    }
  }
})

test('labels follow boxes: moving the main drawing area carries its Q marker, cancel restores both', () => {
  const h = fixture()
  h.page.drawingBoxes.push({ question_number: '2.1', bbox: [400, 400, 600, 600] }, { question_number: '2.1', bbox: [700, 400, 900, 600] })
  h.page.drawingLabels.push({ question_number: '2.1', label_anchor: [385, 500] })
  const [main, second] = h.page.drawingBoxes
  // Dragging the main area left past its marker used to strand it and block save.
  h.startTemplateGeometryGesture(h.pointer(520, 410), h.page, main, 'drawing-move')
  h.moveTemplateGeometry(h.pointer(320, 490))
  assert.deepEqual(plain(main.bbox), [200, 500, 400, 700])
  assert.deepEqual(plain(h.page.drawingLabels[0].label_anchor), [185, 600])
  h.cancelTemplateGeometry({ pointerId: 7 })
  assert.deepEqual(plain(main.bbox), [400, 400, 600, 600])
  assert.deepEqual(plain(h.page.drawingLabels[0].label_anchor), [385, 500])
  h.startTemplateGeometryGesture(h.pointer(520, 410), h.page, main, 'drawing-move')
  h.finishTemplateGeometry(h.pointer(320, 490))
  assert.deepEqual(plain(h.page.drawingLabels[0].label_anchor), [185, 600])
  // Another drawing area of the same question does not own the marker.
  h.startTemplateGeometryGesture(h.pointer(820, 410), h.page, second, 'drawing-move')
  h.finishTemplateGeometry(h.pointer(870, 410))
  assert.deepEqual(plain(second.bbox), [750, 400, 950, 600])
  assert.deepEqual(plain(h.page.drawingLabels[0].label_anchor), [185, 600])
  const saved = serializeTemplateDrawingReview(h.page.questionSchema, h.page.drawingBoxes, h.page.drawingLabels)[0]
  assert.deepEqual(saved.label_anchor, [185, 600])
})

test('reverse drag is normalized and endpoints stay within the page', () => {
  const reverse = fixture()
  reverse.create('answer', [520, 650], [220, 170])
  assert.deepEqual(plain(reverse.page.answerBoxes[0].bbox), [200, 200, 500, 800])
  const clamped = fixture()
  clamped.create('answer', [520, 410], [-900, 1900])
  assert.deepEqual(plain(clamped.page.answerBoxes[0].bbox), [0, 500, 500, 1000])
  const label = fixture()
  label.page.drawingBoxes.push({ question_number: '2.1', bbox: [400, 400, 600, 600] })
  label.create('drawing-label', [520, 410], [1900, -900])
  assert.deepEqual(plain(label.page.drawingLabels[0].label_anchor), [1000, 0])
})

test('tap and tiny accidental drags create no rectangle; drawing Q is deliberately a point tool', () => {
  for (const kind of ['handwriting', 'question', 'answer', 'drawing']) {
    for (const end of [[120, 170], [122, 172]]) {
      const h = fixture(), before = plain(h.page.questionAnchors)
      h.create(kind, [120, 170], end)
      assert.equal(h.page.dirty, false)
      assert.deepEqual(plain(h.page.questionAnchors), before)
      assert.equal(h.page.handwritingBoxes.length + h.page.answerBoxes.length + h.page.drawingBoxes.length, 0)
      assert.equal(h.page.geometrySelection, null)
    }
  }
  const h = fixture()
  h.page.drawingBoxes.push({ question_number: '2.1', bbox: [400, 400, 600, 600] })
  h.create('drawing-label', [120, 170], [120, 170])
  assert.deepEqual(plain(h.page.drawingLabels[0].label_anchor), [100, 200])
})

test('touch pointercancel never commits a new box or marks a question reference dirty', () => {
  const h = fixture()
  h.armTemplateGeometryTool(h.page, 'answer')
  h.startTemplateGeometryCreation(h.pointer(120, 170, { pointerType: 'touch' }), h.page)
  h.moveTemplateGeometry(h.pointer(320, 330, { pointerType: 'touch' }))
  h.cancelTemplateGeometry({ pointerId: 8 })
  assert.ok(h.templateCropGesture.value, 'different pointer cannot cancel this gesture')
  h.cancelTemplateGeometry({ pointerId: 7 })
  assert.equal(h.page.answerBoxes.length, 0)
  assert.equal(h.page.dirty, false)
  assert.equal(h.page.geometryDraft, null)
  assert.equal(h.question.drawing_reference.teacher_confirmed, true)
})

test('cancel restores moved, resized, label, and divider geometry with its original dirty state', () => {
  for (const kind of ['question', 'handwriting', 'answer', 'drawing', 'drawing-label', 'divider']) {
    const h = fixture()
    const target = kind === 'question' ? h.page.questionAnchors[0]
      : kind === 'drawing-label' ? { question_number: '2.1', label_anchor: [100, 200] }
        : { question_number: '2.1', bbox: [100, 200, 300, 400] }
    const before = plain(target), columns = plain(h.page.columns)
    if (kind === 'drawing-label') h.startTemplateDrawingLabelMove(h.pointer(120, 170), h.page, target)
    else if (kind === 'divider') h.startTemplateDividerMove(h.pointer(120, 170), h.page, 0)
    else h.startTemplateGeometryGesture(h.pointer(120, 170), h.page, target, `${kind}-resize`, -1, kind === 'question' ? 'question_number_bbox' : 'bbox')
    h.moveTemplateGeometry(h.pointer(250, 280, { pointerType: 'touch' }))
    assert.equal(h.page.dirty, true)
    h.cancelTemplateGeometry({ pointerId: 7 })
    assert.deepEqual(plain(target), before)
    assert.deepEqual(plain(h.page.columns), columns)
    assert.equal(h.page.dirty, false)
    assert.equal(h.question.drawing_reference.teacher_confirmed, true)
  }
})

test('click selection alone is not an edit and does not invalidate a confirmed reference', () => {
  const h = fixture(), anchor = h.page.questionAnchors[0]
  h.startTemplateQuestionMove(h.pointer(120, 170), h.page, anchor)
  h.finishTemplateGeometry(h.pointer(120, 170))
  assert.equal(h.page.geometrySelection.target, anchor)
  assert.equal(h.page.geometrySelection.kind, 'question')
  assert.equal(h.page.dirty, false)
  assert.equal(h.question.drawing_reference.teacher_confirmed, true)
  assert.equal(anchor.human_confirmed, undefined)
})

test('shared deletion only removes the selected marker and leaves question text, identity, and sibling boxes', () => {
  for (const kind of ['question', 'handwriting', 'answer', 'drawing-label']) {
    const h = fixture(), schema = plain(h.page.questionSchema)
    // The Q marker sits beside a drawing area that stays.
    if (kind === 'drawing-label') h.page.drawingBoxes.push({ question_number: '2.1', bbox: [400, 400, 600, 600] })
    h.create(kind)
    if (kind === 'question') {
      h.page.questionAnchors[0].question_bbox = [1, 2, 3, 4]
      h.page.questionAnchors[0].bbox = [1, 2, 3, 4]
    }
    h.deleteTemplateSelection(h.page)
    assert.equal(h.page.questionAnchors.length, 1)
    assert.equal(h.page.questionAnchors[0].question_number, '2.1')
    assert.equal(h.page.questionAnchors[0].directory_node_id, 'synthetic-leaf')
    assert.deepEqual(plain(h.page.questionSchema), schema)
    assert.equal(h.question.question_text, '合成题干')
    assert.equal(h.question.standard_answer, '合成答案')
    assert.equal(h.page.geometrySelection, null)
    if (kind === 'question') {
      assert.deepEqual(plain(h.page.questionAnchors[0].question_number_bbox), [])
      assert.equal(h.page.questionAnchors[0].question_bbox, undefined)
      assert.equal(h.page.questionAnchors[0].bbox, undefined)
    } else if (kind === 'drawing-label') {
      assert.equal(h.page.drawingLabels.length, 0)
      // Saving re-derives the marker beside the remaining drawing area.
      assert.deepEqual(plain(serializeTemplateDrawingReview(h.page.questionSchema, h.page.drawingBoxes, h.page.drawingLabels)[0].label_anchor), [385, 500])
    } else assert.equal(h.page.handwritingBoxes.length + h.page.answerBoxes.length + h.page.drawingBoxes.length + h.page.drawingLabels.length, 0)
  }
  // Deleting the last drawing area: its Q marker and standard figure go with
  // it and the question saves as non-drawing; text and identity stay.
  const drawn = fixture()
  drawn.create('drawing')
  assert.equal(drawn.page.drawingLabels.length, 1, 'a new drawing area gets its Q marker automatically')
  drawn.page.geometrySelection = { kind: 'drawing', target: drawn.page.drawingBoxes[0] }
  drawn.deleteTemplateSelection(drawn.page)
  assert.equal(drawn.page.drawingBoxes.length + drawn.page.drawingLabels.length, 0)
  assert.equal(drawn.page.questionSchema[0].answer_kind, 'non_graphic')
  assert.equal(drawn.page.questionSchema[0].directory_node_id, 'synthetic-leaf')
  assert.equal(drawn.question.drawing_reference, undefined)
  assert.equal(drawn.question.question_text, '合成题干')
  assert.equal(drawn.question.standard_answer, '合成答案')
  const h = fixture()
  h.page.geometrySelection = { kind: 'divider', index: 0, target: null }
  h.deleteTemplateSelection(h.page)
  assert.deepEqual(plain(h.page.columns), [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }])
  assert.equal(h.page.columnCount, 1)
})

test('reference stroke deletion delegates to the exposed editor without deleting a geometry marker', () => {
  const h = fixture(), calls = []
  h.page.referenceQuestionNumber = '2.1'
  h.setTemplateReferenceEditor(h.page, { question_no: '2.1' }, { deleteSelectedStroke() { calls.push('delete') }, clearSelection() { calls.push('clear') } })
  h.page.geometrySelection = { kind: 'question', target: h.page.questionAnchors[0] }
  h.setTemplateStrokeSelection(h.page, true)
  h.deleteTemplateSelection(h.page)
  assert.deepEqual(calls, ['delete'])
  assert.deepEqual(plain(h.page.questionAnchors[0].question_number_bbox), [30, 100, 70, 150])
  h.clearTemplateSelection(h.page)
  assert.deepEqual(calls, ['delete', 'clear'])
  assert.equal(h.page.referenceStrokeSelected, false)
})

test('existing save and global locks block new creation, dragging, deletion, and reference deletion', () => {
  for (const lock of ['global', 'saving']) {
    const h = fixture(), before = plain(h.page.questionAnchors)
    h.page.geometryTool = 'answer'
    h.page.geometrySelection = { kind: 'question', target: h.page.questionAnchors[0] }
    if (lock === 'global') h.templateEditorBusy.value = true
    else h.page[lock] = true
    h.startTemplateGeometryCreation(h.pointer(120, 170), h.page)
    h.armTemplateGeometryTool(h.page, 'handwriting')
    h.startTemplateQuestionMove(h.pointer(120, 170), h.page, h.page.questionAnchors[0])
    h.deleteTemplateSelection(h.page)
    assert.equal(h.templateCropGesture.value, null)
    assert.equal(h.page.geometryTool, 'answer')
    assert.deepEqual(plain(h.page.questionAnchors), before)
    let deletes = 0
    h.setTemplateReferenceEditor(h.page, { deleteSelectedStroke() { deletes++ } })
    h.page.referenceStrokeSelected = true
    h.deleteTemplateSelection(h.page)
    assert.equal(deletes, 0)
  }
})

test('a lock arriving mid-gesture rolls back geometry rather than committing during save', () => {
  for (const type of ['create', 'move']) {
    const h = fixture(), before = plain(h.page.questionAnchors)
    if (type === 'create') {
      h.armTemplateGeometryTool(h.page, 'answer')
      h.startTemplateGeometryCreation(h.pointer(120, 170), h.page)
    } else h.startTemplateQuestionMove(h.pointer(120, 170), h.page, h.page.questionAnchors[0])
    h.moveTemplateGeometry(h.pointer(320, 330))
    h.templateEditorBusy.value = true
    h.moveTemplateGeometry(h.pointer(400, 410))
    h.finishTemplateGeometry(h.pointer(420, 430))
    assert.deepEqual(plain(h.page.questionAnchors), before)
    assert.equal(h.page.answerBoxes.length, 0)
    assert.equal(h.page.dirty, false)
    assert.equal(h.question.drawing_reference.teacher_confirmed, true)
    assert.equal(h.templateCropGesture.value, null)
  }
})

test('a question can retain multiple boxes, while the ninth box is refused without replacing siblings', () => {
  for (const kind of ['answer', 'drawing']) {
    const h = fixture()
    const boxes = { answer: h.page.answerBoxes, drawing: h.page.drawingBoxes }[kind]
    for (let index = 0; index < 8; index++) h.create(kind, [120 + index * 10, 170], [320 + index * 10, 330])
    assert.equal(boxes.length, 8)
    const before = plain(boxes)
    h.create(kind)
    assert.equal(boxes.length, 8)
    assert.deepEqual(plain(boxes), before)
    assert.match(h.warnings.at(-1), /最多添加 8/)
    h.page.geometrySelection = { kind, target: boxes[3] }
    h.deleteTemplateSelection(h.page)
    assert.deepEqual(plain(boxes), before.filter((_, index) => index !== 3))
  }
})

// 重叠层选择器已按教师标注移除（点击即选择）；原选择 API 一并删除。

test('selecting another Q updates ownership without dirtying it, and fixed footer pagination retains both drafts', () => {
  const h = fixture()
  const other = { question_number: '2.2', question_number_bbox: [30, 500, 70, 550] }
  h.page.questionAnchors.push(other)
  h.page.questionSchema.push({ question_number: '2.2', answer_kind: 'drawing', directory_node_id: 'synthetic-second-leaf' })
  h.startTemplateQuestionMove(h.pointer(120, 170), h.page, other)
  h.finishTemplateGeometry(h.pointer(120, 170))
  for (const field of ['geometryQuestionNumber', 'drawingQuestionNumber', 'editQuestionNumber', 'newHandwritingQuestion', 'referenceQuestionNumber']) assert.equal(h.page[field], '2.2', field)
  assert.equal(h.page.dirty, false)
  assert.equal(h.question.drawing_reference.teacher_confirmed, true)
  h.create('answer')
  assert.equal(h.page.answerBoxes[0].question_number, '2.2')
  const firstDraft = plain(h.page.answerBoxes)
  const second = { ...plain(h.page), pageIndex: 1, handwritingBoxes: [{ question_number: '3', bbox: [40, 100, 200, 250] }], answerBoxes: [], geometrySelection: null, dirty: true }
  h.templateCutPreviewPages.value.push(second)
  h.selectTemplatePreviewPage(second)
  assert.equal(h.templatePreviewPageIndex(), 1)
  h.selectTemplatePreviewPage(h.page)
  assert.equal(h.templatePreviewPageIndex(), 0)
  assert.deepEqual(h.scrollRequests, [
    { id: 'template-reference-page-1', options: { block: 'start' } },
    { id: 'template-reference-page-0', options: { block: 'start' } },
  ])
  assert.equal(h.tickRequests.length, 2)
  assert.deepEqual(plain(h.page.answerBoxes), firstDraft)
  assert.equal(h.page.dirty, true)
  assert.equal(second.dirty, true)
  assert.deepEqual(plain(second.handwritingBoxes), [{ question_number: '3', bbox: [40, 100, 200, 250] }])
  assert.equal(h.templateCutPreviewPages.value[0], h.page)
  assert.equal(h.templateCutPreviewPages.value[1], second)
  assert.match(source, /<article v-for="page in templateCutPreviewPages" v-show="templatePreviewPageIndex\(\) === Number\(page.pageIndex\)"/)
  assert.match(source, /<template #footer>\s*<nav[^\n]*class="tpl-page-tabs"/)
  assert.match(source, /aria-label="当前模板页"/)
  assert.doesNotThrow(() => h.selectTemplatePreviewPage(undefined))
  assert.equal(h.tickRequests.length, 2)
  assert.doesNotThrow(() => h.selectTemplatePreviewPage({ pageIndex: 77 }))
  assert.equal(h.scrollRequests.length, 2, 'a missing DOM page is safe to ignore')
  assert.equal(h.templatePreviewPageIndex(), 0, 'unknown page falls back to an existing draft')
})

test('deleting every box of a question marks it for removal at save; a new box keeps it', () => {
  const h = fixture()
  h.create('answer')
  h.deleteTemplateSelection(h.page)
  assert.equal(h.question._pendingRemoval, true, 'the question number disappears with its last box')
  assert.equal(h.question.question_text, '合成题干', 'nothing is lost until the template is saved')
  h.create('answer')
  assert.equal(h.question._pendingRemoval, undefined, 're-framing a position keeps the question')
})
