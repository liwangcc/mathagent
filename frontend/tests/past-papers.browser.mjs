// 整卷真题 end-to-end against a REAL isolated backend (no API mocks except the
// one forced-failure check). Start an isolated QA server that serves
// frontend/dist and a QA library, then:
//   NODE_PATH=<dir with playwright> node tests/past-papers.browser.mjs <base> <tokens.json> <catalog.json> <out-dir>
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve, join } from 'node:path'

const { chromium } = createRequire(import.meta.url)('playwright')
const [base = 'http://127.0.0.1:8097', tokensPath, catalogPath, outArg] = process.argv.slice(2)
assert.equal(new URL(base).hostname, '127.0.0.1')
const tokens = JSON.parse(readFileSync(tokensPath, 'utf8'))
const catalog = JSON.parse(readFileSync(catalogPath, 'utf8'))
const out = resolve(outArg || '../outputs/past-papers-browser')
mkdirSync(out, { recursive: true })
const sha256 = (buf) => createHash('sha256').update(buf).digest('hex')
const withAnswer = catalog.papers.find((p) => p.files.answer)
const withoutAnswer = catalog.papers.find((p) => !p.files.answer)
const sample = withAnswer || catalog.papers[0]
assert.ok(sample, 'QA library must contain at least one paper')

const results = []
const errors = []
const browser = await chromium.launch({ headless: true, channel: 'msedge' })

async function teacherPage(role, width = 1440) {
  const { token, user } = tokens[role]
  const context = await browser.newContext({ viewport: { width, height: 1000 }, acceptDownloads: true })
  await context.addInitScript(([t, u]) => {
    localStorage.setItem('mathagent_token', t)
    localStorage.setItem('mathagent_user', JSON.stringify(u))
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem(`mathagent_usage_guide_v3:${u.id}`, 'true')
  }, [token, user])
  const page = await context.newPage()
  page.on('pageerror', (e) => errors.push(`${role}@${width}: ${e.message}`))
  return { context, page }
}

async function settle(page) {
  await page.locator('#mathagent-startup').waitFor({ state: 'hidden' }).catch(() => {})
  await page.waitForLoadState('networkidle')
  await page.waitForTimeout(800) // let entrance/dialog transitions finish before screenshots
}

async function downloadAndCheck(page, buttonLocator, expected, name) {
  const pending = page.waitForEvent('download')
  await buttonLocator.click()
  const download = await pending
  const target = join(out, `${name}-${download.suggestedFilename()}`)
  await download.saveAs(target)
  const bytes = readFileSync(target)
  assert.ok(bytes.subarray(0, 5).toString() === '%PDF-', `${name} is a PDF`)
  assert.equal(sha256(bytes), expected.sha256, `${name} matches the catalog checksum`)
  return { file: target, suggested: download.suggestedFilename(), bytes: bytes.length }
}

try {
  // 1. Senior teacher: entry from the bank page, region browsing, preview, downloads.
  {
    const { context, page } = await teacherPage('senior')
    await page.goto(`${base}/bank`)
    await settle(page)
    await page.getByRole('button', { name: '整卷真题' }).first().click()
    await page.waitForURL('**/bank/past-papers')
    await settle(page)
    assert.ok(await page.locator('.el-menu-item.is-active', { hasText: '整卷真题' }).count(), 'sidebar entry active')
    await page.screenshot({ path: join(out, '01-region-chooser.png'), fullPage: true })
    const region = sample.regions[0]
    if (region) {
      const regionName = (await page.locator('.region-grid .el-button').allInnerTexts()).find((t) => t.length)
      assert.ok(regionName, 'region chooser lists regions')
      await page.locator('.region-grid .el-button').first().click()
      await page.locator('.paper-card').first().waitFor()
      results.push({ step: 'region-browse', regionButton: regionName })
    }
    // By paper type, all years.
    // Click the visible segment like a teacher would (forcing the hidden input
    // left the old segment highlighted in screenshots).
    await page.locator('.el-radio-button', { hasText: '按卷型' }).click()
    await page.locator('.el-radio-button.is-active', { hasText: '按卷型' }).waitFor()
    assert.equal(await page.locator('.el-radio-button.is-active', { hasText: '按地区' }).count(), 0)
    await page.locator(`[data-paper-id="${sample.paper_id}"]`).waitFor()
    const cards = await page.locator('.paper-card').count()
    assert.equal(cards, catalog.papers.length, 'series view lists every published paper')
    await settle(page) // segment highlight animates; capture the settled state
    await page.screenshot({ path: join(out, '02-series-list.png'), fullPage: true })

    // Preview dialog renders a real page image from the backend.
    const card = page.locator(`[data-paper-id="${sample.paper_id}"]`)
    await card.getByRole('button', { name: '预览' }).click()
    const img = page.locator('.page-image')
    await img.waitFor()
    await page.waitForFunction(() => {
      const el = document.querySelector('.page-image')
      return el && el.complete && el.naturalWidth > 600
    })
    await page.getByText('版本说明').waitFor()
    await page.waitForTimeout(600)
    await page.screenshot({ path: join(out, '03-preview.png') })
    if ((sample.files.student.previews || []).length > 1) {
      await page.getByRole('button', { name: '下一页' }).click()
      await page.getByText(`第 2 / ${sample.files.student.previews.length} 页`).waitFor()
    }
    await page.keyboard.press('Escape')

    const student = await downloadAndCheck(page, card.getByRole('button', { name: '下载试卷' }), sample.files.student, 'student')
    results.push({ step: 'student-download', ...student })
    const answerButton = card.getByRole('button', { name: '下载答案/解析' })
    if (sample.files.answer) {
      assert.equal(await answerButton.isDisabled(), false)
      const answer = await downloadAndCheck(page, answerButton, sample.files.answer, 'answer')
      results.push({ step: 'answer-download', ...answer })
    }
    if (withoutAnswer) {
      const other = page.locator(`[data-paper-id="${withoutAnswer.paper_id}"]`)
      assert.equal(await other.getByRole('button', { name: '下载答案/解析' }).isDisabled(), true, 'no answer => disabled')
      assert.ok(await other.getByText('暂无答案').count())
      results.push({ step: 'answer-unavailable-state', paper: withoutAnswer.paper_id })
    }

    // Filter that matches nothing -> explicit empty state with a reset.
    await page.locator('.toolbar .el-select').nth(2).click()
    await page.locator('.el-select-dropdown__item', { hasText: '文科' }).last().click()
    if (!catalog.papers.some((p) => p.track === 'liberal')) {
      await page.getByText('没有符合条件的试卷').waitFor()
      await page.getByRole('button', { name: '清除筛选' }).click()
      await page.locator('.paper-card').first().waitFor()
      results.push({ step: 'filtered-empty-state' })
    }
    await context.close()
  }

  // 2. Forced backend failure -> error state with retry (only mocked step).
  {
    const { context, page } = await teacherPage('senior')
    let fail = true
    await context.route('**/api/past-papers?*', (route) => fail
      ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '服务暂不可用' }) })
      : route.continue())
    await context.route('**/api/past-papers', (route) => fail
      ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '服务暂不可用' }) })
      : route.continue())
    await page.goto(`${base}/bank/past-papers`)
    await page.getByText('整卷真题加载失败').waitFor()
    await page.screenshot({ path: join(out, '04-error-state.png') })
    fail = false
    await page.getByRole('button', { name: '重试' }).click()
    await page.getByText('整卷真题加载失败').waitFor({ state: 'detached' })
    await page.locator('.region-grid .el-button, .paper-card, .el-empty').first().waitFor()
    results.push({ step: 'error-state-and-retry' })
    await context.close()
  }

  // 3. Junior-stage teacher gets an explanation, not senior papers.
  {
    const { context, page } = await teacherPage('junior')
    await page.goto(`${base}/bank/past-papers`)
    await page.getByText('仅对高中学段开放').waitFor()
    assert.equal(await page.locator('.paper-card').count(), 0)
    await page.screenshot({ path: join(out, '05-junior-stage.png') })
    results.push({ step: 'stage-scoped' })
    await context.close()
  }

  // 4. Admin sees how many verified papers are withheld (if any).
  {
    const { context, page } = await teacherPage('admin')
    await page.goto(`${base}/bank/past-papers`)
    await settle(page)
    await page.screenshot({ path: join(out, '06-admin.png'), fullPage: true })
    await context.close()
  }

  // 5. Phone width layout.
  {
    const { context, page } = await teacherPage('senior', 390)
    await page.goto(`${base}/bank/past-papers`)
    await settle(page)
    await page.locator('label', { hasText: '按卷型' }).click()
    await page.locator('.paper-card').first().waitFor()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    assert.ok(overflow <= 1, `no horizontal scroll at 390px (overflow ${overflow})`)
    await page.screenshot({ path: join(out, '07-phone.png'), fullPage: true })
    results.push({ step: 'phone-layout', overflow })
    await context.close()
  }

  // 6. No session -> login page; the API itself refuses the file.
  {
    const context = await browser.newContext()
    const page = await context.newPage()
    await page.goto(`${base}/bank/past-papers`)
    await page.waitForURL('**/login**')
    const status = await page.evaluate(async (id) => (await fetch(`/api/past-papers/${id}/download/student`)).status, sample.paper_id)
    assert.equal(status, 401)
    results.push({ step: 'login-required', status })
    await context.close()
  }
  assert.deepEqual(errors, [], 'no page errors')
} finally {
  await browser.close()
  writeFileSync(join(out, 'results.json'), JSON.stringify({ base, results, errors }, null, 2))
}
console.log(JSON.stringify({ ok: true, steps: results.map((r) => r.step) }))
