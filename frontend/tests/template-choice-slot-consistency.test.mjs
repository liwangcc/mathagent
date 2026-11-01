import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import * as review from '../src/utils/templateReview.js'
const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
function fn(name) {
  const start = source.indexOf(`function ${name}(`)
  const end = source.indexOf('\nfunction ', start + 1)
  assert.ok(start >= 0 && end > start)
  return source.slice(start, end)
}
function actions() {
  const names = ['markTemplateAnswerManualEdit', 'setSingleChoiceAnswer', 'onChoiceAnswerChange', 'onChoiceAnswerInput']
  const deps = { ...review, choiceOptions: ['A','B','C','D'] }
  return new Function(...Object.keys(deps), `${names.map(fn).join('\n')}\nreturn {${names.join(',')}}`)(...Object.values(deps))
}
function q() { return { question_no: '14', question_type: 'choice', choice_mode: 'single', standard_answer: 'A', answer_slots: ['A'], answer_slots_text: ['A'], slot_ids: ['14[1]'], region_kinds: ['answer'], blank_count: 1 } }
test('choice button changes parent and physical answer slot together', () => {
  const question = q(); actions().setSingleChoiceAnswer(question, 'B')
  assert.equal(question.standard_answer,'B')
  assert.deepEqual(question.answer_slots_text,['B'])
  assert.deepEqual(question.answer_slots,['B'])
  assert.equal(review.choiceContractValid(question),true)
})
test('multiple choice typing changes the same single physical slot', () => {
  const question = {...q(),choice_mode:'multiple'}
  actions().onChoiceAnswerInput(question,{target:{value:'c a'}})
  assert.equal(question.standard_answer,'AC')
  assert.deepEqual(question.answer_slots_text,['AC'])
})
test('contradictory slots block confirm instead of accepting stale teacher answer', () => {
  assert.equal(review.choiceContractValid({...q(),standard_answer:'B'}),false)
  assert.equal(review.choiceContractValid({...q(),blank_count:2,slot_ids:['14[1]','14[2]'],answer_slots_text:['A','A']}),false)
})
