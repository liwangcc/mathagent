// Template editor edit-sequence fuzzer: the real UI against the real app.
//
// The hermetic harness (tests/e2e/template_editor_harness.py) serves the
// production build and API with scripted model replies only. Each seed imports
// a generated synthetic template, applies random teacher edits through the
// visible editor, then saves. A save may stop only for a teacher decision the
// driver can resolve the way a teacher would (keep a Max flag, fill a missing
// answer); every other outcome is a finding.
//
// Each seed also picks the entry route (/anonymous-grade or the logged-in
// /upload) and the runtime: web, or desktop, where a stand-in for the Tauri
// bridge returns real desktop sidecar canonical pages so the import takes the
// client canonical-candidate path. Pass a value instead of "mixed" to pin one.
// About one seed in five gets a model box over a quarter of the page, which
// the editor must open with and flag; the driver then shrinks or deletes it.
//
//   cd frontend && npm run build
//   (repo root) env -u PYTHONPATH ./venv_local/Scripts/python.exe -m tests.e2e.template_editor_harness --port 8765
//   NODE_PATH=<global node_modules> node tests/template-editor-fuzz.browser.mjs http://127.0.0.1:8765 <out-dir> [first-seed] [count] [actions] [mixed|anonymous|upload] [mixed|web|desktop]
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
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
const output = resolve(process.argv[3] || '../outputs/template-editor-fuzz')
const firstSeed = Number(process.argv[4] || 1)
const seedCount = Number(process.argv[5] || 20)
const actionCount = Number(process.argv[6] || 5)
const routeChoice = process.argv[7] || 'mixed'
const runtimeChoice = process.argv[8] || 'mixed'
assert.ok(['mixed', 'anonymous', 'upload'].includes(routeChoice), 'route must be mixed, anonymous or upload')
assert.ok(['mixed', 'web', 'desktop'].includes(runtimeChoice), 'runtime must be mixed, web or desktop')
mkdirSync(output, { recursive: true })

function rng(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6D2B79F5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const pick = (random, items) => items[Math.floor(random() * items.length)]
const post = async (path, body) => (await fetch(base + path, {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
})).json()

const teacher = await (await fetch(base + '/__harness/teacher')).json()
const browser = await chromium.launch({ headless: true })
const results = []

async function settle(page, ms = 200) { await page.waitForTimeout(ms) }

// The editor pins its toolbar over the scroll area; scroll targets to the
// middle like a teacher would before clicking.
async function tap(locator) {
  await locator.evaluate(el => el.scrollIntoView({ block: 'center', inline: 'center' }))
  await locator.click({ timeout: 5000 })
}

async function visibleStage(page) {
  const stage = page.locator('.drawing-reference-canvas:visible').first()
  if (!(await stage.count())) return null
  // Mouse gestures only land on what is on screen: bring the canvas into view.
  await stage.evaluate(el => el.scrollIntoView({ block: 'center' }))
  return stage.boundingBox()
}

async function dragOnStage(page, from, to) {
  const box = await visibleStage(page)
  if (!box) return false
  const at = ([x, y]) => [box.x + box.width * x / 1000, box.y + box.height * y / 1000]
  const [x1, y1] = at(from)
  const [x2, y2] = at(to)
  await page.mouse.move(x1, y1)
  await page.mouse.down()
  await page.mouse.move((x1 + x2) / 2, (y1 + y2) / 2, { steps: 4 })
  await page.mouse.move(x2, y2, { steps: 4 })
  await page.mouse.up()
  await settle(page)
  return true
}

const escapeRegExp = (text) => String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

async function expandAll(page) {
  for (let i = 0; i < 20; i++) {
    const collapsed = page.locator('.tpl-q.is-collapsed .tpl-q-toggle').first()
    if (!(await collapsed.count())) break
    await tap(collapsed)
    await settle(page, 60)
  }
}

async function questionNumbers(page) {
  return page.locator('.tpl-q .tpl-q-identity').allInnerTexts()
    .then(items => items.map(text => (text.match(/^Q([\d.]+)/) || [])[1]).filter(Boolean))
}

// ---------------------------------------------------------------------------
// Teacher actions. Each returns a short description, or null if not applicable.
// ---------------------------------------------------------------------------
const actions = {
  async deleteMarker(page, random) {
    const chips = page.locator('.template-geometry-legend .legend-items button:not([disabled])')
    const count = await chips.count()
    if (!count) return null
    const chip = chips.nth(Math.floor(random() * count))
    const label = (await chip.innerText()).replace(/\s+/g, ' ')
    await tap(chip)
    await tap(page.locator('.tpl-delete-selected').first())
    await settle(page)
    return `delete marker ${label}`
  },
  async addMarker(page, random) {
    const numbers = await questionNumbers(page)
    if (!numbers.length) return null
    const tool = pick(random, ['蓝色作答框', '绿色画图区', '红色题号', '作图题题号'])
    const button = page.locator('.tpl-add-tools .tpl-tool-btn', { hasText: tool }).first()
    if (!(await button.count()) || await button.isDisabled()) return null
    await tap(button)
    const prompt = page.locator('.el-message-box:visible')
    await prompt.waitFor({ timeout: 3000 }).catch(() => {})
    const number = pick(random, numbers)
    if (await prompt.count()) {
      await prompt.locator('input').fill(number)
      await tap(prompt.getByRole('button', { name: '继续框选' }))
      await settle(page)
    }
    const x = 100 + Math.floor(random() * 600), y = 80 + Math.floor(random() * 800)
    if (tool === '作图题题号') {
      const surface = await visibleStage(page)
      if (!surface) return null
      await page.mouse.click(surface.x + surface.width * x / 1000, surface.y + surface.height * y / 1000)
      await settle(page)
    } else {
      await dragOnStage(page, [x, y], [x + 120 + Math.floor(random() * 200), y + 40 + Math.floor(random() * 120)])
    }
    return `add ${tool} for Q${number} at ${x},${y}`
  },
  async moveMarker(page, random) {
    const boxes = page.locator('.tpl-reference-geometry:visible .tpl-handwriting-region, .tpl-reference-geometry:visible .tpl-drawing-region')
    const count = await boxes.count()
    if (!count) return null
    const target = boxes.nth(Math.floor(random() * count))
    const rect = await target.boundingBox()
    if (!rect) return null
    const name = ((await target.getAttribute('title')) || 'marker').split('；')[0]
    const dx = Math.floor(random() * 80) - 40, dy = Math.floor(random() * 80) - 40
    await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2)
    await page.mouse.down()
    await page.mouse.move(rect.x + rect.width / 2 + dx, rect.y + rect.height / 2 + dy, { steps: 5 })
    await page.mouse.up()
    await settle(page)
    return `move ${name} by ${dx},${dy}`
  },
  async setDrawingKind(page, random) {
    const selects = page.locator('select[aria-label$="题作图属性"]')
    const count = await selects.count()
    if (!count) return null
    const select = selects.nth(Math.floor(random() * count))
    const kind = pick(random, ['non_graphic', 'drawing', 'mixed'])
    const label = await select.getAttribute('aria-label')
    await select.selectOption(kind)
    await settle(page)
    // 作图题 on a question without a drawing area asks the teacher to frame one.
    const prompt = page.locator('.el-message-box:visible')
    if (await prompt.waitFor({ timeout: 1500 }).then(() => true, () => false)) {
      if (random() < 0.7) {
        await tap(prompt.getByRole('button', { name: '继续框选' }))
        await settle(page)
        const x = 120 + Math.floor(random() * 500), y = 100 + Math.floor(random() * 600)
        await dragOnStage(page, [x, y], [x + 200, y + 150])
        return `${label} -> ${kind} (framed drawing area at ${x},${y})`
      }
      await tap(prompt.getByRole('button', { name: '取消' }))
      return `${label} -> ${kind} (cancelled framing)`
    }
    return `${label} -> ${kind}`
  },
  async setQuestionType(page, random) {
    const selects = page.locator('.tpl-q-head-actions select.tpl-q-type:not([aria-label])')
    const count = await selects.count()
    if (!count) return null
    const index = Math.floor(random() * count)
    const type = pick(random, ['choice', 'fill', 'solve'])
    await selects.nth(index).selectOption(type)
    await settle(page)
    return `question #${index + 1} type -> ${type}`
  },
  async editSlotAnswer(page, random) {
    await expandAll(page)
    const inputs = page.locator('.tpl-q-details input.tpl-fill-answer-input:not([disabled]):visible')
    const count = await inputs.count()
    if (!count) return null
    const input = inputs.nth(Math.floor(random() * count))
    const value = pick(random, ['', '7', 'x=3', '2/3', '答：略'])
    const label = await input.getAttribute('aria-label')
    await input.fill(value)
    await input.press('Tab')
    await settle(page)
    return `${label} = "${value}"`
  },
  async editStandardAnswer(page, random) {
    await expandAll(page)
    const areas = page.locator('.tpl-q-details textarea[placeholder="请输入标准答案"]:visible')
    const count = await areas.count()
    if (!count) return null
    const area = areas.nth(Math.floor(random() * count))
    const value = pick(random, ['', '12', 'B', 'x=2; y=3'])
    await area.fill(value)
    await settle(page)
    return `standard answer = "${value}"`
  },
  async drawReference(page, random) {
    const tools = page.locator('.drawing-reference-drawing-tools:visible .drawing-reference-toolbar button', { hasText: /^(直线|矩形|点|圆)$/ })
    const count = await tools.count()
    if (!count) return null
    const tool = tools.nth(Math.floor(random() * count))
    if (await tool.isDisabled()) return null
    const name = await tool.innerText()
    await tap(tool)
    await settle(page)
    const x = 150 + Math.floor(random() * 600), y = 100 + Math.floor(random() * 700)
    if (name === '点') {
      const surface = await visibleStage(page)
      if (!surface) return null
      await page.mouse.click(surface.x + surface.width * x / 1000, surface.y + surface.height * y / 1000)
      await settle(page)
    } else {
      await dragOnStage(page, [x, y], [x + 80, y + 60])
    }
    return `draw reference ${name} at ${x},${y}`
  },
  async undoReference(page) {
    const undo = page.locator('.drawing-reference-drawing-tools:visible button', { hasText: '撤销' }).first()
    if (!(await undo.count()) || await undo.isDisabled()) return null
    await tap(undo)
    await settle(page)
    return 'undo reference stroke'
  },
  async keepMaxFlag(page) {
    const keep = page.locator('.tpl-answer-review button', { hasText: '确认保留' }).first()
    if (!(await keep.count())) return null
    await tap(keep)
    await settle(page)
    return 'keep Max-flagged position'
  },
  async deleteAllOfQuestion(page, random) {
    // The teacher removes every box of one question (the reported workflow).
    const numbers = await questionNumbers(page)
    if (!numbers.length) return null
    const number = pick(random, numbers)
    const includeNumberBox = random() < 0.3
    let removed = 0
    for (let i = 0; i < 30; i++) {
      const chips = page.locator('.template-geometry-legend .legend-items button:not([disabled])')
      let target = null
      for (const chip of await chips.all()) {
        const text = (await chip.innerText()).replace(/\s+/g, ' ')
        if (!new RegExp(`^Q${escapeRegExp(number)}(\\[\\d+\\])? `).test(text)) continue
        if (!includeNumberBox && text.includes('题号位置')) continue
        target = chip
        break
      }
      if (!target) break
      await tap(target)
      await tap(page.locator('.tpl-delete-selected').first())
      await settle(page, 150)
      removed++
    }
    return removed ? `delete all ${removed} boxes of Q${number}${includeNumberBox ? ' incl. number box' : ''}` : null
  },
  async addLargeBox(page, random) {
    const numbers = await questionNumbers(page)
    if (!numbers.length) return null
    const tool = pick(random, ['蓝色作答框', '绿色画图区'])
    const button = page.locator('.tpl-add-tools .tpl-tool-btn', { hasText: tool }).first()
    if (!(await button.count()) || await button.isDisabled()) return null
    await tap(button)
    const prompt = page.locator('.el-message-box:visible')
    await prompt.waitFor({ timeout: 3000 }).catch(() => {})
    const number = pick(random, numbers)
    if (await prompt.count()) {
      await prompt.locator('input').fill(number)
      await tap(prompt.getByRole('button', { name: '继续框选' }))
      await settle(page)
    }
    await dragOnStage(page, [60, 60], [700, 700])
    return `add oversized ${tool} for Q${number}`
  },
  async setChoiceOption(page, random) {
    const options = page.locator('.tpl-choice-options .tpl-choice-option:visible')
    const count = await options.count()
    if (!count) return null
    const option = options.nth(Math.floor(random() * count))
    const text = await option.innerText()
    await tap(option)
    return `choice option ${text}`
  },
  async setPageColumns(page, random) {
    const select = page.locator('.tpl-column-count select:visible').first()
    if (!(await select.count())) return null
    const value = pick(random, ['1', '2'])
    await select.selectOption(value)
    await settle(page)
    return `page columns -> ${value}`
  },
}

// Resolutions a teacher would apply when the save is blocked on their decision.
// Elements are re-queried after every click: Vue re-renders detach old nodes.
async function drainFirst(page, selector, act, limit = 20) {
  let count = 0
  for (; count < limit; count++) {
    const target = page.locator(selector).first()
    if (!(await target.count())) break
    await act(target)
    await settle(page, 120)
  }
  return count
}

async function boxPendingSlots(page, random) {
  // A kept position without geometry: locate it (arms the right tool) and draw.
  let drawn = 0
  for (let i = 0; i < 10; i++) {
    const pending = page.locator('.tpl-slot-row button', { hasText: '待框选' }).first()
    if (!(await pending.count())) break
    await tap(pending)
    await settle(page)
    const x = 120 + Math.floor(random() * 500), y = 100 + Math.floor(random() * 700)
    await dragOnStage(page, [x, y], [x + 150, y + 70])
    drawn++
  }
  return drawn
}

async function resolveTeacherDecisions(page, text, random) {
  const done = []
  if (/Max 判定/.test(text)) {
    // Only Max items: other review items (e.g. missing answers) share 去处理.
    const maxGo = '.tpl-answer-review:has-text("Max 判定") button:text("去处理")'
    const maxKeep = () => page.locator('.tpl-answer-review:has-text("Max 判定") button', { hasText: '确认保留' }).first()
    if (random() < 0.5) {
      const removed = await drainFirst(page, maxGo, async (go) => {
        const before = await page.locator(maxGo).count()
        await tap(go)
        await settle(page)
        const remove = page.locator('.tpl-delete-selected').first()
        if (await remove.isEnabled()) await tap(remove)
        else await tap(maxKeep())
        if (await page.locator(maxGo).count() >= before) await tap(maxKeep())
      }, 10)
      done.push(`deleted ${removed} Max-flagged positions`)
    } else {
      await drainFirst(page, '.tpl-answer-review button:text("确认保留")', tap, 10)
      done.push('kept Max flags')
    }
  }
  if (/还没有框选位置|缺少槽位作答框|待框选/.test(text) || /Max 判定/.test(text)) {
    const drawn = await boxPendingSlots(page, random)
    if (drawn) done.push(`boxed ${drawn} kept positions`)
  }
  if (/未设置标准答案|缺少标准答案/.test(text)) {
    await drainFirst(page, '.tpl-q.is-collapsed .tpl-q-toggle', tap)
    for (let i = 0; i < 40; i++) {
      const empty = page.locator('.tpl-q-details input.tpl-fill-answer-input:not([disabled])')
      const inputs = await empty.all()
      let filled = false
      for (const input of inputs) {
        if ((await input.inputValue()).trim()) continue
        await input.fill('5'); await input.press('Tab'); await settle(page, 80)
        filled = true
        break
      }
      if (!filled) break
    }
    for (const area of await page.locator('.tpl-q-details textarea[placeholder="请输入标准答案"]').all()) {
      if (!(await area.inputValue()).trim()) await area.fill('5')
    }
    done.push('filled missing answers')
  }
  if (/缺少题干/.test(text)) {
    await expandAll(page)
    for (const area of await page.locator('.tpl-q-details textarea[placeholder="请输入题干"]').all()) {
      if (!(await area.inputValue()).trim()) await area.fill('补充题干')
    }
    done.push('filled stems')
  }
  if (/标准图有笔画画在画图区外/.test(text)) {
    // Teacher removes the stray figure strokes (undo / delete scheme).
    for (let i = 0; i < 30; i++) {
      const remove = page.locator('.drawing-reference-drawing-tools button:visible:not([disabled])', { hasText: /^(删除方案|撤销)$/ }).first()
      if (!(await remove.count())) break
      await tap(remove)
      await settle(page, 80)
    }
    done.push('removed stray figure strokes')
  }
  if (/超过整页的四分之一/.test(text)) {
    // Teacher follows the flagged row: 去处理 selects the box, which they then
    // shrink by its corner handle or delete.
    for (let i = 0; i < 10; i++) {
      const go = page.locator('.tpl-answer-review-row:has-text("超过整页的四分之一") button:text("去处理")').first()
      if (!(await go.count())) break
      await tap(go)
      await settle(page)
      const selected = page.locator('.tpl-reference-geometry:visible .is-selected').first()
      const grip = selected.locator('.tpl-handwriting-resize')
      const rect = (await selected.count()) ? await selected.boundingBox() : null
      if (rect && await grip.count() && random() < 0.5) {
        const handle = await grip.boundingBox()
        await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2)
        await page.mouse.down()
        await page.mouse.move(rect.x + rect.width * 0.35, rect.y + rect.height * 0.35, { steps: 6 })
        await page.mouse.up()
        done.push('shrank an oversized box')
      } else {
        const remove = page.locator('.tpl-delete-selected').first()
        if (!(await remove.isEnabled())) break
        await tap(remove)
        done.push('deleted an oversized box')
      }
      await settle(page, 150)
    }
  }
  const unplaced = [...text.matchAll(/第 ([\d.]+) 题缺少作答框或印刷题号位置|第 \d+ 页的题目框已全部删除/g)]
  if (unplaced.length) {
    // Teacher frames an answer position again for the named (or first) question.
    const numbers = await questionNumbers(page)
    const number = unplaced[0][1] || numbers[0]
    const button = page.locator('.tpl-add-tools .tpl-tool-btn', { hasText: '蓝色作答框' }).first()
    if (number && await button.count()) {
      await tap(button)
      const prompt = page.locator('.el-message-box:visible')
      if (await prompt.waitFor({ timeout: 3000 }).then(() => true, () => false)) {
        await prompt.locator('input').fill(number)
        await tap(prompt.getByRole('button', { name: '继续框选' }))
        await settle(page)
      }
      await dragOnStage(page, [150, 300], [400, 380])
      done.push(`framed an answer box for Q${number}`)
    }
  }
  const multiChoice = [...text.matchAll(/Q([\d.]+) 是选择题，但有 \d+ 个作答框/g)].map(match => match[1])
  for (const number of multiChoice) {
    // Teacher keeps one box: delete the other answer boxes of that question.
    for (let i = 0; i < 10; i++) {
      const chips = await page.locator('.template-geometry-legend .legend-items button:not([disabled])', { hasText: new RegExp(`^Q${escapeRegExp(number)}\\[\\d+\\] 作答框`) }).all()
      if (chips.length <= 1) break
      await tap(chips.at(-1))
      await tap(page.locator('.tpl-delete-selected').first())
      await settle(page, 150)
    }
    done.push(`kept one box of choice Q${number}`)
  }
  if (/选择题答题卡中标记为“待处理”|请在选择题答题卡中/.test(text)) {
    await drainFirst(page, '.tpl-choice-answer-cell.is-unresolved', async (cell) => {
      const single = cell.locator('.tpl-choice-mode-button', { hasText: '单选' })
      if (await single.count()) await tap(single)
      const option = cell.locator('.tpl-choice-option', { hasText: 'A' })
      if (await option.count()) await tap(option)
    })
    done.push('resolved choice cards')
  }
  await settle(page)
  return done
}

async function runSeed(seed) {
  const random = rng(seed * 7919)
  // A separate stream keeps each seed's scenario and edit sequence unchanged.
  const surface = rng(seed * 104729)
  const route = routeChoice === 'mixed' ? (surface() < 0.5 ? 'anonymous' : 'upload') : routeChoice
  const runtime = runtimeChoice === 'mixed' ? (surface() < 0.5 ? 'web' : 'desktop') : runtimeChoice
  // Some model replies carry a box over a quarter of the page; the editor
  // must open and flag it for the teacher.
  const oversize = surface() < 0.2 ? (surface() < 0.5 ? 'answer' : 'drawing') : null
  const columns = random() < 0.5 ? 'single' : 'double'
  const scenario = await post('/__harness/scenario', { seed, columns, oversize })
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
  if (runtime === 'desktop') {
    // Stand-in for the Tauri bridge. Only the template command exists; every
    // other desktop command fails like an unavailable runtime.
    await context.addInitScript(() => {
      window.__TAURI_INTERNALS__ = {
        async invoke(command, payload) {
          if (command !== 'canonicalize_template_image') throw new Error(`harness desktop has no ${command}`)
          const response = await fetch('/__harness/desktop/canonicalize', { method: 'POST', body: payload })
          if (!response.ok) throw new Error(await response.text())
          return response.arrayBuffer()
        },
      }
    })
  }
  await context.addInitScript(({ token, id }) => {
    localStorage.setItem('mathagent_token', token)
    localStorage.setItem('disclaimer_agreed', 'true')
    localStorage.setItem(`mathagent_usage_guide_v3:${id}`, 'true')
    window.__messages = []
    new MutationObserver(() => {
      for (const node of document.querySelectorAll('.el-message:not([data-seen])')) {
        node.dataset.seen = '1'
        window.__messages.push(node.innerText.trim())
      }
    }).observe(document, { childList: true, subtree: true })
  }, { token: teacher.token, id: teacher.user_id })
  const page = await context.newPage()
  const network = [], pageErrors = [], steps = []
  let templateId = ''
  page.on('pageerror', error => pageErrors.push(String(error?.stack || error)))
  page.on('response', response => {
    if (new URL(response.url()).pathname === '/api/batch-grade/template/recognize') {
      templateId = response.headers()['x-template-id'] || templateId
    }
  })
  page.on('response', async response => {
    const url = new URL(response.url())
    if (!url.pathname.startsWith('/api/') || response.status() < 400) return
    let body = null
    try { body = await response.json() } catch {}
    network.push({ status: response.status(), method: response.request().method(), path: url.pathname,
      code: body?.error_info?.code, detail: typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? null) })
  })
  const messages = async () => page.evaluate(() => window.__messages.splice(0))
  const result = { seed, spec: scenario.spec.questions.map(q => ({ n: q.number, type: q.type, kinds: q.kinds, stem_drawing: q.stem_drawing, flag: q.flag })), steps, network, pageErrors }
  try {
    const category = random() < 0.3 ? 'exam' : 'daily'
    // Both routes mount the same Upload.vue editor; /upload is the identified flow.
    await page.goto(base + (route === 'upload' ? '/upload' : '/anonymous-grade'))
    await page.locator('select.form-select').first().selectOption(category)
    if (category === 'exam') await page.locator('input[placeholder="如：高一月考"]').fill('合成月考')
    await page.locator('#template-column-mode').selectOption(columns)
    result.setup = { route, runtime, category, columns, oversized: scenario.spec.oversized || null }
    for (let attempt = 0; attempt < 3; attempt++) {
      // The input stays disabled until the service availability check returns.
      await page.waitForFunction(() => document.querySelector('.template-upload + input[type=file]')?.disabled === false, null, { timeout: 30000 })
      await page.locator('.template-upload + input[type=file]').setInputFiles(scenario.image_path)
      if (await page.getByText(/已选 1 页/).waitFor({ timeout: 15000 }).then(() => true, () => false)) break
      await page.reload()
      await page.locator('select.form-select').first().selectOption(category)
      if (category === 'exam') await page.locator('input[placeholder="如：高一月考"]').fill('合成月考')
      await page.locator('#template-column-mode').selectOption(columns)
    }
    const recognize = page.getByRole('button', { name: '识别模板' })
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some(b => b.innerText.trim() === '识别模板' && !b.disabled), null, { timeout: 60000 })
    await recognize.click()
    const save = page.getByRole('button', { name: /保存模板（全部页）|保存中/ })
    const importFailure = page.locator('.template-service-alert, .tpl-recognize-error, [role=alert]', { hasText: /模型服务认证失败/ })
    await Promise.race([save.waitFor({ timeout: 60000 }), importFailure.first().waitFor({ timeout: 60000 })])
    if (!(await save.count())) {
      // Rare synthetic pages fall back to the remote column model, which the
      // isolated harness cannot reach. That is not an editor finding.
      result.outcome = 'harness-skip'
      result.error = 'column layout fell back to the unreachable remote model'
      await context.close()
      return result
    }
    // Which canonicalizer produced the imported pages, as the server stored them.
    const session = await (await fetch(`${base}/__harness/session/${templateId}`)).json()
    const clientPages = session.client_canonical_pages || []
    result.setup.canonical = clientPages.length && clientPages.length === (session.pages || []).length ? 'desktop' : 'server'
    if (runtime === 'desktop' && result.setup.canonical !== 'desktop') {
      // The page silently falls back to server canonicalization when the
      // desktop command fails; that run did not cover the desktop path.
      result.outcome = 'desktop-fallback'
      result.error = `desktop canonical pages were not accepted (template ${templateId || 'unknown'}: ${clientPages.length}/${(session.pages || []).length} pages)`
      await page.screenshot({ path: `${output}/seed-${seed}.png`, fullPage: true }).catch(() => {})
      await context.close()
      return result
    }
    await page.waitForFunction(() => !document.querySelector('.tpl-cut-status') && document.querySelector('.drawing-reference-canvas img'), null, { timeout: 30000 }).catch(() => {})
    await settle(page, 800)
    steps.push({ action: 'opened editor', messages: await messages() })
    if (scenario.spec.oversized) {
      // The oversized model box must be named and highlighted as soon as the editor opens.
      const named = await page.locator('.tpl-answer-review-row', { hasText: '超过整页的四分之一' }).count()
      const highlighted = await page.locator('.tpl-reference-geometry .is-missing-answer').count()
      if (!named || !highlighted) {
        result.outcome = 'unflagged-oversize'
        result.error = `oversized ${scenario.spec.oversized} not flagged on open (rows ${named}, highlighted ${highlighted})`
        await page.screenshot({ path: `${output}/seed-${seed}.png`, fullPage: true }).catch(() => {})
        await context.close()
        return result
      }
    }
    for (let i = 0; i < actionCount; i++) {
      const names = Object.keys(actions)
      for (let attempt = 0; attempt < 4; attempt++) {
        const name = pick(random, names)
        let description = null
        try { description = await actions[name](page, random) } catch (error) { description = `${name} threw: ${error.message.split('\n')[0]}` }
        if (description) { steps.push({ action: description, messages: await messages() }); break }
      }
    }
    let outcome = 'blocked'
    // Each save attempt can surface one more teacher decision; result.saves
    // records how many attempts the teacher needed.
    for (let round = 0; round < 8; round++) {
      await tap(save)
      await page.waitForFunction(() => {
        const button = [...document.querySelectorAll('button')].find(b => /保存模板（全部页）|保存中/.test(b.innerText))
        return !button || !button.disabled
      }, null, { timeout: 60000 })
      await settle(page, 600)
      const text = (await messages()).join(' | ')
      steps.push({ action: `save #${round + 1}`, messages: text })
      result.saves = round + 1
      if (/本次模板已保存/.test(text) || !(await page.locator('.el-dialog:visible').count())) { outcome = 'saved'; break }
      const resolved = await resolveTeacherDecisions(page, text, random)
      if (!resolved.length) break
      steps.push({ action: `teacher resolves: ${resolved.join(', ')}`, messages: await messages() })
    }
    result.outcome = outcome
  } catch (error) {
    result.outcome = 'harness-error'
    result.error = String(error?.message || error).split('\n')[0]
  }
  if (result.outcome !== 'saved') await page.screenshot({ path: `${output}/seed-${seed}.png`, fullPage: true }).catch(() => {})
  await context.close()
  return result
}

for (let seed = firstSeed; seed < firstSeed + seedCount; seed++) {
  const result = await runSeed(seed)
  results.push(result)
  const last = result.steps.at(-1)
  const where = result.setup ? ` [${result.setup.route}/${result.setup.canonical || result.setup.runtime}]` : ''
  console.log(`seed ${seed}${where}: ${result.outcome}${result.outcome === 'saved' ? '' : ` — ${last?.messages || result.error || ''}`}`.slice(0, 400))
  writeFileSync(`${output}/results.json`, JSON.stringify(results, null, 1))
}
await browser.close()
const failed = results.filter(r => r.outcome !== 'saved')
const coverage = {}
for (const r of results) {
  const key = r.setup ? `${r.setup.route}/${r.setup.canonical || r.setup.runtime}` : 'not started'
  coverage[key] = (coverage[key] || 0) + 1
}
const savesNeeded = {}
for (const r of results) if (r.outcome === 'saved') savesNeeded[r.saves] = (savesNeeded[r.saves] || 0) + 1
console.log(`\ncoverage (route/canonical): ${JSON.stringify(coverage)}`)
console.log(`save attempts until saved: ${JSON.stringify(savesNeeded)}`)
console.log(`seeds with an oversized model box: ${results.filter(r => r.setup?.oversized).length}`)
console.log(`\n${results.length - failed.length}/${results.length} saved; ${failed.length} findings → ${output}/results.json`)
