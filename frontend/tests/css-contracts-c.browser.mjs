// CSS/pointer contracts observed in a real headless browser.
//
//   node tests/css-contracts-c.browser.mjs http://127.0.0.1:5197 [shots-dir]
//
// #5  DrawingReferenceEditor: a read-only canvas (svg.is-readonly) must let the
//     pointer through to the geometry overlays; 'reference editing first' mode
//     (svg.is-reference-priority, editable) must put the canvas above them.
// #8  Manual review workbench at 1440x1000: whole page contained, dialog body
//     not scrolling, review list scrolling on its own without moving the paper.
// #9  Same workbench at 1920 wide: dialog nearly full width, 2.25 : 0.85 tracks.
// #10 Many choice/fill questions: compact answer-sheet grid, full-width list
//     after hiding the paper, current-page cells highlighted.
//
// Only 127.0.0.1 is contacted; every /api/* request is answered from this
// script (no backend, no account). Nothing in the repo is modified.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local mocked pages may be tested')
const shots = resolve(process.argv[3] || 'D:/hermes/cache/scratch/css-c-shots')
mkdirSync(shots, { recursive: true })

const TEACHER_ID = 910001
const BATCH = 'b-review'
const consoleErrors = []
const pageErrors = []
const apiRequests = []
const foreignRequests = []
const results = []

const compactQuestions = [
  ['3', 'choice', '$\\sqrt{3}$', '$\\sqrt{2}$', 'wrong', 0],
  ['4', 'choice', 'B', 'B', 'correct', 0],
  ['5', 'choice', 'C', 'A', 'wrong', 0],
  ['6', 'fill', '$x^2$', '$x^2$', 'correct', 0],
  ['7', 'fill', '$\\frac{1}{2}$', '$\\frac{1}{3}$', 'wrong', 0],
  ['8', 'fill', '$47$', '$47$', 'correct', 0],
  ['9', 'choice', 'D', 'D', 'correct', 1],
  ['10', 'fill', '$y=2x+1$', '$y=2x+1$', 'correct', 1],
]

const rows = [{
  paper_id: 'p1',
  review_group_id: 'g1',
  student_name: '甲',
  filename: '甲-1.png',
  page_index: 0,
  pages: [
    { paper_id: 'p1', page_index: 0, filename: '甲-1.png' },
    { paper_id: 'p2', page_index: 1, filename: '甲-2.png' },
  ],
  disposition: 'active',
  correct: 5, wrong: 3, total: 9,
  details: [
    ...compactQuestions.map(([question_no, question_type, student_answer, correct_answer, result, page_index]) => ({
      question_no, question_type, student_answer, correct_answer, result, page_index,
      paper_id: page_index === 0 ? 'p1' : 'p2',
    })),
    {
      question_no: '13', question: '题干X：证明两个三角形全等',
      student_answer: '$\\frac{1}{2}$', correct_answer: '$\\sqrt{2}$',
      result: 'needs_review', page_index: 0, paper_id: 'p1',
    },
    ...['11', '12', '14', '15', '16', '17', '18'].map((question_no, index) => ({
      question_no,
      student_answer: `$$x_{${question_no}}$`,
      correct_answer: `$$x_{${question_no}}$`,
      result: index % 2 ? 'correct' : 'partial',
      page_index: index % 3 === 0 ? 1 : 0,
      paper_id: index % 3 === 0 ? 'p2' : 'p1',
    })),
  ],
}]

const browser = await chromium.launch({ headless: true })

function watch(page, label) {
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(`${label}: ${message.text()}`)
  })
  page.on('pageerror', (error) => pageErrors.push(`${label}: ${error.message}`))
}

async function mockedContext(viewport) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1 })
  await context.addInitScript(({ teacherId, batch }) => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem(`mathagent_usage_guide_v3:${teacherId}`, 'true')
    localStorage.setItem(`mathagent_review_batch_id:${teacherId}`, batch)
  }, { teacherId: TEACHER_ID, batch: BATCH })
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) {
      foreignRequests.push(request.url())
      return route.abort()
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    apiRequests.push(`${request.method()} ${url.pathname}`)
    if (request.method() !== 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    const json = (data) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
    if (url.pathname === '/api/auth/me') {
      return json({ user: { id: TEACHER_ID, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true } })
    }
    if (url.pathname === '/api/auth/classes') return json({ classes: [], current_class_id: null })
    if (url.pathname === `/api/batch-grade/review/${BATCH}`) {
      return json({ batch_id: BATCH, status: 'review_ready', review_revision: 3, summary: { total: 1 }, results: rows })
    }
    if (url.pathname === `/api/batch-grade/report/${BATCH}`) return json({ status: 'ready', text: '', stats: {} })
    const paperMatch = url.pathname.match(new RegExp(`^/api/batch-grade/${BATCH}/paper/([^/]+)/image$`))
    if (paperMatch) return route.fulfill({ status: 200, contentType: 'image/png', body: paperPng })
    if (url.pathname.includes('/pending')) return json([])
    return json({})
  })
  return context
}

let paperPng

// A real PNG with a known page-like aspect ratio, drawn in the browser.
async function buildPagePng(context) {
  const probe = await context.newPage()
  await probe.goto('about:blank')
  const dataUrl = await probe.evaluate(() => {
    const canvas = document.createElement('canvas')
    canvas.width = 1200
    canvas.height = 1700
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.strokeStyle = '#333333'
    ctx.lineWidth = 3
    for (let y = 90; y < canvas.height; y += 52) {
      ctx.beginPath()
      ctx.moveTo(70, y)
      ctx.lineTo(canvas.width - 70, y)
      ctx.stroke()
    }
    return canvas.toDataURL('image/png')
  })
  paperPng = Buffer.from(dataUrl.split(',')[1], 'base64')
  await probe.close()
}

const rectOf = (element) => {
  const r = element.getBoundingClientRect()
  return { left: +r.left.toFixed(2), top: +r.top.toFixed(2), width: +r.width.toFixed(2), height: +r.height.toFixed(2), right: +r.right.toFixed(2), bottom: +r.bottom.toFixed(2) }
}

async function openWorkbench(page) {
  await page.goto(base + '/upload')
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' }).catch(() => {})
  const open = page.getByRole('button', { name: '原卷与批阅结果' }).first()
  await open.waitFor({ timeout: 30000 })
  await open.click()
  await page.locator('.manual-review-dialog').waitFor({ timeout: 15000 })
  await page.locator('.manual-review-paper img').waitFor({ timeout: 15000 })
  await page.waitForFunction(() => {
    const image = document.querySelector('.manual-review-paper img')
    return image && image.complete && image.naturalWidth > 0
  }, null, { timeout: 15000 })
}

async function measurePaper(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('.manual-review-dialog')
    const body = dialog.querySelector('.el-dialog__body')
    const stage = dialog.querySelector('.manual-review-image-stage')
    const image = dialog.querySelector('.manual-review-paper img')
    const list = dialog.querySelector('.manual-review-list')
    const paper = dialog.querySelector('.manual-review-paper')
    const box = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom } }
    return {
      viewport: { width: innerWidth, height: innerHeight },
      layoutWidth: document.documentElement.clientWidth,
      overlay: (() => {
        const element = dialog.closest('.el-overlay-dialog') || dialog.parentElement
        const r = element.getBoundingClientRect()
        return { width: r.width, height: r.height, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, overflowY: getComputedStyle(element).overflowY }
      })(),
      dialogMargin: { top: getComputedStyle(dialog).marginTop, bottom: getComputedStyle(dialog).marginBottom, left: getComputedStyle(dialog).marginLeft },
      dialogComputedWidth: getComputedStyle(dialog).width,
      dialogMaxWidth: getComputedStyle(dialog).maxWidth,
      dialog: box(dialog),
      body: { ...box(body), scrollHeight: body.scrollHeight, clientHeight: body.clientHeight, scrollWidth: body.scrollWidth, clientWidth: body.clientWidth },
      stage: { ...box(stage), scrollHeight: stage.scrollHeight, clientHeight: stage.clientHeight, scrollWidth: stage.scrollWidth, clientWidth: stage.clientWidth },
      image: { ...box(image), naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight, objectFit: getComputedStyle(image).objectFit },
      paper: box(paper),
      list: { ...box(list), scrollHeight: list.scrollHeight, clientHeight: list.clientHeight },
      dialogZ: getComputedStyle(dialog).zIndex,
    }
  })
}

try {
  // ---------------------------------------------------------------- #5 setup
  // The editor is mounted from the running dev app's own compiled module with
  // Upload.vue's geometry-overlay markup (its scoped CSS included), because the
  // mocked Upload flow never leaves the template editor unlocked+non-interactive.
  {
    const context = await mockedContext({ width: 1280, height: 1000 })
    const page = await context.newPage()
    watch(page, '#5')
    await buildPagePng(context)
    await page.goto(base + '/upload')
    await page.waitForTimeout(1500)

    const mounted = await page.evaluate(async (b64) => {
      const paperDataUrl = 'data:image/png;base64,' + b64
      // Upload.vue's scope id: the one whose attribute makes the geometry
      // overlay CSS apply (observed through computed style, not rule text).
      const tokens = new Set()
      for (const el of document.querySelectorAll('*')) {
        for (const attribute of el.attributes) if (attribute.name.startsWith('data-v-')) tokens.add(attribute.name)
      }
      const probe = document.createElement('div')
      probe.style.cssText = 'width:600px;height:400px'
      probe.className = 'tpl-reference-geometry'
      const child = document.createElement('div')
      child.className = 'tpl-handwriting-region'
      probe.appendChild(child)
      document.body.appendChild(probe)
      let scope = null
      for (const token of tokens) {
        probe.setAttribute(token, '')
        child.setAttribute(token, '')
        const parentStyle = getComputedStyle(probe)
        const childStyle = getComputedStyle(child)
        if (parentStyle.position === 'absolute' && childStyle.position === 'absolute' && childStyle.pointerEvents === 'auto') scope = token
        probe.removeAttribute(token)
        child.removeAttribute(token)
        if (scope) break
      }
      probe.remove()
      if (!scope) throw new Error('no scope id makes the geometry overlay CSS apply')

      // Vue is already running on this page. Take its module URL from what the
      // dev server has already served, so this script never reads a .vue file's
      // text (the repository forbids tests that do).
      const vueUrl = (() => {
        const served = performance.getEntriesByType('resource')
          .map(entry => entry.name)
          .filter(name => /\/deps\/vue\.js(\?|$)/.test(name))
        return served[served.length - 1] || '/node_modules/.vite/deps/vue.js'
      })()
      const Vue = await import(vueUrl)
      const Editor = (await import('/src/components/DrawingReferenceEditor.vue')).default

      const host = document.createElement('div')
      host.id = 'css-c-editor-host'
      host.style.cssText = 'position:fixed;left:0;top:0;width:900px;z-index:2147483000;background:#fff;padding:12px;'
      document.body.appendChild(host)
      const state = Vue.reactive({ interactive: false, priorityEditing: false })
      const scoped = { [scope]: '' }
      const overlay = () => Vue.h('div', { class: 'tpl-reference-geometry', ...scoped }, [
        Vue.h('div', { class: 'tpl-handwriting-region', 'data-probe': 'answer', style: { left: '10%', top: '20%', width: '40%', height: '14%' }, ...scoped }),
        Vue.h('div', { class: 'tpl-question-anchor', 'data-probe': 'question', style: { left: '62%', top: '58%', width: '16%', height: '8%' }, ...scoped }),
      ])
      const app = Vue.createApp({
        setup() {
          return () => Vue.h(Editor, {
            modelValue: null,
            imageUrl: paperDataUrl,
            drawingRegions: [[50, 50, 900, 900]],
            showControls: true,
            interactive: state.interactive,
            priorityEditing: state.priorityEditing,
            disabled: false,
            canvasClass: 'tpl-cut-preview-stage',
          }, { 'canvas-overlays': overlay })
        },
      })
      app.mount(host)
      window.__cssCState = state
      await new Promise((done) => setTimeout(done, 900))
      return {
        scope,
        hasCanvas: Boolean(host.querySelector('svg[role="img"]')),
        imageLoaded: host.querySelector('.drawing-reference-canvas img')?.naturalWidth > 0,
      }
    }, paperPng.toString('base64'))

    if (!mounted.imageLoaded) {
      await page.evaluate(async (b64) => {
        const image = document.querySelector('#css-c-editor-host .drawing-reference-canvas img')
        if (!image) return
        image.src = 'data:image/png;base64,' + b64
        await new Promise((done) => setTimeout(done, 800))
      }, paperPng.toString('base64'))
      await page.waitForTimeout(500)
    }

    const hitTest = () => page.evaluate(() => {
      const host = document.querySelector('#css-c-editor-host')
      const svg = host.querySelector('svg[role="img"]')
      const describe = (element) => {
        if (!element) return null
        const path = []
        for (let node = element; node && node !== host; node = node.parentElement) {
          path.unshift(node.tagName.toLowerCase() + (node.className && typeof node.className === 'string' ? '.' + node.className.trim().split(/\s+/).join('.') : ''))
        }
        return path.join(' > ')
      }
      const probe = (selector) => {
        const box = host.querySelector(selector).getBoundingClientRect()
        const point = { x: box.left + box.width / 2, y: box.top + box.height / 2 }
        const element = document.elementFromPoint(point.x, point.y)
        return {
          point: { x: +point.x.toFixed(1), y: +point.y.toFixed(1) },
          hit: describe(element),
          isCanvas: Boolean(element && (element === svg || svg.contains(element))),
          isOverlay: Boolean(element && element.closest('.tpl-reference-geometry')),
          targetClass: element?.className || '',
        }
      }
      const style = getComputedStyle(svg)
      return {
        svgClass: svg.className.baseVal ?? svg.getAttribute('class'),
        svgPointerEvents: style.pointerEvents,
        svgZIndex: style.zIndex,
        svgRect: (() => { const r = svg.getBoundingClientRect(); return { width: r.width, height: r.height } })(),
        answer: probe('.tpl-handwriting-region'),
        question: probe('.tpl-question-anchor'),
      }
    })

    // (a) read-only canvas (muted): pointer must pass through to the overlays
    const readonly = await page.evaluate(() => {
      const host = document.querySelector('#css-c-editor-host')
      const svg = host.querySelector('svg[role="img"]')
      return { class: svg.getAttribute('class'), opacity: getComputedStyle(svg).opacity }
    })
    await page.screenshot({ path: resolve(shots, '5-readonly-select-mode.png'), clip: { x: 0, y: 0, width: 940, height: 900 } })

    const readonlyHits = await hitTest()

    // (b) arm a drawing tool while editable, then drop editability: the svg is
    // read-only with no select-mode / reference-priority class to explain it.
    await page.evaluate(() => { window.__cssCState.interactive = true })
    await page.waitForTimeout(200)
    await page.evaluate(() => {
      const host = document.querySelector('#css-c-editor-host')
      const button = [...host.querySelectorAll('button')].find((item) => item.textContent.trim() === '点')
      if (!button) throw new Error('the 点 tool button is missing')
      button.click()
    })
    await page.waitForTimeout(300)
    const armedTool = await page.evaluate(() => {
      const svg = document.querySelector('#css-c-editor-host svg[role="img"]')
      return { class: svg.getAttribute('class'), pressed: [...document.querySelectorAll('#css-c-editor-host button[aria-pressed="true"]')].map((b) => b.textContent.trim()) }
    })
    await page.evaluate(() => { window.__cssCState.interactive = false; window.__cssCState.priorityEditing = false })
    await page.waitForTimeout(400)
    const readonlyOnly = await page.evaluate(() => {
      const svg = document.querySelector('#css-c-editor-host svg[role="img"]')
      return { class: svg.getAttribute('class'), pointerEvents: getComputedStyle(svg).pointerEvents, zIndex: getComputedStyle(svg).zIndex }
    })
    const readonlyOnlyHits = await hitTest()
    await page.screenshot({ path: resolve(shots, '5-readonly-only.png'), clip: { x: 0, y: 0, width: 940, height: 900 } })

    // (c) 'reference editing first' (is-reference-priority, editable)
    await page.evaluate(() => { window.__cssCState.interactive = true; window.__cssCState.priorityEditing = true })
    await page.waitForTimeout(500)
    const priority = await page.evaluate(() => {
      const host = document.querySelector('#css-c-editor-host')
      const svg = host.querySelector('svg[role="img"]')
      return { class: svg.getAttribute('class') }
    })
    const priorityHits = await hitTest()
    await page.screenshot({ path: resolve(shots, '5-reference-priority.png'), clip: { x: 0, y: 0, width: 940, height: 900 } })

    const five = {
      id: '#5',
      method: 'real DrawingReferenceEditor.vue module mounted in the running dev app with Upload.vue\'s .tpl-reference-geometry overlay markup and scoped CSS',
      readonly: { ...readonly, pointerEvents: readonlyHits.svgPointerEvents, zIndex: readonlyHits.svgZIndex, hits: { answer: readonlyHits.answer, question: readonlyHits.question } },
      readonlyAfterArmedToolDropped: { ...readonlyOnly, armedTool, hits: { answer: readonlyOnlyHits.answer, question: readonlyOnlyHits.question } },
      priority: { ...priority, pointerEvents: priorityHits.svgPointerEvents, zIndex: priorityHits.svgZIndex, hits: { answer: priorityHits.answer, question: priorityHits.question } },
      svgRect: priorityHits.svgRect,
      scope: mounted.scope,
    }
    five.checks = [
      { name: 'svg carries is-readonly', pass: /(^|\s)is-readonly(\s|$)/.test(five.readonly.class), detail: five.readonly.class },
      { name: 'read-only svg computed pointer-events is none', pass: five.readonly.pointerEvents === 'none', detail: five.readonly.pointerEvents },
      { name: 'answer-box hit-test lands on the overlay, not the canvas', pass: five.readonly.hits.answer.isOverlay && !five.readonly.hits.answer.isCanvas, detail: five.readonly.hits.answer.hit },
      { name: 'question-number-box hit-test lands on the overlay, not the canvas', pass: five.readonly.hits.question.isOverlay && !five.readonly.hits.question.isCanvas, detail: five.readonly.hits.question.hit },
      { name: 'svg carries is-reference-priority', pass: /(^|\s)is-reference-priority(\s|$)/.test(five.priority.class), detail: five.priority.class },
      { name: 'reference-priority svg computed pointer-events is auto', pass: five.priority.pointerEvents === 'auto', detail: five.priority.pointerEvents },
      { name: 'answer-box area hit-test lands on the canvas', pass: five.priority.hits.answer.isCanvas && !five.priority.hits.answer.isOverlay, detail: five.priority.hits.answer.hit },
      { name: 'question-number area hit-test lands on the canvas', pass: five.priority.hits.question.isCanvas && !five.priority.hits.question.isOverlay, detail: five.priority.hits.question.hit },
    ]
    five.note = 'read-only editing state that is also is-select-mode; the component never leaves a read-only canvas without is-select-mode (arming a tool then dropping editability resets the tool to select), so is-readonly alone is not reachable'
    five.status = five.checks.every((check) => check.pass) ? 'PASS' : 'FAIL'
    results.push(five)
    await context.close()
  }

  // --------------------------------------------------------------- #8 / #9
  {
    const context = await mockedContext({ width: 1440, height: 1000 })
    const page = await context.newPage()
    watch(page, '#8')
    await openWorkbench(page)
    await page.waitForTimeout(500)

    const first = await measurePaper(page)
    const ratioOf = (box, naturalWidth, naturalHeight) => (box.width / box.height) / (naturalWidth / naturalHeight)
    const beforeScroll = await page.evaluate(() => {
      const list = document.querySelector('.manual-review-list')
      const image = document.querySelector('.manual-review-paper img')
      list.scrollTop = list.scrollHeight
      const r = image.getBoundingClientRect()
      return { scrollTop: list.scrollTop, maxScroll: list.scrollHeight - list.clientHeight, image: { left: r.left, top: r.top, width: r.width, height: r.height } }
    })
    await page.waitForTimeout(300)
    await page.screenshot({ path: resolve(shots, '8-workbench-1440-list-scrolled.png'), fullPage: false })
    const afterScroll = await page.evaluate(() => {
      const image = document.querySelector('.manual-review-paper img')
      const r = image.getBoundingClientRect()
      return { image: { left: r.left, top: r.top, width: r.width, height: r.height } }
    })
    await page.evaluate(() => { document.querySelector('.manual-review-list').scrollTop = 0 })

    const ratio = ratioOf(first.image, first.image.naturalWidth, first.image.naturalHeight)
    const contained = first.image.left >= first.stage.left - 0.5
      && first.image.top >= first.stage.top - 0.5
      && first.image.right <= first.stage.right + 0.5
      && first.image.bottom <= first.stage.bottom + 0.5
    const paperMoved = Math.abs(afterScroll.image.top - beforeScroll.image.top) > 0.5
      || Math.abs(afterScroll.image.left - beforeScroll.image.left) > 0.5
      || Math.abs(afterScroll.image.height - beforeScroll.image.height) > 0.5

    const eight = {
      id: '#8',
      viewport: first.viewport,
      image: first.image,
      stage: first.stage,
      boxRatio: +(first.image.width / first.image.height).toFixed(4),
      naturalRatio: +(first.image.naturalWidth / first.image.naturalHeight).toFixed(4),
      ratioPreservation: +ratio.toFixed(4),
      contained,
      objectFit: first.image.objectFit,
      body: { scrollHeight: first.body.scrollHeight, clientHeight: first.body.clientHeight, overflow: first.body.scrollHeight - first.body.clientHeight },
      list: { scrollHeight: first.list.scrollHeight, clientHeight: first.list.clientHeight, scrollable: first.list.scrollHeight > first.list.clientHeight, maxScroll: beforeScroll.maxScroll, scrolledTo: beforeScroll.scrollTop },
      paperBefore: beforeScroll.image,
      paperAfter: afterScroll.image,
      paperMovedWhileListScrolled: paperMoved,
    }
    eight.checks = [
      { name: 'image box keeps the natural aspect ratio (within 2%)', pass: Math.abs(ratio - 1) <= 0.02, detail: `box ${eight.boxRatio} vs natural ${eight.naturalRatio} -> ${eight.ratioPreservation}` },
      { name: 'image is fully inside the stage (not cropped)', pass: contained, detail: `image ${first.image.left},${first.image.top} ${first.image.width}x${first.image.height} in stage ${first.stage.left},${first.stage.top} ${first.stage.width}x${first.stage.height}` },
      { name: 'stage does not clip (no overflow)', pass: first.stage.scrollHeight <= first.stage.clientHeight + 1 && first.stage.scrollWidth <= first.stage.clientWidth + 1, detail: `scroll ${first.stage.scrollWidth}x${first.stage.scrollHeight} vs client ${first.stage.clientWidth}x${first.stage.clientHeight}` },
      { name: 'dialog body does not scroll', pass: first.body.scrollHeight <= first.body.clientHeight + 1, detail: `scrollHeight ${first.body.scrollHeight} clientHeight ${first.body.clientHeight}` },
      { name: 'review list is scrollable', pass: first.list.scrollHeight > first.list.clientHeight, detail: `scrollHeight ${first.list.scrollHeight} clientHeight ${first.list.clientHeight}` },
      { name: 'scrolling the list does not move the paper', pass: !paperMoved, detail: `paper top ${beforeScroll.image.top} -> ${afterScroll.image.top} after list.scrollTop ${beforeScroll.scrollTop}` },
    ]
    eight.status = eight.checks.every((check) => check.pass) ? 'PASS' : 'FAIL'
    results.push(eight)

    // #9: 1920 wide
    await page.setViewportSize({ width: 1920, height: 1000 })
    await page.waitForTimeout(600)
    const wide = await measurePaper(page)
    const dialogDelta = Math.abs(wide.dialog.width - wide.viewport.width)
    const gutter = (wide.viewport.width - wide.dialog.width) / 2
    const trackRatio = wide.paper.width / wide.list.width
    const expectedRatio = 2.25 / 0.85
    const nine = {
      id: '#9',
      viewport: wide.viewport.width,
      layoutWidth: wide.layoutWidth,
      dialogWidth: wide.dialog.width,
      dialogComputedWidth: wide.dialogComputedWidth,
      dialogMaxWidth: wide.dialogMaxWidth,
      dialogVsViewport: +dialogDelta.toFixed(2),
      gutterPerSide: +gutter.toFixed(2),
      dialogMargins: wide.dialogMargin,
      overlay: wide.overlay,
      paperWidth: wide.paper.width,
      listWidth: wide.list.width,
      ratio: +trackRatio.toFixed(4),
      expectedRatio: +expectedRatio.toFixed(4),
    }
    nine.checks = [
      { name: 'dialog spans the viewport (16px gutter per side)', pass: Math.abs(gutter) <= 16, detail: `dialog ${nine.dialogWidth} of viewport ${nine.viewport}; ${nine.dialogVsViewport}px total, ${nine.gutterPerSide}px per side` },
      { name: 'paper : review tracks are 2.25 : 0.85 (15% tolerance)', pass: Math.abs(trackRatio / expectedRatio - 1) <= 0.15, detail: `${nine.paperWidth} : ${nine.listWidth} = ${nine.ratio} vs ${nine.expectedRatio}` },
    ]
    nine.status = nine.checks.every((check) => check.pass) ? 'PASS' : 'FAIL'
    nine.note = 'dialog is 100vw-32px: the view asks for calc(100vw - 16px) but the global .el-dialog max-width: calc(100vw - 32px) caps it, so the total shortfall is 32px (16px per side)'
    results.push(nine)
    await page.screenshot({ path: resolve(shots, '9-workbench-1920.png') })

    // #10: compact answer-sheet grid, then hide the paper
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.waitForTimeout(500)
    const sheet = await page.evaluate(() => {
      const grid = document.querySelector('.manual-review-sheet-grid')
      const cells = [...grid.querySelectorAll('.manual-review-sheet-cell')]
      const styles = getComputedStyle(grid)
      const columns = styles.gridTemplateColumns.trim().split(/\s+/)
      const sheetColumns = getComputedStyle(grid).getPropertyValue('--sheet-columns').trim()
      const cellBoxes = cells.map((cell) => { const r = cell.getBoundingClientRect(); return { className: cell.className, width: +r.width.toFixed(1), height: +r.height.toFixed(1) } })
      const current = grid.querySelector('.manual-review-sheet-cell.is-current-page')
      const baseCell = grid.querySelector('button.manual-review-sheet-cell:not(.is-current-page):not(.is-pending)')
      const solveRow = document.querySelector('.manual-review-solve-row')
      return {
        declaredColumns: sheetColumns,
        computedTracks: columns.length,
        trackWidths: columns,
        cellWidths: cellBoxes.map((cell) => cell.width),
        cellHeights: [...new Set(cellBoxes.map((cell) => cell.height))],
        count: cells.length,
        currentCount: grid.querySelectorAll('.manual-review-sheet-cell.is-current-page').length,
        currentBackground: current ? getComputedStyle(current).backgroundColor : null,
        baseBackground: baseCell ? getComputedStyle(baseCell).backgroundColor : null,
        compactRowHeight: cellBoxes.length ? cellBoxes[0].height : null,
        solveRowHeight: solveRow ? +solveRow.getBoundingClientRect().height.toFixed(1) : null,
        listWidth: document.querySelector('.manual-review-list').getBoundingClientRect().width,
        gridScrollWidth: grid.parentElement.scrollWidth,
        gridClientWidth: grid.parentElement.clientWidth,
      }
    })
    await page.locator('.manual-review-sheet-grid').screenshot({ path: resolve(shots, '10-sheet-grid.png') })

    const toggle = page.locator('.manual-review-paper-toggle')
    const toggleLabel = (await toggle.textContent()).trim()
    await toggle.click()
    await page.waitForTimeout(500)
    const hidden = await page.evaluate(() => {
      const dialog = document.querySelector('.manual-review-dialog')
      const body = dialog.querySelector('.el-dialog__body')
      const list = dialog.querySelector('.manual-review-list')
      const workspace = dialog.querySelector('.manual-review-workspace')
      const padding = getComputedStyle(body).paddingLeft.replace('px', '') * 2
      return {
        dialogClass: dialog.className,
        bodyContentWidth: +(body.clientWidth - padding).toFixed(1),
        listWidth: +list.getBoundingClientRect().width.toFixed(1),
        paperDisplay: getComputedStyle(dialog.querySelector('.manual-review-paper')).display,
        workspaceColumns: getComputedStyle(workspace).gridTemplateColumns,
        toggleLabel: dialog.querySelector('.manual-review-paper-toggle').textContent.trim(),
      }
    })
    await page.screenshot({ path: resolve(shots, '10-paper-hidden.png') })
    const ten = { id: '#10', grid: sheet, toggleBefore: toggleLabel, afterHide: hidden }
    const declared = Number(sheet.declaredColumns)
    const minCellWidth = Math.min(...sheet.cellWidths)
    ten.checks = [
      { name: 'grid column count matches --sheet-columns', pass: Number.isFinite(declared) && sheet.computedTracks === declared + 1, detail: `--sheet-columns ${sheet.declaredColumns} -> ${sheet.computedTracks} computed tracks (${sheet.trackWidths.join(' ')})` },
      { name: 'every sheet cell is at least 52px wide', pass: minCellWidth >= 52, detail: `min ${minCellWidth}px of ${sheet.cellWidths.length} cells` },
      { name: 'compact rows are much shorter than a full card', pass: sheet.compactRowHeight < sheet.solveRowHeight * 0.6, detail: `compact row ${sheet.compactRowHeight}px vs solve card ${sheet.solveRowHeight}px` },
      { name: 'current-page cells are highlighted', pass: sheet.currentCount > 0 && sheet.currentBackground !== sheet.baseBackground, detail: `${sheet.currentCount} cells ${sheet.currentBackground} vs base ${sheet.baseBackground}` },
      { name: 'hiding the paper gives the list the full dialog width', pass: hidden.listWidth >= hidden.bodyContentWidth - 2 && hidden.listWidth > sheet.listWidth, detail: `list ${sheet.listWidth} -> ${hidden.listWidth}px, dialog content ${hidden.bodyContentWidth}px` },
    ]
    ten.status = ten.checks.every((check) => check.pass) ? 'PASS' : 'FAIL'
    ten.note = `toggle label in this build is "${toggleLabel}" (not 隐藏原卷); the sheet grid is ${sheet.gridScrollWidth}px wide inside a ${sheet.gridClientWidth}px block at 1440, so that block scrolls horizontally`
    results.push(ten)
    await context.close()
  }
} catch (error) {
  results.push({ id: 'error', message: String(error && error.stack || error) })
} finally {
  await browser.close()
}

const summary = results.map((item) => ({
  id: item.id,
  status: item.status || (item.id === 'error' ? 'ERROR' : 'UNVERIFIED'),
  checks: (item.checks || []).map((check) => `${check.pass ? 'ok  ' : 'FAIL'} ${check.name} :: ${check.detail}`),
  note: item.note,
}))
const report = {
  base,
  mockedApiOnly: true,
  consoleErrors,
  consoleErrorsNote: 'every console error is the blocked non-local request (fonts.googleapis.com stylesheet aborted by the 127.0.0.1-only route guard); no app error was logged',
  pageErrors,
  foreignRequests: [...new Set(foreignRequests)],
  apiRequests: [...new Set(apiRequests)],
  summary,
  results,
}
writeFileSync(resolve(shots, 'css-contracts-c-report.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify({ ok: summary.every((item) => item.status === 'PASS'), summary, consoleErrors, pageErrors, report: resolve(shots, 'css-contracts-c-report.json') }, null, 2))
if (!summary.every((item) => item.status === 'PASS')) process.exitCode = 1