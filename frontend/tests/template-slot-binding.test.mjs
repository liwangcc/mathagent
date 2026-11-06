import assert from 'node:assert/strict'
import test from 'node:test'
import { MElement } from './helpers/memoryDom.js'
import { cutPage, openTemplateEditor } from './helpers/templateDrawingSlots.js'
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

// The same Q2.3 as fixture() as the server sends it to the real Upload page:
// the recognized question (templatePages) and its stored cut page (answer
// regions with their slot IDs, schema). `located` lists the slot positions
// the cut page has a box for; `reviewPending` marks the page's geometry as
// still under review, the only state in which a slot may lack its box.
const BOXES = [[10, 20, 200, 100], [10, 120, 200, 200], [10, 220, 200, 300]]
const IDS = ['2.3[2]', '2.3[5]', '2.3[9]']
function serverSlots({ typed = false, pageIndex = 0, answers, located = [0, 1, 2], questionIds = IDS, reviewPending = false, question: extra = {} } = {}) {
  const question = {
    question_no: '2.3', question_type: 'solve', question_text: '解下列方程组并作图', blank_count: 3, subquestion_count: 3,
    answer_slots: answers ?? ['x = 1\ny = 2', typed ? '' : 'second', 'third'],
    standard_answer: 'original summary', blank_scores: [2, 3, 5], subquestion_scores: [2, 3, 5],
    slot_ids: [...questionIds],
    ...(typed ? { region_kinds: ['answer', 'drawing', 'answer'], answer_kind: 'mixed' } : { answer_kind: 'non_graphic' }),
    ...extra,
  }
  const schema = {
    question_number: '2.3', answer_kind: question.answer_kind, slot_ids: [...IDS],
    ...(typed ? { region_kinds: ['answer', 'drawing', 'answer'], drawing_regions: located.includes(1) ? [BOXES[1]] : [] } : {}),
  }
  return {
    question,
    cut: cutPage({
      pageIndex,
      anchors: ['2.3'],
      answerRegions: [{ question_number: '2.3', regions: located.map(i => BOXES[i]), slot_ids: located.map(i => IDS[i]) }],
      schema: [schema],
      ...(reviewPending ? { geometry_review_pending: true } : {}),
    }),
  }
}
const otherQuestion = (number = '1') => ({ question_no: number, question_type: 'solve', question_text: `第 ${number} 题题干`, standard_answer: '2', answer_kind: 'non_graphic' })
const otherCut = (pageIndex, number = '1') => cutPage({
  pageIndex, anchors: [number],
  answerRegions: [{ question_number: number, regions: [[300, 500, 700, 600]] }],
  schema: [{ question_number: number, answer_kind: 'non_graphic' }],
})

// The 作答槽顺序 rows of a page, by slot ID: the locate button and the answer shown.
function slotRows(editor, pageIndex) {
  return Object.fromEntries(editor.article(pageIndex).allByClass('tpl-slot-row').map(row => [row.dataset.templateSlotRow, {
    row,
    button: row.find(el => el.tagName === 'button'),
    answer: row.find(el => el.tagName === 'span').textContent,
  }]))
}
const answerOverlays = (editor, pageIndex) => editor.article(pageIndex).allByClass('tpl-handwriting-region')
const drawingOverlays = (editor, pageIndex) => editor.article(pageIndex).allByClass('tpl-drawing-region')
const contractError = (editor, pageIndex) => editor.article(pageIndex).find(el => el.tagName === 'p' && el.attributes.role === 'alert')?.text ?? ''
const pageCounts = (editor, pageIndex) => editor.article(pageIndex).byClass('tpl-page-counts').text
const hint = (editor, pageIndex) => editor.article(pageIndex).byClass('tpl-pointer-hint').text
const boxStyle = ([left, top, right, bottom]) => ({ left: `${left / 10}%`, top: `${top / 10}%`, width: `${(right - left) / 10}%`, height: `${(bottom - top) / 10}%` })
const styleOf = el => ({ left: el.style.left, top: el.style.top, width: el.style.width, height: el.style.height })

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

test('real Upload initializer surfaces contract errors and per-slot UI answers remain page-scoped', async (t) => {
  const { question, cut } = serverSlots({ typed: true })
  // The recognized pages arrive out of page order, and page 1 has a Q2.3 of
  // its own: page 0's slot rows must show page 0's answers.
  const elsewhere = { ...serverSlots().question, answer_slots: ['page 1 first', 'page 1 second', 'page 1 third'] }
  const editor = await openTemplateEditor(t, {
    pages: [{ page_index: 1, questions: [elsewhere] }, { page_index: 0, questions: [question] }],
    cutPages: [cut],
  })
  assert.equal(contractError(editor, 0), '')
  const rows = slotRows(editor, 0)
  assert.deepEqual(Object.keys(rows).sort(), [...IDS].sort())
  assert.equal(rows['2.3[2]'].answer, 'x = 1\ny = 2')
  assert.equal(rows['2.3[5]'].answer, '作图（不填文字）')
  assert.equal(rows['2.3[9]'].answer, 'third')
  // The teacher clears the third answer in the question's own card.
  const third = editor.questionCard(1, '2.3').find(el => el.tagName === 'input' && el.attributes['aria-label'] === '第 2.3 题第 3 空答案')
  third.value = ''
  await third.dispatch('change')
  await editor.upload.settle()
  assert.equal(slotRows(editor, 0)['2.3[9]'].answer, '待填写答案')
  editor.upload.unmount()

  // The recognized question names other slot IDs than the stored page: the
  // page reports the mismatch, and a position it cannot bind says so.
  const stale = serverSlots({ typed: true, questionIds: ['2.3[88]', '2.3[5]', '2.3[9]'] })
  const reopened = await openTemplateEditor(t, { pages: [{ page_index: 0, questions: [stale.question] }], cutPages: [stale.cut] })
  assert.match(contractError(reopened, 0), /回包不一致/)
  assert.equal(slotRows(reopened, 0)['2.3[2]'].answer, '答案未绑定')
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

test('review drafts retain unlocated middle slots without guessing boxes or shifting answers', async (t) => {
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
  removeTemplateSlot(page, question, pending.slot_id)
  assert.deepEqual(serializeTemplateSlots(page, [question], page.questionSchema)[0].answer_slots, ['x = 1\ny = 2', 'third'])

  // The real page shows the same review draft: the middle slot keeps its
  // answer and waits to be boxed, without a guessed box on the page.
  const draft = serverSlots({ located: [0, 2], reviewPending: true })
  const editor = await openTemplateEditor(t, { pages: [{ page_index: 0, questions: [draft.question] }], cutPages: [draft.cut] })
  assert.deepEqual(answerOverlays(editor, 0).map(el => el.dataset.templateSlotId), ['2.3[2]', '2.3[9]'])
  assert.match(pageCounts(editor, 0), /· 2 个作答框 ·/)
  const rows = slotRows(editor, 0)
  assert.deepEqual(Object.keys(rows).sort(), [...IDS].sort())
  assert.equal(rows['2.3[5]'].button.text, 'Q2.3[5] 待框选')
  assert.deepEqual(IDS.map(id => rows[id].answer), ['x = 1\ny = 2', 'second', 'third'])
})

test('drawing a pending slot fills its existing identity rather than appending another slot', async (t) => {
  const draft = serverSlots({ located: [0, 2], reviewPending: true })
  const editor = await openTemplateEditor(t, { pages: [{ page_index: 0, questions: [draft.question] }], cutPages: [draft.cut] })
  await slotRows(editor, 0)['2.3[5]'].button.dispatch('click')
  await editor.upload.settle()
  assert.match(hint(editor, 0), /请在原图上为 Q2\.3\[5\] 划出作答区域/)
  await editor.drawBox(0, [300, 400, 600, 700])
  assert.deepEqual(editor.warnings(), [])
  const overlays = answerOverlays(editor, 0)
  assert.equal(overlays.length, 3, 'the box fills the pending slot instead of adding another one')
  const filled = overlays.find(el => el.dataset.templateSlotId === '2.3[5]')
  assert.deepEqual(styleOf(filled), boxStyle([300, 400, 600, 700]))
  assert.ok(filled.classList.contains('is-selected'), 'the filled box is the selection')
  const rows = slotRows(editor, 0)
  assert.deepEqual(Object.keys(rows).sort(), [...IDS].sort())
  assert.equal(rows['2.3[5]'].button.text, 'Q2.3[5]')
  assert.ok(rows['2.3[5]'].row.classList.contains('is-selected'))
  assert.doesNotMatch(hint(editor, 0), /划出作答区域/, 'no slot waits for a box any more')
  // Saving sends the same slot identities, the drawn box in the middle one.
  await editor.save()
  const [schema] = editor.layoutSaves()[0].question_schema
  assert.deepEqual(schema.slot_ids, IDS)
  assert.deepEqual(schema.regions, [BOXES[0], [300, 400, 600, 700], BOXES[2]])
})

test('go-to-review selects the right page and missing slot, or arms drawing for an unlocated slot', async (t) => {
  const scrolls = t.mock.method(MElement.prototype, 'scrollIntoView')
  const lastScrolled = () => scrolls.mock.calls.at(-1)?.this
  const missingReview = editor => editor.page.root.find(el => el.tagName === 'section' && el.attributes['aria-label'] === '待补充标准答案')
  const goToReview = async (editor) => {
    await missingReview(editor).byText('button', '去处理').dispatch('click')
    await editor.upload.settle()
  }
  // Template page 7 is the second page; its Q2.3 has no answer for 2.3[5].
  const boxed = serverSlots({ pageIndex: 7, answers: ['x = 1\ny = 2', '', 'third'] })
  let editor = await openTemplateEditor(t, {
    pages: [{ page_index: 0, questions: [otherQuestion()] }, { page_index: 7, questions: [boxed.question] }],
    cutPages: [otherCut(0), boxed.cut],
  })
  assert.ok(editor.shown(editor.article(0)) && !editor.shown(editor.article(7)), 'the editor opens on its first page')
  const card = () => editor.questionCard(1, '2.3')
  await card().byClass('tpl-q-toggle').dispatch('click')
  await editor.upload.settle()
  assert.ok(card().classList.contains('is-collapsed'), 'the teacher folded the question card')
  assert.match(missingReview(editor).text, /第 2\.3 题有 1 个作答区域未设置标准答案/)
  await goToReview(editor)
  assert.ok(editor.shown(editor.article(7)) && !editor.shown(editor.article(0)), 'page 7 is shown')
  const overlay = id => answerOverlays(editor, 7).find(el => el.dataset.templateSlotId === id)
  assert.ok(overlay('2.3[5]').classList.contains('is-selected'))
  assert.equal(lastScrolled(), overlay('2.3[5]'), 'scrolled to the missing slot\'s box')
  assert.equal(card().classList.contains('is-collapsed'), false)
  assert.equal(card().byClass('tpl-q-toggle').attributes['aria-expanded'], 'true')
  assert.ok(overlay('2.3[5]').classList.contains('is-missing-answer'))
  assert.equal(overlay('2.3[2]').classList.contains('is-missing-answer'), false)
  editor.upload.unmount()

  // The missing slot has no box yet: go to its row and arm the box tool for it.
  const unboxed = serverSlots({ pageIndex: 7, answers: ['x = 1\ny = 2', '', 'third'], located: [0, 2], reviewPending: true })
  editor = await openTemplateEditor(t, {
    pages: [{ page_index: 0, questions: [otherQuestion()] }, { page_index: 7, questions: [unboxed.question] }],
    cutPages: [otherCut(0), unboxed.cut],
  })
  await goToReview(editor)
  assert.ok(editor.shown(editor.article(7)))
  assert.equal(lastScrolled(), slotRows(editor, 7)['2.3[5]'].row, 'scrolled to the slot\'s row')
  assert.equal(editor.article(7).byClass('tpl-draw-surface').attributes['aria-label'], '在原图上新增蓝色作答框')
  assert.match(hint(editor, 7), /请在原图上为 Q2\.3\[5\] 划出作答区域/)
  // Pressing on the bare page clears the selection and disarms the slot's tool.
  await editor.article(7).byClass('tpl-reference-geometry').dispatch('pointerdown')
  await editor.upload.settle()
  assert.equal(editor.article(7).byClass('tpl-draw-surface'), null)
  assert.doesNotMatch(hint(editor, 7), /划出作答区域/)
})


test('drawing a pending drawing slot also registers its drawing region', async (t) => {
  const draft = serverSlots({ typed: true, located: [0, 2], reviewPending: true })
  const editor = await openTemplateEditor(t, { pages: [{ page_index: 0, questions: [draft.question] }], cutPages: [draft.cut] })
  const pending = () => slotRows(editor, 0)['2.3[5]']
  assert.equal(pending().button.text, 'Q2.3[5] 待框选')
  assert.equal(pending().answer, '作图（不填文字）')
  assert.equal(drawingOverlays(editor, 0).length, 0)
  await pending().button.dispatch('click')
  await editor.upload.settle()
  assert.equal(editor.article(0).byClass('tpl-draw-surface').attributes['aria-label'], '在原图上新增绿色画图区', 'a drawing slot arms the drawing tool')
  // A text-box tool (the page never arms one for this slot) cannot fill the
  // drawing identity with a text box.
  editor.page.setup.templateCutPreviewPages[0].geometryTool = 'answer'
  await editor.upload.settle()
  await editor.drawBox(0, [300, 400, 600, 700])
  assert.match(editor.warnings()[0], /Q2\.3\[5\] 是作图区/)
  assert.equal(pending().button.text, 'Q2.3[5] 待框选')
  assert.equal(answerOverlays(editor, 0).length + drawingOverlays(editor, 0).length, 2)
  // With the drawing tool the box fills the slot and becomes its drawing region.
  await pending().button.dispatch('click')
  await editor.upload.settle()
  await editor.drawBox(0, [300, 400, 600, 700])
  const regions = drawingOverlays(editor, 0)
  assert.deepEqual(regions.map(el => el.dataset.templateSlotId), ['2.3[5]'])
  assert.deepEqual(styleOf(regions[0]), boxStyle([300, 400, 600, 700]))
  assert.ok(regions[0].classList.contains('is-selected'), 'the drawing region is the selection')
  assert.equal(pending().button.text, 'Q2.3[5]')
  await editor.loadImages()
  await editor.save()
  const [schema] = editor.layoutSaves()[0].question_schema
  assert.deepEqual(schema.drawing_regions, [[300, 400, 600, 700]])
  assert.deepEqual(schema.regions[1], [300, 400, 600, 700])
  assert.deepEqual(schema.region_kinds, ['answer', 'drawing', 'answer'])
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

test('save gate reports Max reports before missing answers and sends teacher keep decisions', async (t) => {
  const flagged = serverSlots({
    answers: ['', 'second', 'third'],
    question: { max_flags: [{ scope: 'slot', slot_id: '2.3[9]', status: 'extra', reason: '印刷示意图', source: 'locator' }] },
  })
  const other = otherCut(0)
  const editor = await openTemplateEditor(t, {
    pages: [{ page_index: 0, questions: [otherQuestion(), flagged.question] }],
    cutPages: [cutPage({
      anchors: ['1', '2.3'],
      answerRegions: [...other.template_answer_regions, ...flagged.cut.template_answer_regions],
      schema: [...other.question_schema, ...flagged.cut.question_schema],
    })],
  })
  await editor.save()
  assert.equal(editor.confirms().length, 0)
  const [warning] = editor.warnings()
  const max = warning.indexOf('第 2.3 题：Max 判定作答位 Q2.3[9]为目录多余项（印刷示意图）')
  const missing = warning.indexOf('第 2.3 题有 1 个作答区域未设置标准答案')
  assert.ok(max >= 0 && missing > max, `the Max report comes before the missing answer: ${warning}`)
  // 确认保留 on the Max row keeps the reported position.
  const maxReview = () => editor.page.root.find(el => el.tagName === 'section' && el.attributes['aria-label'] === 'Max 判定待核对')
  await maxReview().byText('button', '确认保留').dispatch('click')
  await editor.upload.settle()
  assert.equal(maxReview(), null, 'the kept report no longer blocks')
  const first = editor.questionCard(0, '2.3').find(el => el.tagName === 'input' && el.attributes['aria-label'] === '第 2.3 题第 1 空答案')
  first.value = 'x = 1'
  await first.dispatch('change')
  await editor.upload.settle()
  await editor.save()
  const [payload] = editor.confirms()
  assert.deepEqual(Object.fromEntries(payload.pages[0].questions.map(q => [q.question_no, q.max_flags_kept])), {
    1: [], 2.3: ['2.3[9]'],
  })
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
