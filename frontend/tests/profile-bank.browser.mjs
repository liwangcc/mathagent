// Profile is the only stage input: actual Vue navigation, intercepted synthetic APIs.
// Start Vite, then: NODE_PATH=<bundled packages> node tests/profile-bank.browser.mjs <local-url>
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1')
const output = resolve(process.argv[3] || '../outputs/profile-bank-browser')
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const errors = [], results = []
try {
  for (const [role, initialStage] of [['admin', 'senior'], ['teacher', 'junior']]) {
    for (const width of [1440, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } })
      const calls = [], mutations = []
      let user = { id: 910001, name: '模拟教师', role, education_stage: initialStage, profile_complete: true }
      await context.addInitScript(() => {
        localStorage.setItem('mathagent_token', 'mock-only-token')
        localStorage.setItem('disclaimer_agreed', 'true')
        localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
      })
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.origin !== new URL(base).origin) return route.abort()
        if (!url.pathname.startsWith('/api/')) return route.continue()
        const stage = url.searchParams.get('education_stage')
        calls.push({ path: url.pathname, stage, method: request.method() })
        let data = {}
        if (url.pathname === '/api/auth/me') {
          if (request.method() === 'PUT') {
            const payload = request.postDataJSON()
            assert.deepEqual(Object.keys(payload), ['education_stage'])
            mutations.push(payload)
            user = { ...user, education_stage: payload.education_stage }
          }
          data = { user }
        } else {
          assert.equal(request.method(), 'GET', 'No real grading or data mutations')
          if (url.pathname === '/api/auth/classes') data = { classes: [] }
          else if (url.pathname === '/api/teacher/chapters') {
            assert.equal(stage, user.education_stage)
            data = [{ chapter: `${stage}测试章节`, topics: [] }]
          } else if (url.pathname === '/api/teacher/bank') {
            assert.equal(stage, user.education_stage)
            data = { total: 1, items: [{ id: 1, question: `${stage}合成题目`, answer: '3', topic: '示例知识点', is_public: true, education_stage: stage }] }
          } else if (url.pathname === '/api/bank/export') {
            assert.equal(stage, user.education_stage)
            data = { count: 0, questions: [], education_stage: stage }
          } else if (url.pathname.includes('/pending')) data = []
          else if (url.pathname.includes('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
        }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
      })
      const page = await context.newPage()
      page.on('pageerror', error => errors.push(error.message))
      const other = initialStage === 'senior' ? 'junior' : 'senior'
      await page.goto(`${base}/bank?education_stage=${other}`)
      await page.getByText(`${initialStage}合成题目`, { exact: true }).waitFor()
      await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
      assert.equal(await page.getByRole('combobox', { name: /题库学段|历史题迁入学段/ }).count(), 0)
      assert.equal(await page.getByText('历史未标学段', { exact: false }).count(), 0)
      await page.getByText('无需重复选择', { exact: false }).waitFor()
      const download = page.waitForEvent('download')
      await page.getByRole('button', { name: '导出私库', exact: true }).click()
      assert.equal((await download).suggestedFilename(), 'mathagent_bank_export.json')
      await page.screenshot({ path: resolve(output, `${role}-${initialStage}-${width}.png`), fullPage: true })
      // Change the one canonical setting, then return; no per-page selection is required.
      await page.goto(base + '/profile')
      await page.getByRole('radio', { name: other === 'junior' ? /初中数学/ : /高中数学/ }).click()
      await page.getByRole('button', { name: '保存学段', exact: true }).click()
      await page.getByText('教学学段已保存', { exact: true }).waitFor()
      await page.goto(`${base}/bank?education_stage=${initialStage}`)
      await page.getByText(`${other}合成题目`, { exact: true }).waitFor()
      assert.equal(await page.getByText(`${initialStage}合成题目`, { exact: true }).count(), 0)
      assert.deepEqual(mutations, [{ education_stage: other }])
      results.push({ role, initialStage, width, calls, mutations })
      await context.close()
    }
  }
  assert.deepEqual(errors, [])
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, results, errors }, null, 2))
  console.log(JSON.stringify({ ok: true, scenarios: results.length, output }))
} finally { await browser.close() }
