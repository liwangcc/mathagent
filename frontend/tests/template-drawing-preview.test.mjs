import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { parse, compileTemplate } from '@vue/compiler-sfc'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const { descriptor, errors } = parse(source)
const start = source.indexOf('function updateDrawingReference(')
const end = source.indexOf('\nfunction inferSubquestionCount(', start)
const editorSource = readFileSync(new URL('../src/components/DrawingReferenceEditor.vue', import.meta.url), 'utf8')
const editorScript = parse(editorSource).descriptor.script.content
const referenceHelpers = editorScript.slice(editorScript.indexOf('const clone ='), editorScript.indexOf('export default')).replaceAll('export function ', 'function ')
const drawingReferenceError = new Function(`${referenceHelpers}\nreturn drawingReferenceError`)()
const candidate = () => ({ version: 'drawing-reference-v1', coordinate_space: 'normalized_1000_v1', comparison_mode: 'fixed', variants: [{ strokes: [{ kind: 'point', point: [800, 450] }] }], context_regions: [], teacher_confirmed: false })

function harness() {
  const question = { question_no: '2.3', answer_kind: 'drawing', drawing_reference: null, drawing_regions: [[600, 300, 900, 600]] }
  const page = { pageIndex: 0, dirty: false, columnsConfirmed: true, drawingBoxes: [{ question_number: '2.3', bbox: [600, 300, 900, 600] }], previewEditMode: 'geometry', geometrySelection: null, referenceStrokeSelected: false }
  const templatePages = { value: [{ page_index: 0, questions: [{ question_no: '1', answer_kind: 'non_graphic' }, question] }, { page_index: 1, questions: [{ question_no: '2.3', answer_kind: 'mixed' }] }] }
  const templateCutPreviewPages = { value: [page] }
  const templateCropGesture = { value: null }
  const warnings = []
  const scrolled = []
  const cancelledGestures = []
  const timers = new Map()
  const pendingTemplateId = { value: 'template-one' }
  const templateQuestionEditing = { value: false }
  const confirmingTemplate = { value: false }
  const templateActivePreviewPage = { value: 0 }
  const templateEditorBusy = { get value() { return confirmingTemplate.value || templateQuestionEditing.value || templateCutPreviewPages.value.some((item) => item.saving) } }
  const supportFunctions = ['templatePageNeedsSave', 'templatePageEditingLocked'].map((name) => {
    const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))
    assert.ok(match)
    return match[0]
  }).join('\n')
  const names = ['updateDrawingReference', 'templateDrawingReferenceQuestions', 'templateReferenceCanvasVisible', 'templateReferenceEditors', 'templateDrawingReferenceRegionsFor', 'templateDrawingReferenceSummary', 'templateDrawingReferenceSnapshot', 'setTemplatePreviewEditMode', 'selectTemplateDrawingReference', 'openTemplateDrawingReference', 'templateReferenceQuestionEditBlockReason', 'templateDrawingReferenceSaveSnapshot', 'clearSavedTemplateDrawingReferenceChanges']
  const dependencies = {
    templatePages, templateCutPreviewPages, templateCropGesture, pendingTemplateId,
    drawingReferenceError, templateQuestionEditing, confirmingTemplate, templateEditorBusy, templateActivePreviewPage,
    requireTemplateService: () => true, templateReviewPendingPageIndexes: () => new Set(),
    templateDiagnosticPendingPageIndexes: () => new Set(),
    ElMessage: { warning: (value) => warnings.push(value), success() {} },
    document: { getElementById: (id) => ({ scrollIntoView: () => scrolled.push(id) }) },
    window: { setTimeout: (fn, ms) => { timers.set(1, { fn, ms }); return 1 }, clearTimeout: (key) => timers.delete(key) },
    cancelTemplateGeometry: (event) => { cancelledGestures.push(event.pointerId); templateCropGesture.value = null },
    clearTemplateSelection: (item) => { item.geometrySelection = null; item.referenceStrokeSelected = false },
  }
  const methods = new Function(...Object.keys(dependencies), `${supportFunctions}\n${source.slice(start, end)}\nreturn { ${names.join(', ')} }`)(...Object.values(dependencies))
  return { ...methods, question, page, templatePages, templateCutPreviewPages, templateCropGesture, warnings, scrolled, cancelledGestures, timers, pendingTemplateId, templateQuestionEditing, confirmingTemplate, templateEditorBusy, templateActivePreviewPage }
}

test('one shared whole-page canvas hosts geometry and standard reference; per-question cards do not repeat images', () => {
  assert.deepEqual(errors, [])
  assert.doesNotMatch(descriptor.template.content, /async function generateTemplateDrawingReference/)
  assert.doesNotMatch(descriptor.scriptSetup.content, /async function generateTemplateDrawingReference/)
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'Upload.vue', id: 'preview-test' }).errors, [])
  assert.equal((descriptor.template.content.match(/<DrawingReferenceEditor\b/g) || []).length, 1)
  assert.match(descriptor.template.content, /:priority-editing="!!q && page\.previewEditMode === 'reference'/)
  assert.match(descriptor.template.content, /'is-disabled': templatePageEditingLocked\(page\) \|\| \(page\.previewEditMode === 'reference'/)
  assert.match(descriptor.template.content, /<template #canvas-overlays>/)
  assert.match(descriptor.template.content, /canvas-class="tpl-cut-preview-stage"/)
  assert.match(descriptor.template.content, /:image-url="page\.imageUrl"/)
  assert.match(descriptor.template.content, /<template #geometry-tools>/)
  // 教师标注：默认状态即选择，几何工具行的「选择」按钮已移除。
  assert.doesNotMatch(descriptor.template.content, /armTemplateGeometryTool\(page, ''\)/)
  assert.match(descriptor.template.content, /@edit-request="q && selectTemplateDrawingReference\(page, q\.question_no\)"/)
  assert.match(descriptor.template.content, /:interactive="!!q && \(page\.previewEditMode === 'geometry' \|\|/)
  assert.match(descriptor.template.content, /到上方模板核对作图区域与标准图/)
  assert.doesNotMatch(descriptor.template.content, /@click="saveTemplateLayoutPage\(page\)"/)
  assert.match(descriptor.template.content, /统一保存所有页/)
  assert.match(descriptor.template.content, /保存模板（全部页）/)
})

test('exactly one shared template canvas is visible per review state', () => {
  const h = harness()
  const second = { question_no: '3', answer_kind: 'mixed', drawing_reference: null }
  h.templatePages.value[0].questions.push(second)
  // 几何模式：画布固定跟随第一道作图题，其余题目不再各渲染一张一模一样的模板。
  assert.equal(h.templateReferenceCanvasVisible(h.page, h.question), true)
  assert.equal(h.templateReferenceCanvasVisible(h.page, second), false)
  // 参考编辑模式：画布跟随当前编辑的题目，任何时刻只有一张可见。
  h.page.previewEditMode = 'reference'
  h.page.referenceQuestionNumber = '3'
  assert.equal(h.templateReferenceCanvasVisible(h.page, h.question), false)
  assert.equal(h.templateReferenceCanvasVisible(h.page, second), true)
  h.page.referenceQuestionNumber = '2.3'
  assert.equal(h.templateReferenceCanvasVisible(h.page, h.question), true)
  assert.equal(h.templateReferenceCanvasVisible(h.page, second), false)
})

test('pages without graphic questions still keep one template canvas for geometry review', () => {
  const h = harness()
  h.templatePages.value[0].questions = [{ question_no: '1', answer_kind: 'non_graphic' }]
  // 无作图题：仍需一个编辑器宿主承载原图与几何工具（q 为 null 的哨兵）。
  assert.deepEqual(h.templateReferenceEditors(h.page), [null])
  assert.equal(h.templateReferenceCanvasVisible(h.page, null), true)
  h.templatePages.value[0].questions.push(h.question)
  assert.deepEqual(h.templateReferenceEditors(h.page), [h.question])
})

test('graphic references bind by both page and original question number, including nested Q identifiers', () => {
  const h = harness()
  assert.deepEqual(h.templateDrawingReferenceQuestions(h.page), [h.question])
  assert.deepEqual(h.templateDrawingReferenceRegionsFor(h.page, h.question), [[600, 300, 900, 600]])
  h.page.referenceQuestionNumber = 'missing'
})

test('an unset standard figure remains distinct from a saved manual mode', () => {
  const h = harness()
  assert.match(h.templateDrawingReferenceSummary(h.question), /未设置标准图/)
  h.question.drawing_reference = { comparison_mode: 'manual', variants: [{ strokes: [{ kind: 'point', point: [800, 450] }] }] }
  assert.match(h.templateDrawingReferenceSummary(h.question), /自定义作图/)
  h.question.drawing_reference = { comparison_mode: 'manual', variants: [] }
  assert.match(h.templateDrawingReferenceSummary(h.question), /未保存标准笔画/)
  assert.match(h.templateDrawingReferenceSummary(h.question), /题干和作图区直接交给 Flash/)
})

test('reference edit does not mutate the other page', () => {
  const h = harness()
  const reference = { comparison_mode: 'fixed', variants: [], teacher_confirmed: false }
  h.updateDrawingReference(h.question, reference)
  assert.equal(h.question.drawing_reference, reference)
  assert.equal(h.templatePages.value[1].questions[0].drawing_reference, undefined)
  assert.doesNotThrow(() => h.updateDrawingReference(null, reference))
})

test('changing editor ownership clears old geometry gesture and cannot switch while generation saves its candidate', () => {
  const h = harness()
  h.templateCropGesture.value = { pointerId: 4 }
  h.page.geometrySelection = { kind: 'answer' }
  h.page.referenceStrokeSelected = true
  h.page.geometryTool = 'handwriting'
  h.selectTemplateDrawingReference(h.page, '2.3')
  assert.equal(h.templateCropGesture.value, null)
  assert.equal(h.page.previewEditMode, 'reference')
  assert.deepEqual(h.cancelledGestures, [4])
  assert.equal(h.page.geometrySelection, null)
  assert.equal(h.page.referenceStrokeSelected, false)
  assert.equal(h.page.geometryTool, '')
  h.page.saving = true
  h.setTemplatePreviewEditMode(h.page, 'geometry')
  assert.equal(h.page.previewEditMode, 'reference')
  h.selectTemplateDrawingReference(h.page, 'other')
  assert.equal(h.page.referenceQuestionNumber, '2.3')
  h.page.saving = false
  h.openTemplateDrawingReference(0, '2.3')
  assert.deepEqual(h.scrolled, ['template-reference-page-0'])
})

test('full-page snapshot binds geometry, problem, answer and reference for stale-response protection', () => {
  const h = harness()
  const before = h.templateDrawingReferenceSnapshot(h.question)
  h.question.drawing_regions[0][0] = 610
  assert.notEqual(h.templateDrawingReferenceSnapshot(h.question), before)
  h.question.drawing_regions[0][0] = 600
  assert.equal(h.templateDrawingReferenceSnapshot(h.question), before)
  h.question.standard_answer = 'teacher updated answer'
  assert.notEqual(h.templateDrawingReferenceSnapshot(h.question), before)
})

function ready(h) {
  Object.assign(h.question, { question_text: 'test printed problem', standard_answer: 'teacher checked answer' })
  return h
}

test('image readiness propagates to the final template confirmation gate, not only editor checkbox', () => {
  assert.match(descriptor.template.content, /@image-ready="q && markTemplateReferenceImageReady\(page, q\)"/)
  assert.match(source, /templateDrawingReferenceQuestions\(page\)\.length > 0 && !templateReferenceImagesReady\(page\)/)
  assert.match(source, /作图模板原图尚未加载成功/)
})

function geometryReview(diagnostics, overrides = {}, columnMode = 'single') {
  const reviewStart = source.indexOf('function templateGeometryReviewState()')
  const reviewEnd = source.indexOf('\nconst templateGeometryReview =', reviewStart)
  const helperSource = ['templateDiagnosticHasPendingGeometryReview', 'templateDiagnosticReasonText']
    .map((name) => {
      const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))
      assert.ok(match, `${name} must be present in Upload.vue`)
      return match[0]
    })
    .join('\n')
  const page = {
    pageIndex: 0,
    questions: [{ question_no: '2.3', drawing_reference: { ...candidate(), teacher_confirmed: true } }],
    anchors: [{ question_number: '2.3' }], handwriting: [],
    columns: [{ x1_1000: 0, x2_1000: 1000 }],
    columnsSource: 'model', columnsConfirmed: true, diagnostics, ...overrides,
  }
  const boxValidatorSource = source.match(/function templateBBoxIsValid\([^]*?\n\}/)[0]
  const boxValidator = new Function(`${boxValidatorSource}; return templateBBoxIsValid`)()
  return new Function('templateVisualEvidencePages', 'templateQuestionNumber', 'templateReviewPendingPageIndexes', 'templateColumnMode', 'templateBBoxIsValid', 'templateConfidenceRisk', 'templateRegionEntries', 'templateCutResult', 'templateCutPreviewPages', `${helperSource}; ${source.slice(reviewStart, reviewEnd)}; return templateGeometryReviewState()`)(() => [page], String, () => new Set(), { value: columnMode }, boxValidator, (item) => item.lowConfidence === true, () => [], { value: {} }, { value: [{}] })
}

test('a teacher-confirmed page can have a different column count from the original batch default', () => {
  const result = geometryReview([], { columnCount: 1, columnsSource: 'teacher', columnsConfirmed: true }, 'double')
  assert.equal(result.needsReview, false)
  assert.deepEqual(result.invalidColumnPages, [])
  const recovered = geometryReview([], { columnsSource: 'teacher', columnsConfirmed: true }, 'triple')
  assert.equal(recovered.needsReview, false, 'legacy saved columns supply the page count when an explicit count is absent')
})

test('per-page column validation still rejects missing, invalid-count and out-of-bounds drafts', () => {
  for (const patch of [
    { columns: [] }, { columnCount: 0 }, { columnCount: 4 }, { columnCount: 1.5 },
    { columnCount: 2, columns: [{ x1_1000: 0, x2_1000: 1000 }] },
    { columnCount: 1, columns: [{ x1_1000: -1, x2_1000: 1000 }] },
    { columnCount: 1, columns: [{ x1_1000: 200, x2_1000: 200 }] },
    { columnCount: 1, columnsSource: 'teacher_draft', columnsConfirmed: false },
  ]) assert.equal(geometryReview([], patch, 'double').needsReview, true)
})

test('reference-only import failure does not keep the geometry gate closed after reference retry and confirmation', () => {
  const result = geometryReview([{
    needs_review: true, geometry_review_pending: false,
    review_reasons: ['作图标准图候选生成失败，请教师编辑并确认参考图'],
    drawing_reference_candidates: { status: 'timeout' },
    model_stages: { 'template-drawing-reference-v1': { status: 'timeout' } },
  }])
  assert.equal(result.needsReview, false)
  assert.deepEqual(result.diagnosticReasons, [])
})

test('explicit geometry, legacy diagnostics and image association risks still block final confirmation', () => {
  for (const diagnostic of [
    { needs_review: true, geometry_review_pending: true },
    { needs_review: false, geometry_review_pending: true },
    { needs_review: true }, { needsReview: true },
    { needs_review: true, geometry_review_pending: false, image_association: { needs_review: true } },
    { needs_review: false, geometry_review_pending: false, image_association: { status: 'needs_review' } },
  ]) assert.equal(geometryReview([diagnostic]).needsReview, true)
  const unrelatedStageFailure = [{ needs_review: true, geometry_review_pending: false }]
  assert.equal(geometryReview(unrelatedStageFailure, { layoutReviewPending: true }).needsReview, true)
  assert.equal(geometryReview(unrelatedStageFailure, { anchors: [] }).needsReview, true)
  assert.equal(geometryReview(unrelatedStageFailure, { anchors: [{ question_number: '2.3', lowConfidence: true }] }).needsReview, true)
})

test('existing saved references do not block question changes, but generated or edited local references do', async () => {
  const h = ready(harness())
  h.question.drawing_reference = { ...candidate(), teacher_confirmed: true }
  assert.equal(h.templateReferenceQuestionEditBlockReason(), '')
  h.updateDrawingReference(h.question, { ...candidate(), teacher_confirmed: false })
  assert.match(h.templateReferenceQuestionEditBlockReason(), /尚未最终保存/)
  const snapshot = h.templateDrawingReferenceSaveSnapshot()
  h.clearSavedTemplateDrawingReferenceChanges(snapshot)
  assert.equal(h.templateReferenceQuestionEditBlockReason(), '')
  h.updateDrawingReference(h.question, candidate())
  assert.match(h.templateReferenceQuestionEditBlockReason(), /尚未最终保存/)
  h.page.saving = true
  assert.equal(h.templateEditorBusy.value, true)
})

test('successful final save clears only the exact sent reference and cannot clear newer edits', () => {
  const h = ready(harness())
  h.updateDrawingReference(h.question, { ...candidate(), teacher_confirmed: false })
  const saved = h.templateDrawingReferenceSaveSnapshot()
  h.question.standard_answer = 'later teacher answer'
  h.clearSavedTemplateDrawingReferenceChanges(saved)
  assert.equal(h.question._drawingReferenceDirty, true)
  assert.equal(h.question.drawing_reference.teacher_confirmed, false)
  h.question.standard_answer = 'teacher checked answer'
  h.clearSavedTemplateDrawingReferenceChanges(saved)
  assert.equal(h.question._drawingReferenceDirty, false)
  assert.equal(h.question.drawing_reference.teacher_confirmed, true)
  assert.match(source, /if \(!res\.success\) throw new Error\(res\.detail \|\| '确认失败'\)[^]*?clearSavedTemplateDrawingReferenceChanges\(drawingReferenceSnapshot\)/)
})

test('insert and delete reject unsaved references before server requests or deletion confirmation', async () => {
  const calls = []
  const h = ready(harness())
  h.updateDrawingReference(h.question, candidate())
  const editStart = source.indexOf('async function editTemplateQuestion(')
  const editEnd = source.indexOf('\nfunction applyTemplateQuestionEdit(', editStart)
  const warnings = []
  let confirms = 0
  const dependencies = {
    pendingTemplateId: h.pendingTemplateId, templateQuestionEditing: h.templateQuestionEditing,
    templateReferenceQuestionEditBlockReason: h.templateReferenceQuestionEditBlockReason,
    templateCutPreviewPages: h.templateCutPreviewPages, templateCropGesture: h.templateCropGesture,
    templatePageEditingLocked: (item) => h.templateEditorBusy.value || item.saving,
    saveAllTemplateLayoutPages: async () => true,
    ElMessage: { warning: (message) => warnings.push(message), error() {}, success() {} },
    ElMessageBox: { confirm: async () => { confirms++ }, prompt: async () => ({ value: '2.4' }) },
    api: { grade: { templateQuestionEdit: async (...args) => { calls.push(args); return {} } } },
    applyTemplateQuestionEdit() {},
  }
  const handlers = new Function(...Object.keys(dependencies), `${source.slice(editStart, editEnd)}; return { insertTemplateQuestion, deleteTemplateQuestion }`)(...Object.values(dependencies))
  await handlers.insertTemplateQuestion(h.page, '2.3')
  await handlers.deleteTemplateQuestion(h.page, '2.3')
  assert.equal(calls.length, 0)
  assert.equal(confirms, 0)
  assert.equal(warnings.length, 2)
  h.clearSavedTemplateDrawingReferenceChanges(h.templateDrawingReferenceSaveSnapshot())
  await handlers.insertTemplateQuestion(h.page, '2.3')
  assert.equal(calls.length, 1)
  assert.equal(calls[0][1].new_question_number, '2.4')
  assert.equal(h.templateQuestionEditing.value, false)
})

test('editing question text or answers keeps the teacher-drawn standard figure', () => {
  const names = ['markTemplateQuestionInput', 'markTemplateAnswerManualEdit']
  const body = names.map((name) => {
    const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))
    assert.ok(match, name)
    return match[0]
  }).join('\n')
  const actions = new Function(`${body}\nreturn { ${names.join(',')} }`)()
  const question = { question_no: '5', question_type: 'solve', answer_kind: 'mixed', standard_answer: '12', drawing_reference: { ...candidate(), teacher_confirmed: true } }
  const drawn = JSON.stringify(question.drawing_reference.variants)
  actions.markTemplateQuestionInput(question)
  actions.markTemplateAnswerManualEdit(question)
  actions.markTemplateAnswerManualEdit(question, 'solution_steps_text')
  assert.equal(JSON.stringify(question.drawing_reference.variants), drawn)
  assert.equal(question.drawing_reference.teacher_confirmed, false)
  assert.doesNotMatch(source, /_drawingReferenceInvalidatedByAnswer|_drawingReferenceGeometryWarning/)
  // Only the drawing kind gates the figure; answer/text edit state never does.
  assert.match(source, /const drawingReference = graphic \? templateDrawingReference\(question\) : \{\}\n/)
})
