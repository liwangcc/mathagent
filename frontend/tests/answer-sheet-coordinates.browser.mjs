// Real Vue page with intercepted API fixtures; no production requests.
// NODE_PATH may point to a preinstalled Playwright package. Start Vite first.
// node tests/answer-sheet-coordinates.browser.mjs BASE_URL [EXTERNAL_FIXTURE_DIR]
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const root = fileURLToPath(new URL('../../', import.meta.url))
const artifacts = resolve(root, 'live_blank_template_qwen38_only_remote/browser-coordinate-audit')
mkdirSync(artifacts, { recursive: true })
const base = process.argv[2] || 'http://127.0.0.1:5182'
const probe = process.argv[3] ? resolve(process.argv[3]) : null
const result = probe
  ? JSON.parse(readFileSync(resolve(probe, 'audit-grid-20260908/result.json'), 'utf8'))
  : { pages: [{ answer_regions: Array.from({ length: 25 }, (_, i) => ({
    question_no: String(i + 1), bbox: [100, 30 + i * 36, 880, 50 + i * 36],
  })) }] }
const templateImage = probe
  ? readFileSync(resolve(probe, 'canonical-template-question-first.png'))
  : '<svg xmlns="http://www.w3.org/2000/svg" width="2064" height="1564"><rect width="100%" height="100%" fill="white"/></svg>'
const questions = result.pages[0].answer_regions.map(item => ({
  question_no: item.question_no, question_type: Number(item.question_no) <= 15 ? 'choice' : 'fill',
  question_text: '', standard_answer: '', max_score: 4, solution_steps: [],
}))
const session = {
  session_id: 'coordinate-preview-test', status: 'layout_draft', category: 'exam',
  answer_spec_snapshot: questions, source_exam_pages: [], answer_pages: [],
  student_pages: [], grouping_decisions: [], identity_regions: [],
  layout_localization: { provider_route: 'qwen3.8max' },
  answer_sheet_pages: [1, 2, 3].map(n => ({ page_id: `page_${n}`, index: n - 1, working_sha256: `fixture-${n}` })),
  answer_regions: [1, 2, 3].flatMap(n => result.pages[0].answer_regions.map(item => ({
    ...item, region_id: `p${n}-q${item.question_no}`, page_id: `page_${n}`,
    region_type: 'fill', required: true, teacher_confirmed: false,
  }))),
}
const user = { id: 999999, username: 'coordinate-test', real_name: '坐标验证', role: 'teacher', current_class_id: 1, education_stage: 'senior', profile_complete: true }
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let slowPageTwo = null
let imageTwoFailure = false
let imageTwoRequests = 0
const imageCalls = []
await context.addInitScript(({ user }) => {
  localStorage.setItem('mathagent_token', 'local-mock-token')
  localStorage.setItem('mathagent_user', JSON.stringify(user))
  localStorage.setItem('mathagent_answer_sheet_session_id', 'coordinate-preview-test')
}, { user })
await context.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.origin !== new URL(base).origin) return route.abort()
  if (!url.pathname.startsWith('/api/')) return route.continue()
  if (/\/layout\/pages\/page_\d\/image$/.test(url.pathname)) {
    const n = Number(url.pathname.match(/page_(\d)/)[1])
    imageCalls.push(n)
    if (n === 2) {
      imageTwoRequests++
      if (slowPageTwo) await slowPageTwo
    }
    if (n === 2 && imageTwoFailure) return route.fulfill({ status: 200, contentType: 'image/png', body: 'broken-image' })
    if (n === 1) return route.fulfill({ status: 200, contentType: probe ? 'image/png' : 'image/svg+xml', body: templateImage })
    const height = n === 2 ? 2600 : 600
    return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="${height}"><rect width="100%" height="100%" fill="white"/><text x="100" y="100" font-size="50">Page ${n}: coordinate fixture</text></svg>` })
  }
  let data = {}
  if (url.pathname.endsWith('/auth/me')) data = { user }
  else if (url.pathname.endsWith('/auth/classes')) data = { classes: [{ id: 1, class_name: '测试班级' }], current_class_id: 1 }
  else if (url.pathname.endsWith('/availability')) data = { status: 'available', hardware: { status: 'ready' } }
  else if (url.pathname.endsWith('/templates/history')) data = { history: [] }
  else if (url.pathname.includes('/answer-sheet/sessions/')) data = session
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
})

async function measure(label) {
  await page.waitForFunction(() => {
    const img = document.querySelector('.paper-image-surface img')
    return img?.complete && img.naturalWidth > 0 && document.querySelectorAll('.paper-image-surface .answer-box').length === 25
  })
  const measurements = await page.evaluate(() => {
    const img = document.querySelector('.paper-image-surface img')
    const surface = document.querySelector('.paper-image-surface')
    const ir = img.getBoundingClientRect()
    const sr = surface.getBoundingClientRect()
    return {
      natural: [img.naturalWidth, img.naturalHeight],
      rendered: [ir.width, ir.height], surfaceHeight: sr.height,
      maxError: Math.max(...[...surface.querySelectorAll('.answer-box')].flatMap(box => {
        const r = box.getBoundingClientRect()
        return [
          Math.abs(r.x - ir.x - parseFloat(box.style.left) / 100 * ir.width),
          Math.abs(r.y - ir.y - parseFloat(box.style.top) / 100 * ir.height),
          Math.abs(r.width - parseFloat(box.style.width) / 100 * ir.width),
          Math.abs(r.height - parseFloat(box.style.height) / 100 * ir.height),
        ]
      })),
    }
  })
  assert.ok(measurements.maxError < 0.1, `${label}: ${JSON.stringify(measurements)}`)
  assert.ok(Math.abs(measurements.surfaceHeight - measurements.rendered[1]) < 0.1)
  return { label, ...measurements }
}

try {
  await page.goto(`${base}/answer-sheet?session=coordinate-preview-test`)
  const checks = []
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 900, height: 800 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport)
    for (const n of [1, 2, 3]) {
      await page.locator('.page-tabs button').nth(n - 1).click()
      const check = await measure(`${viewport.width}px-page${n}`)
      assert.deepEqual(check.natural, n === 1 ? [2064, 1564] : [1800, n === 2 ? 2600 : 600])
      checks.push(check)
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.locator('.page-tabs button').nth(0).click()
  await measure('before-race')
  let release
  slowPageTwo = new Promise(resolve => { release = resolve })
  const before = imageTwoRequests
  await page.locator('.page-tabs button').nth(1).click()
  while (imageTwoRequests === before) await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(await page.locator('.paper-image-surface .answer-box').count(), 0)
  await page.locator('.page-tabs button').nth(2).click()
  await measure('race-new-page')
  const activeUrl = await page.locator('.paper-image-surface img').getAttribute('src')
  const oldResponse = page.waitForResponse(response => response.url().includes('/page_2/image'))
  release()
  await oldResponse
  // Wait until response decoding and Vue effects have had a chance to run.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  slowPageTwo = null
  assert.equal(await page.locator('.paper-image-surface img').getAttribute('src'), activeUrl)
  checks.push(await measure('old-response-does-not-replace-current-image'))
  imageTwoFailure = true
  await page.locator('.page-tabs button').nth(1).click()
  await page.getByText('答题卡图像无法显示，请重新打开当前页。', { exact: true }).waitFor()
  assert.equal(await page.locator('.paper-image-surface .answer-box').count(), 0)
  imageTwoFailure = false
  await page.locator('.page-tabs button').nth(0).click()
  await measure('final-real-template')
  await page.locator('.paper-stage').screenshot({ path: resolve(artifacts, 'teacher-template.png') })
  await page.screenshot({ path: resolve(artifacts, 'teacher-page.png'), fullPage: true })
  assert.deepEqual(errors, [])
  writeFileSync(resolve(artifacts, 'measurements.json'), JSON.stringify({ checks, race: 'passed', imageFailure: 'passed', errors }, null, 2))
  console.log(JSON.stringify({ checks, race: 'passed', imageFailure: 'passed', errors }, null, 2))
} finally {
  await browser.close()
}
