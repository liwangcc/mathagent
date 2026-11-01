import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  prepareTemplateSlots, templateSlotBoxes, templateSlotIdsValid, templateSlotLabel,
  swapTemplateSlotGeometry, serializeTemplateSlots, addTemplateSlot, removeTemplateSlot, convertTemplateDrawingSlots,
} from '../src/utils/templateSlots.js'
import {
  templateFillAnswerSlots, setTemplateFillAnswerSlot, templateRegionSlotsValid,
  serializeTemplateDrawingReview,
  templateMissingAnswerSlots, templateMissingAnswerNotice,
  templateMaxFlagEntries, templateMaxFlagNotice, keepTemplateMaxFlags,
} from '../src/utils/templateReview.js'

const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const clone = value => structuredClone(value)
function fixture({ typed = false, ids = false, sameBBox = false } = {}) {
  const boxes = [[10, 20, 200, 100], [10, 120, 200, 200], [10, 220, 200, 300]]
  const question = {
    question_no: '2.3', question_type: 'solve', blank_count: 3, subquestion_count: 3,
    answer_slots: ['x = 1\ny = 2', typed ? '' : 'second', 'third'],
    standard_answer: 'original summary', blank_scores: [2, 3, 5], subquestion_scores: [2, 3, 5],
    ...(typed ? { region_kinds: ['answer', 'drawing', 'answer'], answer_kind: 'mixed' } : { answer_kind: 'non_graphic' }),
    ...(ids ? { slot_ids: ['2.3[2]', '2.3[5]', '2.3[9]'] } : {}),
  }
  const page = {
    pageIndex: 0, dirty: false,
    questionSchema: [{ ...clone(question), question_number: '2.3' }],
    answerBoxes: boxes.map(bbox => ({ question_number: '2.3', bbox: clone(sameBBox ? boxes[0] : bbox) })),
    drawingBoxes: typed ? [{ question_number: '2.3', bbox: clone(sameBBox ? boxes[0] : boxes[1]) }] : [],
    drawingLabels: [],
  }
  return { page, question }
}
function uploadFunctions(names, dependencies = {}) {
  const functions = names.map(name => {
    const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`))
    assert.ok(match, `${name} remains a real Upload.vue function`)
    return match[0]
  }).join('\n')
  return new Function(...Object.keys(dependencies), `${functions}\nreturn { ${names.join(',')} }`)(...Object.values(dependencies))
}

test('prepare stages legacy multi-slot IDs and leaves legacy single-box contracts untouched', () => {
  const { page, question } = fixture()
  prepareTemplateSlots(page, [question])
  assert.deepEqual(question.slot_ids, ['2.3[1]', '2.3[2]', '2.3[3]'])
  assert.deepEqual(page.answerBoxes.map(b => b.slot_id), question.slot_ids)
  assert.equal(page.dirty, true)
  assert.deepEqual(question.answer_slots_text, ['x = 1\ny = 2', 'second', 'third'])
  const legacy = { questionSchema: [{ question_number: '1' }], answerBoxes: [{ question_number: '1', bbox: [0, 0, 10, 10] }], dirty: false }
  const q = { question_no: '1', standard_answer: 'legacy' }
  const before = clone({ legacy, q })
  prepareTemplateSlots(legacy, [q])
  assert.deepEqual({ legacy, q }, before)
})

test('explicit non-contiguous IDs survive repeated prepare and drawing bindings use the real drawing box', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  prepareTemplateSlots(page, [question])
  assert.deepEqual(question.slot_ids, ['2.3[2]', '2.3[5]', '2.3[9]'])
  assert.equal(templateSlotLabel(question, 1), 'Q2.3[5]')
  assert.equal(templateSlotBoxes(page)[1], page.drawingBoxes[0])
  assert.equal(page.drawingBoxes[0].slot_id, '2.3[5]')
  assert.equal(page.dirty, false)
})

test('prepare rejects mismatched answer counts, conflicting IDs and partial physical bindings', () => {
  for (const mutate of [
    ({ question }) => { question.answer_slots = ['one'] },
    ({ page }) => { page.questionSchema[0].slot_ids[0] = '2.3[99]' },
    ({ page }) => { page.answerBoxes[0].slot_id = '2.3[2]' },
  ]) {
    const state = fixture({ typed: true, ids: true })
    mutate(state)
    assert.throws(() => prepareTemplateSlots(state.page, [state.question]), /数量不一致|回包不一致|缺失或重复/)
  }
  for (const slot_ids of [['2.3[0]'], ['2.3[01]'], ['3[1]'], ['2.3[1]', '2.3[1]']]) {
    assert.equal(templateSlotIdsValid({ question_no: '2.3', slot_ids }, slot_ids.length), false)
  }
})

test('swap moves only geometry; answer, kind, score and IDs stay bound', () => {
  const { page, question } = fixture({ ids: true })
  prepareTemplateSlots(page, [question])
  const before = clone(question)
  const boxes = page.answerBoxes.map(b => clone(b.bbox))
  assert.equal(swapTemplateSlotGeometry(page, '2.3[2]', '2.3[9]'), true)
  assert.deepEqual(page.answerBoxes.map(b => b.bbox), [boxes[2], boxes[1], boxes[0]])
  assert.deepEqual(question, before)
  assert.equal(page.dirty, true)
  assert.equal(swapTemplateSlotGeometry(page, '2.3[2]', '2.3[2]'), false)
  assert.throws(() => swapTemplateSlotGeometry(page, '2.3[2]', 'missing'), /同一题/)
  const mixed = fixture({ typed: true, ids: true })
  prepareTemplateSlots(mixed.page, [mixed.question])
  const snapshot = clone(mixed)
  assert.throws(() => swapTemplateSlotGeometry(mixed.page, '2.3[2]', '2.3[5]'), /文字槽与作图槽不能互换/)
  assert.deepEqual(mixed, snapshot)
})

test('delete removes matching answer, kind, score and both geometries; add never reuses deleted IDs', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  removeTemplateSlot(page, question, '2.3[5]')
  assert.deepEqual(question.slot_ids, ['2.3[2]', '2.3[9]'])
  assert.deepEqual(question.answer_slots_text, ['x = 1\ny = 2', 'third'])
  assert.deepEqual(question.blank_scores, [2, 5])
  assert.deepEqual(question.subquestion_scores, [2, 5])
  assert.deepEqual(question.region_kinds, ['answer', 'answer'])
  assert.equal(page.drawingBoxes.length, 0)
  removeTemplateSlot(page, question, '2.3[9]')
  const added = addTemplateSlot(page, question, [300, 300, 600, 600], 'drawing')
  assert.equal(added.slot_id, '2.3[10]')
  assert.equal(added, page.drawingBoxes[0])
  assert.deepEqual(question.slot_ids, ['2.3[2]', '2.3[10]'])
  assert.deepEqual(question.answer_slots, ['x = 1\ny = 2', ''])
  // A drawing position scores with the whole question: per-blank scores clear.
  assert.deepEqual(question.blank_scores, [])
  assert.deepEqual(page.questionSchema[0].slot_ids, question.slot_ids)
  assert.equal(question.blank_count, 2)
  assert.equal(question.answer_kind, 'mixed')
})

test('deleting the last position removes its slot numbers and marker but keeps the question', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  for (const id of ['2.3[9]', '2.3[5]', '2.3[2]']) removeTemplateSlot(page, question, id)
  assert.equal(Object.hasOwn(question, 'slot_ids'), false)
  assert.equal(Object.hasOwn(question, 'region_kinds'), false)
  assert.equal(question.answer_kind, 'non_graphic')
  assert.equal(question.blank_count, 1)
  assert.equal(page.answerBoxes.filter(box => box.slot_id).length, 0)
  assert.equal(page.drawingBoxes.length + page.drawingLabels.length, 0)
  assert.equal(question.question_no, '2.3')
  assert.equal(question.standard_answer, 'x = 1\ny = 2', 'the last text answer stays as the question answer')
  const [wire] = serializeTemplateSlots(page, [question], page.questionSchema)
  assert.equal(wire.slot_ids, null, 'the server is told the slots were removed, not left unchanged')
  assert.deepEqual(wire.region_kinds, [])
  assert.equal(Object.hasOwn(wire, '_slotsCleared'), false)
  // A box drawn afterwards starts a fresh list but never reuses a deleted
  // number, so a Max report on an old position cannot attach to it.
  Object.assign(question, { slot_ids: [], region_kinds: [], answer_slots_text: [] })
  assert.equal(addTemplateSlot(page, question, [300, 300, 600, 600], 'answer').slot_id, '2.3[10]')
})

test('deleting the last drawing slot drops the standard figure; deleting a text slot keeps it', () => {
  const reference = { comparison_mode: 'manual', variants: [] }
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  question.drawing_reference = clone(reference)
  question._drawingReferenceDirty = true
  removeTemplateSlot(page, question, '2.3[9]')
  assert.equal(question.answer_kind, 'mixed')
  assert.deepEqual(question.drawing_reference, reference)
  // The model put this drawing position on the printed stem.
  removeTemplateSlot(page, question, '2.3[5]')
  assert.equal(question.answer_kind, 'non_graphic')
  assert.equal(page.questionSchema[0].answer_kind, 'non_graphic')
  assert.equal(question.drawing_reference, undefined)
  assert.equal(question._drawingReferenceDirty, false)
})

test('typed answer edits preserve multiline, empty drawing positions and do not invent slots', () => {
  const { question } = fixture({ typed: true, ids: true })
  assert.equal(templateRegionSlotsValid(question), true)
  setTemplateFillAnswerSlot(question, 0, ' a = 3\nb = 4 ')
  assert.deepEqual(templateFillAnswerSlots(question), ['a = 3\nb = 4', '', 'third'])
  setTemplateFillAnswerSlot(question, 1, 'must not turn drawing into text')
  assert.equal(question.answer_slots_text[1], '')
  assert.equal(question.standard_answer, 'original summary')
  const before = clone(question)
  setTemplateFillAnswerSlot(question, 3, 'out of range')
  assert.deepEqual(question, before)
  setTemplateFillAnswerSlot(question, 2, '')
  assert.equal(question.answer_slots_text.length, 3)
  assert.equal(templateRegionSlotsValid(question), false)
  assert.deepEqual(templateFillAnswerSlots({ slot_ids: ['1[1]', '1[2]'], blank_count: 2, answer_slots: ['only one'] }), ['only one'])
  for (const overrides of [
    { region_kinds: ['answer', 'unknown', 'answer'] },
    { blank_count: 2 }, { answer_slots: ['x', 'drawing text', 'z'] },
    { answer_slots: ['x', '', 2] }, { answer_slots: ['x', ''] },
  ]) assert.equal(templateRegionSlotsValid({ ...fixture({ typed: true }).question, ...overrides }), false)
})

test('serialization orders geometry by immutable slots, preserves answers and includes edited drawing geometry', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  page.answerBoxes.reverse()
  page.drawingBoxes[0].bbox = [300, 400, 700, 800]
  const wire = serializeTemplateSlots(page, [question], serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, []))[0]
  assert.deepEqual(wire.slot_ids, ['2.3[2]', '2.3[5]', '2.3[9]'])
  assert.deepEqual(wire.answer_slots, ['x = 1\ny = 2', '', 'third'])
  assert.deepEqual(wire.regions, [[10, 20, 200, 100], [300, 400, 700, 800], [10, 220, 200, 300]])
  assert.deepEqual(wire.region_kinds, ['answer', 'drawing', 'answer'])
  assert.deepEqual(wire.blank_scores, [2, 3, 5])
  wire.regions[0][0] = 999
  wire.slot_ids[0] = 'changed'
  assert.equal(page.answerBoxes[2].bbox[0], 10)
  assert.equal(question.slot_ids[0], '2.3[2]')
  page.answerBoxes.pop()
  assert.throws(() => serializeTemplateSlots(page, [question], page.questionSchema), /还没有框选位置/)
})

test('identical physical bboxes still serialize independent slots and repeated answers', () => {
  const { page, question } = fixture({ ids: true, sameBBox: true })
  question.answer_slots = ['same', 'same', 'same']
  prepareTemplateSlots(page, [question])
  const wire = serializeTemplateSlots(page, [question], page.questionSchema)[0]
  assert.equal(wire.regions.length, 3)
  assert.equal(new Set(wire.slot_ids).size, 3)
  assert.deepEqual(wire.answer_slots, ['same', 'same', 'same'])
  assert.deepEqual(wire.regions, Array.from({ length: 3 }, () => [10, 20, 200, 100]))
})

test('real Upload initializer surfaces contract errors and per-slot UI answers remain page-scoped', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  const methods = uploadFunctions(['initializeTemplatePageSlots', 'templateSlotAnswer'], {
    prepareTemplateSlots, templateFillAnswerSlots,
    templatePages: { value: [{ page_index: 99, questions: [] }, { page_index: 0, questions: [question] }] },
  })
  methods.initializeTemplatePageSlots(page)
  assert.equal(page.slotContractError, '')
  assert.equal(methods.templateSlotAnswer(page, page.answerBoxes[0]), 'x = 1\ny = 2')
  assert.equal(methods.templateSlotAnswer(page, page.answerBoxes[1]), '作图（不填文字）')
  assert.equal(methods.templateSlotAnswer(page, { question_number: '2.3', slot_id: 'missing' }), '答案未绑定')
  question.answer_slots_text[2] = ''
  assert.equal(methods.templateSlotAnswer(page, page.answerBoxes[2]), '待填写答案')
  question.slot_ids[0] = '2.3[88]'
  methods.initializeTemplatePageSlots(page)
  assert.match(page.slotContractError, /回包不一致/)
})

test('missing-answer notices preserve slot identities and clear after filling or deleting an extra region', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  question.answer_slots[0] = ''
  question.answer_slots[2] = '  '
  prepareTemplateSlots(page, [question])
  assert.deepEqual(templateMissingAnswerSlots(question), [
    { slotId: '2.3[2]', index: 0 }, { slotId: '2.3[9]', index: 2 },
  ])
  assert.equal(templateMissingAnswerNotice(question), '第 2.3 题有 2 个作答区域未设置标准答案。请核对原题：真实作答区域请补充标准答案，误识别的多余区域请删除，完成后再批阅。')
  setTemplateFillAnswerSlot(question, 0, '6')
  assert.equal(templateMissingAnswerSlots(question).length, 1)
  removeTemplateSlot(page, question, '2.3[9]')
  assert.equal(templateMissingAnswerNotice(question), '')
  assert.deepEqual(question.slot_ids, ['2.3[2]', '2.3[5]'])
  assert.deepEqual(templateFillAnswerSlots(question), ['6', '']) // drawing stays empty
})

test('review drafts retain unlocated middle slots without guessing boxes or shifting answers', () => {
  const { page, question } = fixture({ ids: true })
  prepareTemplateSlots(page, [question])
  page.geometryReviewPending = true
  page.answerBoxes.splice(1, 1)
  prepareTemplateSlots(page, [question])
  prepareTemplateSlots(page, [question])
  assert.equal(page.answerBoxes.length, 3)
  const pending = page.answerBoxes.find(b => b.slot_id === '2.3[5]')
  assert.deepEqual(pending.bbox, [])
  assert.equal(pending.pendingGeometry, true)
  assert.deepEqual(question.answer_slots_text, ['x = 1\ny = 2', 'second', 'third'])
  assert.throws(() => serializeTemplateSlots(page, [question], page.questionSchema), /还没有框选位置/)
  const methods = uploadFunctions(['templateVisibleAnswerBoxes'], {})
  assert.equal(methods.templateVisibleAnswerBoxes(page).length, 2)
  removeTemplateSlot(page, question, pending.slot_id)
  assert.deepEqual(serializeTemplateSlots(page, [question], page.questionSchema)[0].answer_slots, ['x = 1\ny = 2', 'third'])
})

test('drawing a pending slot fills its existing identity rather than appending another slot', () => {
  const { page, question } = fixture({ ids: true })
  prepareTemplateSlots(page, [question])
  page.geometryReviewPending = true
  page.answerBoxes.splice(1, 1)
  prepareTemplateSlots(page, [question])
  const pending = page.answerBoxes.find(b => b.slot_id === '2.3[5]')
  page.questionAnchors = [{ question_number: '2.3' }]
  page.pendingSlotId = pending.slot_id
  page.geometryDraft = [300, 400, 600, 700]
  const methods = uploadFunctions(['commitTemplateGeometryCreation', 'selectTemplateGeometryTarget', 'clearTemplateSelection', 'templateAnswerQuestion'], {
    addTemplateSlot,
    templatePages: { value: [{ page_index: 0, questions: [question] }] },
    normalizeTemplateBBoxForPage: value => [...value],
    markTemplateGeometryConfirmed: box => { box.human_confirmed = true },
    markTemplatePageGeometryDirty: page => { page.dirty = true },
    templateHasDrawingQuestion: () => false, activeTemplateReferenceEditor: () => null,
    ElMessage: { warning: message => assert.fail(message) },
  })
  methods.commitTemplateGeometryCreation({ page, kind: 'answer', questionNumber: '2.3', width: 1000, height: 1000 })
  assert.equal(page.answerBoxes.length, 3)
  assert.deepEqual(pending.bbox, [300, 400, 600, 700])
  assert.equal(pending.pendingGeometry, undefined)
  assert.equal(page.pendingSlotId, '')
  assert.equal(page.geometrySelection.target, pending)
  assert.deepEqual(serializeTemplateSlots(page, [question], page.questionSchema)[0].slot_ids, question.slot_ids)
})

test('go-to-review selects the right page and missing slot, or arms drawing for an unlocated slot', async () => {
  const { page, question } = fixture({ ids: true })
  page.pageIndex = 7
  question.answer_slots[1] = ''
  prepareTemplateSlots(page, [question])
  const selectedBox = page.answerBoxes[1]
  const scrolled = []
  const overlay = { dataset: { templateSlotId: selectedBox.slot_id }, scrollIntoView: () => scrolled.push('box') }
  const row = { dataset: { templateSlotRow: selectedBox.slot_id }, scrollIntoView: () => scrolled.push('row') }
  const root = { scrollIntoView: () => scrolled.push('page'), querySelectorAll: selector => selector === '[data-template-slot-row]' ? [row] : selectedBox.bbox.length ? [overlay] : [] }
  const activePage = { value: 0 }
  const entry = { pageIndex: 1, questionIndex: 0, question }
  const methods = uploadFunctions([
    'openTemplateMissingAnswers', 'locateTemplateReviewSlot', 'selectTemplatePreviewPage',
    'selectTemplateGeometryTarget', 'clearTemplateSelection', 'templateSlotNeedsAnswer',
  ], {
    templateMissingAnswerSlots, templateSlotBoxes,
    templatePages: { value: [{ page_index: 0, questions: [] }, { page_index: 7, questions: [question] }] },
    templateCutPreviewPages: { value: [page] }, templateActivePreviewPage: activePage,
    templatePageEditingLocked: () => false, templateCropGesture: { value: null },
    templateHasDrawingQuestion: () => false, activeTemplateReferenceEditor: () => null,
    nextTick: async fn => fn?.(),
    document: { getElementById: id => { assert.equal(id, 'template-reference-page-7'); return root } },
  })
  await methods.openTemplateMissingAnswers(entry)
  assert.equal(activePage.value, 7)
  assert.equal(page.geometrySelection.target, selectedBox)
  assert.equal(scrolled.at(-1), 'box')
  assert.equal(question._open, true)
  assert.equal(methods.templateSlotNeedsAnswer(page, selectedBox), true)
  assert.equal(methods.templateSlotNeedsAnswer(page, page.answerBoxes[0]), false)
  selectedBox.bbox = []
  await methods.openTemplateMissingAnswers(entry)
  assert.equal(scrolled.at(-1), 'row')
  assert.equal(page.pendingSlotId, selectedBox.slot_id)
  assert.equal(page.geometryTool, 'answer')
  methods.clearTemplateSelection(page)
  assert.equal(page.pendingSlotId, '')
  assert.equal(page.geometryTool, '')
})


test('drawing a pending drawing slot also registers its drawing region', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  page.geometryReviewPending = true
  page.answerBoxes.splice(1, 1)
  page.drawingBoxes = []
  prepareTemplateSlots(page, [question])
  const pending = page.answerBoxes.find(b => b.slot_id === '2.3[5]')
  assert.equal(pending.region_kind, 'drawing')
  page.questionAnchors = [{ question_number: '2.3' }]
  page.pendingSlotId = pending.slot_id
  page.geometryDraft = [300, 400, 600, 700]
  const warnings = []
  const methods = uploadFunctions(['commitTemplateGeometryCreation', 'selectTemplateGeometryTarget', 'clearTemplateSelection', 'templateAnswerQuestion'], {
    addTemplateSlot,
    templatePages: { value: [{ page_index: 0, questions: [question] }] },
    normalizeTemplateBBoxForPage: value => [...value],
    markTemplateGeometryConfirmed: box => { box.human_confirmed = true },
    markTemplatePageGeometryDirty: page => { page.dirty = true },
    templateHasDrawingQuestion: () => true, activeTemplateReferenceEditor: () => null,
    ensureTemplateDrawingLabel: () => {},
    ElMessage: { warning: message => warnings.push(message) },
  })
  // The wrong tool never fills a drawing identity with a text box.
  methods.commitTemplateGeometryCreation({ page, kind: 'answer', questionNumber: '2.3', width: 1000, height: 1000 })
  assert.match(warnings[0], /作图区/)
  assert.deepEqual(pending.bbox, [])
  page.pendingSlotId = pending.slot_id
  methods.commitTemplateGeometryCreation({ page, kind: 'drawing', questionNumber: '2.3', width: 1000, height: 1000 })
  assert.deepEqual(pending.bbox, [300, 400, 600, 700])
  assert.deepEqual(page.drawingBoxes.map(b => [b.slot_id, b.bbox]), [['2.3[5]', [300, 400, 600, 700]]])
  assert.equal(page.geometrySelection.target, page.drawingBoxes[0])
  assert.deepEqual(serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, [])[0].drawing_regions, [[300, 400, 600, 700]])
  assert.deepEqual(serializeTemplateSlots(page, [question], page.questionSchema)[0].regions[1], [300, 400, 600, 700])
})

test('Max extra/refused reports block until the slot is deleted or explicitly kept', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  question.max_flags = [
    { scope: 'slot', slot_id: '2.3[9]', status: 'extra', reason: '印刷示意图', source: 'locator' },
    { scope: 'slot', slot_id: '2.3[5]', status: 'refused', reason: '', source: 'solver' },
  ]
  assert.deepEqual(templateMaxFlagEntries(question).map(flag => flag.slot_id), ['2.3[9]', '2.3[5]'])
  assert.match(templateMaxFlagNotice(question), /^第 2\.3 题：Max 判定作答位 Q2\.3\[9\]为目录多余项（印刷示意图）；作答位 Q2\.3\[5\]无法按目录作答。/)
  // A legal blank drawing slot is not a missing text answer, but a report on it still blocks.
  assert.equal(templateMissingAnswerSlots(question).some(slot => slot.slotId === '2.3[5]'), false)
  removeTemplateSlot(page, question, '2.3[9]')
  assert.deepEqual(templateMaxFlagEntries(question).map(flag => flag.slot_id), ['2.3[5]'])
  assert.equal(keepTemplateMaxFlags(question), 1)
  assert.deepEqual(question.max_flags_kept, ['2.3[5]'])
  assert.equal(templateMaxFlagNotice(question), '')
  const whole = { question_no: '7', max_flags: ['solver', 'locator'].map(source => ({ scope: 'question', status: 'extra', reason: '不是独立题目', source })) }
  assert.equal(templateMaxFlagNotice(whole).match(/整题为目录多余项/g).length, 1)
  keepTemplateMaxFlags(whole)
  assert.deepEqual(whole.max_flags_kept, ['*'])
})

test('save gate reports Max reports before missing answers and sends teacher keep decisions', () => {
  assert.match(source, /const maxFlagNotice = templateMaxFlagNotice\(question\)\n\s+if \(maxFlagNotice\) blockers\.push\(maxFlagNotice\)\n\s+const missingNotice/)
  assert.match(source, /max_flags_kept: Array\.isArray\(q\.max_flags_kept\) \? \[\.\.\.q\.max_flags_kept\] : \[\]/)
  assert.match(source, /@click="keepTemplateMaxFlags\(entry\.question\)">确认保留<\/button>/)
})

test('非作图 on a pure drawing question keeps its positions as text answer boxes', () => {
  const { page, question } = fixture({ typed: true, ids: true })
  prepareTemplateSlots(page, [question])
  removeTemplateSlot(page, question, '2.3[2]')
  removeTemplateSlot(page, question, '2.3[9]')
  page.drawingLabels = [{ question_number: '2.3', label_anchor: [5, 160] }]
  assert.equal(question.answer_kind, 'drawing')
  convertTemplateDrawingSlots(page, question)
  assert.deepEqual(question.slot_ids, ['2.3[5]'])
  assert.deepEqual(question.region_kinds, ['answer'])
  assert.equal(question.answer_kind, 'non_graphic')
  assert.equal(page.drawingBoxes.length + page.drawingLabels.length, 0)
  assert.equal(page.answerBoxes.find(box => box.slot_id === '2.3[5]').region_kind, 'answer')
})
