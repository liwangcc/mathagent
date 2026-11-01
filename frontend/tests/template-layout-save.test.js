import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { prepareTemplateSlots, serializeTemplateSlots, templateSlotBoxes } from '../src/utils/templateSlots.js'
import { createErrorNotifier } from '../src/utils/processingError.js'
import { isTemplateDirectoryLeaf, serializeTemplateDrawingReview, templateDrawingReference, templateQuestionMetadata, templateFillAnswerSlots, templateMissingAnswerNotice, templateMaxFlagNotice, templateIsPureDrawing, templateBoxSizeProblems, templateReferenceLeavesRegions, templateChoiceConflictReasons, templateChoiceConflictReason } from '../src/utils/templateReview.js'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const clone = (value) => JSON.parse(JSON.stringify(value))
const ref = (value) => ({ value })
const functionNames = [
  'initializeTemplatePageSlots', 'templatePageNeedsSave',
  'loadTemplateCorrectionCandidates', 'templatePreSaveBlockers', 'warnTemplateSaveBlockers',
  'templateEditorProblems', 'templatePageSaveBoxes', 'templateDrawingReferenceRegionsFor', 'templateAnswerQuestion',
  'saveAllTemplateLayoutPages', 'saveTemplateLayoutPage',
  'onConfirmTemplate', 'saveAndConfirmTemplateDraft', 'removePendingTemplateQuestions',
  'answerSpecFields', 'templateVisibleAnswerBoxes',
  'templateDrawingReferenceQuestions', 'templateReferenceImagesReady',
  'templateLayoutConflictNotice',
]
const functions = functionNames.map((name) => {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))
  assert.ok(match, `${name} must be present in Upload.vue`)
  return match[0]
}).join('\n')

function page(index, overrides = {}) {
  const number = String(index + 1)
  return {
    pageIndex: index, pageLabel: `page_${index + 1}`, dirty: true,
    columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }],
    columnsSource: 'teacher', columnsConfirmed: true,
    questionAnchors: [{ question_number: number }],
    questionSchema: [{ question_number: number, question_type: 'solve', answer_kind: 'drawing', drawing_regions: [[100, 200, 600, 700]] }],
    drawingBoxes: [{ question_number: number, bbox: [100, 200, 600, 700] }],
    drawingLabels: [{ question_number: number, label_anchor: [90, 250] }],
    answerBoxes: [], handwritingBoxes: [], hasAnswerRegionContract: false,
    referenceImageReady: true, referenceImageReadyNumbers: { [number]: true }, saving: false,
    ...overrides,
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function harness(options = {}) {
  const pages = options.pages || [page(0), page(1)]
  const messages = []
  const calls = []
  const timers = new Map()
  const templatePages = ref(pages.map((item) => ({
    page_index: item.pageIndex,
    questions: [{
      question_no: String(item.pageIndex + 1), question_text: 'teacher problem', standard_answer: 'teacher answer',
      answer_kind: 'drawing', drawing_regions: [[100, 200, 600, 700]],
      drawing_reference: { comparison_mode: 'fixed', variants: [], teacher_confirmed: true },
    }],
  })))
  const deps = {
    pendingTemplateId: ref('template-one'), templateCutPreviewPages: ref(pages), templatePages,
    templateCutResult: ref({ pages: pages.map((item) => ({ page_index: item.pageIndex })), geometry_review_pending_pages: [] }),
    templateMode: ref('blank'), templateColumnMode: ref('single'),
    confirmingTemplate: ref(false), importingTemplateBank: ref(false),
    templateQuestionEditing: ref(false), templateCutBusy: ref(false), templateDialogVisible: ref(true),
    confirmedTemplateId: ref(''), templatePageCount: ref(0), templateCropGesture: ref(null),
    templateCanConfirm: ref(true), templateChoiceConflicts: ref([]), templateCorrectionCandidatesLoading: ref(false),
    templateReferenceEditorInstances: new Map(),
    templateGeometryReview: ref({ layoutReviewPendingPages: [], missingQuestionNumbers: [], duplicateQuestionNumbers: [], orphanQuestionNumbers: [], outOfBoundsHandwritingQuestions: [], lowConfidenceQuestionNumbers: [], lowConfidenceHandwritingQuestions: [], diagnosticReasons: [] }),
    form: { category: 'daily', exam_name: '' },
    ElMessage: Object.fromEntries(['error', 'warning', 'success'].map((kind) => [kind, (text) => messages.push({ kind, text: typeof text === 'string' ? text : text.message })])),
    notifyProcessingError: createErrorNotifier(text => messages.push({ kind: 'error', text })),
    window: { setTimeout: (fn, ms) => { const id = timers.size + 1; timers.set(id, { fn, ms }); return id }, clearTimeout: (id) => timers.delete(id) },
    TEMPLATE_REVIEW_PENDING_PAGE_KEYS: ['layout_review_pending_pages', 'anchor_review_pending_pages', 'geometry_review_pending_pages'],
    cloneQuestionAnchors: clone, cloneTemplateColumns: clone,
    normalizeTemplateMode: (mode) => mode, normalizeTemplateColumnMode: (mode) => mode,
    serializeTemplateHandwritingRegions: (boxes, anchors) => anchors.map((anchor) => ({ question_number: anchor.question_number, regions: boxes.filter((box) => box.question_number === anchor.question_number).map((box) => clone(box.bbox)) })),
    prepareTemplateSlots, serializeTemplateSlots, templateSlotBoxes,
    serializeTemplateDrawingReview, templateQuestionMetadata, templateDrawingReference, isTemplateDirectoryLeaf, templateFillAnswerSlots, templateMissingAnswerNotice, templateMaxFlagNotice, templateIsPureDrawing,
    templateBoxSizeProblems, templateReferenceLeavesRegions, templateChoiceConflictReasons, templateChoiceConflictReason,
    questionScoringValid: () => true,
    toStringList: (value) => Array.isArray(value) ? value : String(value || '').split('\n').filter(Boolean),
    defaultGradingMethod: () => 'process',
    applyTemplateDrawingSchema: (target, schema) => { target.questionSchema = clone(schema) },
    flattenTemplateHandwritingRegions: (items) => items.flatMap((item) => (item.regions || []).map((bbox) => ({ question_number: item.question_number, bbox }))),
    markTemplateGeometryConfirmed: (item) => ({ ...item, human_confirmed: true }),
    clearTemplateResolvedGeometryDiagnostics() {},
    applyTemplateBatchScores() {},
    forgetTemplateRecognition() {},
    templateDrawingReferenceSaveSnapshot: () => new Map(), clearSavedTemplateDrawingReferenceChanges() {},
    templateQuestionSignature: () => 'same-text-signature',
    drawingReferenceError: () => '', requireTemplateService: () => true,
    templateDrawingReferenceSnapshot: (question) => JSON.stringify(question),
    updateDrawingReference: (question, reference) => { question.drawing_reference = clone(reference) },
  }
  deps.templateReviewPendingPageIndexes = () => new Set(deps.templateCutResult.value?.geometry_review_pending_pages || [])
  deps.templateDiagnosticPendingPageIndexes = () => new Set()
  const response = (payload, extra = {}) => ({ success: true, template_id: 'template-one', ...clone(payload), ...extra })
  deps.api = { grade: {
    templateCorrectionCandidates: async () => {
      calls.push({ stage: 'corrections' })
      if (options.corrections) return options.corrections()
      throw new Error('Save must not require correction discovery')
    },
    templateLayoutOverrides: async (id, payload) => {
      calls.push({ stage: 'layout', id, payload: clone(payload), locked: deps.confirmingTemplate.value })
      return options.layout ? options.layout(id, payload, deps) : response(payload)
    },
    templateConfirm: async (payload, signal) => {
      calls.push({ stage: 'confirm', payload: clone(payload), signal, locked: deps.confirmingTemplate.value })
      return options.confirm ? options.confirm(payload, signal, deps) : { success: true, page_count: pages.length, template_mode: 'blank', column_mode: 'single' }
    },

  } }
  const methods = new Function(...Object.keys(deps), `
    let templateConfirmController = null
    let templateCorrectionCandidatesGeneration = 0
    ${functions}
    return { ${functionNames.join(', ')} }
  `)(...Object.values(deps))
  return { ...deps, ...methods, calls, messages, timers, pages, response }
}

test('page save predicate includes unsaved edits and server-only review, not clean pages', () => {
  const h = harness({ pages: [page(0, { dirty: false })] })
  assert.equal(h.templatePageNeedsSave(h.pages[0]), false)
  h.templateCutResult.value.geometry_review_pending_pages = [0]
  assert.equal(h.templatePageNeedsSave(h.pages[0]), true)
  for (const field of ['dirty', 'layoutReviewPending', 'anchorReviewPending', 'geometryReviewPending', 'geometry_review_pending']) {
    h.templateCutResult.value.geometry_review_pending_pages = []
    assert.equal(h.templatePageNeedsSave(page(0, { dirty: false, [field]: true })), true)
  }
})

test('a generated draft stays editable while missing answers block final confirmation', async () => {
  const h = harness({ pages: [page(0)] })
  Object.assign(h.templatePages.value[0].questions[0], {
    question_no: '1', blank_count: 3, slot_ids: ['1[1]', '1[2]', '1[3]'],
    region_kinds: ['answer', 'answer', 'answer'], answer_slots: ['6', '', ''],
    answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null,
  })
  assert.equal(await h.onConfirmTemplate(), false)
  assert.equal(h.templateDialogVisible.value, true)
  assert.deepEqual(h.calls, [])
  assert.match(h.messages.at(-1).text, /第 1 题有 2 个作答区域未设置标准答案/)
  h.templatePages.value[0].questions[0].answer_slots = ['6', '7', '8']
  assert.deepEqual(h.templatePreSaveBlockers(), [])
})

test('one save attempt names every blocker together and writes nothing', async () => {
  const h = harness({ pages: [page(0)] })
  // A model box over a quarter of the page, a missing stem and a Max report.
  h.pages[0].drawingBoxes[0].bbox = [40, 60, 900, 700]
  Object.assign(h.templatePages.value[0].questions[0], { question_text: '', max_flags: [{ status: 'extra', reason: '合成' }] })
  const problems = h.templateEditorProblems()
  assert.deepEqual(problems.map((problem) => problem.box), [h.pages[0].drawingBoxes[0], undefined])
  assert.equal(await h.onConfirmTemplate(), false)
  assert.deepEqual(h.calls, [])
  assert.equal(h.messages.length, 1)
  const text = h.messages[0].text
  assert.match(text, /^保存前还有 3 处需要处理/)
  for (const part of [/Max 判定整题为目录多余项/, /Q1 的画图区超过整页的四分之一/, /第 1 页第 1 题缺少题干/]) assert.match(text, part)
  // Fixing all three in the editor lets the very next save go through.
  h.pages[0].drawingBoxes[0].bbox = [100, 200, 600, 700]
  Object.assign(h.templatePages.value[0].questions[0], { question_text: 'teacher problem', max_flags_kept: ['*'] })
  assert.deepEqual(h.templatePreSaveBlockers(), [])
  assert.equal(await h.onConfirmTemplate(), true)
})

test('a question due for removal raises no geometry conflict before it leaves', () => {
  // The teacher deleted every box of Q1, then switched it to 作图题: the save
  // removes Q1 first, so its missing drawing area must not block the save.
  const h = harness({ pages: [page(0)] })
  Object.assign(h.pages[0].questionSchema[0], { drawing_regions: [], label_anchor: null })
  h.pages[0].drawingBoxes = []
  h.pages[0].drawingLabels = []
  h.pages[0].geometryReviewPending = true
  assert.match(h.templateLayoutConflictNotice(h.pages[0]), /Q1 是作图题，但还没有画图区/)
  h.templatePages.value[0].questions[0]._pendingRemoval = true
  assert.equal(h.templateLayoutConflictNotice(h.pages[0]), '')
  assert.ok(!h.templatePreSaveBlockers().some((text) => /还没有画图区/.test(text)))
})

test('a kept position still waiting for its box is named with the other blockers', async () => {
  const h = harness({ pages: [page(0)] })
  Object.assign(h.pages[0].questionSchema[0], { answer_kind: 'non_graphic', slot_ids: ['1[1]', '1[2]'], region_kinds: ['answer', 'answer'], drawing_regions: [] })
  h.pages[0].drawingBoxes = []
  h.pages[0].drawingLabels = []
  h.pages[0].answerBoxes = [
    { question_number: '1', slot_id: '1[1]', bbox: [100, 200, 300, 260] },
    { question_number: '1', slot_id: '1[2]', bbox: [] },
  ]
  Object.assign(h.templatePages.value[0].questions[0], { question_text: '' })
  assert.equal(await h.onConfirmTemplate(), false)
  assert.deepEqual(h.calls, [])
  assert.match(h.messages.at(-1).text, /Q1\[2\] 还没有框选位置/)
  assert.match(h.messages.at(-1).text, /第 1 页第 1 题缺少题干/)
})

test('a standard figure outside its drawing area is named before anything is saved', async () => {
  const h = harness({ pages: [page(0)] })
  h.templatePages.value[0].questions[0].drawing_reference = {
    comparison_mode: 'fixed', teacher_confirmed: true,
    variants: [{ strokes: [{ kind: 'polyline', points: [[150, 250], [800, 250]] }] }],
  }
  assert.equal(await h.onConfirmTemplate(), false)
  assert.deepEqual(h.calls, [])
  assert.match(h.messages.at(-1).text, /第 1 页第 1 题：标准图有笔画画在画图区外/)
})

test('model geometry conflicts are shown to the teacher before any layout request', async () => {
  assert.match(source, /v-if="templateLayoutConflictNotice\(page\)"/)
  assert.match(source, /setTemplateAnswerDrawingKind\(page\.page_index, q, \$event\.target\.value\)/)
  assert.match(source, /<option value="drawing">作图题<\/option>/)
  const h = harness({ pages: [page(0)] })
  const current = h.pages[0]
  current.geometryReviewPending = true
  current.questionSchema[0] = {
    question_number: '5', answer_kind: 'non_graphic',
    drawing_regions: [[120, 180, 720, 800]], label_anchor: [90, 220],
  }
  const answer = h.templatePages.value[0].questions[0]
  answer.standard_answer = '山顶1200m'
  assert.match(h.templateLayoutConflictNotice(current), /Q5 是非作图题.*画图区或作图题号/)
  assert.equal(await h.saveTemplateLayoutPage(current, { silent: true }), false)
  assert.deepEqual(h.calls, [])
  assert.equal(answer.standard_answer, '山顶1200m')
  assert.equal(current.dirty, true)
  assert.match(h.messages.at(-1).text, /修改已保留/)
})

test('a graphic candidate without complete geometry stays unresolved until the teacher fixes it', async () => {
  const h = harness({ pages: [page(0)] })
  h.pages[0].geometryReviewPending = true
  h.pages[0].questionSchema[0] = {
    question_number: '6', answer_kind: 'drawing', drawing_regions: [], label_anchor: null,
  }
  assert.match(h.templateLayoutConflictNotice(h.pages[0]), /Q6 是作图题，但还没有画图区.*绿色画图区/)
  assert.equal(await h.saveTemplateLayoutPage(h.pages[0], { silent: true }), false)
  assert.deepEqual(h.calls, [])
})

test('one final action saves every dirty page and current answers without extra model or correction requests', async () => {
  const h = harness({ pages: [page(0), page(1, { dirty: false }), page(2)] })
  assert.equal(await h.onConfirmTemplate(), true)
  assert.deepEqual(h.calls.map((item) => item.stage), ['layout', 'layout', 'confirm'])
  assert.deepEqual(h.calls.filter((item) => item.stage === 'layout').map((item) => item.payload.page_index), [0, 2])
  assert.ok(h.calls.filter((item) => ['layout', 'answers', 'confirm'].includes(item.stage)).every((item) => item.locked))
  assert.ok(h.pages.every((item) => item.dirty === false))
  assert.equal(h.confirmingTemplate.value, false)
  assert.equal(h.templateDialogVisible.value, false)
  assert.equal(h.messages.filter((item) => item.kind === 'success').length, 1)
})

test('a failed page aborts final confirmation and preserves all unsaved drafts for retry', async () => {
  let failing = true
  let h
  h = harness({ pages: [page(0), page(1), page(2)], layout: async (_id, payload) => {
    if (payload.page_index === 1 && failing) throw new Error('temporary unavailable')
    return h.response(payload)
  } })
  const draft = h.templatePages.value[1].questions[0]
  assert.equal(await h.onConfirmTemplate(), false)
  assert.deepEqual(h.calls.map((item) => item.stage), ['layout', 'layout'])
  assert.deepEqual(h.pages.map((item) => item.dirty), [false, true, true])
  assert.equal(h.templatePages.value[1].questions[0], draft)
  assert.equal(draft.standard_answer, 'teacher answer')
  assert.equal(h.templateDialogVisible.value, true)
  assert.equal(h.confirmingTemplate.value, false)
  assert.match(h.messages.at(-1).text, /第 2 页.*修改已保留/)
  failing = false
  h.calls.length = 0
  assert.equal(await h.onConfirmTemplate(), true)
  assert.deepEqual(h.calls.filter((item) => item.stage === 'layout').map((item) => item.payload.page_index), [1, 2])
})

test('persisting reviewed geometry does not clear the teacher standard-figure checkbox', async () => {
  const h = harness({ pages: [page(0)] })
  assert.equal(await h.onConfirmTemplate(), true)
  const request = h.calls.find((item) => item.stage === 'confirm')
  assert.equal(request.payload.pages[0].questions[0].drawing_reference.teacher_confirmed, true)
})

test('ordinary layout responses preserve the teacher answer type and choice mode instead of replaying the old OCR type', async () => {
  for (const edited of [{ question_type: 'fill', choice_mode: 'unknown' }, { question_type: 'choice', choice_mode: 'multiple' }]) {
    const h = harness({ pages: [page(0)] })
    Object.assign(h.pages[0].questionSchema[0], { answer_kind: 'non_graphic', question_type: 'choice', choice_mode: 'single' })
    const question = h.templatePages.value[0].questions[0]
    Object.assign(question, edited, { answer_kind: 'non_graphic', _teacherAnswerEdited: true })
    assert.equal(await h.saveTemplateLayoutPage(h.pages[0], { silent: true }), true)
    assert.equal(question.question_type, edited.question_type)
    assert.equal(question.choice_mode, edited.choice_mode)
    assert.equal(question.standard_answer, 'teacher answer')
    assert.equal(question._teacherAnswerEdited, true)
  }
})

test('choice and fill drawings retain answer type through geometry save and manual/fixed final confirmation', async () => {
  for (const question_type of ['choice', 'fill']) {
    for (const answer_kind of ['drawing', 'mixed']) {
      for (const comparison_mode of ['manual', 'fixed']) {
        const h = harness({ pages: [page(0)] })
        const choice_mode = question_type === 'choice' ? 'multiple' : 'unknown'
        const question = h.templatePages.value[0].questions[0]
        const reference = { version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode, variants: comparison_mode === 'fixed' ? [{ strokes: [{ kind: 'point', point: [300, 400] }] }] : [], teacher_confirmed: comparison_mode === 'fixed' }
        Object.assign(h.pages[0].questionSchema[0], { question_type, choice_mode, answer_kind })
        Object.assign(question, { question_type, choice_mode, answer_kind, drawing_reference: reference, _teacherAnswerEdited: true, standard_answer: question_type === 'choice' ? 'AC' : '4', blank_count: 1, answer_slots: ['4'] })
        assert.equal(await h.onConfirmTemplate(), true)
        const schema = h.calls.find((call) => call.stage === 'layout').payload.question_schema[0]
        const answer = h.calls.find((call) => call.stage === 'confirm').payload.pages[0].questions[0]
        for (const item of [question, schema, answer]) {
          assert.equal(item.question_type, question_type)
          assert.equal(item.choice_mode, choice_mode)
          assert.equal(item.answer_kind, answer_kind)
        }
        assert.equal(answer.standard_answer, question.standard_answer)
        assert.equal(question._teacherAnswerEdited, true)
        assert.deepEqual(answer.drawing_reference, reference)
        assert.equal(answer.drawing_reference.teacher_confirmed, comparison_mode === 'fixed')
      }
    }
  }
})

test('a stale page-save response cannot mutate a new session or clear the detached draft', async () => {
  const pending = deferred()
  const h = harness({ pages: [page(0)], layout: () => pending.promise })
  const oldPage = h.pages[0]
  const save = h.saveTemplateLayoutPage(oldPage)
  const payload = h.calls[0].payload
  h.pendingTemplateId.value = 'template-two'
  const replacement = page(0, { columnsSource: 'model' })
  h.templateCutPreviewPages.value = [replacement]
  pending.resolve(h.response(payload))
  assert.equal(await save, false)
  assert.equal(oldPage.dirty, true)
  assert.equal(oldPage.saving, false)
  assert.equal(replacement.columnsSource, 'model')
  assert.equal(h.messages.length, 0)
})

test('wrong-template and wrong-page layout receipts fail closed without consuming edits', async () => {
  for (const change of [{ template_id: 'another-template' }, { page_index: 7 }, { success: false }, { columns: [] }]) {
    let h
    h = harness({ pages: [page(0)], layout: async (_id, payload) => h.response(payload, change) })
    assert.equal(await h.saveTemplateLayoutPage(h.pages[0]), false)
    assert.equal(h.pages[0].dirty, true)
    assert.equal(h.pages[0].saving, false)
  }
})

test('missing columns and concurrent page activity stop saving without making an invalid request', async () => {
  for (const overrides of [{ columns: [] }, { saving: true }]) {
    const h = harness({ pages: [page(0, overrides)] })
    assert.equal(await h.onConfirmTemplate(), false)
    assert.equal(h.calls.length, 0)
    assert.equal(h.confirmingTemplate.value, false)
    assert.equal(h.templateDialogVisible.value, true)
  }
})

test('the confirmation lock is acquired before the first await so duplicate clicks do not duplicate writes', async () => {
  const pending = deferred()
  const h = harness({ pages: [page(0)], layout: () => pending.promise })
  const first = h.onConfirmTemplate()
  assert.equal(h.confirmingTemplate.value, true)
  assert.equal(await h.onConfirmTemplate(), false)
  assert.equal(h.calls.length, 1)
  pending.resolve(h.response(h.calls[0].payload))
  assert.equal(await first, true)
  assert.equal(h.calls.filter((item) => item.stage === 'confirm').length, 1)
})

test('failed final confirmation keeps the editor and answers open after successful layout persistence', async () => {
  const h = harness({ pages: [page(0)], confirm: async () => { throw new Error('retry confirmation') } })
  assert.equal(await h.onConfirmTemplate(), false)
  assert.equal(h.pages[0].dirty, false)
  assert.equal(h.templateDialogVisible.value, true)
  assert.equal(h.templatePages.value[0].questions[0].standard_answer, 'teacher answer')
  assert.equal(h.confirmedTemplateId.value, '')
  assert.equal(h.confirmingTemplate.value, false)
})

test('missing required content points to the exact page and question instead of an invisible approval step', async () => {
  for (const field of ['question_text', 'standard_answer']) {
    const h = harness({ pages: [page(0), page(1)] })
    h.templatePages.value[1].questions[0].answer_kind = 'non_graphic'
    h.templatePages.value[1].questions[0][field] = ''
    assert.equal(await h.onConfirmTemplate(), false)
    assert.equal(h.calls.length, 0)
    assert.match(h.messages.at(-1).text, /第 2 页第 2 题缺少/)
    assert.equal(h.templateDialogVisible.value, true)
    assert.equal(h.confirmingTemplate.value, false)
  }
})

test('one save flushes the visible standard figure before locking and includes its last stroke', async () => {
  const h = harness({ pages: [page(0)] })
  const question = h.templatePages.value[0].questions[0]
  h.templateReferenceEditorInstances.set('0:1', { prepareForSave() {
    assert.equal(h.confirmingTemplate.value, false)
    question.drawing_reference.variants = [{ strokes: [{ kind: 'polyline', points: [[100, 200], [300, 400]] }] }]
    return ''
  } })
  assert.equal(await h.onConfirmTemplate(), true)
  assert.deepEqual(h.calls.find((call) => call.stage === 'confirm').payload.pages[0].questions[0].drawing_reference.variants, question.drawing_reference.variants)
})

test('one save adopts the editor draft when the last v-model event is still pending', async () => {
  const h = harness({ pages: [page(0)] })
  const question = h.templatePages.value[0].questions[0]
  const visible = {
    version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1',
    comparison_mode: 'manual', variants: [{ strokes: [{ kind: 'point', point: [500, 450] }] }],
    context_regions: [], teacher_confirmed: false,
  }
  question.drawing_reference = null
  h.templateReferenceEditorInstances.set('0:1', {
    prepareForSave() { return '' },
    valueForSave() { return visible },
  })
  assert.equal(await h.onConfirmTemplate(), true)
  assert.deepEqual(h.calls.find((call) => call.stage === 'confirm').payload.pages[0].questions[0].drawing_reference, visible)
})

test('saving after a geometry change keeps the teacher-visible answer without a model call', async () => {
  const h = harness({ pages: [page(0)] })
  h.templatePages.value[0].questions[0].drawing_reference.teacher_confirmed = false
  assert.equal(await h.onConfirmTemplate(), true)
  assert.equal(h.calls.find((item) => item.stage === 'confirm').payload.pages[0].questions[0].standard_answer, 'teacher answer')
  assert.deepEqual(h.calls.map((item) => item.stage), ['layout', 'confirm'])
})

test('late correction responses cannot change the draft during or after its save', async () => {
  const pending = deferred()
  const h = harness({ pages: [page(0)], corrections: () => pending.promise })
  const request = h.loadTemplateCorrectionCandidates()
  assert.equal(await h.onConfirmTemplate(), true)
  pending.resolve({ candidates: [{ question_no: '1', candidates: [{ cycle_id: 99 }] }] })
  assert.equal(await request, false)
  const question = h.templatePages.value[0].questions[0]
  assert.equal(question.standard_answer, 'teacher answer')
  assert.equal(question.authorized_wrong_cycle_ids, undefined)
  assert.equal(h.calls.filter((call) => call.stage === 'confirm').length, 1)
})

test('the final validity gate remains independent of the in-flight UI lock', () => {
  const match = source.match(/const templateCanConfirm = computed\(\(\) => \{([^]*?)\n\}\)/)
  assert.ok(match)
  assert.doesNotMatch(match[1], /confirmingTemplate/)
  assert.doesNotMatch(match[1], /templateAnswerDraftStatus|_answerDraftStale/)
})
