// Teacher answer-box edits (add/delete) must stay compatible end to end: arrays
// auto-adjust, slot identity survives (gaps allowed, never renumbered), and the
// wire round-trips back through prepareTemplateSlots without blocking reopen.
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  addTemplateSlot, removeTemplateSlot, serializeTemplateSlots,
  prepareTemplateSlots, templateSlotBoxes,
} from '../src/utils/templateSlots.js'
import { serializeTemplateDrawingReview } from '../src/utils/templateReview.js'

function slotQuestion(extra = {}) {
  return {
    question_no: '2.3', question_type: 'solve', answer_kind: 'non_graphic',
    slot_ids: ['2.3[1]', '2.3[2]', '2.3[3]'], region_kinds: ['answer', 'answer', 'answer'],
    answer_slots_text: ['x', 'y', 'z'], blank_count: 3,
    blank_scores: [3, 2, 4], subquestion_scores: [3, 2, 4], ...extra,
  }
}

function page(question) {
  const boxes = question.slot_ids.map((slot_id, i) => ({
    question_number: '2.3', slot_id,
    region_kind: question.region_kinds?.[i] || 'answer',
    bbox: [10 + i * 30, 10, 30 + i * 30, 40],
  }))
  return {
    pageIndex: 0, dirty: false,
    questionSchema: [{ question_no: '2.3', slot_ids: [...question.slot_ids], blank_count: question.blank_count, answer_kind: question.answer_kind, ...(question.region_kinds ? { region_kinds: [...question.region_kinds] } : {}) }],
    answerBoxes: boxes, drawingBoxes: [],
  }
}

const wireOf = (pg, q) => serializeTemplateSlots(pg, [q], [pg.questionSchema[0]])

test('adding a box appends in order and aligns every parallel array', () => {
  const q = slotQuestion(); const pg = page(q)
  const box = addTemplateSlot(pg, q, [100, 100, 140, 140], 'answer')
  assert.deepEqual(q.slot_ids, ['2.3[1]', '2.3[2]', '2.3[3]', '2.3[4]'])
  assert.equal(box.slot_id, '2.3[4]')
  assert.deepEqual(q.region_kinds, ['answer', 'answer', 'answer', 'answer'])
  assert.deepEqual(q.blank_scores, [3, 2, 4, 0])
  assert.deepEqual(q.answer_slots_text, ['x', 'y', 'z', ''])
  const [wire] = wireOf(pg, q)
  assert.equal(wire.regions.length, 4)
  assert.deepEqual(wire.slot_ids, q.slot_ids)
})

test('deleting a middle box keeps identity (gaps) and splices arrays together', () => {
  const q = slotQuestion(); const pg = page(q)
  removeTemplateSlot(pg, q, '2.3[2]')
  assert.deepEqual(q.slot_ids, ['2.3[1]', '2.3[3]'])
  assert.deepEqual(q.region_kinds, ['answer', 'answer'])
  assert.deepEqual(q.blank_scores, [3, 4])
  const [wire] = wireOf(pg, q)
  assert.deepEqual(wire.slot_ids, ['2.3[1]', '2.3[3]'])
  assert.equal(wire.regions.length, 2)
  assert.deepEqual(wire.regions[0], [10, 10, 30, 40])
  assert.deepEqual(wire.regions[1], [70, 10, 90, 40])
})

test('adding after a delete never collides with surviving identities', () => {
  const q = slotQuestion(); const pg = page(q)
  removeTemplateSlot(pg, q, '2.3[2]')
  const box = addTemplateSlot(pg, q, [200, 100, 240, 140], 'answer')
  assert.equal(box.slot_id, '2.3[4]')
  assert.equal(new Set(q.slot_ids).size, q.slot_ids.length)
  wireOf(pg, q)
})

test('wire round-trips through prepareTemplateSlots without blocking reopen', () => {
  const q = slotQuestion(); const pg = page(q)
  removeTemplateSlot(pg, q, '2.3[2]')
  addTemplateSlot(pg, q, [200, 100, 240, 140], 'answer')
  const [wire] = wireOf(pg, q)
  // server echo: the loader response a reopen would carry
  const echoQuestion = {
    question_no: '2.3', question_type: 'solve', answer_kind: wire.answer_kind,
    slot_ids: [...wire.slot_ids], region_kinds: [...wire.region_kinds],
    answer_slots: [...wire.answer_slots], blank_count: wire.blank_count,
    blank_scores: [...wire.blank_scores], standard_answer: 'x; z; ',
  }
  const reopened = {
    pageIndex: 0,
    questionSchema: [{ ...echoQuestion }],
    answerBoxes: wire.slot_ids.map((slot_id, i) => ({ question_number: '2.3', slot_id, bbox: [...wire.regions[i]] })),
    drawingBoxes: [],
  }
  prepareTemplateSlots(reopened, [echoQuestion])
  assert.deepEqual(echoQuestion.slot_ids, ['2.3[1]', '2.3[3]', '2.3[4]'])
  assert.deepEqual(reopened.answerBoxes.map(b => b.slot_id), ['2.3[1]', '2.3[3]', '2.3[4]'])
})

test('deleting with misaligned legacy arrays auto-adjusts to the surviving slots', () => {
  // Legacy corruption: parallel arrays shorter than slot_ids must realign on
  // edit instead of drifting further (the "teacher touches it, it breaks" trap).
  const q = slotQuestion({ blank_scores: [5], region_kinds: ['answer', 'answer'] })
  const pg = page(q)
  removeTemplateSlot(pg, q, '2.3[3]')
  assert.equal(q.blank_scores.length, q.slot_ids.length)
  assert.equal(q.region_kinds.length, q.slot_ids.length)
  assert.equal(q.answer_slots_text.length, q.slot_ids.length)
  const [wire] = wireOf(pg, q)
  assert.equal(wire.blank_scores.length, 2)
  assert.equal(wire.region_kinds.length, 2)
})

test('adding a slot to a mark-less question derives region marks from the boxes', () => {
  const q = slotQuestion({ region_kinds: undefined, answer_kind: 'drawing' })
  const pg = page(q)
  for (const box of pg.answerBoxes) box.region_kind = 'drawing'
  pg.drawingBoxes = pg.answerBoxes.map(box => ({ ...box }))
  const box = addTemplateSlot(pg, q, [300, 100, 340, 140], 'answer')
  assert.equal(box.region_kind, 'answer')
  assert.ok(Array.isArray(q.region_kinds))
  assert.equal(q.region_kinds.length, q.slot_ids.length)
  assert.deepEqual(q.region_kinds.slice(0, 3), ['drawing', 'drawing', 'drawing'])
  assert.equal(q.region_kinds.at(-1), 'answer')
  assert.equal(q.answer_kind, 'mixed')
  wireOf(pg, q)
})

test('drawing boxes stay in sync with answer boxes across add and delete', () => {
  const q = slotQuestion({ region_kinds: ['answer', 'answer', 'drawing'], answer_kind: 'mixed' })
  const pg = page(q)
  pg.drawingBoxes = [{ question_number: '2.3', slot_id: '2.3[3]', region_kind: 'drawing', bbox: [70, 10, 90, 40] }]
  removeTemplateSlot(pg, q, '2.3[3]')
  assert.equal(templateSlotBoxes(pg).length, 2)
  assert.equal(pg.drawingBoxes.length, 0)
  const box = addTemplateSlot(pg, q, [300, 100, 340, 140], 'drawing')
  assert.equal(pg.drawingBoxes.length, 1)
  assert.equal(pg.drawingBoxes[0].slot_id, box.slot_id)
  wireOf(pg, q)
})

test('deleting a fill question\'s one drawing region keeps its six text slots and saves it non-graphic', () => {
  // A printed grid marked as a drawing region: the teacher deletes that region only.
  const ids = ['1[1]', '1[2]', '1[3]', '1[4]', '1[5]', '1[6]', '1[7]']
  const kinds = [...Array(6).fill('answer'), 'drawing']
  const answers = ['2/5', '2/5', '4/5', '2/5', '2', '4/5']
  const grid = [100, 500, 700, 800]
  const q = {
    question_no: '1', question_type: 'fill', answer_kind: 'mixed', slot_ids: [...ids],
    region_kinds: [...kinds], answer_slots_text: [...answers, ''], blank_count: 7,
  }
  const boxes = ids.map((slot_id, i) => ({
    question_number: '1', slot_id, region_kind: kinds[i],
    bbox: i < 6 ? [60 + 150 * i, 300, 180 + 150 * i, 360] : [...grid],
  }))
  const pg = {
    pageIndex: 0, dirty: false, answerBoxes: boxes, drawingBoxes: [{ ...boxes[6], bbox: [...grid] }],
    drawingLabels: [{ question_number: '1', label_anchor: [60, 650] }],
    questionSchema: [{ question_no: '1', question_number: '1', slot_ids: [...ids], blank_count: 7,
      answer_kind: 'mixed', region_kinds: [...kinds], drawing_regions: [[...grid]], label_anchor: [60, 650] }],
  }
  removeTemplateSlot(pg, q, '1[7]')
  assert.deepEqual(q.slot_ids, ids.slice(0, 6))
  assert.deepEqual(q.answer_slots_text, answers)
  assert.equal(q.answer_kind, 'non_graphic')
  assert.equal(pg.drawingBoxes.length + pg.drawingLabels.length, 0)
  const [wire] = serializeTemplateSlots(pg, [q], serializeTemplateDrawingReview(pg.questionSchema, pg.drawingBoxes, pg.drawingLabels))
  assert.deepEqual(wire.slot_ids, ids.slice(0, 6))
  assert.deepEqual(wire.region_kinds, Array(6).fill('answer'))
  assert.deepEqual(wire.answer_slots, answers)
  assert.equal(wire.regions.length, 6)
  assert.equal(wire.answer_kind, 'non_graphic')
  assert.deepEqual(wire.drawing_regions, [])
  assert.equal(wire.label_anchor, null)
})
