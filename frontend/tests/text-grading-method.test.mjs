import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as review from '../src/utils/templateReview.js'
import * as slots from '../src/utils/templateSlots.js'

const upload = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const sheet = readFileSync(new URL('../src/views/AnswerSheet.vue', import.meta.url), 'utf8')
function extract(source, name) {
  const match = source.match(new RegExp(`^(?:async )?function ${name}\\([^\\n]*\\}$|^(?:async )?function ${name}\\([^]*?\\n\\}`, 'm'))
  assert.ok(match, `${name} is present`)
  return match[0]
}
const names = ['normalizeAnswerSpecForEditor', 'answerSpecFields', 'defaultGradingMethod',
  'toTextLines', 'toStringList', 'inferSubquestionCount', 'inferBlankCount',
  'defaultSubquestionScores', 'equalBlankScores', 'normalizedBlankScores']
const deps = { ...review, ...slots, form: { category: 'daily' } }
const editor = new Function(...Object.keys(deps), names.map(n => extract(upload, n)).join('\n')
  + `\nreturn { ${names.join(', ')} }`)(...Object.values(deps))
const normalizeSheet = new Function(extract(sheet, 'normalizeQuestion') + '\nreturn normalizeQuestion')()

for (const grading_method of [undefined, null, '', 'process', 'equivalent', 'exact']) {
  test(`missing method is not converted to process; explicit ${grading_method} survives both editors`, () => {
    const original = { question_no: '2', directory_node_id: 'n002', question_type: 'solve',
      question_text: 'Complete shared question', standard_answer: '6', blank_count: 1,
      slot_ids: ['2[8]'], answer_slots: ['6'], solution_steps: ['Full reference one', 'Full reference two'],
      ...(grading_method === undefined ? {} : { grading_method }) }
    const snapshot = structuredClone(original)
    let stored = structuredClone(original)
    for (let round = 0; round < 2; round++) {
      const draft = editor.normalizeAnswerSpecForEditor(stored)
      const outgoing = { ...draft, ...editor.answerSpecFields(draft) }
      stored = JSON.parse(JSON.stringify(outgoing))
      assert.equal(stored.grading_method, grading_method || '')
      assert.deepEqual(stored.slot_ids, original.slot_ids)
      assert.deepEqual(stored.answer_slots, original.answer_slots)
      assert.deepEqual(stored.solution_steps, original.solution_steps)
      assert.equal(stored.question_text, original.question_text)
      assert.equal(normalizeSheet(stored).grading_method, grading_method || '')
    }
    assert.equal(normalizeSheet(original).grading_method, grading_method || '')
    assert.deepEqual(original, snapshot)
  })
}

for (const [question_type, expected] of [['choice', 'exact'], ['fill', 'equivalent'], ['solve', ''], ['unknown', '']]) {
  test(`${question_type} default matches the backend grading contract`, () => {
    assert.equal(editor.defaultGradingMethod(question_type), expected)
    assert.equal(normalizeSheet({ question_type }).grading_method, expected)
  })
}

test('review detail identity prefers historical IDs and never substitutes model slot IDs', () => {
  const questionNo = new Function(extract(sheet, 'reviewDetailQuestionNo') + '\nreturn reviewDetailQuestionNo')()
  assert.equal(questionNo({ question_no: '2(1)', question_number: 'alias', parent_question_no: '2', slot_id: '2[1]' }, 0), '2(1)')
  assert.equal(questionNo({ question_number: '2(8)', parent_question_no: '2', slot_id: '2[8]' }, 1), '2(8)')
  assert.equal(questionNo({ parent_question_no: '2', slot_id: '2[8]' }, 1), '2')
  assert.equal(questionNo({ slot_id: '2[8]' }, 1), 2)
})

test('saveDraftDecision serializes independent historical detail IDs and reloads the saved session', async () => {
  const currentDraft = { value: { student_id: 101, result: { details: [
    { question_no: '2(1)', question_number: 'alias', parent_question_no: '2', slot_id: '2[1]',
      result: 'correct', teacher_note: 'first correction', paper_ids: ['student_1'] },
    { question_number: '2(8)', parent_question_no: '2', slot_id: '2[8]',
      result: 'wrong', teacher_note: 'eighth correction', paper_ids: ['student_2'] },
  ] } } }
  const session = { value: { session_id: 'detail-id-test', review_revision: 3 } }
  const saved = []
  const applied = []
  const warnings = []
  const latest = { session_id: session.value.session_id, review_revision: 4 }
  const deps = {
    currentDraft, session,
    api: { answerSheet: {
      gradingDecision: async (id, payload) => { saved.push({ id, payload: JSON.parse(JSON.stringify(payload)) }) },
      getSession: async id => { assert.equal(id, session.value.session_id); return latest },
    } },
    run: fn => fn(),
    applySession: (data, options) => { applied.push({ data, options }) },
    ElMessage: { warning: message => warnings.push(message) },
  }
  const save = new Function(...Object.keys(deps),
    extract(sheet, 'reviewDetailQuestionNo') + '\n' + extract(sheet, 'saveDraftDecision')
    + '\nreturn saveDraftDecision')(...Object.values(deps))
  const snapshot = structuredClone(currentDraft.value)
  await save()
  assert.deepEqual(saved, [{ id: 'detail-id-test', payload: {
    student_id: 101, review_revision: 3,
    decisions: [
      { question_no: '2(1)', result: 'correct', note: 'first correction' },
      { question_no: '2(8)', result: 'wrong', note: 'eighth correction' },
    ],
  } }])
  assert.deepEqual(applied, [{ data: latest, options: { chooseStage: false } }])
  assert.deepEqual(warnings, [])
  assert.deepEqual(currentDraft.value, snapshot)
  currentDraft.value.result.details[1].result = 'needs_review'
  await save()
  assert.equal(saved.length, 1, 'unresolved verdicts still block saving')
  assert.equal(warnings.length, 1)
})

for (const [name, source, binding, optionPattern] of [
  ['AnswerSheet', sheet, 'question', /<el-option\b[^>]*label="按题意判分"[^>]*\/>/],
]) {
  test(`${name} exposes the unspecified scoring option only for solve questions`, () => {
    const option = source.match(optionPattern)?.[0]
    assert.ok(option, 'the unspecified scoring option remains available')
    assert.match(option, /\bvalue=""/)
    const condition = option.match(/\bv-if="([^"]+)"/)?.[1]
    assert.ok(condition, 'the unspecified scoring option must have a question-type gate')
    const isVisible = new Function(binding, `return (${condition})`)
    for (const question_type of ['solve', 'choice', 'fill', 'unknown', undefined]) {
      assert.equal(isVisible({ question_type }), question_type === 'solve', `${name}: ${question_type}`)
    }
  })
}
