// The reported case, end to end in the real editor: a mixed question whose
// drawing area the model put on the printed stem. The teacher deletes that
// drawing box and saves; the template must save without the stray drawing
// slot, its Q marker or a standard figure.
//
//   (repo root) env -u PYTHONPATH ./venv_local/Scripts/python.exe -m tests.e2e.template_editor_harness --port 8765
//   NODE_PATH=<global node_modules> node tests/template-stem-drawing.browser.mjs http://127.0.0.1:8765 <out-dir>
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = (() => {
  for (const name of ['playwright', '@playwright/test']) {
    try { return require(name) } catch {}
  }
  throw new Error('Playwright is required on NODE_PATH')
})()
const base = process.argv[2] || 'http://127.0.0.1:8765'
assert.equal(new URL(base).hostname, '127.0.0.1', 'Only the local hermetic harness may be driven')
const output = resolve(process.argv[3] || '../outputs/template-stem-drawing')
mkdirSync(output, { recursive: true })
const post = async (path, body) => (await fetch(base + path, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
})).json()

// A single-column seed whose first stem-drawing question also has text answers
// and no Max report, so the only teacher action is deleting the stem box.
let scenario
for (let seed = 1; seed < 500 && !scenario; seed++) {
  const candidate = await post('/__harness/scenario', { seed, columns: 'single' })
  const target = candidate.spec.questions.find((q) => q.stem_drawing && q.kinds.includes('answer'))
  if (target && candidate.spec.questions.every((q) => !q.flag && q.values.every((value, i) => value || q.kinds[i] === 'drawing'))) {
    scenario = { ...candidate, target }
  }
}
assert.ok(scenario, 'a synthetic stem-drawing scenario exists')
const { target } = scenario
const drawingSlot = target.slots[target.kinds.indexOf('drawing')].slot_id
const teacher = await (await fetch(base + '/__harness/teacher')).json()
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
await context.addInitScript(({ token, id }) => {
  localStorage.setItem('mathagent_token', token)
  localStorage.setItem('disclaimer_agreed', 'true')
  localStorage.setItem(`mathagent_usage_guide_v3:${id}`, 'true')
}, { token: teacher.token, id: teacher.user_id })
const page = await context.newPage()
const failures = []
let templateId = ''
page.on('request', (request) => {
  if (request.url().endsWith('/api/batch-grade/template/confirm')) templateId = JSON.parse(request.postData() || '{}').template_id
})
page.on('response', (response) => {
  if (new URL(response.url()).pathname.startsWith('/api/') && response.status() >= 400) failures.push(`${response.status()} ${new URL(response.url()).pathname}`)
})
try {
  await page.goto(base + '/anonymous-grade')
  await page.locator('select.form-select').first().selectOption('daily')
  await page.locator('#template-column-mode').selectOption('single')
  // The input stays disabled until the service availability check returns.
  await page.waitForFunction(() => document.querySelector('.template-upload + input[type=file]')?.disabled === false, null, { timeout: 30000 })
  await page.locator('.template-upload + input[type=file]').setInputFiles(scenario.image_path)
  await page.getByText(/已选 1 页/).waitFor({ timeout: 20000 })
  await page.getByRole('button', { name: '识别模板' }).click()
  await page.getByRole('button', { name: '保存模板（全部页）' }).waitFor({ timeout: 60000 })
  await page.waitForTimeout(1500)
  const chip = page.locator('.template-geometry-legend .legend-items button', { hasText: `Q${drawingSlot} 画图区` })
  await chip.waitFor({ timeout: 10000 })
  await chip.scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${output}/1-before-delete.png` })
  await chip.click()
  await page.locator('.tpl-delete-selected').first().click()
  await page.waitForTimeout(400)
  assert.equal(await chip.count(), 0, 'the stem drawing box and its slot number are gone')
  await page.screenshot({ path: `${output}/2-after-delete.png` })
  await page.getByRole('button', { name: '保存模板（全部页）' }).click()
  await page.locator('.el-message', { hasText: '本次模板已保存' }).first().waitFor({ timeout: 60000 })
  await page.screenshot({ path: `${output}/3-saved.png` })
} finally {
  await browser.close()
}
assert.deepEqual(failures, [], 'no rejected request on the way')
const session = await (await fetch(`${base}/__harness/session/${templateId}`)).json()
const saved = session.pages.flatMap((p) => p.questions).find((q) => String(q.question_no) === target.number)
assert.equal(session.status, 'confirmed')
assert.ok(!saved.slot_ids.includes(drawingSlot), 'the deleted slot is not saved')
assert.ok(!(saved.region_kinds || []).includes('drawing'))
assert.equal(saved.answer_kind ?? 'non_graphic', 'non_graphic')
assert.ok(!saved.drawing_reference && !(saved.drawing_regions || []).length && !saved.label_anchor)
console.log(`ok: Q${target.number} saved as ${saved.slot_ids.join(', ')} after deleting the stem drawing ${drawingSlot}`)
