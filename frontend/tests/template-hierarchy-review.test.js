import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import { prepareTemplateSlots, serializeTemplateSlots, templateSlotBoxes } from '../src/utils/templateSlots.js'

import {
  isTemplateDirectoryLeaf,
  mergeTemplateDrawingCandidates,
  serializeTemplateDrawingReview,
  templateDrawingReview,
  templateQuestionIdentity,
  templateQuestionMetadata,
  templateDrawingReference,
  templateFillAnswerSlots,
} from '../src/utils/templateReview.js'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')

test('template dialog stays review-only before its first preview has loaded', () => {
  const start = source.indexOf('function templateGeometryReviewState()')
  const end = source.indexOf('const templateGeometryReview = computed', start)
  const helperSource = ['templateDiagnosticHasPendingGeometryReview', 'templateDiagnosticReasonText']
    .map((name) => {
      const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))
      assert.ok(match, `${name} must be present in Upload.vue`)
      return match[0]
    })
    .join('\n')
  const sandbox = {
    templateVisualEvidencePages: () => [{ pageIndex: 0, questions: [], columns: [], anchors: [], handwriting: [], diagnostics: [] }],
    templateReviewPendingPageIndexes: () => new Set(),
    templateRegionEntries: () => [], templateColumnMode: { value: 'single' },
    templateCutResult: { value: null }, templateCutPreviewPages: { value: [] },
  }
  vm.createContext(sandbox)
  vm.runInContext(`${helperSource}\n${source.slice(start, end)}`, sandbox)
  const state = sandbox.templateGeometryReviewState()
  assert.equal(state.needsReview, true)
  assert.deepEqual(Array.from(state.layoutReviewPendingPages), [0])
})

test('three stages preserve flat or hierarchical Q as strings and show printed path', () => {
  const examples = [
    { stage: 'primary', question_no: '2.1.3', printed_path: ['二', '1', '③'], expected: 'Q2.1.3 · 原卷 二 / 1 / ③' },
    { stage: 'junior', question_no: '3.2', printed_path: ['三', '(2)'], expected: 'Q3.2 · 原卷 三 / (2)' },
    { stage: 'senior', question_no: '17', printed_path: ['三', '17'], expected: 'Q17 · 原卷 三 / 17' },
  ]
  for (const example of examples) assert.equal(templateQuestionIdentity(example), example.expected)
  assert.equal(templateQuestionIdentity({ question_number: '12' }), 'Q12')
  assert.equal(templateQuestionIdentity({ question_no: '' }), '待绑定题号')
})

test('explicit metadata clone does not mutate identity, expose answers, or reuse drawing arrays', () => {
  const question = {
    directory_node_id: 'n006', directory_parent_id: 'n002', printed_path: ['二', '3'],
    answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor: [690, 370],
    standard_answer: 'not geometry', parent_question_no: 'old-scoring-parent',
  }
  const metadata = templateQuestionMetadata(question)
  assert.deepEqual(Object.keys(metadata).sort(), ['answer_kind', 'directory_node_id', 'directory_parent_id', 'drawing_regions', 'label_anchor', 'printed_path'])
  metadata.drawing_regions[0][0] = 600
  metadata.label_anchor[0] = 590
  metadata.printed_path[0] = 'changed'
  assert.equal(question.drawing_regions[0][0], 700)
  assert.equal(question.label_anchor[0], 690)
  assert.equal(question.printed_path[0], '二')
  assert.equal(isTemplateDirectoryLeaf(question), true)
  assert.equal(isTemplateDirectoryLeaf({ question_no: '17', parent_question_no: '17' }), false)
})

test('one graphic Q per leaf, mixed question keeps multiple drawing regions, normal solve unchanged', () => {
  const schema = [
    { question_number: '2.2', question_type: 'solve', answer_kind: 'non_graphic', label_anchor: null, drawing_regions: [], printed_path: ['二', '2'] },
    { question_number: '2.3', question_type: 'solve', answer_kind: 'drawing', label_anchor: [690, 370], drawing_regions: [[700, 350, 900, 500]], directory_node_id: 'n006' },
    { question_number: '3', question_type: 'solve', answer_kind: 'mixed', label_anchor: [130, 820], drawing_regions: [[135, 770, 530, 880], [135, 890, 530, 990]], printed_path: ['三'] },
  ]
  const review = templateDrawingReview(schema)
  assert.equal(review.drawingBoxes.length, 3)
  assert.deepEqual(review.drawingLabels.map((item) => item.question_number), ['2.3', '3'])
  review.drawingLabels[0].label_anchor = [680, 360]
  review.drawingBoxes[0].bbox = [695, 340, 900, 510]
  const saved = serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels)
  assert.deepEqual(saved[0], schema[0])
  assert.deepEqual(saved[1].label_anchor, [680, 360])
  assert.deepEqual(saved[1].drawing_regions, [[695, 340, 900, 510]])
  assert.equal(saved[1].directory_node_id, 'n006')
  assert.equal(saved[2].question_number, '3')
  assert.equal(saved[2].drawing_regions.length, 2)
  assert.deepEqual(schema[1].label_anchor, [690, 370])
})

test('pending non-graphic candidates keep conflicting geometry visible for teacher resolution', () => {
  const review = templateDrawingReview([
    { question_number: '5', answer_kind: 'non_graphic', drawing_regions: [[120, 180, 720, 800]], label_anchor: [90, 220] },
  ], { includeConflictingGeometry: true })
  assert.deepEqual(review.drawingBoxes, [{ question_number: '5', bbox: [120, 180, 720, 800] }])
  assert.deepEqual(review.drawingLabels, [{ question_number: '5', label_anchor: [90, 220] }])
})

test('deleting a conflicting non-graphic marker clears stale geometry from the save payload', () => {
  const schema = [{ question_number: '5', answer_kind: 'non_graphic',
    drawing_regions: [[120, 180, 720, 800]], label_anchor: [90, 220] }]
  const saved = serializeTemplateDrawingReview(schema, [], [])[0]
  assert.deepEqual(saved.drawing_regions, [])
  assert.equal(saved.label_anchor, null)
  const pending = serializeTemplateDrawingReview(schema, [], [], { preserveConflictingGeometry: true })[0]
  assert.deepEqual(pending.drawing_regions, schema[0].drawing_regions)
  assert.deepEqual(pending.label_anchor, schema[0].label_anchor)
})

test('missing or malformed Q markers are never clamped; saving re-derives one beside the drawing area', () => {
  for (const label_anchor of [null, [1001, 50], [-1, 50], [5], ['5', 50], [NaN, 50]]) {
    const schema = [{ question_number: '2.3', answer_kind: 'drawing', drawing_regions: [[700, 350, 900, 500]], label_anchor }]
    const review = templateDrawingReview(schema)
    assert.equal(review.drawingLabels.length, 0)
    assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, [])[0].label_anchor, [685, 425])
  }
  // Without a drawing area there is nothing to place a marker beside.
  const bare = [{ question_number: '2.3', answer_kind: 'drawing', drawing_regions: [], label_anchor: null }]
  assert.equal(serializeTemplateDrawingReview(bare, [], [])[0].label_anchor, null)
})

test('a Q marker its drawing area moved past is re-placed beside that area on save; valid markers are kept', () => {
  const schema = [{ question_number: '8', answer_kind: 'mixed', drawing_regions: [[540, 600, 900, 800]], label_anchor: [525, 700] }]
  const review = templateDrawingReview(schema)
  // The teacher moved the area left of the auto-placed marker.
  review.drawingBoxes[0].bbox = [500, 600, 860, 800]
  assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels)[0].label_anchor, [485, 700])
  // Boundary contact and any marker left of the area are the teacher's and stay.
  for (const label_anchor of [[500, 650], [120, 90]]) {
    assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, [{ question_number: '8', label_anchor }])[0].label_anchor, label_anchor)
  }
  // The pending-review conflict view keeps the draft as it is.
  assert.deepEqual(serializeTemplateDrawingReview(schema, review.drawingBoxes, review.drawingLabels, { preserveConflictingGeometry: true })[0].label_anchor, [525, 700])
})

function editorFunctions() {
  const start = source.indexOf('function normalizeAnswerSpecForEditor(question)')
  const end = source.indexOf('function questionScoringValid(question)', start)
  const sandbox = {
    isTemplateDirectoryLeaf, templateQuestionMetadata, templateDrawingReference,
    inferBlankCount: () => 4,
    inferSubquestionCount: () => 3,
    normalizeChoiceAnswer: () => '',
    templateFillAnswerSlots,
    toTextLines: (value) => Array.isArray(value) ? value.join('\n') : String(value || ''),
    toStringList: (value) => Array.isArray(value) ? value : String(value || '').split('\n').filter(Boolean),
    defaultGradingMethod: () => 'process',
    normalizedBlankScores: (_question, count) => Array(count).fill(1),
    defaultSubquestionScores: (_question, count) => Array(count).fill(5),
    form: { category: 'exam' },
  }
  vm.createContext(sandbox)
  vm.runInContext(source.slice(start, end), sandbox)
  return sandbox
}

test('directory region counts survive editing and confirmation without text-based re-expansion', () => {
  const editor = editorFunctions()
  for (const question_type of ['fill', 'solve']) {
    const question = {
      question_no: '2.1.3', directory_node_id: 'n006', directory_parent_id: 'n003',
      printed_path: ['二', '1', '③'], question_type,
      blank_count: 3, subquestion_count: 3, subquestion_scores: [7, 8, 9],
      standard_answer: '(5,3)',
    }
    const normalized = editor.normalizeAnswerSpecForEditor(question)
    assert.equal(normalized.blank_count, 3)
    assert.equal(normalized.subquestion_count, 1)
    assert.deepEqual(Array.from(normalized.subquestion_scores), [7])
    const serialized = editor.answerSpecFields(normalized)
    assert.equal(serialized.blank_count, 3)
    assert.equal(serialized.subquestion_count, 1)
    assert.equal(serialized.directory_node_id, 'n006')
    assert.deepEqual(Array.from(serialized.printed_path), ['二', '1', '③'])
    assert.ok(serialized.subquestion_scores.length <= 1)
  }
  const legacy = editor.normalizeAnswerSpecForEditor({ question_no: '17', question_type: 'solve' })
  assert.equal(legacy.subquestion_count, 3)
  assert.equal(legacy.blank_count, 4)
})

test('reference geometry is copied separately without changing Q metadata', () => {
  const editor = editorFunctions()
  const question = { question_no: '2.3', question_type: 'solve', directory_node_id: 'n006',
    answer_kind: 'drawing', drawing_reference: { teacher_confirmed: true,
      variants: [{ strokes: [{ kind: 'point', point: [800, 450] }] }] } }
  const saved = editor.answerSpecFields(question)
  assert.equal(saved.drawing_reference.teacher_confirmed, true)
  saved.drawing_reference.variants[0].strokes[0].point[0] = 700
  assert.equal(question.drawing_reference.variants[0].strokes[0].point[0], 800)
  assert.equal(templateQuestionMetadata(question).drawing_reference, undefined)
})

test('a stale figure on a question whose marks are no longer graphic is never submitted', () => {
  const editor = editorFunctions()
  const drawing_reference = { comparison_mode: 'manual', variants: [] }
  const base = { question_no: '3', question_type: 'solve', slot_ids: ['3[1]'], answer_slots: ['x=2'], blank_count: 1, drawing_reference }
  assert.equal('drawing_reference' in editor.answerSpecFields({ ...base, region_kinds: ['answer'], answer_kind: 'mixed' }), false)
  assert.equal('drawing_reference' in editor.answerSpecFields({ question_no: '4', answer_kind: 'non_graphic', drawing_reference }), false)
  assert.deepEqual(editor.answerSpecFields({ ...base, region_kinds: ['drawing'], answer_kind: 'drawing' }).drawing_reference, drawing_reference)
})

test('natural Q group keeps repeated answers and an empty middle region in editor round trips', () => {
  const editor = editorFunctions()
  for (const question_type of ['fill', 'solve']) {
    const question = { question_no: '1', directory_node_id: 'n001', directory_parent_id: null,
      question_type, blank_count: 3, answer_slots: ['2', '', '2'], standard_answer: '2；；2' }
    const normalized = editor.normalizeAnswerSpecForEditor(question)
    assert.equal(normalized.blank_count, 3)
    assert.equal(normalized.answer_slots_text, '2\n\n2')
    const saved = editor.answerSpecFields(normalized)
    assert.equal(saved.blank_count, 3)
    assert.deepEqual(Array.from(saved.answer_slots), ['2', '', '2'])
  }
  const single = editor.normalizeAnswerSpecForEditor({ question_no: '1.1', directory_node_id: 'n002',
    directory_parent_id: 'n001', question_type: 'fill', blank_count: 1, answer_slots: ['2'] })
  assert.equal(single.blank_count, 1)
  assert.deepEqual(Array.from(editor.answerSpecFields(single).answer_slots), ['2'])
})

test('preview schema is loaded, editable, saved, and refreshed after server renumbering', () => {
  assert.match(source, /page\.question_schema \|\| artifact\.question_schema/)
  assert.match(source, /question_schema: confirmedSchema/)
  assert.match(source, /applyTemplateDrawingSchema\(page, res\.question_schema \|\| confirmedSchema\)/)
  assert.match(source, /applyTemplateDrawingSchema\(previewPage, artifact\.question_schema/)
  assert.match(source, /previewPage\.answerBoxes = mergeTemplateAnswerRegionSources\(/)
  assert.match(source, /previewPage\.handwritingBoxes = previewPage\.answerBoxes/)
  assert.match(source, /Object\.assign\(question, templateQuestionMetadata\(schema\)\)/)
  assert.match(source, /<template #geometry-labels>/)
  assert.match(source, /<TemplateGeometryLegend/)
  assert.match(source, /gesture\.point\[0\] \+ dx/)
  assert.match(source, /gesture\.point\[1\] \+ dy/)
  assert.match(source, /:disabled="isTemplateDirectoryLeaf\(q\)"/)
  assert.match(source, /key: 'question', label: '红色题号'/)
  assert.match(source, /key: 'drawing-label', label: '作图题题号'/)
  assert.match(source, /红色为印刷题号/)
  assert.match(source, /:aria-label="`拖动 Q\$\{label\.question_number\} 到作图区域左侧`"/)
})

test('pure drawing suppresses duplicate answer boxes and mixed drawing retains only text boxes', () => {
  const start = source.indexOf('function templateHasDrawingQuestion(page, number)')
  const end = source.indexOf('function templateDrawingLabelsMissing(page)', start)
  const sandbox = {}
  vm.createContext(sandbox)
  vm.runInContext(source.slice(start, end), sandbox)
  const page = {
    questionSchema: [{ question_number: '2.3', answer_kind: 'drawing' }, { question_number: '3', answer_kind: 'mixed' }],
    drawingBoxes: [{ question_number: '3', bbox: [100, 800, 500, 950] }],
    answerBoxes: [
      { question_number: '1', geometry_kind: 'label_anchor', bbox: [100, 100, 101, 101] },
      { question_number: '2.3', geometry_kind: 'label_anchor', bbox: [600, 400, 601, 401] },
      { question_number: '2.3', bbox: [700, 350, 900, 500] },
      { question_number: '3', bbox: [100, 800, 500, 950] },
      { question_number: '3', bbox: [600, 800, 900, 950] },
    ],
  }
  const visible = sandbox.templateVisibleAnswerBoxes(page)
  assert.equal(visible.length, 2)
  assert.equal(visible[0].question_number, '1')
  assert.equal(visible[1].question_number, '3')
  assert.deepEqual(visible[1].bbox, [600, 800, 900, 950])
  assert.equal(page.answerBoxes.length, 5)
  assert.match(source, /const textBoxes = \[\.\.\.templateVisibleAnswerBoxes\(page\)\.filter\(box => !box\.slot_id\), \.\.\.templateSlotBoxes\(page\)\]/)
  assert.match(source, /return \{ textBoxes, drawingBoxes: page\.drawingBoxes\.filter\(box => !box\.slot_id\) \}/)
  assert.match(source, /serializeTemplateHandwritingRegions\(\[\.\.\.textBoxes, \.\.\.drawingBoxes\]/)
})

test('pending drawing candidates edit geometry only and cannot replace server-owned Q identity', () => {
  const schema = [{ question_number: '2.3', directory_node_id: 'n003', printed_path: ['二', '3'], answer_kind: 'non_graphic', drawing_regions: [], label_anchor: null }]
  const candidates = [
    { question_number: '2.3', directory_node_id: 'forged', printed_path: ['wrong'], answer_kind: 'drawing', drawing_regions: [], label_anchor: null },
    { question_number: '99', answer_kind: 'drawing', drawing_regions: [] },
  ]
  assert.deepEqual(mergeTemplateDrawingCandidates(schema, candidates, false), schema)
  const draft = mergeTemplateDrawingCandidates(schema, candidates, true)
  assert.equal(draft.length, 1)
  assert.equal(draft[0].answer_kind, 'drawing')
  assert.equal(draft[0].directory_node_id, 'n003')
  assert.deepEqual(draft[0].printed_path, ['二', '3'])
  assert.equal(schema[0].answer_kind, 'non_graphic')
  assert.match(source, /page\.question_schema_candidates \|\| \[\], reviewPending/)
})

test('drawing subtype changes invalidate only the affected reference and never manufacture a drawing region', () => {
  const ownReference = { comparison_mode: 'fixed', variants: [], teacher_confirmed: true }
  const otherReference = { comparison_mode: 'fixed', variants: [], teacher_confirmed: true }
  const answer = { question_no: '2.3', question_type: 'fill', answer_kind: 'non_graphic', drawing_reference: ownReference }
  const otherAnswer = { question_no: '3', answer_kind: 'drawing', drawing_reference: otherReference }
  const functions = ['templateDrawingKind', 'setTemplateDrawingKind', 'markTemplatePageGeometryDirty'].map((name) => {
    const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))
    assert.ok(match)
    return match[0]
  }).join('\n')
  const sandbox = {
    ElMessage: { warning() {}, info() {} },
    templatePageEditingLocked: () => false,
    templatePages: { value: [{ page_index: 0, questions: [answer, otherAnswer] }] }, templateQuestionMetadata,
    clearTemplateSelection: (page) => { page.geometrySelection = null },
  }
  vm.createContext(sandbox)
  vm.runInContext(functions, sandbox)
  const page = {
    pageIndex: 0, drawingQuestionNumber: '2.3',
    questionSchema: [{ question_number: '2.3', question_type: 'fill', answer_kind: 'non_graphic' }],
    drawingBoxes: [], drawingLabels: [], dirty: false, geometrySelection: { kind: 'answer' }, geometryTool: 'drawing',
  }
  sandbox.setTemplateDrawingKind(page, 'mixed')
  assert.equal(page.questionSchema[0].question_type, 'fill')
  assert.equal(page.questionSchema[0].answer_kind, 'mixed')
  assert.equal(page.drawingBoxes.length, 0, 'selecting subtype cannot pretend a region was localized')
  assert.equal(page.dirty, true)
  assert.equal(page.geometrySelection, null)
  assert.equal(page.geometryTool, '')
  assert.equal(answer.answer_kind, 'mixed')
  assert.equal(answer.question_type, 'fill')
  assert.equal(answer.drawing_reference.teacher_confirmed, false)
  assert.equal(answer._drawingReferenceDirty, true)
  assert.equal(otherAnswer.drawing_reference, otherReference)
  assert.equal(otherAnswer.drawing_reference.teacher_confirmed, true)

  // Existing real geometry is removed when the teacher rules out drawing;
  // creation and resizing are covered by template-geometry-tools.test.js.
  page.drawingBoxes.push({ question_number: '2.3', bbox: [300, 350, 700, 700] })
  page.drawingLabels.push({ question_number: '2.3', label_anchor: [230, 400] })
  const otherBox = { question_number: '3', bbox: [100, 100, 200, 200] }
  page.drawingBoxes.push(otherBox)
  sandbox.setTemplateDrawingKind(page, 'non_graphic')
  assert.equal(page.drawingBoxes.length, 1)
  assert.equal(page.drawingBoxes[0], otherBox)
  assert.equal(page.drawingLabels.length, 0)
  assert.equal(page.questionSchema[0].label_anchor, null)
  assert.equal(page.questionSchema[0].drawing_regions.length, 0)
  assert.equal(answer.answer_kind, 'non_graphic')
  assert.equal(answer.drawing_reference, undefined, 'a hidden drawing reference must not prevent final confirmation of a non-graphic question')
})

test('insert placeholders remain pending until an explicit successful layout save', () => {
  const start = source.indexOf('function applyTemplateQuestionEdit(res)')
  const end = source.indexOf('async function loadTemplateCutPreviews', start)
  const body = source.slice(start, end)
  assert.match(body, /artifact\.geometry_review_pending === true/)
  assert.match(body, /res\.geometry_review_pending_pages/)
  assert.match(body, /previewPage\.dirty = reviewPending/)
  assert.match(body, /previewPage\.columnsConfirmed = !reviewPending/)
  assert.match(body, /teacherConfirmed: !reviewPending/)
  assert.match(source, /新增题号为占位，请拖至正确位置；最后保存模板即可/)
  assert.match(source, /page\.insertedPlaceholder = false/)
})

test('layout save retains marked writing areas but omits an absent legacy contract', async () => {
  const start = source.indexOf('async function saveTemplateLayoutPage(')
  const end = source.indexOf('async function editTemplateQuestion', start)
  const cases = [
    { mode: 'blank', hasContract: false, regions: [], sends: true },
    { mode: 'marked', hasContract: false, regions: [], sends: false },
    { mode: 'marked', hasContract: undefined, regions: [], sends: false },
    { mode: 'marked', hasContract: true, regions: [[300, 150, 600, 300]], sends: true },
    // Removing the last box must be explicit, unlike a legacy absent contract.
    { mode: 'marked', hasContract: true, regions: [], sends: true },
  ]
  for (const scenario of cases) {
    const confirmed = [{ question_number: '2.2', regions: scenario.regions }]
    const page = {
      pageIndex: 0, hasAnswerRegionContract: scenario.hasContract,
      questionAnchors: [], columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }],
      answerBoxes: [], handwritingBoxes: [], questionSchema: [], drawingBoxes: [],
    }
    let submitted
    const sandbox = {
      prepareTemplateSlots, serializeTemplateSlots, templateSlotBoxes,
      templatePages: { value: [] },
      pendingTemplateId: { value: 'synthetic' }, templateMode: { value: scenario.mode },
      templateCutPreviewPages: { value: [page] },
      cloneQuestionAnchors: (value) => value, cloneTemplateColumns: (value) => value,
      serializeTemplateHandwritingRegions: () => confirmed,
      serializeTemplateDrawingReview: () => [], normalizeTemplateMode: (value) => value,
      ElMessage: { error() {} },
      notifyProcessingError() {},
      api: { grade: { async templateLayoutOverrides(_id, payload) {
        submitted = JSON.parse(JSON.stringify(payload))
        throw new Error('Stop after capturing the real request payload')
      } } },
    }
    vm.createContext(sandbox)
    const visibleBoxes = source.match(/function templateVisibleAnswerBoxes\([^]*?\n\}/)[0]
    const initializeSlots = source.match(/function initializeTemplatePageSlots\([^]*?\n\}/)[0]
    const conflictNotice = source.match(/function templateLayoutConflictNotice\([^]*?\n\}/)[0]
    const saveBoxes = source.match(/function templatePageSaveBoxes\([^]*?\n\}/)[0]
    const answerQuestion = source.match(/function templateAnswerQuestion\([^]*?\n\}/)[0]
    vm.runInContext(initializeSlots + '\n' + visibleBoxes + '\n' + conflictNotice + '\n' + saveBoxes + '\n' + answerQuestion + '\n' + source.slice(start, end), sandbox)
    assert.equal(await sandbox.saveTemplateLayoutPage(page), false)
    assert.ok(submitted, 'valid page geometry must reach the request capture')
    assert.equal(Object.hasOwn(submitted, 'template_answer_regions'), scenario.sends)
    if (scenario.sends) assert.deepEqual(submitted.template_answer_regions, confirmed)
  }
  assert.match(source, /hasAnswerRegionContract: mergedAnswerBoxes\.length > 0/)
  assert.match(source, /const rawAnswerRegions = reviewPending && templateRegionCandidateUsable\(page\.template_answer_region_candidates\)/)
  assert.match(source, /delete resultPage\.template_answer_region_candidates/)
})
