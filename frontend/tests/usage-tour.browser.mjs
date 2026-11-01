// Spotlight usage tour: real Vue pages in Edge, synthetic data, every API intercepted.
// Walks the tour at desktop and phone widths and checks that each step really
// highlights its control, that the bubble stays on screen without covering it,
// and that the tour never navigates away from a page with work in flight.
//   (frontend/) node tests/usage-tour.browser.mjs [artifacts-dir]
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

let playwright
try {
  playwright = await import('playwright')
} catch {
  const entry = process.env.PLAYWRIGHT_ENTRY
  if (!entry) throw new Error('playwright is not installed; set PLAYWRIGHT_ENTRY to its index.mjs')
  playwright = await import(pathToFileURL(entry))
}
const output = resolve(process.argv[2] || '../outputs/usage-tour-browser')
mkdirSync(output, { recursive: true })
// No project dotenv or backend proxy is loaded by this isolated test server.
const server = await createServer({
  configFile: false, envFile: false, plugins: [vue()],
  resolve: { alias: { '@': resolve('src') } },
  server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await playwright.chromium.launch({ headless: true, channel: 'msedge' })
const errors = []
const results = []
const USER_ID = 910001
const GUIDE_KEY = `mathagent_usage_guide_v3:${USER_ID}`
const withStudents = [{ id: 1, class_name: '高一(3)班', student_count: 42 }]

async function open({ classes = withStudents, guideSeen = false, viewport = { width: 1366, height: 900 }, desktop = false } = {}) {
  const context = await browser.newContext({ viewport })
  await context.addInitScript(({ guideKey, guideSeen, desktop, userId }) => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('disclaimer_agreed', 'true')
    if (guideSeen) localStorage.setItem(guideKey, 'true')
    if (desktop) {
      // A desktop client whose processor check was already resolved for this binding.
      localStorage.setItem(`mathagent_desktop_setup_v1:${userId}`, 'synthetic-node')
      window.__nativeCalls = []
      window.__TAURI_INTERNALS__ = { invoke: async (command) => {
        window.__nativeCalls.push(command)
        if (command === 'runtime_info') return { runtime: 'desktop', webFallbackConfigured: true }
        if (command === 'desktop_binding_status') return { configured: true, nodeId: 'synthetic-node' }
        if (command === 'get_worker_status') {
          return {
            paused: false, localProcessingAvailable: true, computeBackend: 'windows_cpu_opencv', webFallbackRequired: false,
            handoff: { configured: true, running: true, connected: true, lastErrorCode: null },
            cpuAcceleration: { workerCount: 2, reasonCodes: [] },
          }
        }
        throw new Error(`Unexpected native command: ${command}`)
      } }
    }
  }, { guideKey: GUIDE_KEY, guideSeen, desktop, userId: USER_ID })
  const writes = []
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
      writes.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
    const currentClassId = classes[0]?.id ?? null
    let data = {}
    if (url.pathname === '/api/auth/me') {
      data = { user: { id: USER_ID, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: currentClassId } }
    } else if (url.pathname === '/api/auth/classes') data = { classes, current_class_id: currentClassId }
    else if (/^\/api\/class\/\d+\/students$/.test(url.pathname)) data = { students: [], class_name: classes[0]?.class_name }
    else if (url.pathname.endsWith('/availability')) {
      data = { status: 'available', service_status: 'available', registration_hardware: { status: 'ready', ready: true }, hardware: { status: 'ready' } }
    } else if (url.pathname.includes('/pending')) data = []
    else if (url.pathname === '/api/file/recent') data = { items: [] }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto(`${base}/`)
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' })
  return { context, page, writes }
}

const tourCard = (page) => page.locator('.guided-tour__card')

// Settled = placed, not resolving, showing `title`, and (when `key` is given)
// the spotlight has finished moving onto that control.
async function settled(page, title, key) {
  await page.waitForFunction(({ title, key }) => {
    const card = document.querySelector('.guided-tour__card.is-placed:not(.is-resolving)')
    if (!card || card.querySelector('.guided-tour__title')?.textContent.trim() !== title) return false
    if (!key) return true
    const spot = document.querySelector('.guided-tour__spot')?.getBoundingClientRect()
    const target = [...document.querySelectorAll(`[data-tour="${key}"]`)]
      .map((el) => el.getBoundingClientRect()).find((rect) => rect.width > 0)
    if (!spot || !target) return false
    const covers = spot.left <= target.left + 1 && spot.top <= target.top + 1
      && spot.right >= Math.min(target.right, innerWidth) - 1 && spot.bottom >= Math.min(target.bottom, innerHeight) - 1
    const prev = window.__tourLastSpot
    window.__tourLastSpot = `${spot.left},${spot.top},${spot.width},${spot.height}`
    return covers && prev === window.__tourLastSpot
  }, { title, key }, { timeout: 20000, polling: 120 })
  return page.evaluate(() => {
    const card = document.querySelector('.guided-tour__card')
    const cardRect = card.getBoundingClientRect()
    const spot = document.querySelector('.guided-tour__spot')?.getBoundingClientRect() || null
    const placement = [...card.classList].find((name) => /^is-(top|bottom|left|right|center|floating)$/.test(name))
    return {
      count: card.querySelector('.guided-tour__count').textContent.trim(),
      notice: card.querySelector('.guided-tour__notice')?.textContent.trim() || '',
      path: location.pathname,
      placement,
      focused: card.contains(document.activeElement) ? document.activeElement.textContent.trim() : null,
      card: { left: cardRect.left, top: cardRect.top, right: cardRect.right, bottom: cardRect.bottom },
      spot: spot && { left: spot.left, top: spot.top, right: spot.right, bottom: spot.bottom },
      viewport: { width: innerWidth, height: innerHeight },
    }
  })
}

function assertLayout(info, label) {
  const { card, spot, viewport } = info
  assert.ok(card.left >= 0 && card.top >= 0 && card.right <= viewport.width && card.bottom <= viewport.height,
    `${label}: bubble must stay on screen ${JSON.stringify(card)}`)
  if (spot && info.placement !== 'is-floating') {
    const overlaps = card.left < spot.right && card.right > spot.left && card.top < spot.bottom && card.bottom > spot.top
    assert.equal(overlaps, false, `${label}: bubble must not cover the highlighted control`)
  }
}

async function clickPrimary(page) {
  await tourCard(page).locator('.el-button--primary').click()
}

const fullTour = [
  { key: 'class-switcher', title: '先确认当前班级', path: '/' },
  { key: 'student-add', title: '名单有变化，点这里补充', path: /^\/(students|class\/)/ },
  { key: 'grade-category', title: '批改前，先选批次类型', path: '/upload' },
  { key: 'template-import', title: '点这里导入答案模板', path: '/upload' },
  { key: 'paper-upload', title: '把学生试卷放进来', path: '/upload' },
  { key: 'service-state', title: '看这个标识，再决定开始', path: '/upload' },
  { key: 'start-grading', title: '材料齐了，点「开始批阅」', path: '/upload' },
  { key: 'grade-workflow', title: '批改完，就在这一页复核归档', path: '/upload' },
  { key: 'guide-button', title: '忘了在哪点，就点这里', path: '/upload' },
]

async function walk(page, prefix) {
  const steps = []
  for (const [index, step] of fullTour.entries()) {
    const info = await settled(page, step.title, step.key)
    assert.equal(info.count, `第 ${index + 1} / ${fullTour.length} 步`)
    if (step.path instanceof RegExp) assert.match(info.path, step.path, step.key)
    else assert.equal(info.path, step.path, step.key)
    assert.equal(info.notice, '', `${step.key}: no fallback notice on a fresh page`)
    assert.equal(info.focused, index === fullTour.length - 1 ? '完成' : '下一步', `${step.key}: primary button focused`)
    assertLayout(info, `${prefix}/${step.key}`)
    await page.screenshot({ path: resolve(output, `${prefix}-${String(index + 1).padStart(2, '0')}-${step.key}.png`) })
    steps.push({ key: step.key, path: info.path, placement: info.placement })
    await clickPrimary(page)
  }
  await page.locator('.guided-tour').waitFor({ state: 'detached' })
  assert.equal(await page.evaluate((key) => localStorage.getItem(key), GUIDE_KEY), 'true')
  return steps
}

try {
  {
    // First login on desktop width: the tour starts by itself and walks the real path.
    const { context, page, writes } = await open()
    await settled(page, '先确认当前班级', 'class-switcher')
    // The dimmed page swallows clicks: the class dropdown under the spotlight must not open.
    const chip = await page.locator('[data-tour="class-switcher"]').boundingBox()
    await page.mouse.click(chip.x + chip.width / 2, chip.y + chip.height / 2)
    await page.waitForTimeout(300)
    assert.equal(await page.locator('.class-menu').isVisible(), false, 'clicks must not reach the page during the tour')
    const steps = await walk(page, 'desktop')
    assert.deepEqual(writes, [], 'walking the tour must not write anything')
    results.push({ case: 'desktop-first-login-full-walk', steps })
    await context.close()
  }
  {
    // A teacher without a class is pointed at 「添加班级」; Esc skips and remembers.
    const { context, page } = await open({ classes: [] })
    await settled(page, '这里显示当前班级', 'class-switcher')
    await page.keyboard.press('ArrowRight')
    const info = await settled(page, '点「添加班级」创建班级', 'class-create')
    assert.equal(info.path, '/students')
    assertLayout(info, 'no-class/class-create')
    await page.screenshot({ path: resolve(output, 'no-class-02-class-create.png') })
    await page.keyboard.press('ArrowLeft')
    await settled(page, '这里显示当前班级', 'class-switcher')
    await page.keyboard.press('Escape')
    await page.locator('.guided-tour').waitFor({ state: 'detached' })
    await page.getByText('已跳过引导，可随时点右上角「使用引导」重看。').waitFor()
    assert.equal(await page.evaluate((key) => localStorage.getItem(key), GUIDE_KEY), 'true')
    results.push({ case: 'no-class-points-at-create-then-escape' })
    await context.close()
  }
  {
    // Seen before: no auto start. Replay from the header while upload work is in flight:
    // the tour must stay on the page instead of triggering the leave-confirm.
    const { context, page } = await open({ guideSeen: true })
    await page.goto(`${base}/upload`)
    await page.locator('[data-tour="paper-upload"]').waitFor()
    await page.waitForTimeout(1200)
    assert.equal(await page.locator('.guided-tour').count(), 0, 'a seen tour must not start by itself')
    let dialogs = 0
    page.on('dialog', async (dialog) => { dialogs += 1; await dialog.dismiss() })
    await page.evaluate(async () => (await import('/src/utils/routeBusy.js')).setRouteBusy('upload', true))
    await page.getByRole('button', { name: '打开使用引导' }).click()
    await settled(page, '先确认当前班级', 'class-switcher')
    await clickPrimary(page)
    const blocked = await settled(page, '名单有变化，点这里补充')
    assert.equal(blocked.path, '/upload')
    assert.equal(blocked.spot, null)
    assert.equal(blocked.placement, 'is-center')
    assert.match(blocked.notice, /引导不会切换页面，以免中断任务/)
    await page.screenshot({ path: resolve(output, 'busy-02-stays-on-page.png') })
    await clickPrimary(page)
    const sameTab = await settled(page, '批改前，先选批次类型', 'grade-category')
    assert.equal(sameTab.notice, '')
    await page.evaluate(async () => (await import('/src/utils/routeBusy.js')).setRouteBusy('upload', false))
    await tourCard(page).getByRole('button', { name: '上一步' }).click()
    const moved = await settled(page, '名单有变化，点这里补充', 'student-add')
    assert.match(moved.path, /^\/(students|class\/)/)
    assert.equal(dialogs, 0, 'the tour must never raise the leave-confirm')
    await page.keyboard.press('Escape')
    await page.locator('.guided-tour').waitFor({ state: 'detached' })
    results.push({ case: 'replay-from-header-respects-busy-page' })
    await context.close()
  }
  {
    // Phone width: every step still lands on a visible control with the bubble on screen.
    const { context, page } = await open({ viewport: { width: 390, height: 844 } })
    const steps = await walk(page, 'phone')
    results.push({ case: 'phone-full-walk', viewport: { width: 390, height: 844 }, steps })
    await context.close()
  }
  {
    // Desktop client: 「使用引导」 opens the processor check first; continuing starts the tour
    // and leaves the recorded processor resolution untouched.
    const { context, page } = await open({ guideSeen: true, desktop: true })
    await page.waitForTimeout(800)
    assert.equal(await page.locator('.guided-tour').count(), 0)
    await page.getByRole('button', { name: '打开使用引导' }).click()
    const setup = page.getByRole('dialog', { name: /使用引导 · 本机处理检查/ })
    await setup.waitFor()
    await setup.getByText('CPU 已通过自检，本机处理可用。').waitFor()
    await page.screenshot({ path: resolve(output, 'desktop-client-00-processor-check.png') })
    await setup.getByRole('button', { name: '下一步：操作引导' }).click()
    await setup.waitFor({ state: 'hidden' })
    await settled(page, '先确认当前班级', 'class-switcher')
    assert.equal(await page.evaluate((id) => localStorage.getItem(`mathagent_desktop_setup_v1:${id}`), USER_ID), 'synthetic-node')
    const nativeCalls = await page.evaluate(() => window.__nativeCalls)
    assert.equal(nativeCalls.some((command) => /_teacher_desktop$/.test(command)), false, 'no binding changes from the guide')
    await page.keyboard.press('Escape')
    await page.locator('.guided-tour').waitFor({ state: 'detached' })
    results.push({ case: 'desktop-client-check-then-tour', nativeCalls })
    await context.close()
  }
  assert.deepEqual(errors, [], 'Actual Vue runtime must not emit errors')
  writeFileSync(resolve(output, 'audit.json'), JSON.stringify({ passed: true, mockedApiOnly: true, results, errors }, null, 2))
  console.log(JSON.stringify({ passed: true, cases: results.length, output }))
} catch (error) {
  writeFileSync(resolve(output, 'audit-failed.json'), JSON.stringify({ passed: false, error: String(error.stack), results, errors }, null, 2))
  throw error
} finally {
  await browser.close()
  await server.close()
}
