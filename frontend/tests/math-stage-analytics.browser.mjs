// Real Vue frontend; every API call is intercepted with synthetic records only.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5189'
assert.equal(new URL(base).hostname, '127.0.0.1')
const output = resolve(process.argv[3] || '../outputs/math-stage-profile-analytics-browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const errors = [], calls = [], checks = []
const pages = [
  ['/class/1/students', '/api/class/1/students', '.class-table-page'],
  ['/student/1/detail', '/api/student/1/detail', '.student-detail-page'],
  ['/stats/1', '/api/stats/1', '.stats-page'],
  ['/reports', '/api/reports/summary', '.reports-page'],
]
const stageLabel = stage => stage === 'junior' ? '初中数学' : '高中数学'
let profileStage = 'senior', heldRequest = null, releaseHeld = null
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(() => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    localStorage.setItem('disclaimer_agreed', 'true')
  })
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const p = url.pathname, payload = req.postData() ? req.postDataJSON() : null
    const stage = url.searchParams.get('education_stage') || payload?.education_stage || ''
    calls.push({ path: p, stage, profileStage, method: req.method(), query: url.search, ...(payload ? { payload } : {}) })
    const marker = `${stage || 'UNSCOPED'}-示例`
    const student = { id: 1, name: marker, student_no: '001', class_id: 1, class_name: '测试班', wrong_rate: .5, correction_rate: 0, weakness_count: 1, risk_level: 'watch', latest_exam_score: 80 }
    const rows = [{ education_stage: stage, catalog_version: `${stage}-synthetic`, chapter: '示例章', topic: marker, topic_key: `${stage}||示例章||知识点`, count: 1, occurrences: 1, student_count: 1, pct: 1, status: 'pending' }]
    let data = {}
    if (p === '/api/auth/me') data = { user: { id: 910001, name: '示例管理员', role: 'admin', education_stage: profileStage, profile_complete: true, current_class_id: 1 } }
    else if (p === '/api/auth/classes') data = { classes: [{ id: 1, class_name: '测试班' }], current_class_id: 1 }
    else if (p === '/api/class/1/students') data = { class_name: '测试班', students: [student], coverage: { class_size: 1, effective_students: 1, observation_students: 0 } }
    else if (p === '/api/student/list') data = { students: [{ ...student, name: '示例学生' }] }
    else if (p === '/api/student/1/detail') data = { student, weaknesses: rows, wrong_questions: [], exam_scores: rows.map(row => ({ ...row, exam_name: '示例月考', score: 80, date: '2026-09-09' })), wrong_rate: .5, correction_rate: 0, latest_exam_delta: null, latest_exam_rank_delta: null }
    else if (p === '/api/stats/1') data = { student, total_gradings: 1, total_questions: 1, correct_count: 0, accuracy: 0, knowledge_distribution: rows }
    else if (p === '/api/reports/summary') data = { summary: marker, total: 1, total_questions: 1, accuracy: 0, knowledge_distribution: rows }
    else if (p === '/api/class/1/letter-shares') data = { shares: [], education_stage: stage }
    else if (p === '/api/class/1/report/batch') data = { mode: 'share', shares: [], skipped: [], education_stage: stage }
    else if (p.includes('/pending')) data = []
    else if (p.includes('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
    if (heldRequest?.path === p && heldRequest.stage === stage) {
      heldRequest = null
      await new Promise(resolve => { releaseHeld = resolve })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(12000)
  page.on('pageerror', error => errors.push(error.message))
  for (const stage of ['senior', 'junior']) {
    profileStage = stage
    for (const [path, requestPath, selector] of pages) {
      console.log(`Checking profile=${stage} ${path}`)
      const start = calls.length
      await page.goto(`${base}${path}?education_stage=${stage === 'senior' ? 'junior' : 'senior'}`)
      const view = page.locator(selector)
      await view.locator('.profile-stage-label').filter({ hasText: stageLabel(stage) }).waitFor()
      await view.getByText(`${stage}-示例`, { exact: true }).first().waitFor()
      await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
      assert.equal(await page.getByRole('combobox', { name: /统计学段|报告学段/ }).count(), 0)
      assert.ok(calls.slice(start).some(call => call.path === requestPath && call.stage === stage))
      for (const call of calls.slice(start).filter(call => [requestPath, '/api/class/1/letter-shares', '/api/class/1/students'].includes(call.path))) {
        assert.equal(call.stage, stage, `query must not override profile stage: ${call.path}`)
      }
      if (path === '/reports') {
        const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/class/1/report/batch')
        await page.getByRole('button', { name: '准备并复制链接', exact: true }).click()
        assert.equal((await response).request().postDataJSON().education_stage, stage)
      }
      await page.screenshot({ path: resolve(output, `${stage}${path.replaceAll('/', '_')}.png`), fullPage: true })
      checks.push({ stage, path, queryIgnored: true, noStageSelector: true })
    }
  }
  // Deliver an old-scope API response only after the new scope has rendered.
  for (const [path, requestPath, selector] of pages) {
    profileStage = 'junior'
    releaseHeld = null
    heldRequest = { path: requestPath, stage: 'junior' }
    await page.goto(base + path)
    await page.waitForFunction(() => document.querySelector('.profile-stage-label')?.textContent.includes('初中数学'))
    const deadline = Date.now() + 12000
    while (!releaseHeld && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20))
    assert.ok(releaseHeld, `old response must be held for ${path}`)
    profileStage = 'senior'
    await page.evaluate(async () => {
      const { useAuthStore } = await import('/src/stores/auth.js')
      const auth = useAuthStore()
      auth.user = { ...auth.user, education_stage: 'senior' }
    })
    const view = page.locator(selector)
    await view.getByText('senior-示例', { exact: true }).first().waitFor()
    const oldResponse = page.waitForResponse(response => new URL(response.url()).pathname === requestPath && (
      requestPath.endsWith('/summary') ? response.request().postDataJSON()?.education_stage === 'junior' : new URL(response.url()).searchParams.get('education_stage') === 'junior'
    ))
    releaseHeld()
    await oldResponse
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    assert.equal(await view.getByText('junior-示例', { exact: true }).count(), 0)
    assert.ok(await view.getByText('senior-示例', { exact: true }).count())
    checks.push({ path, lateOldResponseIgnored: true, liveProfileReload: true })
  }
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, output, apiCalls: calls.length, checks: checks.length, errors }))
} finally {
  releaseHeld?.()
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, errors, checks, calls }, null, 2))
  await browser.close()
}
