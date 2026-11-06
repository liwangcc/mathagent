import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { openTemplateEditor } from './helpers/templateHierarchyEditor.js'
import * as review from '../src/utils/templateReview.js'

// A choice question has one physical answer slot: the answer card of the real
// template editor changes the teacher's answer and that slot together.

function q() { return { question_no: '14', question_type: 'choice', choice_mode: 'single', standard_answer: 'A', answer_slots: ['A'], answer_slots_text: ['A'], slot_ids: ['14[1]'], region_kinds: ['answer'], blank_count: 1 } }

const answerCell = editor => editor.dialog().find(el => el.classList.contains('tpl-choice-answer-cell'))
const slotAnswer = editor => editor.field('14', '各空标准答案').find(el => el.tagName === 'input' && el.attributes['aria-label'] === '第 14 题第 1 空答案')

test('choice button changes parent and physical answer slot together', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [[q()]] })
  const option = name => answerCell(editor).find(el => el.classList.contains('tpl-choice-option') && el.text === name)
  await editor.press(option('B'))
  assert.equal(option('B').getAttribute('aria-pressed'), 'true')
  assert.equal(option('A').getAttribute('aria-pressed'), 'false')
  assert.equal(slotAnswer(editor).value, 'B')
  // The contract holds: the card is resolved and the template can be confirmed.
  assert.equal(answerCell(editor).classList.contains('is-unresolved'), false)
  assert.equal(editor.bankEnabled(), true)
  await editor.save()
  const question = editor.sentQuestions(editor.confirms()[0])['14']
  assert.equal(question.standard_answer, 'B')
  assert.deepEqual(question.answer_slots, ['B'])
  assert.equal(review.choiceContractValid({ ...question, answer_slots_text: question.answer_slots }), true)
})

test('multiple choice typing changes the same single physical slot', async (t) => {
  const editor = await openTemplateEditor(t, { pages: [[{ ...q(), choice_mode: 'multiple' }]] })
  const input = () => answerCell(editor).find(el => el.tagName === 'input' && el.attributes['aria-label'] === '第 14 题多选答案')
  assert.equal(answerCell(editor).classList.contains('is-unresolved'), true, 'one letter is no multiple-choice answer')
  await editor.upload.type(input(), 'c a')
  assert.equal(input().value, 'AC')
  assert.equal(slotAnswer(editor).value, 'AC')
  assert.equal(answerCell(editor).classList.contains('is-unresolved'), false)
  await editor.save()
  const question = editor.sentQuestions(editor.confirms()[0])['14']
  assert.equal(question.standard_answer, 'AC')
  assert.deepEqual(question.answer_slots, ['AC'])
})

test('contradictory slots block confirm instead of accepting stale teacher answer', () => {
  assert.equal(review.choiceContractValid({ ...q(), standard_answer: 'B' }), false)
  assert.equal(review.choiceContractValid({ ...q(), blank_count: 2, slot_ids: ['14[1]', '14[2]'], answer_slots_text: ['A', 'A'] }), false)
})
