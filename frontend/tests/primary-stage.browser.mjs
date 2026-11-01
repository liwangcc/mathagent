// Actual Vue pages with every API request intercepted. No real accounts,
// student files, model calls or production mutations are used.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5189'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only the local mocked frontend is allowed')
const output = resolve(process.argv[3] || '../output/primary-stage-frontend-20260909/browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
const errors = [], checks = [], calls = [], saves = []
const labels = { primary: '小学数学', junior: '初中数学', senior: '高中数学' }
const analytics = [
  ['/class/1/students', '/api/class/1/students', '.class-table-page'],
  ['/student/1/detail', '/api/student/1/detail', '.student-detail-page'],
  ['/stats/1', '/api/stats/1', '.stats-page'],
  ['/reports', '/api/reports/summary', '.reports-page'],
]
const primaryContext = { education_stage: 'primary', curriculum_version: 'synthetic-primary-catalog', rules_version: 'synthetic-primary-rules' }

async function fixture(width) {
  let user = { id: 910001, name: '合成小学教师', role: 'teacher', education_stage: null, profile_complete: false, current_class_id: 1 }
  const context = await browser.newContext({ viewport: { width, height: width < 500 ? 844 : 1000 } })
  await context.addInitScript(() => {
    localStorage.setItem('mathagent_token', 'mock-only-primary-stage')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    localStorage.setItem('mathagent-bank-import:910001', 'synthetic-primary-import')
  })
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const path = url.pathname, method = request.method()
    const payload = request.postData() ? request.postDataJSON() : null
    const explicitStage = url.searchParams.get('education_stage') || payload?.education_stage || null
    const stage = explicitStage || user.education_stage
    calls.push({ width, path, method, explicitStage, profileStage: user.education_stage, ...(payload ? { payload } : {}) })
    assert.ok(method === 'GET' || (method === 'PUT' && path === '/api/auth/me') || (method === 'POST' && path === '/api/reports/summary'), `Unexpected mutation: ${method} ${path}`)
    const student = { id: 1, name: `${stage}-合成学生`, student_no: '001', class_id: 1, class_name: '合成班级', wrong_rate: 0.5, correction_rate: 0, weakness_count: 1, latest_exam_score: 80 }
    const rows = [{ education_stage: stage, catalog_version: `${stage}-synthetic`, chapter: '示例章', topic: `${stage}-示例知识点`, topic_key: `${stage}||示例章||知识点`, count: 1, occurrences: 1, student_count: 1, pct: 1, status: 'pending' }]
    let data = {}
    if (path === '/api/auth/me') {
      if (method === 'PUT') {
        assert.deepEqual(Object.keys(payload), ['education_stage'], 'Do not add grade or textbook requirements')
        assert.ok(Object.hasOwn(labels, payload.education_stage))
        saves.push({ width, payload })
        user = { ...user, education_stage: payload.education_stage, profile_complete: true }
      }
      data = { user }
    } else if (path === '/api/auth/classes') data = { classes: [{ id: 1, class_name: '合成班级' }], current_class_id: 1 }
    else if (path === '/api/teacher/chapters') data = [{ chapter: `${stage}-章节`, topics: ['示例知识点'] }]
    else if (path === '/api/teacher/bank') data = { total: 1, items: [{ id: 1, question: `${stage}-合成题目`, answer: '5', topic: '示例知识点', education_stage: stage }] }
    else if (path === '/api/student/list') data = { students: [student] }
    else if (path === '/api/class/1/students') data = { class_name: '合成班级', students: [student], coverage: { class_size: 1, effective_students: 1, observation_students: 0 } }
    else if (path === '/api/student/1/detail') data = { student, weaknesses: rows, wrong_questions: [], exam_scores: [], wrong_rate: 0.5, correction_rate: 0 }
    else if (path === '/api/stats/1') data = { student, total_gradings: 1, total_questions: 1, correct_count: 0, accuracy: 0, knowledge_distribution: rows }
    else if (path === '/api/reports/summary') data = { summary: `${stage}-合成报告`, total: 1, total_questions: 1, accuracy: 0, knowledge_distribution: rows }
    else if (path === '/api/class/1/letter-shares') data = { shares: [], education_stage: stage }
    else if (path === '/api/bank/photo-import/synthetic-primary-import') data = { import_id: 'synthetic-primary-import', teaching_context: primaryContext, paired: [{ question_no: '1', content: '2+3=____', answer: '5' }] }
    else if (path === '/api/bank/pending-answers') data = { items: [{ id: 1, question_preview: '2+3=____', student_answer: '5', standard_answer: '5', teaching_context: primaryContext }] }
    else if (path === '/api/answer-sheet/sessions/synthetic-primary-session') data = { session_id: 'synthetic-primary-session', status: 'draft', teaching_context: primaryContext, source_exam_pages: [], answer_sheet_pages: [], source_question_drafts: [], student_pages: [] }
    else if (path.includes('/pending')) data = []
    else if (path.includes('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(15000)
  page.on('pageerror', error => errors.push({ width, message: error.message }))
  return { context, page }
}

async function screenshot(page, file) {
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  await page.screenshot({ path: resolve(output, file), fullPage: true })
}

try {
  for (const width of [1440, 390]) {
    const { context, page } = await fixture(width)
    try {
      await page.goto(`${base}/upload?education_stage=senior`)
      await page.getByRole('heading', { name: '先选择您的教学学段' }).waitFor()
      const radios = page.locator('.stage-options [role="radio"]')
      assert.equal(await radios.count(), 3)
      assert.deepEqual(await radios.evaluateAll(items => items.map(item => item.getAttribute('tabindex'))), ['0', '-1', '-1'])
      assert.equal(await page.getByRole('button', { name: '保存并进入工作台' }).isDisabled(), true)
      assert.equal(await page.getByRole('combobox', { name: /年级|教材/ }).count(), 0)
      await radios.nth(0).focus()
      const keyboardCases = [
        ['ArrowRight', 1], ['End', 2], ['ArrowRight', 0], ['ArrowLeft', 2],
        ['Home', 0], ['ArrowDown', 1], ['ArrowUp', 0],
      ]
      for (const [key, selected] of keyboardCases) {
        await page.keyboard.press(key)
        assert.equal(await radios.nth(selected).getAttribute('aria-checked'), 'true')
        assert.equal(await radios.nth(selected).evaluate(element => element === document.activeElement), true)
      }
      const boxes = await radios.evaluateAll(items => items.map(item => {
        const r = item.getBoundingClientRect()
        return { x: r.x, right: r.right, top: r.top, bottom: r.bottom, width: r.width }
      }))
      for (const box of boxes) assert.ok(box.x >= 0 && box.right <= width + 1, JSON.stringify({ width, box }))
      if (width > 640) assert.ok(boxes.every(box => Math.abs(box.top - boxes[0].top) < 1))
      else assert.ok(boxes[0].bottom < boxes[1].top && boxes[1].bottom < boxes[2].top)
      await screenshot(page, `profile-setup-${width}.png`)
      await page.getByRole('button', { name: '保存并进入工作台' }).click()
      await page.waitForURL(url => url.pathname === '/upload')
      await page.locator('.upload-page').getByText('小学数学', { exact: true }).first().waitFor()
      assert.deepEqual(saves.filter(item => item.width === width).map(item => item.payload), [{ education_stage: 'primary' }])
      await screenshot(page, `upload-primary-${width}.png`)
      await page.goto(`${base}/profile`)
      await page.getByRole('heading', { name: '教学学段', exact: true }).waitFor()
      assert.equal(await page.getByRole('radio', { name: /小学数学/ }).getAttribute('aria-checked'), 'true')
      await screenshot(page, `profile-saved-${width}.png`)
      checks.push({ width, primarySaved: true, onlyStageRequired: true, keyboardCases, boxes })

      await page.goto(`${base}/bank?education_stage=senior`)
      await page.getByText('primary-合成题目', { exact: true }).waitFor()
      await page.getByText('小学数学', { exact: true }).first().waitFor()
      assert.equal(await page.getByRole('combobox', { name: /学段/ }).count(), 0)
      await screenshot(page, `bank-primary-${width}.png`)
      await page.goto(`${base}/exercise?education_stage=senior`)
      await page.getByText('小学数学', { exact: false }).first().waitFor()
      await screenshot(page, `exercise-primary-${width}.png`)
      for (const [path, requestPath, selector] of analytics) {
        const before = calls.length
        await page.goto(`${base}${path}?education_stage=senior`)
        await page.locator(selector).locator('.profile-stage-label').filter({ hasText: '小学数学' }).waitFor()
        await page.waitForFunction(() => !document.querySelector('.el-loading-mask') || !document.querySelector('.el-loading-mask')?.offsetParent)
        assert.ok(calls.slice(before).some(call => call.path === requestPath && call.explicitStage === 'primary'), requestPath)
        assert.equal(await page.getByRole('combobox', { name: /统计学段|报告学段/ }).count(), 0)
        await screenshot(page, `primary-${path.replaceAll('/', '_')}-${width}.png`)
        checks.push({ width, path, explicitPrimaryScope: true, conflictingQueryIgnored: true })
      }
      await page.goto(`${base}/anonymous-grade`)
      await page.locator('.upload-page').getByText('小学数学', { exact: true }).first().waitFor()
      await page.goto(`${base}/pending-answers`)
      await page.locator('.pending-answers-page').getByText('小学数学', { exact: true }).first().waitFor()
      await screenshot(page, `pending-primary-${width}.png`)
      for (const [path, selector, filename] of [
        ['/answer-sheet?session=synthetic-primary-session', '.session-meta', 'answer-sheet'],
        ['/bank/photo-import', '.photo-import-page .card-head', 'photo-import'],
      ]) {
        await page.goto(base + path)
        await page.locator(selector).getByText('小学数学', { exact: false }).first().waitFor()
        await screenshot(page, `${filename}-primary-${width}.png`)
      }

      // Switch only in Profile. An existing primary session/import must not
      // be relabelled as the teacher's newly selected secondary stage.
      await page.goto(`${base}/profile`)
      await page.getByRole('radio', { name: /初中数学/ }).click()
      await page.getByRole('button', { name: '保存学段', exact: true }).click()
      await page.getByText('教学学段已保存', { exact: true }).waitFor()
      await page.goto(`${base}/answer-sheet?session=synthetic-primary-session`)
      await page.locator('.session-meta').getByText('小学数学', { exact: true }).waitFor()
      await page.goto(`${base}/bank/photo-import`)
      await page.locator('.photo-import-page .card-head').getByText('小学数学', { exact: false }).first().waitFor()
      await page.goto(`${base}/bank?education_stage=primary`)
      await page.getByText('junior-合成题目', { exact: true }).waitFor()
      assert.deepEqual(saves.filter(item => item.width === width).map(item => item.payload), [{ education_stage: 'primary' }, { education_stage: 'junior' }])
      checks.push({ width, frozenPrimaryContextPreservedAfterProfileSwitch: true, newBankFollowsJunior: true })
    } finally { await context.close() }
  }
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, output, checks: checks.length, screenshots: 'desktop-and-mobile', realAccountRequests: 0 }))
} finally {
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, errors, checks, calls, saves }, null, 2))
  await browser.close()
}
