// Actual Vue pages with synthetic completed batches; no account/model requests.
// Start Vite/preview, then run with Playwright on NODE_PATH:
// node tests/batch-report-layout.browser.mjs http://127.0.0.1:5197 <artifacts-dir>
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local mocked pages may be tested')
const output = resolve(process.argv[3] || '../outputs/batch-report-layout-browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const results = [], errors = [], mutations = []
const topics = [
  ['函数', '函数的单调性与最值'],
  ['方程与不等式', '一元二次方程根与系数关系及实际问题中的综合应用'],
  ['平面几何', '相似三角形的判定与性质'],
  ['概率与统计', '随机事件的概率'],
  ['综合应用', 'KnowledgePointWithoutSpaces'.repeat(4)],
  ['数与式', '实数的运算'],
]
const report = {
  status: 'ready', text: '建议先讲评函数与方程，再安排针对性练习。',
  stats: {
    question_count: 100, paper_count: 4, correct_count: 72, partial_count: 8,
    wrong_count: 20, needs_review_count: 0, error_rate: .28,
    top_errors: [1, 2, 3].map(question_no => ({ question_no, error_count: 3, judged_count: 4, attempt_count: 4, error_rate: .75, question: '合成测试题目摘要' })),
    knowledge_points: topics.map(([chapter, topic], index) => ({ chapter, topic, error_count: index + 1, attempt_count: 8, error_rate: (index + 1) / 8 })),
  },
}

try {
  for (const mode of ['identified', 'anonymous']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.addInitScript(({ mode }) => {
      localStorage.setItem('mathagent_token', 'mock-only-token')
      localStorage.setItem('disclaimer_agreed', 'true')
      localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
      localStorage.setItem(`mathagent_review_batch_id${mode === 'anonymous' ? ':anonymous' : ''}:910001`, `mock-${mode}`)
    }, { mode })
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin !== new URL(base).origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      if (request.method() !== 'GET') {
        mutations.push({ path: url.pathname, method: request.method() })
        return route.abort()
      }
      let data = {}
      if (url.pathname === '/api/auth/me') data = { user: { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true } }
      else if (url.pathname === '/api/auth/classes') data = { classes: [], current_class_id: null }
      else if (url.pathname.endsWith('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
      else if (url.pathname === `/api/batch-grade/review/mock-${mode}`) data = {
        batch_id: `mock-${mode}`, identity_mode: mode, summary: { total: 4 }, report,
        results: [{ paper_id: 'synthetic-page', student_name: '示例记录', category: 'daily', disposition: 'active', correct: 18, total: 25, correct_rate: .72 }],
      }
      else if (url.pathname === `/api/batch-grade/report/mock-${mode}`) data = { report }
      else if (url.pathname.includes('/pending')) data = []
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
    })
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(base + (mode === 'anonymous' ? '/anonymous-grade' : '/upload'))
    await page.locator('.batch-report-distribution-block').waitFor()
    await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    for (const width of [1440, 1127, 901, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      const card = page.locator('.batch-report-distribution-card')
      await card.scrollIntoViewIfNeeded()
      const geometry = await card.evaluate(element => {
        const grid = element.querySelector('.batch-report-distributions')
        const block = element.querySelector('.batch-report-distribution-block')
        const list = element.querySelector('.batch-report-bar-list')
        const labels = [...element.querySelectorAll('.batch-report-bar-label')]
        return {
          availableWidth: grid.getBoundingClientRect().width,
          usedWidth: block.getBoundingClientRect().width,
          listWidth: list.clientWidth, listScrollWidth: list.scrollWidth,
          labels: labels.map(label => {
            const title = label.querySelector('span'), count = label.querySelector('strong')
            return { titleWidth: title.clientWidth, titleScrollWidth: title.scrollWidth,
              whiteSpace: getComputedStyle(title).whiteSpace,
              countTop: count.getBoundingClientRect().top, countHeight: count.getBoundingClientRect().height,
              countWhiteSpace: getComputedStyle(count).whiteSpace }
          }),
        }
      })
      // The old leftover two-column layout fails here at every desktop width.
      assert.ok(geometry.usedWidth >= geometry.availableWidth - 2, `${mode} ${width}: knowledge distribution uses only ${geometry.usedWidth}/${geometry.availableWidth}px`)
      assert.ok(geometry.listScrollWidth <= geometry.listWidth + 1, `${mode} ${width}: horizontal clipping`)
      for (const label of geometry.labels) {
        assert.notEqual(label.whiteSpace, 'nowrap', 'Long topic names must wrap, not disappear behind ellipsis')
        assert.ok(label.titleScrollWidth <= label.titleWidth + 1, 'Long unbroken topic overflows')
        assert.equal(label.countWhiteSpace, 'nowrap', 'Error count must stay on one line')
      }
      assert.equal(await page.locator('.batch-report-bar-row').count(), topics.length)
      await page.locator('.batch-report-bar-list').evaluate(element => { element.scrollTop = element.scrollHeight })
      await page.locator('.batch-report-bar-row').last().scrollIntoViewIfNeeded()
      await page.locator('.batch-report-bar-list').evaluate(element => { element.scrollTop = 0 })
      await card.screenshot({ path: resolve(output, `${mode}-${width}.png`) })
      if (width === 1440) await page.locator('.batch-report-panel').screenshot({ path: resolve(output, `${mode}-report.png`) })
      results.push({ mode, width, ...geometry })
    }
    await context.close()
  }
  assert.deepEqual(errors, [])
  assert.deepEqual(mutations, [])
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, results, errors, mutations }, null, 2))
  console.log(JSON.stringify({ ok: true, scenarios: results.length, output }))
} finally { await browser.close() }
