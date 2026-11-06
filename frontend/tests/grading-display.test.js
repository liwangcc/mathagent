import assert from 'node:assert/strict'
import test from 'node:test'
import './helpers/srcModules.js'
import { loadApiClient } from './helpers/apiClient.js'
import { installFetch } from './helpers/memoryFetch.js'
import { mountComponent } from './helpers/mountComponent.js'

import { DONUT_CIRCUMFERENCE, donutArcs } from '../src/utils/outcomeDonut.js'
import { gradingPhases } from '../src/utils/gradingPhases.js'

const outcomes = (correct, partial, wrong) => [
  { key: 'correct', label: '正确', value: correct },
  { key: 'partial', label: '部分正确', value: partial },
  { key: 'wrong', label: '错误', value: wrong },
]

test('donut arcs split the ring by judged answers, with gaps only between neighbours', () => {
  const arcs = donutArcs(outcomes(222, 12, 51))
  assert.equal(Math.round(arcs.reduce((sum, arc) => sum + arc.share, 0)), 100)
  assert.equal(Math.round(arcs[0].share), 78)
  // Each arc starts where the previous span ended; the gap is taken from the arc itself.
  assert.ok(Math.abs(arcs[1].offset - (arcs[0].length + 3)) < 1e-9)
  assert.ok(Math.abs(arcs[2].offset + arcs[2].length + 3 - DONUT_CIRCUMFERENCE) < 1e-9)

  const lone = donutArcs(outcomes(5, 0, 0))
  assert.ok(Math.abs(lone[0].length - DONUT_CIRCUMFERENCE) < 1e-9, 'a lone outcome closes the ring')
})

test('donut arcs scale to a wider total and survive missing or invalid counts', () => {
  const arcs = donutArcs(outcomes(70, 10, 10), 100)
  assert.equal(arcs[0].share, 70, 'the correct arc matches correct / every counted question')
  const drawn = arcs.reduce((sum, arc) => sum + arc.length, 0)
  assert.ok(drawn < DONUT_CIRCUMFERENCE * 0.9, 'uncounted answers stay as bare track')

  const empty = donutArcs(outcomes(null, undefined, 'x'))
  assert.deepEqual(empty.map((arc) => [arc.value, arc.share, arc.length]), [[0, 0, 0], [0, 0, 0], [0, 0, 0]])
  assert.equal(donutArcs(outcomes(-4, 2, 2))[0].value, 0, 'negative counts never draw')
})

test('grading phases follow the batch from recognition to archive', () => {
  assert.deepEqual(gradingPhases(), { labels: ['识别', '身份', '判分', '复核', '归档'], current: 0 })
  assert.equal(gradingPhases({ identityPending: true }).current, 1)
  assert.equal(gradingPhases({ grading: true }).current, 2)
  assert.equal(gradingPhases({ grading: true, reviewing: true }).current, 3)
  assert.equal(gradingPhases({ reviewing: true, archived: true }).current, 4)
})

test('anonymous batches never show identity or archive phases', () => {
  const anonymous = gradingPhases({ anonymous: true, identityPending: true })
  assert.deepEqual(anonymous, { labels: ['识别', '判分', '复核'], current: 0 })
  assert.equal(gradingPhases({ anonymous: true, reviewing: true, archived: true }).current, 2)
})

const TEACHER = { id: 1, role: 'teacher', education_stage: 'senior', profile_complete: true }
const signedIn = (entries = {}) => loadApiClient({
  localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER), ...entries },
})
const byRole = (root, role) => root.findAll(el => el.getAttribute('role') === role)

// Element Plus's v-loading mask (a no-op stand-in when mounted), recording
// every value a page binds it to.
function loadingMaskRecorder() {
  const values = []
  const record = (_el, binding) => { values.push(binding.value) }
  return { values, plugin: { install: app => { app.directive('loading', { mounted: record, updated: record }) } } }
}

// Node paints no frames: animation callbacks a component asks for never run.
function withoutAnimationFrames(t) {
  const previous = ['requestAnimationFrame', 'cancelAnimationFrame'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)])
  globalThis.requestAnimationFrame = () => 0
  globalThis.cancelAnimationFrame = () => {}
  t.after(() => {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  })
}

test('grading loaders are decorative and labelled by their text', async (t) => {
  const loaders = {
    InkSpinner: (await import('../src/components/InkSpinner.vue?sfc')).default,
    SheetLoader: (await import('../src/components/SheetLoader.vue?sfc')).default,
  }
  for (const [name, Loader] of Object.entries(loaders)) {
    const page = await mountComponent(t, Loader)
    const [svg] = page.root.byTag('svg')
    assert.equal(svg.getAttribute('aria-hidden'), 'true', name)
    assert.equal(svg.getAttribute('focusable'), 'false', name)
    page.unmount()
  }

  const { default: GradingProgress } = await import('../src/components/GradingProgress.vue?sfc')
  const unknownTotal = await mountComponent(t, GradingProgress, { props: { current: 3, verb: '已识别', unit: '张', message: '正在识别学生试卷' } })
  const [track] = byRole(unknownTotal.root, 'progressbar')
  assert.equal(track.getAttribute('aria-label'), '已识别 3 张')
  assert.equal(track.hasAttribute('aria-valuenow'), false, 'no value while the total is unknown')
  assert.equal(track.hasAttribute('aria-valuemax'), false)
  assert.ok(track.classList.contains('is-indeterminate'))
  // The sheet loader is decorative: the message beside it carries the status.
  assert.equal(unknownTotal.root.byClass('grading-progress__live').find(el => el.tagName === 'svg').getAttribute('aria-hidden'), 'true')
  assert.equal(unknownTotal.root.byClass('grading-progress__copy').text, '正在识别学生试卷')
  unknownTotal.unmount()

  const known = await mountComponent(t, GradingProgress, { props: { current: 3, total: 10, verb: '已批阅', elapsed: '0:42', live: false } })
  const [knownTrack] = byRole(known.root, 'progressbar')
  assert.equal(knownTrack.getAttribute('aria-label'), '已批阅 3 / 10 份')
  assert.equal(knownTrack.getAttribute('aria-valuenow'), '3')
  assert.equal(knownTrack.getAttribute('aria-valuemax'), '10')
  assert.equal(knownTrack.getAttribute('aria-valuemin'), '0')
  const elapsed = known.root.byClass('grading-progress__elapsed')
  assert.equal(elapsed.getAttribute('aria-live'), 'off', 'the running clock is not announced')
  assert.equal(elapsed.text, '已用时 0:42')
})

test('grading pages use the shared loaders: Upload reports recognition and grading through GradingProgress', async (t) => {
  // A teacher comes back to a recognized batch; grading starts and its stream
  // has not answered yet.
  const client = await signedIn({ 'mathagent_orientation_batch_id:1': 'b1' })
  const paper = (n) => ({ paper_id: `p${n}`, filename: `${n}.jpg`, identity: { status: 'matched', student_id: n, student_name: `学生${n}` }, answers: [] })
  client.respond((config) => {
    if (config.url === '/batch-grade/availability') return { data: { status: 'available', label: '可用' } }
    if (config.url === '/batch-grade/template/availability') return { data: { status: 'available' } }
    if (config.url === '/batch-grade/b1/orientation') {
      return { data: { batch_id: 'b1', status: 'recognized', papers: [paper(1), paper(2)], questions: [{ question_no: '1' }], uploaded_papers: 2, total_papers: 2, succeeded_papers: 2, can_grade: true } }
    }
    return { data: {} }
  })
  const backend = installFetch(t, () => new Promise(() => {}))
  const { default: Upload } = await import('../src/views/Upload.vue?sfc')
  const page = await mountComponent(t, Upload, { path: '/upload' })
  assert.deepEqual(backend.requests.map(request => request.url), ['/api/batch-grade/confirm'])
  const progress = page.root.allByClass('grading-progress')
  assert.deepEqual(progress.map(section => byRole(section, 'progressbar')[0]?.getAttribute('aria-label')), ['已识别 2 / 2 张', '已批阅 0 / 2 份'])
  assert.match(progress[1].text, /^已批阅 0 \/ 2 份/)
  assert.ok(progress[1].byClass('grading-progress__elapsed'), 'grading shows its running clock')
  assert.equal(page.root.find(el => el.classList.contains('spinner') || el.classList.contains('progress-bar-wrap')), null, 'no old ring spinner or bar')
})

test('grading pages use the shared loaders: AnswerSheet covers busy work with a status veil, not an Element mask', async (t) => {
  const client = await signedIn()
  let answer
  client.respond((config) => {
    if (config.url === '/answer-sheet/sessions/s1') return new Promise((resolve) => { answer = () => resolve({ status: 404, data: { detail: '会话不存在' } }) })
    return { data: {} }
  })
  const masks = loadingMaskRecorder()
  const { default: AnswerSheet } = await import('../src/views/AnswerSheet.vue?sfc')
  const page = await mountComponent(t, AnswerSheet, { path: '/answer-sheet?session=s1', plugins: [masks.plugin] })
  // Restoring the session from the link keeps the page busy.
  const veil = page.root.byClass('busy-veil')
  assert.ok(veil, 'the page is covered while it works')
  assert.equal(veil.getAttribute('role'), 'status')
  assert.equal(veil.getAttribute('aria-live'), 'polite')
  assert.equal(veil.find(el => el.tagName === 'svg' && el.classList.contains('sheet-loader')).getAttribute('aria-hidden'), 'true')
  assert.match(veil.text, /正在处理，进度会自动保存/)
  assert.deepEqual(masks.values.filter(Boolean), [], 'no Element Plus loading mask over the busy page')
  answer()
  await page.flush()
  assert.equal(page.root.byClass('busy-veil'), null)
})

test('grading pages use the shared loaders: Stats loads behind the sheet loader and rings every counted question', async (t) => {
  withoutAnimationFrames(t)
  const client = await signedIn()
  let answer
  client.respond((config) => {
    if (config.url === '/student/list') return { data: { items: [{ id: 5, name: '王一', student_no: 'S05', class_name: '高一（1）班' }], total: 1 } }
    if (config.url === '/stats/5') {
      // 100 counted questions, of which 90 are judged.
      return new Promise((resolve) => { answer = () => resolve({ data: { total_gradings: 3, total_questions: 100, correct_count: 70, partial_count: 10, wrong_count: 10, accuracy: 0.7 } }) })
    }
    return { data: {} }
  })
  const masks = loadingMaskRecorder()
  const { default: Stats } = await import('../src/views/Stats.vue?sfc')
  const page = await mountComponent(t, Stats, { path: '/stats/5', plugins: [masks.plugin] })
  const loading = page.root.byClass('stats-loading')
  assert.equal(loading.getAttribute('role'), 'status')
  assert.ok(loading.find(el => el.tagName === 'svg' && el.classList.contains('sheet-loader')))
  assert.equal(loading.text, '正在载入统计…')
  answer()
  await page.flush()
  assert.equal(page.root.byClass('stats-loading'), null)
  const legend = page.root.byClass('outcome-donut__legend').byTag('li')
  assert.deepEqual(legend.map(item => [item.byTag('span').at(-1).text, item.byTag('strong')[0].text, item.byTag('em')[0].text]), [
    ['正确', '70', '70%'],
    ['部分正确', '10', '10%'],
    ['错误', '10', '10%'],
  ], 'shares are of all 100 counted questions, not of the 90 judged')
  assert.deepEqual(masks.values, [], 'Stats never asks for an Element Plus loading mask')
})
