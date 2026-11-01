// Actual Vue pages, fully intercepted API requests, synthetic teacher metadata only.
// Start Vite, then: node tests/education-stage-profile.browser.mjs http://127.0.0.1:5189
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5189'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only a local mocked frontend is allowed')
const artifacts = resolve(fileURLToPath(new URL('../../', import.meta.url)), 'outputs/education-stage-profile-browser')
mkdirSync(artifacts, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
const errors = []
const results = []

async function makeContext(viewport, user) {
  const context = await browser.newContext({ viewport })
  const state = { user, meCalls: 0, saves: [], logoutCalls: 0, apiCalls: [], requireStageOnce: false }
  await context.addInitScript(() => {
    if (!sessionStorage.getItem('stage-fixture-initialized')) {
      sessionStorage.setItem('stage-fixture-initialized', 'true')
      localStorage.setItem('mathagent_token', 'mock-only-token')
      // Deliberately forged persisted profile must never bypass the server /me result.
      localStorage.setItem('mathagent_user', JSON.stringify({ id: 910001, education_stage: 'senior', profile_complete: true }))
    }
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
  })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    state.apiCalls.push(`${request.method()} ${url.pathname}`)
    if (state.requireStageOnce && url.pathname === '/api/teacher/pending') {
      state.requireStageOnce = false
      return route.fulfill({ status: 428, contentType: 'application/json', body: JSON.stringify({ detail: {
        code: 'teaching_profile_required', message: '请先在个人资料中选择初中或高中学段',
      } }) })
    }
    let data = {}
    if (url.pathname === '/api/auth/me') {
      if (request.method() === 'PUT') {
        const payload = request.postDataJSON()
        state.saves.push(payload)
        assert.deepEqual(Object.keys(payload), ['education_stage'])
        state.user = { ...state.user, education_stage: payload.education_stage, profile_complete: true }
      } else state.meCalls++
      data = { user: state.user }
    } else if (url.pathname === '/api/auth/logout') {
      state.logoutCalls++
      data = { ok: true }
    } else if (url.pathname === '/api/auth/classes') {
      data = { classes: [], current_class_id: null }
    } else if (url.pathname.includes('/pending')) {
      data = []
    } else if (url.pathname.includes('/availability')) {
      data = { status: 'available', hardware: { status: 'ready' } }
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  return { context, page, state }
}

const blankUser = { id: 910001, name: '示例教师', role: 'teacher', current_class_id: null, education_stage: null, profile_complete: false }
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const { context, page, state } = await makeContext(viewport, { ...blankUser })
    await page.goto(`${base}/upload?from=stage-test`)
    await page.getByRole('heading', { name: '先选择您的教学学段' }).waitFor()
    await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    assert.equal(new URL(page.url()).pathname, '/profile')
    assert.equal(new URL(page.url()).searchParams.get('redirect'), '/upload?from=stage-test')
    assert.equal(await page.getByRole('button', { name: '保存并进入工作台' }).isDisabled(), true)
    assert.equal(await page.getByText('外观主题', { exact: true }).count(), 0)
    assert.equal(await page.locator('.el-overlay-dialog:visible').count(), 0, 'A first-use guide must not cover the mandatory stage form')
    const layout = await page.locator('.teaching-card').evaluate(el => {
      const rect = el.getBoundingClientRect()
      return { x: rect.x, right: rect.right, width: rect.width, viewport: innerWidth }
    })
    assert.ok(layout.x >= 0 && layout.right <= layout.viewport + 1, JSON.stringify(layout))
    await page.screenshot({ path: resolve(artifacts, `setup-${viewport.width}.png`), fullPage: true })
    await page.getByRole('radio', { name: /初中数学/ }).click()
    await page.getByRole('button', { name: '保存并进入工作台' }).click()
    await page.waitForURL(url => url.pathname === '/upload' && url.searchParams.get('from') === 'stage-test')
    assert.deepEqual(state.saves, [{ education_stage: 'junior' }])
    assert.ok(state.meCalls >= 2, 'Save must re-read /me')
    await page.reload()
    await page.waitForURL(url => url.pathname === '/upload')
    await page.locator('.page-title').waitFor()
    assert.equal(await page.getByRole('heading', { name: '先选择您的教学学段' }).count(), 0)
    await page.goto(`${base}/profile`)
    await page.getByRole('heading', { name: '教学学段', exact: true }).waitFor()
    await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    assert.equal(await page.getByRole('radio', { name: /初中数学/ }).getAttribute('aria-checked'), 'true')
    await page.screenshot({ path: resolve(artifacts, `saved-${viewport.width}.png`), fullPage: true })
    results.push({ viewport, ...layout, persistedStage: state.user.education_stage, meCalls: state.meCalls, savePayload: state.saves[0] })
    await context.close()
  }
  {
    const { context, page, state } = await makeContext({ width: 1100, height: 800 }, {
      ...blankUser, education_stage: 'senior', profile_complete: true,
    })
    await page.goto(`${base}/upload?from=server-gate`)
    await page.locator('.page-title').waitFor()
    await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
    state.user = { ...blankUser }
    state.requireStageOnce = true
    await page.evaluate(async () => {
      const { default: api } = await import('/src/api/index.js')
      await api.pending.list().catch(() => {})
    }).catch(error => {
      if (!error.message.includes('Execution context was destroyed')) throw error
    })
    await page.getByRole('heading', { name: '先选择您的教学学段' }).waitFor()
    assert.equal(new URL(page.url()).searchParams.get('redirect'), '/upload?from=server-gate')
    assert.equal(await page.evaluate(() => localStorage.getItem('mathagent_token')), 'mock-only-token')
    results.push({ server428Redirect: true, preservedToken: true })
    await context.close()
  }
  {
    const { context, page, state } = await makeContext({ width: 1100, height: 800 }, { ...blankUser })
    await page.goto(`${base}/profile?setup=education-stage&redirect=https%3A%2F%2Fexample.org`)
    await page.getByRole('heading', { name: '先选择您的教学学段' }).waitFor()
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await page.waitForURL(url => url.pathname === '/login')
    assert.equal(state.logoutCalls, 1)
    assert.equal(await page.evaluate(() => localStorage.getItem('mathagent_token')), null)
    results.push({ logoutFromSetup: true })
    await context.close()
  }
  assert.deepEqual(errors, [])
  writeFileSync(resolve(artifacts, 'audit.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, errors, results }, null, 2))
  console.log(JSON.stringify({ ok: true, artifacts, results }, null, 2))
} finally {
  await browser.close()
}
