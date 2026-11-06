import './helpers/srcModules.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { loadApiClient } from './helpers/apiClient.js'
import { mountComponent } from './helpers/mountComponent.js'

const { default: Stats } = await import('../src/views/Stats.vue?sfc')

const TEACHER = { id: 1, role: 'teacher', education_stage: 'junior', profile_complete: true }

// The page's figures count up on animation frames and its outcome ring inks
// in on one; with reduced motion the figures show their value at once.
const reducedMotion = {
  install() {
    window.matchMedia = query => ({
      matches: query === '(prefers-reduced-motion: reduce)', media: query, addEventListener() {}, removeEventListener() {},
    })
    globalThis.requestAnimationFrame = callback => window.requestAnimationFrame(callback)
    globalThis.cancelAnimationFrame = id => window.cancelAnimationFrame(id)
  },
}

// The statistics page of a signed-in teacher, opened on the first of
// `students`; the backend answers /stats/<id> with statsById[id].
async function openStats(t, statsById) {
  const client = await loadApiClient({
    localStorage: { mathagent_token: 'teacher-token', mathagent_user: JSON.stringify(TEACHER) },
  })
  const ids = Object.keys(statsById).map(Number)
  client.respond((config) => {
    if (config.url === '/student/list') {
      return { data: { items: ids.map(id => ({ id, name: `学生${id}`, student_no: String(id), class_name: '七年级一班' })), total: ids.length } }
    }
    const id = /^\/stats\/(\d+)$/.exec(config.url)?.[1]
    if (id) return { data: statsById[id] }
    return { data: {} }
  })
  const page = await mountComponent(t, Stats, { path: `/stats/${ids[0]}`, plugins: [reducedMotion] })
  t.after(() => {
    delete globalThis.requestAnimationFrame
    delete globalThis.cancelAnimationFrame
  })
  // Picking a student in the selector, as ElSelect reports it.
  const choose = async (id) => {
    const select = page.root.find(el => el.tagName === 'el-select' && el.getAttribute('aria-label') === '选择学生')
    select.handlers['update:modelValue'](id)
    await select.handlers.change(id)
    await page.flush()
  }
  return { client, page, choose }
}

// What the page shows for the selected student: the ledger's figures, the
// accuracy in the outcome ring and the ring's correct / partial / wrong counts.
function shown(page) {
  const ledger = Object.fromEntries(page.root.byClass('stats-ledger').children.map(cell => [cell.byTag('dt')[0].text, cell.byTag('dd')[0].text]))
  const legend = page.root.byClass('outcome-donut__legend').byTag('li').map(item => [item.byTag('span')[0].text, item.byTag('strong')[0].text])
  return {
    gradings: ledger['批阅份数'],
    questions: ledger['统计题数'],
    accuracy: page.root.byClass('outcome-donut__center').byTag('strong')[0].text,
    correct: ledger['正确题数'],
    partial: ledger['部分正确'],
    wrong: ledger['错误题数'],
    legend,
  }
}

test('student statistics consumes the complete backend contract', async (t) => {
  const { client, page, choose } = await openStats(t, {
    // The current contract, next to every legacy field it supersedes.
    7: {
      total_gradings: 4, total_exams: 9, total: 11, count: 12, total_questions: 30,
      accuracy: 0.8, average_correct_rate: 0.5, accuracy_percent: 20,
      correct_count: 20, partial_count: 4, wrong_count: 6, right_count: 1, error_count: 2,
    },
    // An older payload: exam count, average rate, no question total.
    8: { total_exams: 9, average_correct_rate: 0.5, accuracy_percent: 20 },
    // Only a percentage, already scaled.
    9: { total_gradings: 2, total_questions: 10, accuracy_percent: 72.5 },
    // A zero accuracy is a measured value, not a missing one.
    10: { total_gradings: 1, total_questions: 5, accuracy: 0, average_correct_rate: 0.5, correct_count: 0, partial_count: 0, wrong_count: 5 },
  })
  assert.deepEqual(client.requests.find(request => request.url === '/stats/7').params, { education_stage: 'junior' })
  assert.deepEqual(shown(page), {
    gradings: '4', questions: '30', accuracy: '80%', correct: '20', partial: '4', wrong: '6',
    legend: [['正确', '20'], ['部分正确', '4'], ['错误', '6']],
  })

  await choose(8)
  assert.deepEqual(shown(page), {
    gradings: '9', questions: '0', accuracy: '50%', correct: '-', partial: '-', wrong: '-',
    legend: [['正确', '0'], ['部分正确', '0'], ['错误', '0']],
  })

  await choose(9)
  assert.equal(shown(page).accuracy, '72.5%')

  await choose(10)
  assert.deepEqual(shown(page), {
    gradings: '1', questions: '5', accuracy: '0%', correct: '0', partial: '0', wrong: '5',
    legend: [['正确', '0'], ['部分正确', '0'], ['错误', '5']],
  })
})

test('student statistics hides empty knowledge buckets instead of showing rows of zeroes', async (t) => {
  const { page, choose } = await openStats(t, {
    7: {
      total_gradings: 3, total_questions: 12, accuracy: 0.6,
      knowledge_distribution: [
        { education_stage: 'junior', chapter: '方程', topic: '一元一次方程', count: 3, pct: 0.75, student_count: 1 },
        { education_stage: 'junior', chapter: '有理数', topic: '有理数的加法', count: 0 },
        { education_stage: 'junior', chapter: '代数式', topic: '整式', count: 1 },
        { education_stage: 'junior', chapter: '几何', topic: '三角形' },
      ],
      error_distribution: [{ topic: '计算错误', count: 5 }],
    },
    8: {
      total_gradings: 1, total_questions: 4, accuracy: 1,
      knowledge_distribution: [{ topic: '有理数的加法', count: 0 }, { topic: '整式', count: '0' }],
    },
  })
  const rows = () => page.root.byClass('stats-bars')?.byTag('li')
    .map(item => [item.byClass('stats-bars__label').byTag('strong')[0].text, item.byClass('stats-bars__label').byTag('span')[0].text]) ?? []
  assert.deepEqual(rows(), [['一元一次方程', '3 题 · 75%'], ['整式', '1 题 · 25%']])
  assert.equal(page.root.byClass('stats-knowledge__empty'), null)
  assert.doesNotMatch(page.root.byClass('stats-knowledge').text, /计算错误|有理数的加法|三角形/)

  await choose(8)
  assert.deepEqual(rows(), [])
  assert.equal(page.root.byClass('stats-knowledge__empty').text, '暂无错题分布数据')
})
