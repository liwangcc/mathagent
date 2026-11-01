import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { DONUT_CIRCUMFERENCE, donutArcs } from '../src/utils/outcomeDonut.js'
import { gradingPhases } from '../src/utils/gradingPhases.js'

const source = (relativePath) => readFile(new URL(`../${relativePath}`, import.meta.url), 'utf8')
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

test('grading loaders are decorative, labelled by their text, and still under reduced motion', async () => {
  const [spinner, sheet, progress] = await Promise.all([
    source('src/components/InkSpinner.vue'),
    source('src/components/SheetLoader.vue'),
    source('src/components/GradingProgress.vue'),
  ])
  for (const loader of [spinner, sheet]) {
    assert.match(loader, /aria-hidden="true"/)
    assert.match(loader, /@media \(prefers-reduced-motion: reduce\)[\s\S]*animation: none;/)
  }
  assert.match(progress, /role="progressbar"/)
  assert.match(progress, /:aria-valuenow="known \? current : undefined"/)
  assert.match(progress, /aria-live="off">已用时/)
})

test('grading pages use the shared loaders instead of the old ring spinner and Element mask', async () => {
  const [upload, answerSheet, stats] = await Promise.all([
    source('src/views/Upload.vue'),
    source('src/views/AnswerSheet.vue'),
    source('src/views/Stats.vue'),
  ])
  assert.doesNotMatch(upload, /class="spinner"|@keyframes spin\b|progress-bar-wrap/)
  assert.match(upload, /<GradingProgress[\s\S]*?verb="已批阅"/)
  assert.match(upload, /<GradingProgress[\s\S]*?verb="已识别"/)
  assert.doesNotMatch(answerSheet, /v-loading="busy"/)
  assert.match(answerSheet, /class="busy-veil" role="status" aria-live="polite"/)
  assert.doesNotMatch(stats, /v-loading/)
  assert.match(stats, /<OutcomeDonut[\s\S]*?:total="metric\.questions"/)
})
