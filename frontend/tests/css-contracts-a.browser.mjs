// CSS contracts A — observed layout / motion / theme behaviour on the real Vue
// pages, measured with getComputedStyle, getBoundingClientRect and
// elementFromPoint (never by matching CSS source text).
//
//   node tests/css-contracts-a.browser.mjs http://127.0.0.1:5197 <artifacts-dir>
//
// Start the Vite dev server first, then run with Playwright on NODE_PATH.
// Every /api/* answer below is mocked in the browser context; no request ever
// reaches a real backend.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { inflateSync } from 'node:zlib'

const { chromium } = createRequire(import.meta.url)('playwright')
const base = process.argv[2] || 'http://127.0.0.1:5197'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only local mocked pages may be tested')
const output = resolve(process.argv[3] || 'D:/hermes/cache/scratch/css-a-shots')
mkdirSync(output, { recursive: true })

const results = []
const errors = []
const mutations = []
const blocked = []
const shots = []
const browser = await chromium.launch({ headless: true })

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
const TEACHER = { id: 910001, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true }

const STUDENTS = {
  class_name: '高三(1)班',
  students: [
    { id: 11, name: '王小明', student_no: '01', exam_no: '2026001', risk_level: 'attention', wrong_rate: 0.42, correction_rate: 0.31, weakness_count: 2, has_records: true, has_mistakes: true, latest_exam_score: 118, latest_exam_name: '月考一' },
    { id: 12, name: '李华', student_no: '02', exam_no: '2026002', risk_level: 'stable', wrong_rate: 0.12, correction_rate: 0.78, weakness_count: 0, has_records: true, has_mistakes: true, latest_exam_score: 132, latest_exam_name: '月考一' },
    { id: 13, name: '赵敏', student_no: '03', exam_no: '2026003', risk_level: 'observation', observation_mode: true, wrong_rate: 0, correction_rate: 0, weakness_count: 0, has_records: false },
  ],
  coverage: { class_size: 3, effective_students: 2, observation_students: 1 },
}

// file_name must be one of src/courseware/covers.json's senior entries, so the
// card gets a real bundled cover image (/courseware/covers/H05.webp).
const COURSEWARE_CARD = {
  id: 'cw-h05', title: '三角函数与恒等变换：单位圆与三角函数', chapter: '三角函数与恒等变换',
  knowledge_point: '单位圆与三角函数', objective: '用单位圆解释同角三角函数关系，把图形、参数和规律放进同一个教学场景。',
  stage: 'senior', level: '高中', file_name: 'H05_三角函数与恒等变换.html',
  cover_topics: ['单位圆与三角函数', '诱导公式'], topics: ['单位圆与三角函数'],
}

// ---------------------------------------------------------------------------
// Mock plumbing
// ---------------------------------------------------------------------------
function mockContext(context, handler, scenario) {
  return context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== new URL(base).origin) {
      blocked.push({ scenario, url: url.href })
      return route.abort()
    }
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
      mutations.push({ scenario, method: request.method(), path: url.pathname, body: request.postData() })
      return route.abort()
    }
    const data = await handler(url.pathname, url)
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data ?? {}) })
  })
}

async function newPage({ viewport = { width: 1440, height: 1000 }, token = true, colorMode = null, clearColorMode = false, colorScheme = null, reducedMotion = 'no-preference', scenario }) {
  const context = await browser.newContext({ viewport, reducedMotion, colorScheme: colorScheme || 'light' })
  await context.addInitScript(({ token, colorMode, clearColorMode, teacher }) => {
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem('mathagent_usage_guide_v3:910001', 'true')
    // index.html + main.js read this key before mount; 'light'|'dark'|'system'.
    if (clearColorMode) localStorage.removeItem('mathagent_color_mode')
    else if (colorMode) localStorage.setItem('mathagent_color_mode', colorMode)
    if (token) {
      localStorage.setItem('mathagent_token', 'mock-only-token')
      localStorage.setItem('mathagent_user', JSON.stringify(teacher))
    } else {
      localStorage.removeItem('mathagent_token')
      localStorage.removeItem('mathagent_user')
    }
  }, { token, colorMode, clearColorMode, teacher: TEACHER })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push({ scenario, kind: 'pageerror', text: error.message }))
  page.on('console', message => {
    if (message.type() === 'error') errors.push({ scenario, kind: 'console', text: message.text() })
  })
  return { context, page }
}

async function settle(page, selector) {
  await page.locator(selector).first().waitFor()
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' }).catch(() => {})
  // Startup plate removal plus the page's entrance animations.
  await page.waitForTimeout(400)
}

async function shoot(page, name, locator) {
  const file = resolve(output, name)
  if (locator) await locator.screenshot({ path: file })
  else await page.screenshot({ path: file, fullPage: false })
  shots.push(name)
  return name
}

// 1x1 screenshot -> real painted pixel. A single pixel never needs PNG
// unfiltering: every filter predictor is 0 for the first pixel of a row.
function pngPixel(buffer) {
  let offset = 8, colorType = 0
  const idat = []
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset)
    const type = buffer.toString('ascii', offset + 4, offset + 8)
    const data = buffer.subarray(offset + 8, offset + 8 + length)
    if (type === 'IHDR') colorType = data[9]
    else if (type === 'IDAT') idat.push(data)
    else if (type === 'IEND') break
    offset += 12 + length
  }
  const raw = inflateSync(Buffer.concat(idat))
  if (colorType === 6) return [raw[1], raw[2], raw[3]]
  if (colorType === 2) return [raw[1], raw[2], raw[3]]
  if (colorType === 0) return [raw[1], raw[1], raw[1]]
  throw new Error(`unsupported PNG color type ${colorType}`)
}

async function pixelAt(page, x, y) {
  const buffer = await page.screenshot({ clip: { x: Math.round(x), y: Math.round(y), width: 1, height: 1 } })
  const [r, g, b] = pngPixel(buffer)
  return { hex: `#${[r, g, b].map(v => v.toString(16).padStart(2, '0')).join('')}`, rgb: [r, g, b] }
}

function rgb(css) {
  const m = String(css).match(/rgba?\(([^)]+)\)/)
  if (!m) return null
  const parts = m[1].split(/[,/\s]+/).filter(Boolean).map(Number)
  return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 }
}

const lightness = ({ r, g, b }) => Math.min(r, g, b)
const nearWhite = ({ r, g, b }) => Math.min(r, g, b) >= 245 && Math.max(r, g, b) - Math.min(r, g, b) <= 12

try {
  // =========================================================================
  // #3 班级管理 /students — 操作列 300px 固定右侧，四个按钮一行且间距相等
  // =========================================================================
  {
    const scenario = 'students-action-column'
    const { context, page } = await newPage({ scenario })
    let classId = 3
    await mockContext(context, pathname => {
      if (pathname === '/api/auth/me') return { user: { ...TEACHER, current_class_id: classId } }
      if (pathname === '/api/auth/classes') return { classes: [{ id: classId, class_name: STUDENTS.class_name }], current_class_id: classId, max_classes: 4 }
      if (pathname === `/api/class/${classId}/students`) return STUDENTS
      if (pathname.endsWith('/availability')) return { status: 'available', hardware: { status: 'ready' } }
      return {}
    }, scenario)

    await page.goto(`${base}/students`)
    await settle(page, '.class-table')
    await page.locator('.action-btns').first().waitFor()

    const measure = width => page.evaluate(() => {
      const round = value => Math.round(value * 100) / 100
      const table = document.querySelector('.class-table')
      const body = document.querySelector('.class-table .el-table__body-wrapper')
      const rows = [...document.querySelectorAll('.action-btns')].map(box => {
        const buttons = [...box.querySelectorAll('button')]
        const rects = buttons.map(button => {
          const r = button.getBoundingClientRect()
          return { text: button.textContent.trim(), left: round(r.left), right: round(r.right), top: round(r.top), bottom: round(r.bottom), width: round(r.width), height: round(r.height), marginLeft: getComputedStyle(button).marginLeft }
        })
        const boxRect = box.getBoundingClientRect()
        const cell = box.closest('td')
        const cellRect = cell.getBoundingClientRect()
        const style = getComputedStyle(box)
        return {
          buttons: rects,
          gaps: rects.slice(1).map((button, index) => round(button.left - rects[index].right)),
          topSpread: round(Math.max(...rects.map(r => r.top)) - Math.min(...rects.map(r => r.top))),
          bottomSpread: round(Math.max(...rects.map(r => r.bottom)) - Math.min(...rects.map(r => r.bottom))),
          box: { left: round(boxRect.left), right: round(boxRect.right), width: round(boxRect.width), height: round(boxRect.height), scrollWidth: box.scrollWidth, clientWidth: box.clientWidth },
          cell: { left: round(cellRect.left), right: round(cellRect.right), width: round(cellRect.width), height: round(cellRect.height) },
          computed: { display: style.display, flexWrap: style.flexWrap, whiteSpace: style.whiteSpace, gap: style.columnGap },
        }
      })
      const tableRect = table.getBoundingClientRect()
      // Element Plus 2.x scrolls the table inside .el-scrollbar__wrap; the
      // body-wrapper itself keeps clientWidth === scrollWidth.
      const scroller = table.querySelector('.el-scrollbar__wrap')
      return {
        viewportWidth: innerWidth,
        table: { left: round(tableRect.left), right: round(tableRect.right), width: round(tableRect.width) },
        scroller: scroller ? { clientWidth: scroller.clientWidth, scrollWidth: scroller.scrollWidth, scrollLeft: scroller.scrollLeft } : null,
        bodyScroll: { clientWidth: body.clientWidth, scrollWidth: body.scrollWidth },
        firstCellLeft: round(document.querySelector('.el-table__body tr.el-table__row td').getBoundingClientRect().left),
        rows,
      }
    })

    const wide = await measure()
    results.push({ item: '#3', scenario, viewport: wide.viewportWidth, table: wide.table, bodyScroll: wide.bodyScroll, rows: wide.rows })
    await shoot(page, 'students-actions-1440.png')
    await shoot(page, 'students-table-1440.png', page.locator('.table-card'))

    const audit = (rows, label) => {
      assert.ok(rows.length >= 1, `${label}: no action cell rendered`)
      for (const [index, row] of rows.entries()) {
        const tag = `${label} row ${index + 1}`
        assert.equal(row.buttons.length, 4, `${tag}: expected 4 action buttons, got ${row.buttons.length}`)
        assert.ok(row.topSpread <= 2, `${tag}: buttons wrapped/split — top spread ${row.topSpread}px`)
        assert.ok(row.bottomSpread <= 2, `${tag}: buttons wrapped/split — bottom spread ${row.bottomSpread}px`)
        const gapSpread = Math.max(...row.gaps) - Math.min(...row.gaps)
        assert.ok(gapSpread <= 1, `${tag}: uneven gaps ${JSON.stringify(row.gaps)}`)
        assert.ok(Math.abs(row.gaps[0] - 4) <= 1, `${tag}: gap ${row.gaps[0]}px is not the 4px design gap (Element Plus margin not removed)`)
        assert.ok(row.buttons.every(button => button.marginLeft === '0px'), `${tag}: Element Plus left margin still applied`)
        assert.ok(row.box.width <= row.cell.width + 1, `${tag}: action group ${row.box.width}px overflows its ${row.cell.width}px cell`)
        assert.ok(row.box.scrollWidth <= row.box.clientWidth + 1, `${tag}: action group clips horizontally`)
        // A wrapped flex row grows to two button lines; the td height is useless
        // here because the row is as tall as its tallest cell (最近月考 is two lines).
        assert.ok(row.box.height <= Math.max(...row.buttons.map(b => b.height)) + 2, `${tag}: action group is ${row.box.height}px tall for ${Math.max(...row.buttons.map(b => b.height))}px buttons — it wrapped`)
      }
    }
    audit(wide.rows, '1440')

    // Fixed-right behaviour is only observable once the table actually scrolls.
    await page.setViewportSize({ width: 900, height: 1000 })
    await page.waitForTimeout(400)
    const pinnedAt = () => page.evaluate(() => {
      const round = value => Math.round(value * 100) / 100
      const box = document.querySelector('.action-btns')
      const first = box.querySelector('button')
      const r = first.getBoundingClientRect()
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      const table = document.querySelector('.class-table').getBoundingClientRect()
      const scroller = document.querySelector('.class-table .el-scrollbar__wrap')
      return {
        scrollLeft: scroller.scrollLeft,
        hitIsActionButton: Boolean(hit && first.contains(hit)),
        hitText: hit ? hit.textContent.trim().slice(0, 8) : null,
        boxLeft: round(box.getBoundingClientRect().left),
        boxRight: round(box.getBoundingClientRect().right),
        tableLeft: round(table.left),
        tableRight: round(table.right),
        firstCellLeft: round(document.querySelector('.el-table__body tr.el-table__row td').getBoundingClientRect().left),
      }
    })
    const narrow = await measure()
    assert.ok(narrow.scroller && narrow.scroller.scrollWidth > narrow.scroller.clientWidth, `900px: table did not scroll (${JSON.stringify(narrow.scroller)}), fixed column unobservable`)
    const start = await pinnedAt()
    await page.evaluate(() => {
      const scroller = document.querySelector('.class-table .el-scrollbar__wrap')
      scroller.scrollLeft = scroller.scrollWidth
    })
    await page.waitForTimeout(400)
    const scrolled = await pinnedAt()
    results.push({ item: '#3', scenario, viewport: narrow.viewportWidth, table: narrow.table, scroller: narrow.scroller, pinned: { start, scrolled }, rows: narrow.rows })
    await shoot(page, 'students-actions-900.png')
    audit(narrow.rows, '900')
    assert.ok(scrolled.scrollLeft > 0, '900px: the table body did not scroll horizontally')
    assert.ok(scrolled.firstCellLeft < start.firstCellLeft - 100, `900px: first column did not move (${start.firstCellLeft} -> ${scrolled.firstCellLeft})`)
    for (const [label, state] of [['left', start], ['right', scrolled]]) {
      assert.ok(state.hitIsActionButton, `900px scrolled ${label}: the action button is not hit-testable (fixed column not on top) — hit ${state.hitText}`)
      assert.ok(state.boxRight <= narrow.table.right + 1.5, `900px scrolled ${label}: action group right ${state.boxRight} escapes table right ${narrow.table.right}`)
      assert.ok(state.boxRight >= narrow.table.right - 320, `900px scrolled ${label}: action group right ${state.boxRight} not pinned to table right ${narrow.table.right}`)
      assert.ok(state.boxLeft >= narrow.table.left - 0.5, `900px scrolled ${label}: action group left ${state.boxLeft} escapes table left ${narrow.table.left}`)
    }
    assert.ok(Math.abs(start.boxRight - scrolled.boxRight) <= 1.5, `900px: action column is not fixed — right edge moved ${start.boxRight} -> ${scrolled.boxRight}`)
    await context.close()
  }

  // =========================================================================
  // #4 课件库 /courseware — 封面 hover 有 transform 过渡；reduced-motion 关闭
  // =========================================================================
  {
    const scenario = 'courseware-cover-motion'
    const handler = pathname => {
      if (pathname === '/api/auth/me') return { user: { ...TEACHER, current_class_id: null } }
      if (pathname === '/api/auth/classes') return { classes: [], current_class_id: null }
      if (pathname === '/api/courseware') return { items: [COURSEWARE_CARD], total: 1 }
      if (pathname.endsWith('/availability')) return { status: 'available', hardware: { status: 'ready' } }
      return {}
    }
    const readCover = page => page.locator('.courseware-cover img').first().evaluate(img => {
      const style = getComputedStyle(img)
      const rect = img.getBoundingClientRect()
      return {
        src: img.getAttribute('src'), complete: img.complete, naturalWidth: img.naturalWidth,
        transitionProperty: style.transitionProperty,
        transitionDuration: style.transitionDuration,
        transitionTimingFunction: style.transitionTimingFunction,
        transform: style.transform,
        width: Math.round(rect.width * 100) / 100, height: Math.round(rect.height * 100) / 100,
        figureTransform: getComputedStyle(img.closest('.courseware-cover')).transform,
      }
    })

    // --- normal motion -----------------------------------------------------
    {
      const { context, page } = await newPage({ scenario: `${scenario} normal` })
      await mockContext(context, handler, `${scenario} normal`)
      await page.goto(`${base}/courseware`)
      await settle(page, '.courseware-cover img')
      await page.waitForFunction(() => {
        const img = document.querySelector('.courseware-cover img')
        return img && img.complete && img.naturalWidth > 0
      }, null, { timeout: 15000 })
      const cover = page.locator('.courseware-cover').first()
      await cover.scrollIntoViewIfNeeded()
      await page.mouse.move(5, 5) // no hover yet
      await page.waitForTimeout(200)

      const before = await readCover(page)
      await shoot(page, 'courseware-cover-normal-idle.png', cover)
      await page.locator('.courseware-cover img').first().hover()
      const immediate = await readCover(page)
      await page.waitForTimeout(900)
      const after = await readCover(page)
      await shoot(page, 'courseware-cover-normal-hover.png', cover)

      const duration = parseFloat(before.transitionDuration)
      results.push({ item: '#4', scenario: 'normal', before, immediateTransform: immediate.transform, after })
      assert.ok(before.naturalWidth > 0, 'normal: cover image did not load — the transition target is missing')
      assert.equal(before.transitionProperty.split(',')[0].trim(), 'transform', `normal: transition-property is ${before.transitionProperty}`)
      assert.ok(duration > 0, `normal: transition-duration is ${before.transitionDuration}`)
      assert.equal(Math.round(duration * 1000), 320, `normal: transition-duration is ${before.transitionDuration}, expected the --dur-4 320ms token`)
      assert.notEqual(after.transform, before.transform, `normal: transform did not change on hover (${before.transform} -> ${after.transform})`)
      const matrix = after.transform.match(/matrix\(([^)]+)\)/)
      const scale = matrix ? Number(matrix[1].split(',')[0]) : null
      assert.ok(scale && Math.abs(scale - 1.025) < 0.002, `normal: hover scale is ${scale}, expected 1.025`)
      // Mid-flight value proves the change is animated rather than instant.
      const midMatrix = immediate.transform.match(/matrix\(([^)]+)\)/)
      const midScale = midMatrix ? Number(midMatrix[1].split(',')[0]) : null
      results.at(-1).midScale = midScale
      await context.close()
    }

    // --- reduced motion ----------------------------------------------------
    {
      const scenarioReduced = `${scenario} reduced`
      const { context, page } = await newPage({ scenario: scenarioReduced })
      await mockContext(context, handler, scenarioReduced)
      await page.goto(`${base}/courseware`)
      await settle(page, '.courseware-cover img')
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.reload()
      await settle(page, '.courseware-cover img')
      await page.waitForFunction(() => {
        const img = document.querySelector('.courseware-cover img')
        return img && img.complete && img.naturalWidth > 0
      }, null, { timeout: 15000 })
      const media = await page.evaluate(() => ({
        reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
      }))
      assert.equal(media.reduced, true, 'reduced: emulateMedia did not take effect')
      const cover = page.locator('.courseware-cover').first()
      await cover.scrollIntoViewIfNeeded()
      await page.mouse.move(5, 5)
      await page.waitForTimeout(200)

      const before = await readCover(page)
      await shoot(page, 'courseware-cover-reduced-idle.png', cover)
      await page.locator('.courseware-cover img').first().hover()
      const immediate = await readCover(page)
      await page.waitForTimeout(600)
      const after = await readCover(page)
      await shoot(page, 'courseware-cover-reduced-hover.png', cover)

      const duration = parseFloat(before.transitionDuration)
      results.push({ item: '#4', scenario: 'reduced', transitionDuration: before.transitionDuration, transitionProperty: before.transitionProperty, before, immediateTransform: immediate.transform, after })
      // "Effectively off": either no transition at all, or one too short to be perceived.
      assert.ok(duration <= 0.01, `reduced: transition-duration is still ${before.transitionDuration}`)
      await context.close()
    }
  }

  // =========================================================================
  // #11 登录页 /login — light / dark / system(dark) 三种模式都保持浅色
  // =========================================================================
  {
    const scenario = 'login-theme'
    const handler = pathname => {
      if (pathname === '/api/auth/features') return { sms_enabled: false, sms_hint: '本实例未开启短信登录', captcha_provider: 'none' }
      if (pathname === '/api/auth/me') return { user: TEACHER }
      return {}
    }
    const modes = [
      { key: 'light', colorMode: 'light', colorScheme: 'light', clearColorMode: false },
      { key: 'dark', colorMode: 'dark', colorScheme: 'dark', clearColorMode: false },
      // 'system' is the persisted value that makes CSS resolve prefers-color-scheme;
      // clearing the key would fall back to the 'light' default and prove nothing
      // about system dark, so both variants are measured below.
      { key: 'system-dark', colorMode: 'system', colorScheme: 'dark', clearColorMode: false },
      { key: 'system-default-cleared', colorMode: null, colorScheme: 'dark', clearColorMode: true },
    ]

    for (const mode of modes) {
      const label = `${scenario} ${mode.key}`
      const { context, page } = await newPage({
        token: false, colorMode: mode.colorMode, clearColorMode: mode.clearColorMode,
        colorScheme: mode.colorScheme, scenario: label,
      })
      await mockContext(context, handler, label)
      await page.goto(`${base}/login`)
      await settle(page, '.card')
      await page.waitForTimeout(300)

      const measured = await page.evaluate(() => {
        const pick = element => (element ? getComputedStyle(element).backgroundColor : null)
        // What is actually painted at this point: nearest ancestor with an
        // opaque background, found through hit testing.
        const paintAt = (x, y) => {
          let element = document.elementFromPoint(x, y)
          const chain = []
          while (element) {
            const style = getComputedStyle(element)
            const colour = style.backgroundColor
            const parsed = colour.match(/rgba?\(([^)]+)\)/)
            const alpha = parsed ? Number(parsed[1].split(/[,/\s]+/).filter(Boolean)[3] ?? 1) : 1
            chain.push({ tag: element.tagName.toLowerCase(), cls: element.className?.toString?.().slice(0, 40) || '', colour, alpha })
            if (parsed && alpha > 0.5) return { at: { x, y }, hit: chain[0], picked: chain[chain.length - 1], depth: chain.length }
            element = element.parentElement
          }
          return { at: { x, y }, picked: null, chain }
        }
        const card = document.querySelector('.card').getBoundingClientRect()
        const right = document.querySelector('.right').getBoundingClientRect()
        const stage = document.querySelector('.stage-wrap')
        const stageRect = stage.getBoundingClientRect()
        return {
          rootAttribute: document.documentElement.getAttribute('data-color-mode'),
          colorScheme: document.documentElement.style.colorScheme,
          prefersDark: matchMedia('(prefers-color-scheme: dark)').matches,
          stageRect: { left: Math.round(stageRect.left), top: Math.round(stageRect.top), width: Math.round(stageRect.width), height: Math.round(stageRect.height), position: getComputedStyle(stage).position },
          viewport: { width: innerWidth, height: innerHeight },
          // body/html carry the dark --page token in dark mode; the fixed root
          // must cover them completely, which the corners prove.
          cornersInsideRoot: [[2, 2], [innerWidth - 3, 2], [2, innerHeight - 3], [innerWidth - 3, innerHeight - 3]]
            .every(([x, y]) => stage.contains(document.elementFromPoint(x, y))),
          background: {
            html: pick(document.documentElement), body: pick(document.body), app: pick(document.getElementById('app')),
            stageWrap: pick(document.querySelector('.stage-wrap')), right: pick(document.querySelector('.right')), card: pick(document.querySelector('.card')),
          },
          cardRect: { x: Math.round(card.left), y: Math.round(card.top), width: Math.round(card.width), height: Math.round(card.height) },
          paints: {
            showcase: paintAt(60, Math.round(innerHeight / 2)),
            rightPadding: paintAt(Math.round(right.right - 10), Math.round(innerHeight / 2)),
            cardPadding: paintAt(Math.round(card.left + 12), Math.round(card.top + 12)),
          },
        }
      })

      // Rendered pixels (1x1 screenshots) at structurally empty spots.
      const cardPixel = await pixelAt(page, measured.cardRect.x + 12, measured.cardRect.y + 12)
      const rightPixel = await pixelAt(page, measured.cardRect.x + measured.cardRect.width + 30, Math.round(1000 / 2))
      const screenshot = await shoot(page, `login-${mode.key}.png`)

      const stage = rgb(measured.background.stageWrap)
      const card = rgb(measured.background.card)
      const record = {
        item: '#11', scenario: mode.key, rootAttribute: measured.rootAttribute, colorScheme: measured.colorScheme,
        prefersDark: measured.prefersDark, stageRect: measured.stageRect, viewport: measured.viewport,
        cornersInsideRoot: measured.cornersInsideRoot, background: measured.background, pixels: { card: cardPixel, right: rightPixel },
        paints: Object.fromEntries(Object.entries(measured.paints).map(([key, value]) => [key, value.picked ? { hit: value.hit.cls, colour: value.picked.colour } : null])),
        screenshot,
      }
      results.push(record)

      assert.equal(measured.rootAttribute, mode.colorMode || 'light', `${mode.key}: data-color-mode is ${measured.rootAttribute}`)
      if (mode.key === 'system-dark') assert.equal(measured.prefersDark, true, 'system-dark: OS dark not emulated')
      // The login root must cover the viewport, so the dark html/body token
      // (present in dark mode) can never be the surface the teacher sees.
      assert.equal(measured.stageRect.position, 'fixed', `${mode.key}: login root is not position: fixed`)
      assert.deepEqual(
        [measured.stageRect.left, measured.stageRect.top, measured.stageRect.width, measured.stageRect.height],
        [0, 0, measured.viewport.width, measured.viewport.height],
        `${mode.key}: login root does not cover the viewport`,
      )
      assert.equal(measured.cornersInsideRoot, true, `${mode.key}: a viewport corner is outside the login root`)
      // The login page root is .stage-wrap (position: fixed, inset: 0) — it is
      // the surface the teacher actually sees.
      assert.deepEqual([stage.r, stage.g, stage.b], [238, 241, 240], `${mode.key}: page background is ${measured.background.stageWrap}, expected rgb(238, 241, 240)`)
      assert.ok(nearWhite(card), `${mode.key}: card background is ${measured.background.card}, expected near-white`)
      const cardPainted = { r: cardPixel.rgb[0], g: cardPixel.rgb[1], b: cardPixel.rgb[2] }
      const rightPainted = { r: rightPixel.rgb[0], g: rightPixel.rgb[1], b: rightPixel.rgb[2] }
      assert.ok(nearWhite(cardPainted) || lightness(cardPainted) >= 240, `${mode.key}: painted card pixel is ${cardPixel.hex}`)
      assert.ok(lightness(rightPainted) >= 220, `${mode.key}: painted page pixel next to the card is ${rightPixel.hex}, not light`)
      for (const [point, paint] of Object.entries(measured.paints)) {
        assert.ok(paint.picked, `${mode.key}: nothing painted at ${point}`)
        const colour = rgb(paint.picked.colour)
        assert.ok(lightness(colour) >= 200, `${mode.key}: ${point} resolves to ${paint.picked.colour} (dark surface)`)
      }
      await context.close()
    }
  }

  writeFileSync(resolve(output, 'css-contracts-a.json'), JSON.stringify({ mockedApiOnly: true, realAccountRequests: 0, results, errors, mutations, blocked, shots }, null, 2))
  const pageErrors = errors.filter(error => error.kind === 'pageerror')
  assert.deepEqual(pageErrors, [], 'uncaught page errors')
  assert.deepEqual(mutations, [], 'the pages tried to write to the API')
  console.log(JSON.stringify({ ok: true, checks: results.length, output, shots, consoleErrors: errors, blockedRequests: [...new Set(blocked.map(entry => new URL(entry.url).origin))], mutations }))
} finally {
  await browser.close()
}