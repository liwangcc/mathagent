// Directory leaves score by answer regions; slot edits must not build a
// subquestion layer on them ("目录末级题按作答区域计分，不能另建小问层级").
// Regression for incidents 01a7aa598b73 (confirm brick via persisted [5, 0])
// and dfe229faafbd (save brick): updateSlotStructure mapped subquestion_count
// to the slot count for every solve question, including directory leaves.
import test from 'node:test'
import assert from 'node:assert/strict'
import { addTemplateSlot } from '../src/utils/templateSlots.js'

function makeQuestion({ directory = false } = {}) {
  return {
    question_number: '1',
    question_type: 'solve',
    ...(directory ? { directory_node_id: 'n001', directory_parent_id: null } : {}),
    slot_ids: ['1[1]'],
    region_kinds: ['answer'],
    blank_count: 1,
    answer_slots_text: [''],
    subquestion_count: 1,
    subquestion_scores: [],
  }
}

function makePage(question) {
  return { questionSchema: [question], answerBoxes: [], drawingBoxes: [], dirty: false }
}

test('directory leaf keeps subquestion_count=1 on slot edits', () => {
  const question = makeQuestion({ directory: true })
  addTemplateSlot(makePage(question), question, [0, 0, 10, 10], 'answer')
  assert.equal(question.slot_ids.length, 2)
  assert.equal(question.subquestion_count, 1)
  assert.deepEqual(question.subquestion_scores, [])
})

test('non-directory solve question still maps subquestion_count to slots', () => {
  const question = makeQuestion()
  addTemplateSlot(makePage(question), question, [0, 0, 10, 10], 'answer')
  assert.equal(question.subquestion_count, 2)
})
