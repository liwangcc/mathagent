// Behavioural CSS-contract checks (batch B) against the real Vue pages in headless Chromium.
// Every request is mocked: only 127.0.0.1 is reachable and /api/* is answered in-process,
// so no account, model or grading backend is ever contacted.
//
//   NODE_PATH="D:/npm/node_modules/@playwright/test/node_modules" \
//     node tests/css-contracts-b.browser.mjs [base] [output-dir]
//
// Checks
//   #1 /answer-sheet, availability {status:'fault'}, batch already holding student papers:
//      page root is .workflow-readonly, the student tabs and the paper pane stay clickable
//      (pointer-events !== 'none', opacity 1) and switching students really swaps the paper.
//   #6 Grading loaders (SheetLoader, InkSpinner) under prefers-reduced-motion: reduce are
//      static (animation-name none / paused, no running animation), with a normal-motion
//      control run proving the same nodes animate when motion is allowed.
//   #7 /upload while template recognition is in progress: the shared loader is on screen and
//      no legacy ring/spinner element is animating.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import zlib from 'node:zlib'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local mocked pages may be tested')
const output = resolve(process.argv[3] || 'D:/hermes/cache/scratch/css-b-shots')
mkdirSync(output, { recursive: true })

// ── A real PNG (so the page's own <img> and canvas decoders see a decodable page) ──
function crc32(buffer) {
  let value = ~0
  for (const byte of buffer) {
    value ^= byte
    for (let bit = 0; bit < 8; bit += 1) value = (value >>> 1) ^ (0xedb88320 & -(value & 1))
  }
  return ~value
}
function png(width, height) {
  const raw = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = y * (width * 3 + 1) + 1 + x * 3
      raw[offset] = 244; raw[offset + 1] = 242; raw[offset + 2] = 236
    }
  }
  const chunk = (type, body) => {
    const length = Buffer.alloc(4); length.writeUInt32BE(body.length)
    const name = Buffer.from(type, 'ascii')
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(Buffer.concat([name, body])) >>> 0)
    return Buffer.concat([length, name, body, crc])
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0)),
  ])
}
const PAGE_IMAGE = png(320, 440)
const TEACHER = { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true }

// The session shape tests/answer-sheet-workflow.test.js uses for its fault read-only case.
const REVIEW_SESSION = {
  session_id: 's1',
  teacher_id: 910001,
  class_id: 10,
  status: 'review',
  review_revision: 3,
  answer_source: 'embedded',
  source_exam_pages: [],
  answer_pages: [],
  source_question_drafts: [],
  answer_spec_snapshot: [],
  answer_sheet_pages: [],
  answer_regions: [],
  identity_regions: [],
  student_pages: [
    { paper_id: 'p1', filename: '张三-1.jpg', orientation_status: 'auto_passed' },
    { paper_id: 'p2', filename: '张三-2.jpg', orientation_status: 'auto_passed' },
    { paper_id: 'p3', filename: '李四-1.jpg', orientation_status: 'auto_passed' },
  ],
  grouping_decisions: [
    { student_id: 7, paper_ids: ['p1', 'p2'], result: { student_name: '张三', needs_review_count: 0, details: [{ question_no: '1', result: 'correct', paper_ids: ['p1'] }] } },
    { student_id: 8, paper_ids: ['p3'], result: { student_name: '李四', needs_review_count: 0, details: [{ question_no: '1', result: 'correct', paper_ids: ['p3'] }] } },
  ],
}

const results = []
let browser

// ── Page factory: mocked API, recorded traffic, console/network evidence ──
async function openPage({
  path,
  reducedMotion = 'no-preference',
  hang = [],
  session = null,
  answerSheetAvailability = { status: 'available' },
  stallImageBitmap = false,
} = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  await context.addInitScript(({ stall }) => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    if (stall) window.createImageBitmap = () => new Promise(() => {})
  }, { stall: stallImageBitmap })
  const page = await context.newPage()
  if (reducedMotion !== 'no-preference') await page.emulateMedia({ reducedMotion })

  const requests = []
  const pageErrors = []
  const consoleErrors = []
  const blockedExternal = []
  const localFailures = []
  const hung = new Set(hang)
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('requestfailed', (request) => {
    const entry = `${request.method()} ${request.url()} (${request.failure()?.errorText || 'failed'})`
    if (new URL(request.url()).hostname === '127.0.0.1') localFailures.push(entry)
    else blockedExternal.push(entry)
  })

  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    requests.push(`${request.method()} ${url.pathname}`)
    // A hung request: never answered, so the page stays in its in-progress state.
    if (hung.has(url.pathname)) return undefined
    if (/\/(students|pages)\/[^/]+\/image$/.test(url.pathname)) {
      return route.fulfill({ status: 200, contentType: 'image/png', body: PAGE_IMAGE })
    }
    let data = {}
    if (url.pathname === '/api/auth/me') data = { user: TEACHER }
    else if (url.pathname === '/api/auth/classes') data = { classes: [{ id: 10, class_name: '一班' }], current_class_id: 10 }
    else if (url.pathname === '/api/answer-sheet/availability') data = answerSheetAvailability
    else if (url.pathname === '/api/answer-sheet/templates/history') data = { templates: [] }
    else if (url.pathname === '/api/answer-sheet/paper-templates/sources') data = { sources: [] }
    else if (url.pathname === '/api/answer-sheet/paper-templates') data = { templates: [] }
    else if (url.pathname === '/api/answer-sheet/sessions') data = { batches: [] }
    else if (url.pathname === '/api/answer-sheet/sessions/s1' && session) data = session
    else if (url.pathname === '/api/batch-grade/availability') data = { status: 'available', hardware: { status: 'ready' } }
    else if (url.pathname === '/api/batch-grade/template/availability') data = { status: 'available' }
    else if (url.pathname === '/api/bank/pending-answers') data = []
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })

  // Navigation happens only after the mock routes are installed.
  if (path) await page.goto(base + path)

  return {
    page,
    context,
    requests,
    // Console noise caused by the mock's own external blocking (fonts.googleapis.com) is
    // not an application error; 127.0.0.1 failures are.
    report: () => ({
      pageErrors,
      consoleErrors,
      externalBlocked: blockedExternal,
      localRequestFailures: localFailures,
      appErrors: [...pageErrors, ...consoleErrors.filter((text) => !/ERR_FAILED/.test(text))],
    }),
    async close() { await context.close() },
  }
}

async function computed(page, selector) {
  return page.evaluate((target) => {
    const element = document.querySelector(target)
    if (!element) return null
    const style = getComputedStyle(element)
    return {
      pointerEvents: style.pointerEvents,
      opacity: style.opacity,
      visibility: style.visibility,
      display: style.display,
    }
  }, selector)
}

// Every node of a loader, with the animation properties the reduced-motion contract names.
async function loaderMetrics(page, selector) {
  return page.evaluate((target) => {
    const loader = document.querySelector(target)
    if (!loader) return null
    const read = (node) => {
      const style = getComputedStyle(node)
      return {
        tag: node.tagName,
        cls: node.getAttribute('class') || '',
        animationName: style.animationName,
        animationDuration: style.animationDuration,
        animationPlayState: style.animationPlayState,
      }
    }
    const nodes = [read(loader), ...[...loader.querySelectorAll('*')].map(read)]
    const animations = loader.getAnimations({ subtree: true })
    const longest = (value) => Math.max(...String(value).split(',').map((part) => {
      const seconds = /([\d.]+(?:e-?\d+)?)s/.exec(part.trim())
      return seconds ? Number(seconds[1]) : 0
    }))
    return {
      nodes,
      named: nodes.filter((node) => node.animationName !== 'none').length,
      longestDurationSeconds: Math.max(...nodes.map((node) => longest(node.animationDuration))),
      runningAnimations: animations.filter((animation) => (
        animation.playState === 'running'
        && Number(animation.effect?.getTiming?.().duration || 0) > 1
      )).length,
      totalAnimations: animations.length,
    }
  }, selector)
}

function assertLoaderStatic(metrics, label) {
  assert.ok(metrics, `${label}: loader not found`)
  for (const node of metrics.nodes) {
    assert.ok(
      node.animationName === 'none' || node.animationPlayState === 'paused',
      `${label}: ${node.cls || node.tag} still animates (${node.animationName}, ${node.animationDuration}, ${node.animationPlayState})`,
    )
  }
  assert.equal(metrics.runningAnimations, 0, `${label}: ${metrics.runningAnimations} animation(s) still running`)
  // Reduced motion is expressed two ways here: the component's own `animation: none` and the
  // app-wide clamp in styles/global.css (animation-duration 0.01ms). Neither may exceed that.
  assert.ok(
    metrics.longestDurationSeconds <= 0.00002,
    `${label}: a loader node keeps a ${metrics.longestDurationSeconds}s animation duration`,
  )
}

async function check(id, body) {
  const details = {}
  try {
    await body(details)
    results.push({ id, status: 'PASS', ...details })
  } catch (error) {
    results.push({ id, status: 'FAIL', error: error.message, ...details })
  }
}

// The usage-guide dialog opens on a fresh profile; close it so it cannot intercept clicks.
async function dismissGuide(page) {
  const skip = page.locator('button', { hasText: '跳过引导' })
  if (await skip.count()) await skip.first().click({ timeout: 3000 }).catch(() => {})
  await page.waitForTimeout(150)
}

const templateFile = { name: '模板-1.png', mimeType: 'image/png', buffer: PAGE_IMAGE }

// /upload → import one template page → start template recognition, which is left hanging.
// Both the page and its mocks must already be in place (openPage({ path: '/upload' })).
async function startTemplateRecognition(page) {
  await page.locator('input[type=file]').first().waitFor({ state: 'attached', timeout: 20000 })
  await dismissGuide(page)
  await page.locator('input[type=file]').first().setInputFiles(templateFile)
  await page.locator('.file-summary').waitFor({ timeout: 15000 })
  // No force: the button must actually be enabled, and the click must be the one the page handles.
  await page.locator('button', { hasText: '识别模板' }).first().click({ timeout: 15000 })
  await page.locator('.tpl-recognize-state').waitFor({ timeout: 15000 })
}

try {
  browser = await chromium.launch({ headless: true })

  // ── #1 read-only answer sheet stays interactive ──
  await check('1-answer-sheet-readonly-interactive', async (details) => {
    const app = await openPage({ path: '/answer-sheet?session=s1', session: REVIEW_SESSION, answerSheetAvailability: { status: 'fault' } })
    try {
      const page = app.page
      await page.locator('.answer-sheet-page').waitFor({ timeout: 20000 })
      await page.locator('.answer-sheet-page.workflow-readonly').waitFor({ timeout: 20000 })
      await dismissGuide(page)

      details.readonlyClass = true
      details.serviceTag = await page.locator('.service-state').first().innerText().catch(() => null)
      details.tabTexts = await page.locator('.review-student-tabs button').allInnerTexts()
      details.before = {
        tabs: await computed(page, '.review-student-tabs'),
        pane: await computed(page, '.student-paper-pane'),
        activeIndex: await page.evaluate(() => [...document.querySelectorAll('.review-student-tabs button')].findIndex((b) => b.classList.contains('active'))),
        paneTitle: (await page.locator('.review-pane-title span').first().innerText()).trim(),
        imageAlt: await page.locator('.student-paper-pane img').getAttribute('alt'),
      }

      for (const [name, style] of [['tabs', details.before.tabs], ['pane', details.before.pane]]) {
        assert.ok(style, `.review-student-${name}: ${name} not rendered`)
        assert.notEqual(style.pointerEvents, 'none', `${name}: pointer-events is none`)
        assert.equal(style.opacity, '1', `${name}: opacity is ${style.opacity}`)
        assert.notEqual(style.visibility, 'hidden', `${name}: visibility hidden`)
      }
      assert.equal(details.before.activeIndex, 0)
      assert.equal(details.tabTexts.length, 2)
      assert.equal(details.before.paneTitle, '张三')

      await page.locator('.review-student-tabs button').nth(1).click({ timeout: 10000 })
      await page.waitForFunction(
        () => (document.querySelector('.review-pane-title span')?.textContent || '').trim() === '李四',
        null,
        { timeout: 10000 },
      )
      details.after = {
        activeIndex: await page.evaluate(() => [...document.querySelectorAll('.review-student-tabs button')].findIndex((b) => b.classList.contains('active'))),
        paneTitle: (await page.locator('.review-pane-title span').first().innerText()).trim(),
        imageAlt: await page.locator('.student-paper-pane img').getAttribute('alt'),
        imageSrc: await page.locator('.student-paper-pane img').getAttribute('src'),
        pageCounter: (await page.locator('.review-pager span').first().innerText()).trim(),
        tabs: await computed(page, '.review-student-tabs'),
        pane: await computed(page, '.student-paper-pane'),
      }
      details.paperImageRequests = app.requests.filter((entry) => /students\/[^/]+\/image$/.test(entry))

      assert.equal(details.after.activeIndex, 1, 'clicking the second student tab did not activate it')
      assert.equal(details.after.paneTitle, '李四', 'the paper pane still shows the first student')
      assert.match(String(details.after.imageAlt), /李四/, 'the visible paper did not change')
      assert.match(String(details.after.imageSrc), /^blob:/)
      assert.ok(
        details.paperImageRequests.includes('GET /api/answer-sheet/sessions/s1/students/p3/image'),
        `no image request for the second student: ${details.paperImageRequests.join(', ')}`,
      )
      assert.equal(details.after.tabs.pointerEvents, 'auto')
      assert.equal(details.after.pane.opacity, '1')

      await page.screenshot({ path: resolve(output, 'check1-answer-sheet-readonly.png'), fullPage: true })
      await page.locator('.grading-review').screenshot({ path: resolve(output, 'check1-grading-review.png') }).catch(() => {})
      details.screenshots = ['check1-answer-sheet-readonly.png', 'check1-grading-review.png']
    } finally {
      details.console = app.report()
      await app.close()
    }
  })

  // ── #6a SheetLoader static under reduced motion (answer-sheet busy veil) ──
  await check('6-sheet-loader-reduced-motion', async (details) => {
    const app = await openPage({
      path: '/answer-sheet?session=s1', session: REVIEW_SESSION, answerSheetAvailability: { status: 'fault' },
      hang: ['/api/answer-sheet/sessions/s1'], reducedMotion: 'reduce',
    })
    try {
      const page = app.page
      await page.locator('.busy-plate .sheet-loader').waitFor({ state: 'visible', timeout: 20000 })
      details.media = await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
      details.selector = '.busy-plate .sheet-loader'
      details.veilText = (await page.locator('.busy-plate p').first().innerText()).replace(/\s+/g, ' ').trim()
      details.loader = await loaderMetrics(page, '.busy-plate .sheet-loader')
      assertLoaderStatic(details.loader, 'SheetLoader under reduce')
      await page.screenshot({ path: resolve(output, 'check6-sheet-loader-reduced.png') })
      details.screenshots = ['check6-sheet-loader-reduced.png']

      // Control: the same veil and loader with motion allowed must animate.
      const control = await openPage({
        path: '/answer-sheet?session=s1', session: REVIEW_SESSION, answerSheetAvailability: { status: 'fault' },
        hang: ['/api/answer-sheet/sessions/s1'], reducedMotion: 'no-preference',
      })
      try {
        await control.page.locator('.busy-plate .sheet-loader').waitFor({ state: 'visible', timeout: 20000 })
        details.control = await loaderMetrics(control.page, '.busy-plate .sheet-loader')
        assert.ok(details.control.runningAnimations > 0, 'control: the loader does not animate even with motion allowed')
        await control.page.screenshot({ path: resolve(output, 'check6-sheet-loader-control-motion.png') })
        details.screenshots.push('check6-sheet-loader-control-motion.png')
      } finally { await control.close() }
    } finally {
      details.console = app.report()
      await app.close()
    }
  })

  // ── #6b InkSpinner static under reduced motion (/upload template recognition) ──
  await check('6-ink-spinner-reduced-motion', async (details) => {
    const app = await openPage({
      path: '/upload', hang: ['/api/batch-grade/template/recognize'], reducedMotion: 'reduce',
    })
    try {
      const page = app.page
      await startTemplateRecognition(page)
      await page.locator('.tpl-recognize-state .ink-spinner').waitFor({ state: 'visible', timeout: 20000 })
      details.media = await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
      details.selector = '.tpl-recognize-state .ink-spinner'
      details.stateText = (await page.locator('.tpl-recognize-state').first().innerText()).trim()
      details.loader = await loaderMetrics(page, '.tpl-recognize-state .ink-spinner')
      assertLoaderStatic(details.loader, 'InkSpinner under reduce')
      await page.screenshot({ path: resolve(output, 'check6-ink-spinner-reduced.png') })
      details.screenshots = ['check6-ink-spinner-reduced.png']

      const control = await openPage({ path: '/upload', hang: ['/api/batch-grade/template/recognize'] })
      try {
        await startTemplateRecognition(control.page)
        await control.page.locator('.tpl-recognize-state .ink-spinner').waitFor({ state: 'visible', timeout: 20000 })
        details.control = await loaderMetrics(control.page, '.tpl-recognize-state .ink-spinner')
        assert.ok(details.control.runningAnimations > 0, 'control: the ink spinner does not animate even with motion allowed')
        await control.page.screenshot({ path: resolve(output, 'check6-ink-spinner-control-motion.png') })
        details.screenshots.push('check6-ink-spinner-control-motion.png')
      } finally { await control.close() }
    } finally {
      details.console = app.report()
      await app.close()
    }
  })

  // ── #7 /upload: shared loader in progress, no legacy ring spinner animating ──
  const scanLoaders = (page) => page.evaluate(() => {
    const read = (element) => {
      const style = getComputedStyle(element)
      return {
        cls: element.getAttribute('class') || element.tagName,
        animationName: style.animationName,
        animationDuration: style.animationDuration,
        animationPlayState: style.animationPlayState,
        visible: Boolean(element.getClientRects().length),
      }
    }
    const all = [...document.querySelectorAll('[class]')]
    const shared = all.filter((element) => (
      element.classList.contains('ink-spinner') || element.classList.contains('sheet-loader')
    ))
    const matching = all.filter((element) => /spinner|ring/i.test(element.getAttribute('class') || ''))
    const legacy = matching.filter((element) => (
      !element.classList.contains('ink-spinner') && !element.classList.contains('sheet-loader')
    ))
    return {
      shared: { count: shared.length, running: shared.filter((element) => read(element).animationName !== 'none' && read(element).animationPlayState === 'running').length },
      matchingClasses: [...new Set(matching.map((element) => element.getAttribute('class') || ''))],
      legacy: legacy.map(read),
      legacyAnimating: legacy.filter((element) => {
        const node = read(element)
        return node.animationName !== 'none' && node.animationPlayState === 'running'
      }).length,
      sheetLoaderCount: all.filter((element) => element.classList.contains('sheet-loader')).length,
      inkSpinnerCount: all.filter((element) => element.classList.contains('ink-spinner')).length,
    }
  })

  await check('7-upload-recognition-loader', async (details) => {
    const app = await openPage({ path: '/upload', hang: ['/api/batch-grade/template/recognize'] })
    try {
      const page = app.page
      await startTemplateRecognition(page)
      await page.locator('.tpl-recognize-state .ink-spinner').waitFor({ state: 'visible', timeout: 20000 })
      details.state = 'template recognition in progress'
      details.stateText = (await page.locator('.tpl-recognize-state').first().innerText()).trim()
      details.recognition = await scanLoaders(page)
      assert.ok(details.recognition.inkSpinnerCount >= 1, 'the shared InkSpinner is not on screen during recognition')
      assert.equal(details.recognition.legacyAnimating, 0, `${details.recognition.legacyAnimating} legacy spinner/ring element(s) animating`)
      assert.deepEqual(details.recognition.legacy.map((node) => node.cls), [], 'legacy ring/spinner elements present')
      await page.screenshot({ path: resolve(output, 'check7-upload-recognizing.png') })
      await page.locator('.tpl-recognize-state').screenshot({ path: resolve(output, 'check7-upload-recognizing-loader.png') }).catch(() => {})
      details.screenshots = ['check7-upload-recognizing.png', 'check7-upload-recognizing-loader.png']
    } finally {
      details.console = app.report()
      await app.close()
    }
  })

  // The shared SheetLoader on /upload: a template page stuck in preprocessing.
  await check('7-upload-sheet-loader-present', async (details) => {
    const app = await openPage({ path: '/upload', stallImageBitmap: true })
    try {
      const page = app.page
      await page.locator('input[type=file]').first().waitFor({ state: 'attached', timeout: 20000 })
      await dismissGuide(page)
      await page.locator('input[type=file]').first().setInputFiles(templateFile)
      await page.locator('.template-upload .sheet-loader').waitFor({ state: 'visible', timeout: 20000 })
      details.state = 'template image preprocessing in progress'
      details.stateText = (await page.locator('.template-upload .t-label').first().innerText()).trim()
      details.acceptance = await scanLoaders(page)
      assert.ok(details.acceptance.sheetLoaderCount >= 1, 'the shared SheetLoader is not on screen while preprocessing')
      assert.equal(details.acceptance.legacyAnimating, 0, `${details.acceptance.legacyAnimating} legacy spinner/ring element(s) animating`)
      await page.screenshot({ path: resolve(output, 'check7-upload-preprocessing.png') })
      details.screenshots = ['check7-upload-preprocessing.png']
    } finally {
      details.console = app.report()
      await app.close()
    }
  })
} finally {
  await browser?.close()
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ base, mockedApiOnly: true, results }, null, 2))
}

const summary = results.map(({ id, status, error }) => ({ id, status, ...(error ? { error } : {}) }))
console.log(JSON.stringify({ summary, output }, null, 2))
if (results.some((result) => result.status === 'FAIL')) process.exitCode = 1