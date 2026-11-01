import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { EDUCATION_STAGES, historyStageLabel, educationProfileRedirect } from '../src/utils/educationStage.js'

const view = name => readFileSync(new URL(`../src/views/${name}.vue`, import.meta.url), 'utf8')
const profile = view('Profile')
const keyboardSource = profile.match(/function onStageKeydown\(event, value\) \{[\s\S]*?\n\}/)?.[0]
assert.ok(keyboardSource, 'Use the actual profile keyboard handler')

test('three stage radios support directional wrap, Home, End, and keyboard focus', () => {
  const selectedStage = { value: '' }
  const handler = new Function('EDUCATION_STAGES', 'selectedStage', `${keyboardSource}; return onStageKeydown`)(EDUCATION_STAGES, selectedStage)
  for (const [index, item] of EDUCATION_STAGES.entries()) {
    for (const [key, expected] of [
      ['ArrowRight', (index + 1) % 3], ['ArrowDown', (index + 1) % 3],
      ['ArrowLeft', (index + 2) % 3], ['ArrowUp', (index + 2) % 3], ['Home', 0], ['End', 2],
    ]) {
      let focused = -1, prevented = false
      handler({ key, preventDefault() { prevented = true }, currentTarget: { parentElement: {
        querySelectorAll: () => EDUCATION_STAGES.map((_, option) => ({ focus() { focused = option } })),
      } } }, item.value)
      assert.equal(selectedStage.value, EDUCATION_STAGES[expected].value, `${item.value}: ${key}`)
      assert.equal(focused, expected)
      assert.equal(prevented, true)
    }
  }
  let prevented = false
  selectedStage.value = 'primary'
  handler({ key: 'Tab', preventDefault() { prevented = true } }, 'primary')
  assert.equal(selectedStage.value, 'primary')
  assert.equal(prevented, false)
  assert.match(profile, /!selectedStage && item\.value === EDUCATION_STAGES\[0\]\.value/)
  assert.match(profile, /auth\.updateProfile\(\{ education_stage: stage \}\)/)
  assert.match(profile, /无需填写年级或教材版本/)
})

test('primary profiles enter all teaching routes and historical stages remain explicit', () => {
  const auth = { token: 'mock-only', user: { education_stage: 'primary', profile_complete: true, role: 'teacher' } }
  for (const path of ['/upload', '/anonymous-grade', '/answer-sheet', '/bank', '/exercise', '/reports', '/stats']) {
    assert.equal(educationProfileRedirect({ name: path.slice(1), fullPath: path, meta: {} }, auth), null)
  }
  assert.equal(historyStageLabel('primary'), '小学数学')
  assert.equal(historyStageLabel('junior'), '初中数学')
  assert.equal(historyStageLabel('senior'), '高中数学')
  assert.equal(historyStageLabel(null), '历史未标学段')
  assert.equal(historyStageLabel('unknown'), '历史未标学段')
})

test('frozen session, pending answer, and photo import labels use their own primary context', () => {
  for (const [page, binding] of [['AnswerSheet', 'session'], ['PendingAnswers', 'row'], ['BankPhotoImport', 'teachingContext']]) {
    const source = view(page)
    const expression = source.match(/\{\{\s*([^{}]*?education_stage === 'primary'[^{}]*?)\s*\}\}/)?.[1]
    assert.ok(expression, page)
    const label = new Function(binding, `return ${expression}`)
    for (const [stage, expected] of [['primary', '小学数学'], ['junior', '初中数学'], ['senior', '高中数学']]) {
      const context = { education_stage: stage }
      assert.equal(label(binding === 'teachingContext' ? context : { teaching_context: context }), expected)
    }
    assert.match(label(binding === 'teachingContext' ? {} : {}), /历史/)
  }
})

test('ordinary paper template labels preserve frozen context instead of rewriting to current profile', () => {
  const source = view('Upload')
  const body = source.match(/const templateStageLabel = computed\(\(\) => \{([\s\S]*?)\n\}\)/)?.[1]
  assert.ok(body)
  const label = new Function('pendingTemplateId', 'confirmedTemplateId', 'templateTeachingContext', 'auth', body)
  const auth = { user: { education_stage: 'primary' } }
  assert.equal(label({ value: '' }, { value: '' }, { value: null }, auth), '小学数学')
  assert.equal(label({ value: 'mock-template' }, { value: '' }, { value: { education_stage: 'senior' } }, auth), '高中数学')
  assert.equal(label({ value: '' }, { value: 'mock-template' }, { value: { education_stage: 'primary' } }, { user: { education_stage: 'junior' } }), '小学数学')
  assert.equal(label({ value: 'mock-legacy' }, { value: '' }, { value: null }, auth), '历史模板 · 学段未标记')
})
