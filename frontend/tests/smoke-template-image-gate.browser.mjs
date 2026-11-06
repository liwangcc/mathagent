// Smoke test for the drawing-template “original image loaded” save gate
// (frontend/src/views/Upload.vue, saveAndConfirmTemplateDraft).
//
// Real Upload page at http://127.0.0.1:5197, every /api request mocked in the
// browser; no backend, no account. The template is imported the way a teacher
// does it: choose the batch type, pick one blank template page, press
// “识别模板”, let the mocked recognition stream (SSE) return one 作图题 page,
// and the “编辑本次模板” dialog opens on its own. Then “保存模板（全部页）”:
//
//   F3a  the page image request is still pending  -> save must be blocked with
//        the 作图模板原图尚未加载成功 warning and no confirm request.
//   F3b  the page image request fails (404)       -> same block, same warning,
//        still no confirm request.
//   F3c  the page image loads normally            -> the save goes through and a
//        confirm request is sent (or the dialog closes on the success path).
//
// The editor's original image is fetched by the app as bytes and shown through
// an object URL, so the <img> request the gate actually depends on is the blob
// load. The script replaces that URL (only for template page images, only
// after the template file is prepared) with a local http URL it can control:
//   - held pending by the route handler  => the image never finishes loading
//   - answered 404 by the route handler  => the image load fails
//
// Run: node tests/smoke-template-image-gate.browser.mjs [base] [artifacts]
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import zlib from 'node:zlib'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
const output = resolve(process.argv[3] || 'D:/hermes/cache/scratch/smoke-f3-shots')
if (new URL(base).hostname !== '127.0.0.1') throw new Error('only 127.0.0.1 may be tested')
mkdirSync(output, { recursive: true })

const TEMPLATE = 'smoke-tpl-f3'
const WARNING = '作图模板原图尚未加载成功，请先完成图像核对再确认模板'
const SAVE = '保存模板（全部页）'
const CONFIRM_PATH = '/api/batch-grade/template/confirm'
const HOLD_IMAGE = '/__smoke/hold-template-image.png'
const MISSING_IMAGE = '/__smoke/missing-template-image.png'

// ── a real blank template page (800 × 1000) so the upload pre-processing
// (decode + recompress) works exactly as it does for a teacher ────────────
function blankPagePng(width = 800, height = 1000) {
  const table = (() => {
    const values = new Int32Array(256)
    for (let n = 0; n < 256; n += 1) {
      let c = n
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      values[n] = c
    }
    return values
  })()
  const crc32 = (buffer) => {
    let c = 0xffffffff
    for (const byte of buffer) c = table[(c ^ byte) & 0xff] ^ (c >>> 8)
    return (c ^ 0xffffffff) >>> 0
  }
  const chunk = (type, data) => {
    const length = Buffer.alloc(4)
    length.writeUInt32BE(data.length)
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
    const crc = Buffer.alloc(4)
    crc.writeUInt32BE(crc32(body))
    return Buffer.concat([length, body, crc])
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 2 // truecolour
  const raw = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1)
    for (let x = 0; x < width; x += 1) {
      const border = y < 3 || y >= height - 3 || x < 3 || x >= width - 3
      const value = border ? 90 : 250
      raw[row + 1 + x * 3] = value
      raw[row + 2 + x * 3] = value
      raw[row + 3 + x * 3] = value
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}
const templatePng = resolve(output, 'blank-template-page.png')
writeFileSync(templatePng, blankPagePng())

// ── what the mocked recognition reports: one page, one 作图题 ─────────────
const DRAWING_QUESTION = {
  question_no: '1', question_type: 'solve',
  question_text: '在平面直角坐标系中画出该函数的图象。',
  standard_answer: '略（作图题）',
  answer_kind: 'drawing',
  drawing_regions: [[100, 200, 600, 700]],
  label_anchor: [90, 250],
  drawing_reference: null,
}
const CUT_PAGE = {
  page_index: 0, page_label: 'page_1',
  anchor_crop_plan: { parameters: { columns: [{ column_index: 1, x1_1000: 0, x2_1000: 1000 }], column_count: 1 }, blocks: [] },
  question_anchors: [{ question_number: '1', question_number_bbox: [40, 200, 90, 240] }],
  question_schema: [{
    question_number: '1', question_type: 'solve', answer_kind: 'drawing',
    drawing_regions: [[100, 200, 600, 700]], label_anchor: [90, 250],
  }],
  template_answer_regions: [],
}
const RECOGNITION_PAGES = [{ page_index: 0, page_label: 'page_1', questions: [DRAWING_QUESTION] }]
const sseBody = [
  'event: progress',
  `data: ${JSON.stringify({ template_id: TEMPLATE, message: '模板识别中…' })}`,
  '',
  'event: done',
  `data: ${JSON.stringify({
    template_id: TEMPLATE, template_mode: 'marked', column_mode: 'single',
    pages: RECOGNITION_PAGES,
  })}`,
  '',
  '',
].join('\n')

const json = (route, data, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(data),
})

const browser = await chromium.launch({ headless: true })
const report = { base, startedAt: new Date().toISOString(), checks: [], observations: [] }

async function openEditor({ name, imageMode, pageImageStatus = 200, awaitImage = true }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  await context.addInitScript(() => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    // Set-by-step switch: template page images are only diverted after the
    // template file itself has been prepared, so upload decoding is untouched.
    window.__smokeImageMode = 'normal'
    const create = URL.createObjectURL.bind(URL)
    URL.createObjectURL = (object) => {
      const target = window.__smokeImageMode === 'hold'
        ? '/__smoke/hold-template-image.png'
        : window.__smokeImageMode === 'missing' ? '/__smoke/missing-template-image.png' : ''
      if (target && object instanceof Blob && String(object.type || '').startsWith('image/')) return target
      return create(object)
    }
  })
  const held = []
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (url.pathname === HOLD_IMAGE) { held.push(route); return } // never answers: the image load never finishes
    if (url.pathname === MISSING_IMAGE) return route.fulfill({ status: 404, contentType: 'text/plain', body: 'not found' })
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const method = request.method()
    if (method === 'POST' && url.pathname === '/api/batch-grade/template/recognize') {
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream', 'X-Template-ID': TEMPLATE, 'Cache-Control': 'no-cache' },
        body: sseBody,
      })
    }
    if (method === 'GET' && url.pathname === `/api/batch-grade/template/${TEMPLATE}/cut-candidates`) {
      return json(route, { status: 'complete', template_id: TEMPLATE, pages: [CUT_PAGE] })
    }
    if (url.pathname === `/api/batch-grade/template/${TEMPLATE}/pages/0/image`) {
      if (pageImageStatus !== 200) return route.fulfill({ status: pageImageStatus, contentType: 'application/json', body: '{"detail":"not found"}' })
      return route.fulfill({ status: 200, contentType: 'image/png', body: blankPagePng() })
    }
    if (method === 'POST' && url.pathname === CONFIRM_PATH) {
      return json(route, { success: true, page_count: 1, template_mode: 'marked', column_mode: 'single' })
    }
    if (method === 'POST' && url.pathname === '/api/batch-grade/template/correction-candidates') {
      return json(route, { candidates: [] })
    }
    if (method === 'PUT' && url.pathname === `/api/batch-grade/template/${TEMPLATE}/layout-overrides`) {
      return json(route, { success: true, template_id: TEMPLATE })
    }
    if (method !== 'GET') return json(route, { success: true })
    if (url.pathname === '/api/auth/me') {
      return json(route, { user: { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true } })
    }
    if (url.pathname === '/api/auth/classes') return json(route, { classes: [], current_class_id: null })
    if (url.pathname === '/api/batch-grade/availability') return json(route, { status: 'available', hardware: { status: 'ready' } })
    if (url.pathname === '/api/batch-grade/template/availability') {
      return json(route, { status: 'available', service_status: 'available', label: '可用', registration_hardware: { status: 'ready' } })
    }
    return json(route, {})
  })

  const page = await context.newPage()
  const requests = []
  const failedRequests = []
  const consoleErrors = []
  const pageErrors = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    requests.push({ method: request.method(), path: url.pathname + url.search })
  })
  page.on('requestfailed', (request) => failedRequests.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText || '' }))
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('pageerror', (error) => pageErrors.push(error.message))

  await page.goto(base + '/upload')
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden', timeout: 30_000 })
  await page.selectOption('.batch-setup-fields select', 'daily')
  await page.setInputFiles('.template-card input[type="file"]', templatePng)
  await page.locator('.template-card .file-summary').waitFor({ timeout: 30_000 })
  // From here on every image object URL the page creates is a template page image.
  await page.evaluate((mode) => { window.__smokeImageMode = mode }, imageMode)
  await page.getByRole('button', { name: '识别模板' }).click()
  const dialog = page.locator('.template-editor-dialog')
  await dialog.waitFor({ state: 'visible', timeout: 60_000 })
  await dialog.getByRole('button', { name: SAVE }).waitFor({ timeout: 60_000 })
  const image = dialog.locator('img[alt="教师模板原图"]').first()
  if (awaitImage) await image.waitFor({ state: 'attached', timeout: 60_000 })
  else await page.waitForTimeout(2500)
  return { context, page, dialog, image, requests, failedRequests, consoleErrors, pageErrors, held }
}

async function imageState(image) {
  return image.evaluate((element) => ({
    src: element.currentSrc || element.src,
    complete: element.complete,
    naturalWidth: element.naturalWidth,
  }))
}

const messages = async (page) => page.locator('.el-message').evaluateAll(
  (nodes) => nodes.map((node) => ({ text: node.textContent.trim(), warning: node.classList.contains('el-message--warning'), error: node.classList.contains('el-message--error') })),
)
const postPaths = (requests) => requests.filter((item) => item.method !== 'GET').map((item) => `${item.method} ${item.path}`)

async function blockedSave({ name, imageMode, pageImageStatus, expectImage, expectStatusLine }) {
  const run = await openEditor({ name, imageMode, pageImageStatus })
  const { page, dialog, image, requests, failedRequests, consoleErrors, pageErrors, held, context } = run
  const shot = (label) => resolve(output, `${name}-${label}.png`)
  const result = {
    check: name, imageMode, imageBeforeSave: null, drawingQuestions: [], imageStatusLine: null,
    warningText: null, nonGetRequests: [], confirmSent: false, dialogOpenAfter: null,
    screenshots: [], consoleErrors, pageErrors, failedRequests: [],
  }
  try {
    await page.waitForTimeout(1200)
    result.imageBeforeSave = await imageState(image)
    result.imageBeforeSave.state = expectImage
    result.drawingQuestions = await dialog.locator('.tpl-reference-caption').allTextContents()
    result.imageStatusLine = await dialog.evaluate((element) => (element.textContent.match(/模板原图加载中|模板原图加载失败/g) || []).join(' | ') || null)
    result.imageRequestHeld = requests.filter((item) => item.path === HOLD_IMAGE).length
    await page.screenshot({ path: shot('1-editor-open') })
    result.screenshots.push(shot('1-editor-open').split(/[\\/]/).pop())

    const before = requests.length
    await dialog.getByRole('button', { name: SAVE }).click()
    await page.waitForTimeout(1500)
    const toasts = await messages(page)
    result.warningText = toasts.filter((toast) => toast.warning).map((toast) => toast.text).join(' | ') || null
    result.messages = toasts.map((toast) => `${toast.warning ? 'warning' : toast.error ? 'error' : 'info'}: ${toast.text}`)
    result.nonGetRequests = postPaths(requests.slice(before))
    result.confirmSent = requests.slice(before).some((item) => item.path.startsWith(CONFIRM_PATH))
    result.dialogOpenAfter = await dialog.isVisible()
    await page.screenshot({ path: shot('2-after-save') })
    result.screenshots.push(shot('2-after-save').split(/[\\/]/).pop())
    // Snapshot before teardown releases the held image request.
    result.failedRequests = [...failedRequests]
    result.passed = Boolean(
      result.warningText && result.warningText.includes(WARNING)
      && !result.confirmSent && result.dialogOpenAfter
      && result.drawingQuestions.length > 0
      && result.imageStatusLine === expectStatusLine
      && expectImageOk(result.imageBeforeSave)
      && pageErrors.length === 0,
    )
    return result
  } finally {
    for (const route of held) await route.abort().catch(() => {})
    await context.close()
  }
}

function expectImageOk(state) {
  if (state.state === 'not-loaded') return state.complete === false && state.naturalWidth === 0
  if (state.state === 'failed') return state.naturalWidth === 0
  return state.naturalWidth > 0
}

// F3a — the original image request never answers.
report.checks.push(await blockedSave({ name: 'F3a-image-pending', imageMode: 'hold', expectImage: 'not-loaded', expectStatusLine: '模板原图加载中' }))
// F3b — the original image request answers 404.
report.checks.push(await blockedSave({ name: 'F3b-image-404', imageMode: 'missing', expectImage: 'failed', expectStatusLine: '模板原图加载失败' }))

// F3c — the original image loads; the same click must now reach the server.
async function loadedSave() {
  const name = 'F3c-image-loaded'
  const { page, dialog, image, requests, failedRequests, consoleErrors, pageErrors, context } = await openEditor({ name, imageMode: 'normal' })
  const shot = (label) => resolve(output, `${name}-${label}.png`)
  const result = {
    check: name, imageBeforeSave: null, drawingQuestions: [], imageStatusLine: null,
    warningText: null, nonGetRequests: [], confirmSent: false, dialogOpenAfter: null,
    screenshots: [], consoleErrors, pageErrors, failedRequests: [],
  }
  try {
    await image.evaluate((element) => element.complete ? null : new Promise((resolve) => {
      element.addEventListener('load', resolve, { once: true })
      element.addEventListener('error', resolve, { once: true })
    }))
    await page.waitForTimeout(800)
    result.imageBeforeSave = await imageState(image)
    result.imageBeforeSave.state = 'loaded'
    result.drawingQuestions = await dialog.locator('.tpl-reference-caption').allTextContents()
    result.imageStatusLine = await dialog.evaluate((element) => (element.textContent.match(/模板原图加载中|模板原图加载失败/g) || []).join(' | ') || null)
    await page.screenshot({ path: shot('1-editor-open') })
    result.screenshots.push(shot('1-editor-open').split(/[\\/]/).pop())
    const before = requests.length
    await dialog.getByRole('button', { name: SAVE }).click()
    const sent = await page.waitForTimeout(2500).then(() => requests.slice(before))
    const toasts = await messages(page)
    result.warningText = toasts.filter((toast) => toast.warning).map((toast) => toast.text).join(' | ') || null
    result.nonGetRequests = postPaths(sent)
    result.confirmSent = sent.some((item) => item.path.startsWith(CONFIRM_PATH))
    result.dialogOpenAfter = await dialog.isVisible()
    result.messages = toasts.map((toast) => `${toast.warning ? 'warning' : toast.error ? 'error' : 'info'}: ${toast.text}`)
    await page.screenshot({ path: shot('2-after-save') })
    result.screenshots.push(shot('2-after-save').split(/[\\/]/).pop())
    result.failedRequests = [...failedRequests]
    result.passed = result.confirmSent && !(result.warningText || '').includes(WARNING) && result.imageBeforeSave.naturalWidth > 0 && result.imageStatusLine === null && pageErrors.length === 0
    return result
  } finally {
    await context.close()
  }
}
report.checks.push(await loadedSave())

// Observation (not a sub-check): what a 404 on the image *bytes* endpoint does,
// since the app fetches those with axios before the <img> can load them.
async function apiImage404Observation() {
  const name = 'OBS-api-image-404'
  const { page, dialog, requests, failedRequests, consoleErrors, pageErrors, context } = await openEditor({ name, imageMode: 'normal', pageImageStatus: 404, awaitImage: false })
  const shot = (label) => resolve(output, `${name}-${label}.png`)
  const result = { name, editorText: '', nonGetRequests: [], confirmSent: false, consoleErrors, pageErrors, failedRequests, screenshots: [] }
  try {
    result.editorText = (await dialog.textContent()).replace(/\s+/g, ' ').slice(0, 400)
    await page.screenshot({ path: shot('1-editor-open') })
    result.screenshots.push(shot('1-editor-open').split(/[\\/]/).pop())
    const before = requests.length
    await dialog.getByRole('button', { name: SAVE }).click()
    await page.waitForTimeout(2500)
    const toasts = await messages(page)
    result.messages = toasts.map((toast) => `${toast.warning ? 'warning' : toast.error ? 'error' : 'info'}: ${toast.text}`)
    result.nonGetRequests = postPaths(requests.slice(before))
    result.confirmSent = requests.slice(before).some((item) => item.path.startsWith(CONFIRM_PATH))
    result.dialogOpenAfter = await dialog.isVisible().catch(() => null)
    await page.screenshot({ path: shot('2-after-save') })
    result.screenshots.push(shot('2-after-save').split(/[\\/]/).pop())
    return result
  } finally {
    await context.close()
  }
}
report.observations.push(await apiImage404Observation())

report.finishedAt = new Date().toISOString()
writeFileSync(resolve(output, 'smoke-f3-report.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify({
  ok: report.checks.every((check) => check.passed),
  checks: report.checks.map((check) => ({
    check: check.check, passed: !!check.passed, image: check.imageBeforeSave,
    warning: check.warningText, nonGet: check.nonGetRequests, confirm: check.confirmSent,
    dialogOpen: check.dialogOpenAfter, shots: check.screenshots,
    pageErrors: check.pageErrors, consoleErrors: check.consoleErrors.slice(0, 5),
  })),
  output,
}, null, 2))
await browser.close()