import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { installFetch, jsonResponse } from './helpers/memoryFetch.js'
import { memoryDocument } from './helpers/memoryDom.js'
import { messages } from './helpers/elementPlus.js'
import {
  bodyOf,
  button,
  flush,
  holdingBackend,
  installElementQueries,
  mountView,
  route,
  signIn,
  teacher,
} from './helpers/viewsStageReports.js'
import { historyStageLabel, educationProfileRedirect } from '../src/utils/educationStage.js'

const { default: Profile } = await import('../src/views/Profile.vue?sfc')

const STAGES = ['primary', 'junior', 'senior']
const LABELS = { primary: '小学数学', junior: '初中数学', senior: '高中数学' }

test('three stage radios support directional wrap, Home, End, and keyboard focus', async (t) => {
  installElementQueries(t)
  const { client, pinia } = await signIn({ stage: '', user: { profile_complete: false } })
  holdingBackend(client, (config) => {
    if (route(config) === 'get /auth/me') return { data: { user: teacher({ education_stage: 'primary' }) } }
    return { data: {} }
  })
  const page = await mountView(t, Profile, { path: '/profile?setup=education-stage&redirect=/reports', pinia })
  const radios = () => page.root.findAll(el => el.getAttribute('role') === 'radio' && el.classList.contains('stage-option'))
  const checked = () => radios().findIndex(radio => radio.props['aria-checked'] === true)

  assert.deepEqual(radios().map(radio => radio.text.slice(0, 4)), STAGES.map(stage => LABELS[stage]))
  assert.equal(checked(), -1, 'no stage is chosen yet')
  assert.deepEqual(radios().map(radio => radio.getAttribute('tabindex')), ['0', '-1', '-1'], 'the first radio is tabbable when nothing is selected')
  assert.match(page.root.byClass('teaching-heading').text, /无需填写年级或教材版本/)

  for (const index of STAGES.keys()) {
    for (const [key, expected] of [
      ['ArrowRight', (index + 1) % 3], ['ArrowDown', (index + 1) % 3],
      ['ArrowLeft', (index + 2) % 3], ['ArrowUp', (index + 2) % 3], ['Home', 0], ['End', 2],
    ]) {
      let prevented = false
      memoryDocument.activeElement = null
      await radios()[index].dispatch('keydown', { key, preventDefault() { prevented = true } })
      await flush()
      assert.equal(checked(), expected, `${STAGES[index]}: ${key}`)
      assert.equal(radios().indexOf(memoryDocument.activeElement), expected, `${STAGES[index]}: ${key} moves focus`)
      assert.equal(prevented, true)
      assert.deepEqual(radios().map(radio => radio.getAttribute('tabindex')), radios().map((_, i) => (i === expected ? '0' : '-1')))
    }
  }

  await radios()[0].dispatch('click')
  await flush()
  let prevented = false
  memoryDocument.activeElement = null
  await radios()[0].dispatch('keydown', { key: 'Tab', preventDefault() { prevented = true } })
  await flush()
  assert.equal(checked(), 0)
  assert.equal(prevented, false)
  assert.equal(memoryDocument.activeElement === null, true, 'Tab is left to the browser: the page moves no focus')

  // Saving sends the stage alone, then re-reads the saved profile.
  const before = client.requests.length
  await button(page, '保存并进入工作台').dispatch('click')
  await flush()
  const saved = client.requests.slice(before)
  assert.deepEqual(saved.map(config => [route(config), bodyOf(config)]), [
    ['put /auth/me', { education_stage: 'primary' }],
    ['get /auth/me', null],
  ])
  assert.deepEqual(messages.filter(message => message.type === 'success').map(message => message.message), ['教学学段已保存'])
  assert.equal(page.router.currentRoute.value.fullPath, '/reports')
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

// The profile is set to a different stage each time, so a label that followed
// the current profile instead of the record's own frozen context would show.
const otherStage = stage => (stage === 'senior' ? 'junior' : 'senior')
const FROZEN = [...STAGES.map(stage => [stage, LABELS[stage]]), [undefined, null]]

test('frozen session, pending answer, and photo import labels use their own primary context', async (t) => {
  // Answer-sheet sessions.
  const { default: AnswerSheet } = await import('../src/views/AnswerSheet.vue?sfc')
  installFetch(t, () => jsonResponse({}, { status: 404 }))
  for (const [stage, label] of FROZEN) {
    const { client, pinia } = await signIn({ stage: otherStage(stage) })
    holdingBackend(client, (config) => {
      if (route(config) === 'get /answer-sheet/sessions/s1') {
        return { data: { session_id: 's1', status: 'draft', teaching_context: stage ? { education_stage: stage } : {} } }
      }
      return { data: {} }
    })
    const page = await mountView(t, AnswerSheet, { path: '/answer-sheet?session=s1', pinia })
    const tags = page.root.byClass('session-meta').byTag('el-tag').map(tag => tag.text)
    assert.equal(tags[0], label ?? '历史会话 · 学段未标记', `answer sheet ${stage}`)
    page.unmount()
  }

  // Pending answers, one row per frozen context.
  const { default: PendingAnswers } = await import('../src/views/PendingAnswers.vue?sfc')
  {
    const { client, pinia } = await signIn({ stage: 'senior' })
    const rows = FROZEN.map(([stage], index) => ({
      question: `第 ${index + 1} 题`,
      question_preview: `第 ${index + 1} 题`,
      student_answer: '3',
      ai_answer: '3',
      teaching_context: stage ? { education_stage: stage, catalog_version: 'v1' } : null,
    }))
    holdingBackend(client, config => (route(config) === 'get /bank/pending-answers' ? { data: { items: rows, total: rows.length } } : { data: [] }))
    const page = await mountView(t, PendingAnswers, { path: '/pending-answers', pinia })
    assert.deepEqual(page.root.allByClass('stage-cell').map(cell => cell.text), FROZEN.map(([, label]) => label ?? '历史未标学段'))
    page.unmount()
  }

  // A photo import restored from its saved batch.
  const { default: BankPhotoImport } = await import('../src/views/BankPhotoImport.vue?sfc')
  for (const [stage, label] of [...FROZEN, ['legacy', null]]) {
    const { client, pinia } = await signIn({ stage: otherStage(stage), localStorage: { 'mathagent-bank-import:1': 'import-1' } })
    holdingBackend(client, (config) => {
      if (route(config) === 'get /bank/photo-import/import-1') {
        return { data: { import_id: 'import-1', teaching_context: stage ? { education_stage: stage } : {}, paired: [] } }
      }
      return { data: [] }
    })
    const page = await mountView(t, BankPhotoImport, { path: '/bank/photo-import', pinia })
    const fixed = page.root.byText('el-tag', '本批次已固定')
    if (stage === undefined) {
      assert.equal(fixed === null, true, 'a batch without a frozen stage claims none')
    } else {
      assert.equal(fixed.text, `${label ?? '历史未标学段'} · 本批次已固定`, `photo import ${stage}`)
    }
    page.unmount()
  }
})

test('ordinary paper template labels preserve frozen context instead of rewriting to current profile', async (t) => {
  const { default: Upload } = await import('../src/views/Upload.vue?sfc')
  const templateCheckpoint = JSON.stringify({ template_id: 'mock-template', category: 'daily' })
  const cases = [
    { name: 'no template yet: the profile stage', profile: 'primary', expected: '小学数学' },
    {
      name: 'pending template: its frozen stage',
      profile: 'primary',
      storage: { 'mathagent_template_recognition:1': templateCheckpoint },
      templateContext: { education_stage: 'senior' },
      expected: '高中数学',
    },
    {
      name: 'confirmed template: its frozen stage',
      profile: 'junior',
      storage: { 'mathagent_orientation_batch_id:1': 'batch-1' },
      batch: { source_mode: 'template', template_id: 'mock-template', teaching_context: { education_stage: 'primary' } },
      expected: '小学数学',
    },
    {
      name: 'legacy template without a frozen stage',
      profile: 'primary',
      storage: { 'mathagent_template_recognition:1': templateCheckpoint },
      templateContext: null,
      expected: '历史模板 · 学段未标记',
    },
  ]
  const backend = installFetch(t, () => jsonResponse({}, { status: 404 }))
  for (const { name, profile, storage = {}, templateContext, batch, expected } of cases) {
    const { client, pinia } = await signIn({ stage: profile, localStorage: storage })
    holdingBackend(client, (config) => {
      if (batch && route(config) === 'get /batch-grade/batch-1/orientation') {
        return { data: { batch_id: 'batch-1', status: 'orientation_review', papers: [{ paper_id: 'p1', status: 'orientation_review', filename: 'p1.jpg' }], ...batch } }
      }
      return { data: {} }
    })
    backend.respond((request) => {
      if (request.url === '/api/batch-grade/template/mock-template/recognition') {
        return jsonResponse({
          status: 'ocr_done',
          template_id: 'mock-template',
          pages: [{ page_index: 0, questions: [] }],
          ...(templateContext ? { teaching_context: templateContext } : {}),
        })
      }
      return jsonResponse({}, { status: 404 })
    })
    const page = await mountView(t, Upload, { path: '/upload', pinia })
    await flush()
    const label = page.root.byClass('template-card').byTag('el-tag')[0].text
    assert.equal(label, expected, name)
    page.unmount()
  }
})
