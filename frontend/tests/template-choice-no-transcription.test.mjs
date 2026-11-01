import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { templateIsPureDrawing } from '../src/utils/templateReview.js'
const source = readFileSync(new URL('../src/views/Upload.vue', import.meta.url), 'utf8')
const start = source.indexOf('const templateCanConfirm = computed(() => {')
const end = source.indexOf('\nconst templateChoiceEntries', start)
assert.ok(start >= 0 && end > start)
const expression = source.slice(start, end).replace('const templateCanConfirm = ', '')
function canConfirm(q) {
  const deps = {
    computed: fn => fn(), form: { category: 'daily' },
    templatePages: { value: [{ page_index: 0, questions: [q] }] },
    templateCutPreviewPages: { value: [] },
    templateReviewPendingPageIndexes: () => new Set(),
    templateGeometryReview: { value: { needsReview: false } },
    templateChoiceConflictEntries: () => [],
    questionScoringValid: () => true,
    templateIsPureDrawing,
  }
  return new Function(...Object.keys(deps), `return ${expression}`)(...Object.values(deps))
}
test('actual confirmation gate accepts deliberately untranscribed choice text', () => {
  assert.equal(canConfirm({ question_no: '3', question_type: 'choice', choice_mode: 'single', question_text: '', standard_answer: 'D' }), true)
})
test('actual confirmation gate still requires non-choice text and all reference answers', () => {
  for (const question_type of ['fill','solve']) {
    assert.equal(canConfirm({ question_no: '2', question_type, question_text: '', standard_answer: '8' }), false)
  }
  assert.equal(canConfirm({ question_no: '3', question_type: 'choice', question_text: '', standard_answer: '' }), false)
})

test('pure drawing may save an empty standard but must retain the problem statement', () => {
  for (const question_type of ['fill', 'solve', 'choice']) {
    const question = { question_no: '2', question_type, answer_kind: 'drawing', question_text: '按条件画图', standard_answer: '' }
    assert.equal(canConfirm(question), true)
    assert.equal(canConfirm({ ...question, question_text: '' }), false)
    assert.equal(canConfirm({ ...question, answer_kind: 'mixed' }), false)
  }
})
