// Home-page notice behaviour in a real browser: auto-open on entry, 新 marks,
// the entry-card badge, acknowledgement on close, and the first-visit guards.
//   (frontend/) node tests/notice-popup.browser.mjs [artifacts-dir]
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
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
const output = resolve(process.argv[2] || '../outputs/notice-popup-browser')
mkdirSync(output, { recursive: true })

const server = await createServer({
  configFile: false, envFile: false, plugins: [vue()],
  resolve: { alias: { '@': resolve('src') } },
  server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await playwright.chromium.launch({ headless: true, channel: 'msedge' })
const errors = []
const USER_ID = 910001
const SEEN_KEY = `mathagent_system_notice_seen_v1:${USER_ID}`
const user = { id: USER_ID, name: '模拟教师', role: 'teacher', education_stage: 'senior', profile_complete: true, current_class_id: 1 }
const classes = [{ id: 1, class_name: '高一(3)班', student_count: 42 }]
const OLD_KEYS = [
  '2026-07-30-先确认班级，每一页都填写姓名和学号',
  '2026-07-30-页面完整、文字正立、光线均匀',
  '2026-07-30-100 页以内，系统流程通常 20 分钟内完成',
  '2026-08-21-批改服务连续超过 24 小时不可用',
]

async function open({ disclaimer = true, guideSeen = true, seen = null } = {}) {
  const context = await browser.newContext({ viewport: { width: 1366, height: 900 } })
  await context.addInitScript(({ user, disclaimer, guideSeen, seen, seenKey }) => {
    localStorage.setItem('mathagent_token', 'mock-only-token')
    localStorage.setItem('mathagent_user', JSON.stringify(user))
    if (disclaimer) localStorage.setItem('disclaimer_agreed', 'true')
    if (guideSeen) localStorage.setItem(`mathagent_usage_guide_v3:${user.id}`, 'true')
    if (seen) localStorage.setItem(seenKey, JSON.stringify(seen))
  }, { user, disclaimer, guideSeen, seen, seenKey: SEEN_KEY })
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    let data = {}
    if (url.pathname === '/api/auth/me') data = { user }
    else if (url.pathname === '/api/auth/classes') data = { classes, current_class_id: 1 }
    else if (/^\/api\/class\/\d+\/students$/.test(url.pathname)) data = { students: [], class_name: classes[0].class_name }
    else if (url.pathname.endsWith('/availability')) {
      data = { status: 'available', service_status: 'available', registration_hardware: { status: 'ready', ready: true }, hardware: { status: 'ready' } }
    } else if (url.pathname.includes('/pending')) data = []
    else if (url.pathname === '/api/file/recent') data = { items: [] }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) })
  })
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.goto(`${base}/`, { waitUntil: 'networkidle' })
  return { context, page }
}

const noticeDialog = (page) => page.locator('.el-dialog', { hasText: '系统公告' })
const newTags = (page) => noticeDialog(page).locator('.el-tag', { hasText: '新' })
const badge = (page) => page.locator('.command-row__badge')
const shot = (page, name) => page.screenshot({ path: resolve(output, `${name}.png`) })

// A. Existing teacher, nothing acknowledged yet: pops up, everything marked, closing acknowledges.
{
  const { context, page } = await open()
  await noticeDialog(page).waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(2600) // let the startup plate fade before the shot
  assert.equal(await newTags(page).count(), 5)
  const first = await noticeDialog(page).locator('.notice-item').first().innerText()
  assert.match(first, /2026-10-28/)
  assert.match(first, /在外找工作/)
  assert.equal(await badge(page).innerText(), '5 条新公告')
  await shot(page, 'a1-auto-open')
  await noticeDialog(page).locator('.el-dialog__headerbtn').click()
  await noticeDialog(page).waitFor({ state: 'hidden' })
  await page.waitForTimeout(600)
  assert.equal(await badge(page).count(), 0)
  const stored = await page.evaluate((key) => localStorage.getItem(key), SEEN_KEY)
  assert.equal(JSON.parse(stored).length, 5)
  await page.locator('.command-row', { hasText: '系统公告' }).scrollIntoViewIfNeeded()
  await shot(page, 'a2-after-close')
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  assert.equal(await noticeDialog(page).isVisible(), false, 'no auto-open after acknowledgement')
  assert.equal(await badge(page).count(), 0)
  await page.locator('.command-row', { hasText: '系统公告' }).click()
  await noticeDialog(page).waitFor({ state: 'visible' })
  assert.equal(await newTags(page).count(), 0)
  await shot(page, 'a3-reopened-no-new')
  await context.close()
  console.log('A ok: auto-open, 5 new, acknowledged on close, quiet after reload')
}

// B. Teacher who had closed the older four: only the 2026-10-28 item is new.
{
  const { context, page } = await open({ seen: OLD_KEYS })
  await noticeDialog(page).waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(2600) // let the startup plate fade before the shot
  assert.equal(await newTags(page).count(), 1)
  assert.equal(await noticeDialog(page).locator('.notice-item.is-new').count(), 1)
  assert.match(await noticeDialog(page).locator('.notice-item.is-new').innerText(), /2026-10-28/)
  assert.equal(await badge(page).innerText(), '1 条新公告')
  await shot(page, 'b1-one-new')
  await context.close()
  console.log('B ok: only the newest item is marked')
}

// C. Disclaimer still pending: the disclaimer shows, the notice waits.
{
  const { context, page } = await open({ disclaimer: false })
  await page.locator('.el-dialog', { hasText: '用户协议与免责声明' }).waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1200)
  assert.equal(await noticeDialog(page).isVisible(), false, 'notice must not stack on the disclaimer')
  await shot(page, 'c1-disclaimer-first')
  await context.close()
  console.log('C ok: disclaimer first')
}

// D. First-run tour still pending: no popup, but the card badge still shows.
{
  const { context, page } = await open({ guideSeen: false })
  await page.waitForTimeout(2500)
  assert.equal(await noticeDialog(page).isVisible(), false, 'notice must not stack on the tour')
  assert.equal(await badge(page).innerText(), '5 条新公告')
  await badge(page).scrollIntoViewIfNeeded()
  await shot(page, 'd1-tour-pending')
  await context.close()
  console.log('D ok: tour pending, badge only')
}

await browser.close()
await server.close()
assert.deepEqual(errors, [], `page errors: ${errors.join('; ')}`)
console.log('notice popup browser check passed')
