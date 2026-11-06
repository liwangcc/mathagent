// Smoke checks for two fixed defects, against the real pages with mocked APIs.
// Start Vite, then: node tests/smoke-courseware-fixes.browser.mjs http://127.0.0.1:5197 <shots-dir>
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local mocked pages may be tested')
const output = resolve(process.argv[3] || '../outputs/smoke-courseware')
mkdirSync(output, { recursive: true })

const browser = await chromium.launch({ headless: true })
const results = []

async function scenario(name, { failPaths, path, expectCards }, check) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  await context.addInitScript(() => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('mathagent_usage_guide')) localStorage.setItem(key, 'true')
    }
  })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (failPaths.includes(url.pathname)) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'mock outage' }),
      })
    }
    let data = {}
    if (url.pathname === '/api/auth/me') {
      data = { user: { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true } }
    } else if (url.pathname === '/api/auth/classes') {
      data = { classes: [], current_class_id: null }
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(base + path)
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(1200)
  const observed = await check(page)
  await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true })
  assert.deepEqual(errors, [], `${name}: page errors`)
  results.push({ name, ...observed, errors })
  await context.close()
}

try {
  // F1: the recommendation route returns 503 -> no bundled cards, explicit empty state,
  // and no ReferenceError from the catch block (the old bug: `recommended` undefined).
  await scenario('F1-recommendations-503', {
    failPaths: ['/api/courseware/recommendations'],
    path: '/courseware?knowledge_point=' + encodeURIComponent('一次函数的图像'),
  }, async page => {
    const cards = await page.locator('.course-card').count()
    const empty = await page.locator('.el-empty__description').allInnerTexts()
    assert.equal(cards, 0, 'a failed recommendation route must not show bundled cards')
    assert.ok(empty.some(text => text.includes('暂无与该知识点匹配的课件')), `empty state text was ${JSON.stringify(empty)}`)
    return { cards, empty }
  })

  // F2: the catalogue route returns 503 -> bundled chapter courseware still renders.
  await scenario('F2-catalogue-503', {
    failPaths: ['/api/courseware'],
    path: '/courseware',
  }, async page => {
    const cards = await page.locator('.course-card').count()
    assert.ok(cards > 0, 'the bundled catalogue must survive an API outage')
    return { cards }
  })

  writeFileSync(resolve(output, 'smoke.json'), JSON.stringify({ mockedApiOnly: true, results }, null, 2))
  console.log(JSON.stringify({ ok: true, results }, null, 2))
} finally {
  await browser.close()
}